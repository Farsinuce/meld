"""
osm_pack.py — bake a local OpenStreetMap .pbf into the stable OSM tile grid.

WHY
    prefetch.run_prefetch now caches OSM on a fixed z-grid (osm_grid.py), so overlapping
    selections reuse tiles instead of re-querying Overpass. This module fills that same grid
    OFFLINE from a Geofabrik-style .osm.pbf, so a whole country is "downloaded once, fast
    forever" with ZERO server calls — exactly like the elevation data pack, but for OSM.

    It is the structural twin of datapack.py: a `coverage_osm()` pure-disk check, a bounded-
    concurrency bake with progress + cooperative stop, and the same {pct,cached,total,missing}
    contract the route layer + UI already speak. Baked tiles land in meld_osm_cache_dir() with
    osm_grid filenames, so run_prefetch's cache-hit path consumes them with no plumbing change.

DEPENDENCY
    pyosmium (`pip install osmium`) — a genuinely NEW dependency, unlike datapack (stdlib + PIL).
    It is imported LAZILY inside the bake so the server and coverage check still work without it;
    only the actual .pbf bake needs it. Coverage/scan that don't parse geometry stay dep-free.

COMPLETE-WAYS, MEMORY-BOUNDED
    Arnis reads Overpass-shaped JSON: nodes carry lat/lon, ways carry node-id REFS, and every
    referenced node must be present in the file. To match that per tile without buffering the
    whole planet:
      • every node is emitted ONCE to its home tile (the tile its coordinate falls in);
      • a way is emitted to each tile its vertices touch, and any of its nodes whose home is a
        DIFFERENT tile (boundary "foreign" nodes) are copied into that tile so the refs resolve;
      • relations are placed via a cheap relations-only pre-pass that records which tiles their
        member ways/nodes live in.
    Home nodes need no dedup (seen once); only the small set of boundary foreign nodes is tracked
    per tile. The node-location index is pyosmium's own (optionally disk-backed for huge extracts).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from pathlib import Path

from . import osm_grid
from .prefetch import meld_osm_cache_dir
from .survey import _lat_lng_to_tile

# A baked tile is "present" if it exists and is at least a minimal valid object. An empty-but-valid
# tile is `{"version":0.6,"generator":"...","elements":[]}` (ocean / no-data inside the pbf) — still
# a real answer, so the floor is small; below it is truncated junk.
_OSM_MIN_BYTES = 12
# pyosmium member-type letters -> Overpass full words (Arnis OsmMember.type is "node"/"way"/...).
_MEMBER_TYPE = {"n": "node", "w": "way", "r": "relation"}


def _tags_suffix(osmtags) -> str:
    """Pre-serialize an element's tags as the `,"tags":{...}` JSON fragment, or '' when untagged.
    Most nodes are untagged way-vertices, so this returns '' for the bulk and never builds a dict
    or calls json for them — the single biggest bake cost (14.7M json.dumps) is gone."""
    if not len(osmtags):
        return ""
    d = {t.k: t.v for t in osmtags}
    return ',"tags":' + json.dumps(d, separators=(",", ":"), ensure_ascii=False)
# Grid tile filename pattern (matches osm_grid.tile_filename): osm_<ver>_z<z>_<x>_<y>.json
_GRID_RE = re.compile(r"^osm_" + re.escape(osm_grid.OSM_GRID_VERSION) + r"_z(\d+)_(\d+)_(\d+)\.json$")


# ── coverage (pure disk, no network, no pyosmium) ─────────────────────────────
def _cached_grid_tiles(z: int = osm_grid.OSM_GRID_Z) -> set:
    """One scandir of the OSM cache -> set of (x,y) grid tiles present at this zoom."""
    have: set = set()
    d = meld_osm_cache_dir()
    try:
        with os.scandir(d) as it:
            for ent in it:
                m = _GRID_RE.match(ent.name)
                if not m or int(m.group(1)) != z:
                    continue
                try:
                    if ent.stat().st_size >= _OSM_MIN_BYTES:
                        have.add((int(m.group(2)), int(m.group(3))))
                except OSError:
                    pass
    except FileNotFoundError:
        pass
    return have


def coverage_osm(bbox: dict, z: int = osm_grid.OSM_GRID_Z) -> dict:
    """Which grid tiles covering `bbox` are already baked/cached. Pure disk, mirrors
    datapack.coverage_elevation: {grid_z, total, cached, missing:[{z,x,y}], pct}. pct is a
    float to 1 dp, 100.0 when the area is empty (so an empty selection reads as fully covered)."""
    tiles = osm_grid.grid_tiles_for_bbox(bbox, z)
    have = _cached_grid_tiles(z)
    missing = [{"z": z, "x": x, "y": y} for (x, y) in tiles if (x, y) not in have]
    total = len(tiles)
    cached = total - len(missing)
    pct = round(100.0 * cached / total, 1) if total else 100.0
    return {"grid_z": z, "total": total, "cached": cached, "missing": missing, "pct": pct}


# ── is baking possible at all? ────────────────────────────────────────────────
def osmium_status() -> dict:
    """{ok, reason} - can this install bake a .pbf, and if not, what does the user do about it?

    Worth a function rather than a bare try/except at each call site because the honest answer
    differs by install. A packaged build that shipped without osmium cannot be fixed with
    `pip install`: the bundled runtime is not the one pip writes to, so telling a user to run pip
    sends them somewhere that cannot help. That is exactly what happened - the flag was excluded
    from the build, the UI said "(no header bbox)" on every file, and the only real fix was a
    source install nobody mentioned.
    """
    try:
        import osmium  # noqa: F401
        return {"ok": True, "reason": ""}
    except Exception as ex:                                   # noqa: BLE001
        from .paths import is_frozen
        if is_frozen():
            return {"ok": False, "reason":
                    "This build of Meld was packaged without the .pbf reader, so offline OSM "
                    "baking is unavailable. Everything else works; online OSM is unaffected. "
                    f"({ex})"}
        return {"ok": False, "reason":
                f"pyosmium is not installed in this Python, so .pbf files cannot be read. "
                f"Install it with:  pip install osmium   ({ex})"}


# ── .pbf discovery (pyosmium for the header bbox; gracefully degrades) ─────────
def scan_pbf_folder(folder: str) -> dict:
    """List .osm.pbf files in `folder` with their header bbox (if present) and size. Used by the
    UI to confirm a drop folder before baking.

    Returns {ok, osmium:{ok,reason}, files:[{path,name,size_bytes,bbox|None}]}. The osmium status
    rides along so the UI can say why every bbox is missing instead of implying the FILES are at
    fault - "(no header bbox)" on all eight continent extracts means the reader is absent, not
    that Geofabrik shipped eight broken files.
    """
    d = Path(folder).expanduser()
    if not d.is_dir():
        return {"ok": False, "error": f"not a folder: {folder}", "files": []}
    st = osmium_status()
    try:
        import osmium  # lazy; scan still lists files if pyosmium is absent (bbox=None)
    except Exception:
        osmium = None
    files = []
    for p in sorted(d.glob("*.pbf")) + sorted(d.glob("*.osm.pbf")):
        bbox = None
        if osmium is not None:
            try:
                box = osmium.FileProcessor(str(p)).header.box()
                if box and box.valid():
                    bbox = {"south": box.bottom_left.lat, "west": box.bottom_left.lon,
                            "north": box.top_right.lat, "east": box.top_right.lon}
            except Exception:
                bbox = None
        try:
            sz = p.stat().st_size
        except OSError:
            sz = 0
        files.append({"path": str(p), "name": p.name, "size_bytes": sz, "bbox": bbox})
    # de-dup (the two globs overlap on *.osm.pbf)
    seen, uniq = set(), []
    for f in files:
        if f["path"] in seen:
            continue
        seen.add(f["path"]); uniq.append(f)
    return {"ok": True, "osmium": st, "files": uniq}


# ── planning a bake before it eats the machine ────────────────────────────────
#
# Every constant here was measured on a real bake rather than reasoned about, because the first
# guess at each of them was wrong by an order of magnitude.

# Peak RSS per worker, per GB of .pbf. Measured with pyosmium 4.3.1 over ukraine-latest
# (871 MB, 125.7M elements): 1910 MB peak, i.e. 2192 MB per GB. It is the node-location index;
# the same pass WITHOUT locations is flat at 124 MB whatever the file size.
#
# This is what turned a 33 GB europe-latest into a 72 GB demand on a 68 GB machine, four workers
# at a time, and pushed Windows into a 190 GB pagefile. A disk-backed index (sparse_file_array)
# was measured too and saved nothing: 1919 MB peak plus a 1.8 GB index file.
RAM_GB_PER_PBF_GB = 2.2

# Baked JSON per byte of .pbf, for a region-matched extract. Measured: 2.03 GB of .pbf across six
# Balkan countries produced 59.6 GB of tiles. The output is uncompressed Overpass-style JSON, so
# it expands hard, and this is the number nobody had before running out of disk.
BAKED_PER_PBF = 29.0

# Elements per second, single worker. Measured: 125.7M elements in ~147 s.
PBF_MB_PER_SEC = 6.0


def _bbox_overlaps(a: dict, b: dict) -> bool:
    """Do two lat/lon boxes intersect at all? Touching edges count - a way can sit exactly on one."""
    if not a or not b:
        return True                      # unknown bbox: never exclude on a guess
    return not (a["north"] < b["south"] or a["south"] > b["north"]
                or a["east"] < b["west"] or a["west"] > b["east"])


def select_pbfs(files: list[dict], region_bbox: dict | None) -> tuple[list[dict], list[dict]]:
    """Split scanned .pbf files into (needed, skipped) for this region.

    The scan already reads every header bbox and the bake then ignored it, so dropping eight
    continent extracts in a folder to render New Hampshire read all 75 GB of them. north-america
    alone is 19 GB; the state extract is 0.07 GB. Same output, 275x less memory.

    A file whose bbox cannot be read is kept, never skipped: excluding data because we failed to
    parse a header would silently produce a world with holes in it.
    """
    if not region_bbox:
        return list(files), []
    need = [f for f in files if _bbox_overlaps(f.get("bbox"), region_bbox)]
    skip = [f for f in files if f not in need]
    return need, skip


def plan_bake(files: list[dict], tiles: list, workers_requested: int = 0,
              region_bbox: dict | None = None, cache_dir: Path | None = None) -> dict:
    """What this bake will cost, before a byte is read.

    Returns RAM, disk and time estimates plus a worker count that fits in the memory actually
    available. `fits` is False when even one worker cannot run, and `reason` then says what to do
    instead - which for a continent extract is almost always "use the country or state file".
    """
    import shutil as _sh
    try:
        import psutil
        avail = psutil.virtual_memory().available
        cores = os.cpu_count() or 4
    except Exception:                                          # noqa: BLE001
        avail, cores = 8 * 1e9, 4

    need, skipped = select_pbfs(files, region_bbox)
    total_gb = sum(f.get("size_bytes", 0) for f in need) / 1e9
    largest_gb = max((f.get("size_bytes", 0) for f in need), default=0) / 1e9

    # Peak is driven by the LARGEST file a worker might take, not the average: workers are handed
    # one .pbf each, so the worst case is several big ones running at once.
    per_worker_gb = max(0.2, largest_gb * RAM_GB_PER_PBF_GB)
    budget = (avail / 1e9) * 0.6            # leave the OS, the browser and any render some room
    fit = int(budget // per_worker_gb)
    workers = max(1, min(fit if fit > 0 else 1, cores, len(need) or 1,
                         workers_requested or cores))

    final_gb = total_gb * BAKED_PER_PBF
    # Peak disk holds the per-.pbf temp trees AND the merged output at the same time.
    peak_gb = final_gb * 2.0
    free_gb = 0.0
    try:
        free_gb = _sh.disk_usage(str(cache_dir or Path("."))).free / 1e9
    except Exception:                                          # noqa: BLE001
        pass

    fits, reason = True, ""
    if fit < 1:
        fits = False
        reason = (f"This bake needs about {per_worker_gb:.0f} GB of memory for a single worker "
                  f"and only {avail / 1e9:.0f} GB is free. The file is far larger than the area "
                  f"being rendered - download the country or state extract for this region "
                  f"instead of the continent one; it is typically hundreds of times smaller.")
    elif free_gb and peak_gb > free_gb * 0.9:
        fits = False
        reason = (f"This bake would need about {peak_gb:.0f} GB of disk at its peak "
                  f"({final_gb:.0f} GB of tiles, briefly doubled while merging) and only "
                  f"{free_gb:.0f} GB is free.")

    return {
        "fits": fits, "reason": reason,
        "workers": workers, "cores": cores,
        "pbf_used": len(need), "pbf_skipped": len(skipped),
        "skipped_names": [f["name"] for f in skipped][:12],
        "pbf_gb": round(total_gb, 2), "largest_gb": round(largest_gb, 2),
        "ram_peak_gb": round(per_worker_gb * workers, 1),
        "ram_free_gb": round(avail / 1e9, 1),
        "disk_final_gb": round(final_gb, 1), "disk_peak_gb": round(peak_gb, 1),
        "disk_free_gb": round(free_gb, 1),
        "tiles": len(tiles),
        "eta_min": round(total_gb * 1000 / PBF_MB_PER_SEC / max(1, workers) / 60, 1),
    }


def project_from_progress(done_tiles: int, bytes_written: int, total_tiles: int) -> dict:
    """Refine the disk estimate from what this region has actually produced.

    The up-front number comes from one sampled region, and tile sizes span 88x between sparse and
    dense (measured: median 5.5 MB, p99 116 MB, max 1089 MB). So once real tiles exist, they are
    a far better predictor of the rest than any constant - and this is what lets the UI stop
    quoting a range and start quoting a number.
    """
    if done_tiles < 5 or total_tiles <= 0:
        return {}
    per = bytes_written / done_tiles
    return {"projected_final_gb": round(per * total_tiles / 1e9, 1),
            "per_tile_mb": round(per / 1e6, 1),
            "from_tiles": done_tiles}


# ── the bake ──────────────────────────────────────────────────────────────────
class _TileWriter:
    """Streams one grid tile's Overpass JSON to a per-pid/thread tmp file, published atomically on
    close. Home nodes are written once (no dedup); only boundary foreign nodes are deduped (small)."""

    def __init__(self, final_path: Path):
        self.final = final_path
        self.tmp = final_path.with_name(f"{final_path.stem}.{os.getpid()}.{threading.get_ident()}.tmp")
        self.f = open(self.tmp, "w", encoding="utf-8")
        self.f.write('{"version":0.6,"generator":"meld-osm-pbf","elements":[')
        self._first = True
        self.foreign_seen: set = set()
        self.count = 0

    def _raw(self, s: str) -> None:
        # Elements are serialized by the caller (manual string for the hot node path, json only for
        # tag/member payloads), so the bulk of elements never touch json.dumps. Just framing here.
        if self._first:
            self.f.write(s)
            self._first = False
        else:
            self.f.write(",")
            self.f.write(s)
        self.count += 1

    def node(self, nid: int, lon: float, lat: float, tags_suffix: str) -> None:
        self._raw('{"type":"node","id":%d,"lat":%r,"lon":%r%s}' % (nid, lat, lon, tags_suffix))

    def foreign_node(self, nid: int, lon: float, lat: float) -> None:
        if nid in self.foreign_seen:
            return
        self.foreign_seen.add(nid)
        self._raw('{"type":"node","id":%d,"lat":%r,"lon":%r}' % (nid, lat, lon))

    def way(self, wid: int, refs: list, tags_suffix: str) -> None:
        self._raw('{"type":"way","id":%d,"nodes":[%s]%s}'
                  % (wid, ",".join(map(str, refs)), tags_suffix))

    def relation(self, rid: int, members: list, tags_suffix: str) -> None:
        self._raw('{"type":"relation","id":%d,"members":%s%s}'
                  % (rid, json.dumps(members, separators=(",", ":"), ensure_ascii=False), tags_suffix))

    def close(self) -> int:
        self.f.write("]}")
        self.f.close()
        os.replace(self.tmp, self.final)
        return self.count

    def abort(self) -> None:
        try:
            self.f.close()
        finally:
            try:
                self.tmp.unlink(missing_ok=True)
            except Exception:
                pass


def bake_tiles(pbf_paths, tiles, *, on_progress=None, should_stop=None, log=None,
               node_storage: str | None = None, force: bool = False) -> dict:
    """Slice the given .pbf file(s) into Overpass-shaped JSON for exactly the `tiles` (a list of
    (x,y) at OSM_GRID_Z) and publish them into the shared OSM cache.

    on_progress(done, total, ok, skip, absent, fail): done/total are tiles resolved; ok=written
    with data, absent=written empty (no OSM in that tile), skip=already cached, fail=errored.
    should_stop(): cooperative cancel, polled per pbf and periodically mid-pass. Returns a counts
    dict. Raises RuntimeError if pyosmium is unavailable (the route turns that into a clean error)."""
    try:
        import osmium
    except Exception as ex:  # noqa: BLE001
        raise RuntimeError(
            "pyosmium is required to bake a .pbf (pip install osmium). It is a new dependency; "
            f"the rest of Meld runs without it. Import error: {ex}")

    def _log(m):
        if log:
            log(m)

    z = osm_grid.OSM_GRID_Z
    target = set(tiles)
    total = len(target)
    cache_dir = meld_osm_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)

    have = _cached_grid_tiles(z) if not force else set()
    todo = {t for t in target if t not in have}
    skip = total - len(todo)
    # Union lat/lon bbox of the tiles we still need, so a .pbf whose extent doesn't reach them is
    # skipped without a full multi-GB scan (a Romania-side gap never needs the Serbia/Bulgaria file).
    if todo:
        _tb = [osm_grid.tile_bounds_ll(x, y, z) for (x, y) in todo]
        target_bbox = {"south": min(b["south"] for b in _tb), "west": min(b["west"] for b in _tb),
                       "north": max(b["north"] for b in _tb), "east": max(b["east"] for b in _tb)}
    else:
        target_bbox = None
    if on_progress:
        on_progress(skip, total, 0, skip, 0, 0)
    if not todo:
        _log(f"  [OSM bake] all {total} tile(s) already cached")
        return {"ok": True, "total": total, "baked": 0, "empty": 0, "skip": skip, "fail": 0, "elements": 0}

    writers: dict = {}            # (x,y) -> _TileWriter (only created when a tile first gets content)

    def tile_of(lat: float, lon: float):
        return _lat_lng_to_tile(lat, lon, z)

    def W(xy):
        w = writers.get(xy)
        if w is None:
            w = _TileWriter(cache_dir / osm_grid.tile_filename(xy[0], xy[1]))
            writers[xy] = w
        return w

    total_elems = 0
    bad_pbf = 0
    try:
        for pi, pbf in enumerate(pbf_paths):
            if should_stop and should_stop():
                raise _Stopped()      # discard partial tiles (a border tile may need a later .pbf too)
            pbf = str(pbf)
            # Skip a .pbf whose header bbox doesn't overlap any tile we still need — the header is at
            # the start of the file, so this avoids streaming a whole country for nothing. Header
            # missing/unreadable → fall through and scan it (safe default).
            if target_bbox is not None:
                try:
                    hb = osmium.FileProcessor(pbf).header.box()
                    if hb.valid():
                        pb_s, pb_w = hb.bottom_left.lat, hb.bottom_left.lon
                        pb_n, pb_e = hb.top_right.lat, hb.top_right.lon
                        if not (pb_w <= target_bbox["east"] and pb_e >= target_bbox["west"]
                                and pb_s <= target_bbox["north"] and pb_n >= target_bbox["south"]):
                            _log(f"  [OSM bake] skipping {Path(pbf).name} "
                                 f"({pi + 1}/{len(pbf_paths)}) — bbox doesn't reach the needed tile(s)")
                            continue
                except Exception:  # noqa: BLE001
                    pass
            _log(f"  [OSM bake] reading {Path(pbf).name} ({pi + 1}/{len(pbf_paths)})…")
            # A single corrupt/truncated/unreadable .pbf (e.g. a half-copied file or a flaky drive
            # dropping mid-read → 'failed to uncompress data: buffer error') must NOT kill the whole
            # bake. Skip just that file and keep the others' tiles; only _Stopped (cancel) unwinds.
            try:
                # Pass 0 (relations only): which way/node ids do relations reference, so we record
                # just those members' tiles in the main pass (keeps the maps tiny).
                rel_ways: set = set()
                rel_nodes: set = set()
                rel_seen = 0
                for o in osmium.FileProcessor(pbf, osmium.osm.RELATION):   # relations only
                    rel_seen += 1
                    # Minutes on a country .pbf, so Stop is honoured here too.
                    if (rel_seen & 0xFFFF) == 0 and should_stop and should_stop():
                        _log("  [OSM bake] stopped during the relation pass")
                        raise _Stopped()
                    for m in o.members:
                        if m.type == "w":
                            rel_ways.add(m.ref)
                        elif m.type == "n":
                            rel_nodes.add(m.ref)

                way_tile: dict = {}    # way id -> set of target tiles it touches (only for rel_ways)
                node_tile: dict = {}   # node id -> home tile (only for rel_nodes)

                # Pass 1 (main): nodes -> home tile, ways -> touched tiles + foreign nodes, relations last.
                fp = osmium.FileProcessor(pbf)
                fp = fp.with_locations(node_storage) if node_storage else fp.with_locations()
                seen = 0
                for o in fp:
                    seen += 1
                    if (seen & 0x3FFFF) == 0:      # ~ every 260k elements: sub-second stop latency
                        if should_stop and should_stop():
                            _log("  [OSM bake] stopped mid-file")
                            raise _Stopped()
                        if (seen & 0x1FFFFF) == 0:  # ~ every 2M elements: progress line
                            _log(f"  [OSM bake] {Path(pbf).name}: {seen // 1_000_000}M elements…")

                    if isinstance(o, osmium.osm.Node):
                        loc = o.location
                        if not loc.valid():
                            continue
                        home = tile_of(loc.lat, loc.lon)
                        if o.id in rel_nodes:
                            node_tile[o.id] = home
                        if home in target:
                            W(home).node(o.id, loc.lon, loc.lat, _tags_suffix(o.tags))

                    elif isinstance(o, osmium.osm.Way):
                        refs = []
                        homes = {}     # node ref -> (home tile, lon, lat) for valid nodes
                        touched: set = set()
                        for n in o.nodes:
                            refs.append(n.ref)
                            nl = n.location
                            if nl.valid():
                                h = tile_of(nl.lat, nl.lon)
                                homes[n.ref] = (h, nl.lon, nl.lat)
                                touched.add(h)
                        emit = touched & target
                        if o.id in rel_ways and emit:
                            way_tile[o.id] = set(emit)
                        if not emit:
                            continue
                        tsuf = _tags_suffix(o.tags)
                        for T in emit:
                            w = W(T)
                            w.way(o.id, refs, tsuf)
                            for ref, (h, lon, lat) in homes.items():
                                if h != T:                      # boundary node foreign to T
                                    w.foreign_node(ref, lon, lat)

                    elif isinstance(o, osmium.osm.Relation):
                        place: set = set()
                        members = []
                        for m in o.members:
                            members.append({"type": _MEMBER_TYPE.get(m.type, m.type),
                                            "ref": m.ref, "role": m.role})
                            if m.type == "w" and m.ref in way_tile:
                                place |= way_tile[m.ref]
                            elif m.type == "n" and m.ref in node_tile:
                                place.add(node_tile[m.ref])
                        place &= target
                        if not place:
                            continue
                        tsuf = _tags_suffix(o.tags)
                        for T in place:
                            W(T).relation(o.id, members, tsuf)
            except _Stopped:
                raise
            except Exception as ex:  # noqa: BLE001
                bad_pbf += 1
                _log(f"  [OSM bake] SKIPPED {Path(pbf).name} — corrupt/unreadable .pbf ({ex}); "
                     f"continuing with the other file(s). Re-download it (avoid flaky drives).")
                continue

            if on_progress:
                on_progress(skip + len(writers), total, len(writers), skip, 0, 0)

    except _Stopped:
        # Discard EVERY partially-written tile — close() would append ']}' and publish a tile that
        # LOOKS complete but holds only the elements seen before the stop, masquerading as cached.
        for w in writers.values():
            w.abort()
        if on_progress:
            on_progress(skip, total, 0, skip, 0, 0)
        _log("  [OSM bake] stopped — partial tiles discarded (re-bake to finish)")
        return {"ok": False, "stopped": True, "total": total, "baked": 0, "empty": 0,
                "skip": skip, "fail": 0, "elements": 0}
    except Exception as ex:  # noqa: BLE001
        for w in writers.values():
            w.abort()
        raise RuntimeError(f"bake failed: {ex}")

    # Finalize: atomically publish each tile that got content. A target tile that got NOTHING (no OSM
    # in the .pbf there, e.g. open sea, or outside the .pbf's coverage) is left missing → that cell
    # falls back to live Overpass, which also returns empty. One tile's publish failing never aborts
    # the rest, and never leaves a half-written tmp behind.
    baked = fail = 0
    for xy, w in list(writers.items()):
        try:
            w.close()
            baked += 1
            total_elems += w.count
        except Exception as ex:  # noqa: BLE001
            w.abort()
            fail += 1
            _log(f"  [OSM bake] could not finalize tile {xy} ({ex}) — left missing")
    # Empty sentinel: a TARGET tile the .pbf had NO OSM for (open sea / outside the .pbf) gets a valid
    # empty tile written, so coverage reads a truthful 100% and a build NEVER re-fetches it live again.
    written = set(writers.keys())
    empty = 0
    for xy in todo:
        if xy in written or (should_stop and should_stop()):
            continue
        p = cache_dir / osm_grid.tile_filename(xy[0], xy[1])
        tmp = p.with_name(f"{p.stem}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write('{"version":0.6,"generator":"meld-osm-empty","elements":[]}')
            os.replace(tmp, p)
            empty += 1
        except Exception:  # noqa: BLE001
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
    if on_progress:
        on_progress(total, total, baked + empty, skip, empty, fail)
    _log(f"  [OSM bake] wrote {baked} tile(s) ({total_elems} elements) + {empty} empty sentinel(s) "
         f"for no-OSM/sea tiles ({skip} already cached"
         + (f", {fail} failed" if fail else "") + ") → coverage is now truthful")
    return {"ok": True, "total": total, "baked": baked, "empty": empty, "skip": skip,
            "fail": fail, "elements": total_elems}


def _write_empty_sentinel(path: Path) -> bool:
    """Atomically write a valid empty tile so coverage reads it as cached (sea / no-OSM)."""
    tmp = path.with_name(f"{path.stem}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write('{"version":0.6,"generator":"meld-osm-empty","elements":[]}')
        os.replace(tmp, path)
        return True
    except Exception:  # noqa: BLE001
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def _scan_pbf_into(pbf: str, target_list, out_dir: str, z: int, stop_path):
    """Bake ONE .pbf into out_dir — one tile JSON per touched target tile (same complete-ways logic as
    the sequential bake, to a PRIVATE dir). TOP-LEVEL + picklable so it runs in a worker PROCESS (real
    parallelism past the GIL). Border tiles written by several .pbf are reconciled by the caller via
    osm_grid.merge_tiles. Cooperative stop: aborts if `stop_path` exists. A corrupt/unreadable .pbf
    returns ok=False (caller keeps the others). Returns a plain picklable dict."""
    import osmium
    target = {(int(a), int(b)) for a, b in target_list}
    od = Path(out_dir)
    od.mkdir(parents=True, exist_ok=True)
    writers: dict = {}

    def tile_of(lat, lon):
        return _lat_lng_to_tile(lat, lon, z)

    def W(xy):
        w = writers.get(xy)
        if w is None:
            w = _TileWriter(od / osm_grid.tile_filename(xy[0], xy[1], z))
            writers[xy] = w
        return w

    def stopped():
        return bool(stop_path) and os.path.exists(stop_path)

    try:
        rel_ways: set = set()
        rel_nodes: set = set()
        rel_seen = 0
        for o in osmium.FileProcessor(pbf, osmium.osm.RELATION):
            rel_seen += 1
            # The relation pre-pass alone runs for minutes on a country .pbf, so it has to
            # honour the stop too — otherwise Stop does nothing until the pass finishes.
            if (rel_seen & 0xFFFF) == 0 and stopped():
                return {"written": [], "elements": 0, "ok": True, "stopped": True}
            for m in o.members:
                if m.type == "w":
                    rel_ways.add(m.ref)
                elif m.type == "n":
                    rel_nodes.add(m.ref)
        way_tile: dict = {}
        node_tile: dict = {}
        seen = 0
        for o in osmium.FileProcessor(pbf).with_locations():
            seen += 1
            # ~260k elements between checks: a fraction of a second, versus the 2M
            # (tens of seconds) this used to wait before noticing.
            if (seen & 0x3FFFF) == 0 and stopped():
                for w in writers.values():
                    w.abort()
                return {"written": [], "elements": 0, "ok": True, "stopped": True}
            if isinstance(o, osmium.osm.Node):
                loc = o.location
                if not loc.valid():
                    continue
                home = tile_of(loc.lat, loc.lon)
                if o.id in rel_nodes:
                    node_tile[o.id] = home
                if home in target:
                    W(home).node(o.id, loc.lon, loc.lat, _tags_suffix(o.tags))
            elif isinstance(o, osmium.osm.Way):
                refs = []
                homes = {}
                touched: set = set()
                for n in o.nodes:
                    refs.append(n.ref)
                    nl = n.location
                    if nl.valid():
                        h = tile_of(nl.lat, nl.lon)
                        homes[n.ref] = (h, nl.lon, nl.lat)
                        touched.add(h)
                emit = touched & target
                if o.id in rel_ways and emit:
                    way_tile[o.id] = set(emit)
                if not emit:
                    continue
                tsuf = _tags_suffix(o.tags)
                for T in emit:
                    w = W(T)
                    w.way(o.id, refs, tsuf)
                    for ref, (h, lon, lat) in homes.items():
                        if h != T:
                            w.foreign_node(ref, lon, lat)
            elif isinstance(o, osmium.osm.Relation):
                place: set = set()
                members = []
                for m in o.members:
                    members.append({"type": _MEMBER_TYPE.get(m.type, m.type),
                                    "ref": m.ref, "role": m.role})
                    if m.type == "w" and m.ref in way_tile:
                        place |= way_tile[m.ref]
                    elif m.type == "n" and m.ref in node_tile:
                        place.add(node_tile[m.ref])
                place &= target
                if not place:
                    continue
                tsuf = _tags_suffix(o.tags)
                for T in place:
                    W(T).relation(o.id, members, tsuf)
    except Exception:  # noqa: BLE001
        for w in writers.values():
            try:
                w.abort()
            except Exception:
                pass
        return {"written": [], "elements": 0, "ok": False, "stopped": False}

    written = []
    elems = 0
    for xy, w in writers.items():
        try:
            w.close()
            written.append([xy[0], xy[1]])
            elems += w.count
        except Exception:  # noqa: BLE001
            try:
                w.abort()
            except Exception:
                pass
    return {"written": written, "elements": elems, "ok": True, "stopped": False}


def bake_tiles_parallel(pbf_paths, tiles, *, on_progress=None, should_stop=None, log=None,
                        force=False, workers=None) -> dict:
    """Parallel front end to the bake: skip non-overlapping .pbf, bake each overlapping one in its OWN
    process into a private temp dir, then MERGE per tile into the shared cache (border tiles deduped by
    osm_grid.merge_tiles) + empty sentinels for the rest. ~3-5x vs sequential on a multi-core box.
    DEGRADES to the sequential bake_tiles() when there is <2 overlapping .pbf, the pool errors, or
    parallel is otherwise unavailable — so it never does worse than today. Same return contract."""
    def _log(m):
        if log:
            log(m)
    try:
        import osmium
    except Exception as ex:  # noqa: BLE001
        raise RuntimeError(f"pyosmium required (pip install osmium): {ex}")

    z = osm_grid.OSM_GRID_Z
    target = set(tiles)
    total = len(target)
    cache_dir = meld_osm_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    have = _cached_grid_tiles(z) if not force else set()
    todo = {t for t in target if t not in have}
    skip = total - len(todo)
    if on_progress:
        on_progress(skip, total, 0, skip, 0, 0)
    if not todo:
        _log(f"  [OSM bake] all {total} tile(s) already cached")
        return {"ok": True, "total": total, "baked": 0, "empty": 0, "skip": skip, "fail": 0, "elements": 0}

    # bbox-skip: keep only .pbf whose header reaches a needed tile.
    _tb = [osm_grid.tile_bounds_ll(x, y, z) for (x, y) in todo]
    tbb = {"south": min(b["south"] for b in _tb), "west": min(b["west"] for b in _tb),
           "north": max(b["north"] for b in _tb), "east": max(b["east"] for b in _tb)}
    pbfs = []
    for pbf in pbf_paths:
        pbf = str(pbf)
        try:
            hb = osmium.FileProcessor(pbf).header.box()
            if hb.valid() and not (
                    hb.bottom_left.lon <= tbb["east"] and hb.top_right.lon >= tbb["west"]
                    and hb.bottom_left.lat <= tbb["north"] and hb.top_right.lat >= tbb["south"]):
                _log(f"  [OSM bake] skipping {Path(pbf).name} — bbox doesn't reach the needed tile(s)")
                continue
        except Exception:  # noqa: BLE001
            pass
        pbfs.append(pbf)

    if len(pbfs) < 2:   # nothing to parallelize → the proven sequential path
        return bake_tiles(pbfs or [str(p) for p in pbf_paths], tiles, on_progress=on_progress,
                          should_stop=should_stop, log=log, force=force)

    target_list = [[x, y] for (x, y) in todo]
    tmp_root = cache_dir / ".bake_parallel"
    shutil.rmtree(tmp_root, ignore_errors=True)
    tmp_root.mkdir(parents=True, exist_ok=True)
    stop_path = str(tmp_root / "STOP")
    cap = max(1, min(int(workers) if workers else 4, len(pbfs)))
    _log(f"  [OSM bake] parallel: {len(pbfs)} .pbf across {cap} process(es), then merge seams…")

    results = []
    try:
        import concurrent.futures as _cf
        import multiprocessing as _mp
        ctx = _mp.get_context("spawn")
        with _cf.ProcessPoolExecutor(max_workers=cap, mp_context=ctx) as ex:
            futs = {ex.submit(_scan_pbf_into, pbf, target_list, str(tmp_root / f"p{i}"), z, stop_path):
                    (i, pbf) for i, pbf in enumerate(pbfs)}
            done_p = 0
            pending = set(futs)
            stopped_now = False
            while pending:
                # Poll the stop flag on a TIMER, not once per finished .pbf: one country .pbf
                # runs for minutes, so waiting for a completion made "Stop bake" look dead.
                # The workers watch `stop_path`, so writing it is what actually stops them.
                done_set, pending = _cf.wait(pending, timeout=1.0,
                                             return_when=_cf.FIRST_COMPLETED)
                if not stopped_now and should_stop and should_stop():
                    stopped_now = True
                    open(stop_path, "w").close()
                    _log("  [OSM bake] stop requested — asking the bake workers to abort…")
                for fut in done_set:
                    i, pbf = futs[fut]
                    res = fut.result()
                    res["dir"] = str(tmp_root / f"p{i}")
                    results.append(res)
                    done_p += 1
                    _log(f"  [OSM bake] baked {Path(pbf).name} ({done_p}/{len(pbfs)}) — "
                         f"{len(res['written'])} tile(s)"
                         + ("" if res["ok"] else " (SKIPPED, unreadable)"))
                    if on_progress:
                        on_progress(skip + int(len(todo) * 0.85 * done_p / len(pbfs)),
                                    total, done_p, skip, 0, 0)
    except Exception as ex:  # noqa: BLE001
        shutil.rmtree(tmp_root, ignore_errors=True)
        _log(f"  [OSM bake] parallel pool failed ({ex}) — falling back to sequential")
        return bake_tiles(pbfs, tiles, on_progress=on_progress, should_stop=should_stop,
                          log=log, force=force)

    if should_stop and should_stop():
        shutil.rmtree(tmp_root, ignore_errors=True)
        if on_progress:
            on_progress(skip, total, 0, skip, 0, 0)
        _log("  [OSM bake] stopped — partial tiles discarded (re-bake to finish)")
        return {"ok": False, "stopped": True, "total": total, "baked": 0, "empty": 0,
                "skip": skip, "fail": 0, "elements": 0}

    # Merge per tile: gather each tile's versions across the per-pbf dirs.
    sources: dict = {}
    elements = 0
    for res in results:
        elements += res.get("elements", 0)
        od = Path(res["dir"])
        for xy in res.get("written", []):
            sources.setdefault((xy[0], xy[1]), []).append(
                od / osm_grid.tile_filename(xy[0], xy[1], z))
    baked = empty = fail = 0
    for xy in todo:
        final = cache_dir / osm_grid.tile_filename(xy[0], xy[1], z)
        srcs = sources.get(xy, [])
        try:
            if len(srcs) == 1:
                os.replace(srcs[0], final)         # one .pbf → move (atomic, same drive)
                baked += 1
            elif len(srcs) >= 2:
                osm_grid.merge_tiles(srcs, final)  # border tile → dedup the countries' copies
                baked += 1
            elif _write_empty_sentinel(final):     # no .pbf had OSM here → truthful empty
                empty += 1
        except Exception as ex:  # noqa: BLE001
            fail += 1
            _log(f"  [OSM bake] could not finalize tile {xy} ({ex}) — left missing")

    shutil.rmtree(tmp_root, ignore_errors=True)
    if on_progress:
        on_progress(total, total, baked + empty, skip, empty, fail)
    _log(f"  [OSM bake] wrote {baked} tile(s) ({elements} elements) + {empty} empty sentinel(s) "
         f"({skip} already cached" + (f", {fail} failed" if fail else "") + ") → coverage truthful")
    return {"ok": True, "total": total, "baked": baked, "empty": empty, "skip": skip,
            "fail": fail, "elements": elements}


class _Stopped(Exception):
    """Internal: cooperative-stop unwind."""
