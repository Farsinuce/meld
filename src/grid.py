"""
grid.py — turn a selection bbox into a uniform grid of region-aligned cells.

v1 is a uniform grid (no adaptive recursive subdivision). Every cell edge falls
on an integer multiple of the cell degree-step from the origin, so cells tile
the world with no gaps at region granularity (light-docs/02).
"""

from __future__ import annotations

import math
import os

from .coords import job_cell_deg, cell_bbox

# A plan is a list of dicts held in RAM and sent to the browser as JSON, so an absurd
# selection (the whole planet at 1:1 is ~2e8 cells) has to be refused BEFORE the list is
# built — materialising it first is what killed the server. Override for real monsters.
MAX_PLAN_CELLS = max(100, int(os.environ.get("MELD_MAX_PLAN_CELLS", "20000")))


class TooManyCells(ValueError):
    """Selection would plan more cells than `limit`. Carries the numbers for the UI."""

    def __init__(self, count: int, limit: int, size: int, scale: float):
        self.count, self.limit, self.size, self.scale = count, limit, size, scale
        bigger = min(64, max(size * 2, 8))
        super().__init__(
            f"That selection plans about {count:,} cells (limit {limit:,}). "
            f"At scale {scale:g} with {size}-region cells it would not fit in memory or on disk. "
            f"Try: a bigger cell size (e.g. {bigger} regions, ~{(bigger / size) ** 2:.0f}x fewer cells), "
            f"a smaller scale, or a smaller area."
        )


def count_cells_for_bbox(bbox: dict, origin: dict, scale: float, size: int) -> int:
    """How many cells `cells_for_bbox` would produce — arithmetic only, no allocation."""
    olat, olon = float(origin["lat"]), float(origin["lon"])
    d_lat, d_lon = job_cell_deg(size, olat, scale)
    if d_lat <= 0 or d_lon <= 0:
        return 0
    south, north = float(bbox["south"]), float(bbox["north"])
    west, east = float(bbox["west"]), float(bbox["east"])
    nz = math.ceil((north - olat) / d_lat) - math.floor((south - olat) / d_lat)
    nx = math.ceil((east - olon) / d_lon) - math.floor((west - olon) / d_lon)
    return max(0, nz) * max(0, nx)


def cells_for_bbox(bbox: dict, origin: dict, scale: float, size: int,
                   max_cells: int | None = MAX_PLAN_CELLS) -> list[dict]:
    """
    Return a list of cells covering `bbox`:
        [{ "cell_key": "rx,rz,size", "bbox": {south,west,north,east} }, ...]

    Indices are integer steps from the origin; a cell is included when it
    overlaps the selection bbox at all.

    Raises `TooManyCells` when the grid would exceed `max_cells` (checked by arithmetic
    before anything is allocated). Pass max_cells=None to skip the guard.
    """
    olat, olon = float(origin["lat"]), float(origin["lon"])
    d_lat, d_lon = job_cell_deg(size, olat, scale)
    if d_lat <= 0 or d_lon <= 0:
        return []

    south, north = float(bbox["south"]), float(bbox["north"])
    west, east = float(bbox["west"]), float(bbox["east"])

    if max_cells is not None:
        n = count_cells_for_bbox(bbox, origin, scale, size)
        if n > max_cells:
            raise TooManyCells(n, max_cells, size, scale)

    rz_start = math.floor((south - olat) / d_lat)
    rz_end = math.ceil((north - olat) / d_lat)
    rx_start = math.floor((west - olon) / d_lon)
    rx_end = math.ceil((east - olon) / d_lon)

    cells: list[dict] = []
    for rz in range(rz_start, rz_end):
        cell_south = olat + rz * d_lat
        cell_north = cell_south + d_lat
        if cell_north <= south or cell_south >= north:
            continue
        for rx in range(rx_start, rx_end):
            cell_west = olon + rx * d_lon
            cell_east = cell_west + d_lon
            if cell_east <= west or cell_west >= east:
                continue
            cells.append({
                "cell_key": f"{rx},{rz},{size}",
                "bbox": cell_bbox(rx, rz, size, olat, olon, scale),
            })
    return cells


def _point_in_poly(lat: float, lon: float, poly: list[tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon. `poly` is a list of (lat, lon) vertices."""
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        yi, xi = poly[i]
        yj, xj = poly[j]
        if ((yi > lat) != (yj > lat)) and \
           (lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-18) + xi):
            inside = not inside
        j = i
    return inside


def _perp_dist(p, a, b) -> float:
    """Perpendicular distance from point p to segment a-b (planar, small-area ok).
    Points are (lat, lon)."""
    py, px = p
    ay, ax = a
    by, bx = b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


def _simplify_ring(points, eps: float):
    """Iterative Douglas-Peucker. Drops vertices closer than `eps` (degrees) to the
    line they sit on, so a high-detail country border becomes a low-resolution outline
    with far fewer angles. Iterative (no recursion limit) for huge OSM rings."""
    n = len(points)
    if n < 4 or eps <= 0:
        return points
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        s, e = stack.pop()
        a, b = points[s], points[e]
        dmax, idx = 0.0, -1
        for i in range(s + 1, e):
            d = _perp_dist(points[i], a, b)
            if d > dmax:
                dmax, idx = d, i
        if idx != -1 and dmax > eps:
            keep[idx] = True
            stack.append((s, idx))
            stack.append((idx, e))
    out = [points[i] for i in range(n) if keep[i]]
    return out if len(out) >= 3 else points


def cells_for_polygons(rings, origin: dict, scale: float, size: int,
                       max_cells: int | None = MAX_PLAN_CELLS) -> list[dict]:
    """Tile the bounding box of one or more polygon rings, then keep only cells whose
    CENTER falls inside ANY ring. Lets the selection follow a region/country shape
    (incl. multi-polygon island nations), not just a square. Each ring is a list of
    [lat, lon] vertices.

    Country outlines from OSM are very high-detail, which makes a jagged tile edge, so
    each ring is first SIMPLIFIED to roughly cell resolution (border detail finer than a
    cell is invisible once tiled anyway)."""
    norm: list[list[tuple[float, float]]] = []
    for r in rings or []:
        pts = [(float(p[0]), float(p[1])) for p in r]
        if len(pts) >= 3:
            norm.append(pts)
    if not norm:
        return []
    # Simplify each ring to ~0.7 of a cell so the boundary has far fewer angles.
    mid_lat = sum(p[0] for r in norm for p in r) / sum(len(r) for r in norm)
    d_lat, d_lon = job_cell_deg(size, mid_lat, scale)
    eps = max(d_lat, d_lon) * 0.7
    norm = [_simplify_ring(r, eps) for r in norm]
    lats = [p[0] for r in norm for p in r]
    lons = [p[1] for r in norm for p in r]
    bbox = {"south": min(lats), "north": max(lats), "west": min(lons), "east": max(lons)}
    # A country's bbox holds more cells than the country does (Chile, island nations), so the
    # SCAN is allowed a few times the cap; the kept set still has to respect it.
    scan_cap = None if max_cells is None else max_cells * 4
    out: list[dict] = []
    for c in cells_for_bbox(bbox, origin, scale, size, max_cells=scan_cap):
        b = c["bbox"]
        clat = (b["south"] + b["north"]) / 2.0
        clon = (b["west"] + b["east"]) / 2.0
        if any(_point_in_poly(clat, clon, r) for r in norm):
            out.append(c)
    if max_cells is not None and len(out) > max_cells:
        raise TooManyCells(len(out), max_cells, size, scale)
    return out


def cells_for_polygon(poly, origin: dict, scale: float, size: int,
                      max_cells: int | None = MAX_PLAN_CELLS) -> list[dict]:
    """Single-ring convenience wrapper around cells_for_polygons."""
    return cells_for_polygons([poly], origin, scale, size, max_cells=max_cells)
