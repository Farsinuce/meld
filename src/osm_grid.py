"""
osm_grid.py — the stable OSM tile grid that makes prefetched OSM reusable.

WHY
    The old prefetch cached one Overpass response per *clump* — a bbox derived from
    whichever cells happened to be in the current selection. Re-running a 90%-identical
    selection produced different clump bboxes, so the cache never hit and the whole
    region re-downloaded (the exact pain the user reported).

    The fix: cache OSM on a FIXED web-mercator slippy grid (same tiling Arnis/terrain
    use). A z9 tile is a stable patch of the planet (~4300 km², ~110 km across) keyed
    only by (z, x, y) — independent of scale, of the selection, and of which Overpass
    mirror served it (OSM is OSM). Two selections that overlap share their interior
    tiles verbatim; only genuinely-new edge tiles fetch. The same grid is what a local
    .pbf bake fills offline (see osm_pack.py), so "downloaded once, fast forever".

WHAT THIS MODULE IS
    A dependency-light leaf: pure tile<->bbox math (reusing survey._lat_lng_to_tile,
    the canonical primitive that matches the Arnis Rust tiler), plus the per-tile filename
    helper and a `merge_tiles` used only by the offline .pbf bake to combine a border tile's
    copies. Per-cell generation no longer merges anything: Arnis reads the grid tiles straight
    from this cache via --osm-tile-dir. It takes the cache dir as an argument and imports
    nothing from prefetch/datapack, so both can import it without a cycle.

GRID ZOOM
    z11 (~13.7 km / ~190 km² per tile, a few MB of OSM each). Finer than the old z9, so a cell's
    covering set is just its own roughly 9 to 16 tiles, which keeps each cell's Arnis parse small.
    Still a stable, shareable unit and well under the single-Overpass-query budget. Changing this
    REQUIRES a re-bake (z is in the tile filename; old-zoom tiles are simply orphaned, never
    mis-read). Keep it a grid-WIDE constant: a per-run zoom would stop overlapping selections from
    sharing tiles and silently defeat the cache.
"""

from __future__ import annotations

import json
import math
import os
import threading
from pathlib import Path

from .survey import _lat_lng_to_tile   # canonical web-mercator forward tiler (matches Arnis)

# Fixed grid zoom for the OSM cache. See module docstring for the z11 rationale (smaller tiles →
# much cheaper per-cell assembly + Arnis parse). Changing this needs a re-bake of the .pbf pack.
OSM_GRID_Z = 11
# Query-shape version. The grid tile key is pure geography (z/x/y) PLUS this marker, so a
# tile is reused across scales/selections/endpoints. Bump it only if the Overpass query
# shape (tags fetched) changes, to invalidate every old grid tile. It is baked into the
# tile filename so an old-shape tile can never be mistaken for a new-shape one.
OSM_GRID_VERSION = "g1"


def grid_tiles_for_bbox(bbox: dict, z: int = OSM_GRID_Z) -> list[tuple[int, int]]:
    """Every (x, y) grid tile whose rectangle overlaps `bbox` {south,west,north,east}.

    Slippy y increases SOUTHWARD, so the NW corner maps to (min x, min y) and the SE
    corner to (max x, max y). Identical convention to datapack.tiles_for_bbox."""
    x1, y1 = _lat_lng_to_tile(bbox["north"], bbox["west"], z)   # NW -> min x, min y
    x2, y2 = _lat_lng_to_tile(bbox["south"], bbox["east"], z)   # SE -> max x, max y
    xlo, xhi = sorted((x1, x2))
    ylo, yhi = sorted((y1, y2))
    return [(x, y) for x in range(xlo, xhi + 1) for y in range(ylo, yhi + 1)]


def tile_bounds_ll(x: int, y: int, z: int = OSM_GRID_Z) -> dict:
    """Inverse of the tiler: web-mercator tile (x, y, z) -> its lat/lon bbox (EPSG:4326).
    Tiles abut exactly (no gaps/overlap), so a cell's covering tiles union to cover it."""
    n = 2 ** z
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    north = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * (y + 1) / n))))
    return {"south": south, "west": west, "north": north, "east": east}


def tile_filename(x: int, y: int, z: int = OSM_GRID_Z) -> str:
    """On-disk name for a grid tile. Starts with `osm_` so the existing data-pack folder
    import (datapack.import_pack_folder, which globs `osm_*.json`) round-trips grid tiles
    for free; the `g{z}_` infix distinguishes them from legacy clump files osm_<hex>.json
    and carries the query-shape version so a shape bump invalidates the name space."""
    return f"osm_{OSM_GRID_VERSION}_z{z}_{x}_{y}.json"


# ---------------------------------------------------------------------------
# SIDECAR LIFECYCLE (perf phase 5, task A7)
#
# Arnis (WS-A) bakes a bincode sidecar next to every grid tile so repeat renders
# skip the serde_json decode. The pairing contract is purely positional: the
# sidecar for `<tile>.json` is `<tile>.osmbin` in the same directory (same stem,
# suffix swapped). The Rust reader is fail-open — it re-hashes the .json on every
# read and falls back to JSON on any mismatch — so a stale sidecar is never
# MIS-read; these helpers exist so it is also never LEAKED. Rules:
#
#   * whenever a tile .json is deleted (TTL prune, cache cleanup, re-download
#     that removes first), delete the paired .osmbin in the same operation
#     (`remove_tile`, or `reap_sidecar` if the caller deletes the .json itself);
#   * whenever a tile .json is rewritten in place with different bytes (TTL
#     re-download, .pbf bake, merge_tiles below), reap the paired .osmbin — the
#     content hash no longer matches, so the sidecar is dead weight until the
#     next bake anyway;
#   * any cache walk may call `sweep_orphan_sidecars`: an .osmbin with no .json
#     sibling has lost its source of truth and is deleted. Without the sweep,
#     orphaned sidecars accumulate forever and roughly double the OSM cache
#     footprint (the sidecar is the same order of size as the JSON).
#
# Every delete here is best-effort (missing_ok + OSError swallowed): on Windows
# an .osmbin held open by a concurrently rendering Arnis refuses deletion with a
# sharing violation, and the correct response is to leave it for the next sweep,
# never to fail the prune/bake that triggered the reap.
# ---------------------------------------------------------------------------

SIDECAR_SUFFIX = ".osmbin"


def sidecar_path(json_path: Path) -> Path:
    """The bincode sidecar paired with grid-tile `json_path` (suffix swap, same dir)."""
    return Path(json_path).with_suffix(SIDECAR_SUFFIX)


def reap_sidecar(json_path: Path) -> bool:
    """Delete the .osmbin paired with `json_path`, if any. Call this in the same
    operation as any delete/rewrite of the tile .json. Best-effort: returns True
    only if a sidecar existed and was removed; a locked file is left for the next
    orphan sweep rather than raising."""
    sc = sidecar_path(json_path)
    try:
        sc.unlink()                         # racing reapers: first wins, second no-ops
        return True
    except FileNotFoundError:
        return False                        # nothing to reap
    except OSError:
        return False                        # held open (Windows share lock) — next sweep


def remove_tile(json_path: Path) -> None:
    """Delete a grid tile .json AND its paired .osmbin as one operation. The one
    entry point TTL prune / cache cleanup should use instead of unlinking the
    .json directly (which would orphan the sidecar)."""
    reap_sidecar(json_path)                 # sidecar first: never leave .osmbin without .json
    try:
        Path(json_path).unlink(missing_ok=True)
    except OSError:
        pass


def sweep_orphan_sidecars(cache_dir: Path) -> int:
    """Delete every .osmbin in `cache_dir` whose .json sibling is gone; returns the
    number removed. O(#sidecars) stat calls — cheap enough to run on any cache walk
    (prefetch startup, bake coverage scan). Valid pairs and bare .json tiles are
    never touched. Concurrency-safe by fail-open design: the worst race (a bake
    republishing the .json while we look) leaves a deletable sidecar for the next
    sweep or a reaped one the bake simply re-bakes."""
    n = 0
    try:
        sidecars = list(Path(cache_dir).glob(f"*{SIDECAR_SUFFIX}"))
        # Crash orphans: a bake that died between writing its tmp and the atomic rename
        # leaves <tile>.osmbin.tmp<pid>. Nothing else ever deletes those; reap on sight.
        for tmp in Path(cache_dir).glob(f"*{SIDECAR_SUFFIX}.tmp*"):
            try:
                tmp.unlink()
                n += 1
            except OSError:
                pass
    except OSError:
        return 0
    for sc in sidecars:
        if sc.with_suffix(".json").exists():
            continue                        # valid pair — leave it alone
        try:
            sc.unlink(missing_ok=True)
            n += 1
        except OSError:
            pass                            # locked — count nothing, next sweep gets it
    return n


def merge_tiles(tile_paths: list[Path], out_path: Path) -> int:
    """Stitch several grid-tile Overpass JSONs into ONE file. Used by the offline .pbf bake
    to combine a border tile's copies baked from neighbouring countries.

    Concatenate every tile's `elements`, dedup on (type, id) so a boundary way/node that
    appears in two abutting tiles is kept once, and write a single valid Overpass-shaped
    object. Returns the merged element count. (Per-cell generation no longer merges: Arnis
    reads the grid tiles directly via --osm-tile-dir.)
    """
    tmp = out_path.with_name(f"{out_path.stem}.{os.getpid()}.{threading.get_ident()}.tmp")
    seen: set = set()
    n = 0
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write('{"version":0.6,"generator":"meld-osm-merge","elements":[')
            first = True
            for p in tile_paths:           # read ONE tile at a time, never hold them all (no OOM)
                with open(p, encoding="utf-8") as tf:
                    els = json.load(tf).get("elements", [])
                for el in els:
                    k = (el.get("type"), el.get("id"))
                    if k in seen:
                        continue
                    seen.add(k)
                    f.write((json.dumps(el, separators=(",", ":")) if first
                             else "," + json.dumps(el, separators=(",", ":"))))
                    first = False
                    n += 1
            f.write("]}")
        os.replace(tmp, out_path)         # atomic publish; peak mem ~ the seen-set + one tile
        reap_sidecar(out_path)            # rewrite stales any paired .osmbin — reap it (A7)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    return n
