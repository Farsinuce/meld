"""Post-generation missing-region final check.

After a run finishes, enumerate the region files each MERGED cell should own (its canonical
region rectangle, coords.canonical_region_bounds) and compare against what actually landed on
disk. Report only INTERIOR holes - a missing region bracketed by present regions, or a merged
cell that produced NOTHING while surrounded by merged neighbours - so the legitimate ragged
coast / ocean-trim edges that merge.py already treats as normal are never flagged.

The owning cells feed straight into the existing retry path (server._start_generation), the same
one the failed-cell system uses, so leftover holes get regenerated + re-merged seamlessly.

Pure read-only disk scan. Counts `.linear` and `.b_linear` regions as present too, so it still
works after an export has converted some `.mca` regions and for worlds the fork generated
natively in Leaf's B_Linear container.
"""
from __future__ import annotations

import re
from pathlib import Path

from .constants import METERS_PER_DEG_LAT, REGION_BLOCKS
from .coords import canonical_region_bounds, mpd_lon

_MCA_RE = re.compile(r"^r\.(-?\d+)\.(-?\d+)\.mca$")
_LINEAR_RE = re.compile(r"^r\.(-?\d+)\.(-?\d+)\.linear$")
_BLINEAR_RE = re.compile(r"^r\.(-?\d+)\.(-?\d+)\.b_linear$")
# A header-only Anvil region (two 4 KiB sector tables, zero chunks) is exactly 8192 bytes; any
# region holding even one chunk is larger. So <= this = no chunks = treat as absent.
_EMPTY_MCA_BYTES = 8192
# The B_Linear v3 equivalent: a 14-byte header plus a 16-entry all-zero bucket offset table,
# and not one byte more until a bucket carries a chunk.
_EMPTY_BLINEAR_BYTES = 142


def _scan_present(region_dir: Path) -> tuple[set, set]:
    """(present, empty): region coords with a real file on disk, and the subset holding no
    chunks (treated as absent). `.linear` files always count as present (post-export); `.mca`
    and `.b_linear` each have a known chunkless size to compare against."""
    present: set = set()
    empty: set = set()
    if not region_dir.is_dir():
        return present, empty
    for f in region_dir.iterdir():
        empty_at = _EMPTY_MCA_BYTES
        m = _MCA_RE.match(f.name)
        if not m:
            m = _BLINEAR_RE.match(f.name)
            empty_at = _EMPTY_BLINEAR_BYTES
        if not m:
            # Linear carries no cheap chunkless-size tell, so it only counts as present.
            m = _LINEAR_RE.match(f.name)
            empty_at = None
        if not m:
            continue
        rx, rz = int(m.group(1)), int(m.group(2))
        present.add((rx, rz))
        if empty_at is not None:
            try:
                if f.stat().st_size <= empty_at:
                    empty.add((rx, rz))
            except OSError:
                pass
    return present, empty


def find_missing_regions(grid: dict, world_dir, origin: dict, scale: float) -> list[dict]:
    """Return a list of {cell_key, rx, rz, reason, bbox} for every detected interior hole.

    Detection (both false-positive-safe against ragged edges):
      1. A missing/empty region bracketed by present regions on either axis (an interior gap).
      2. A merged cell that produced ZERO regions while it has a merged grid-neighbour on all four
         sides (a whole interior cell that silently dropped out - can't be a coastal/ocean edge).
    """
    region_dir = Path(world_dir) / "region"
    present, empty = _scan_present(region_dir)
    live = present - empty   # a region "counts" only if a real, non-empty file exists

    merged = [k for k, v in grid.items() if v == "merged"]
    # cell index set (rx_idx, rz_idx, size) for the whole-cell neighbour test.
    idx_present: set = set()
    for k in merged:
        parts = k.split(",")
        if len(parts) == 3:
            try:
                idx_present.add((int(parts[0]), int(parts[1]), int(parts[2])))
            except ValueError:
                pass

    expected: dict = {}       # (rx,rz) -> owning cell_key
    cell_expected: dict = {}  # cell_key -> [(rx,rz), ...]
    for cell_key in merged:
        cb = canonical_region_bounds(cell_key)
        if cb is None:
            continue
        rx_min, rx_max, rz_min, rz_max = cb
        lst = []
        for rx in range(rx_min, rx_max + 1):
            for rz in range(rz_min, rz_max + 1):
                expected[(rx, rz)] = cell_key
                lst.append((rx, rz))
        cell_expected[cell_key] = lst

    olat = float(origin["lat"])
    olon = float(origin["lon"])
    ml = mpd_lon(olat)
    rb = float(REGION_BLOCKS)

    def region_bbox(rx: int, rz: int) -> dict:
        # +Z is south, so the region's north edge is the smaller rz.
        return {
            "south": olat - ((rz + 1) * rb) / (METERS_PER_DEG_LAT * scale),
            "west": olon + (rx * rb) / (ml * scale),
            "north": olat - (rz * rb) / (METERS_PER_DEG_LAT * scale),
            "east": olon + ((rx + 1) * rb) / (ml * scale),
        }

    flagged: dict = {}   # (rx,rz) -> reason, deduped

    # 1) interior single-region holes
    for (rx, rz) in expected:
        if (rx, rz) in live:
            continue
        interior = (
            ((rx - 1, rz) in live and (rx + 1, rz) in live)
            or ((rx, rz - 1) in live and (rx, rz + 1) in live)
        )
        if interior:
            flagged[(rx, rz)] = "empty" if (rx, rz) in empty else "missing"

    # 2) whole-cell total loss for a cell surrounded by merged neighbours (never an ocean edge)
    for cell_key, lst in cell_expected.items():
        if any(rc in live for rc in lst):
            continue   # partial coverage -> the interior-region rule handles it (avoids edge FPs)
        parts = cell_key.split(",")
        try:
            cx, cz, cs = int(parts[0]), int(parts[1]), int(parts[2])
        except (ValueError, IndexError):
            continue
        neighboured = all(
            (cx + dx, cz + dz, cs) in idx_present
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1))
        )
        if neighboured:
            for rc in lst:
                flagged.setdefault(rc, "missing")

    missing = []
    for (rx, rz), reason in flagged.items():
        missing.append({
            "cell_key": expected[(rx, rz)],
            "rx": rx, "rz": rz,
            "reason": reason,
            "bbox": region_bbox(rx, rz),
        })
    return missing


def missing_cell_keys(missing: list[dict]) -> list[str]:
    """De-duplicated owning cell_keys (the retry unit - Arnis regenerates a whole cell)."""
    out: list[str] = []
    for m in missing:
        k = m.get("cell_key")
        if k and k not in out:
            out.append(k)
    return out
