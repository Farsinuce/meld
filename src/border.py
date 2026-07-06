"""border.py - country-border / zone geometry for the Meld "Border & zones" advanced feature.

A *zone* is a named area (one or more countries, or a drawn polygon) with concentric rings:
  - actual : the zone boundary itself (the coast/country line).
  - soft   : boundary buffered out by `soft_km`.
  - hard   : boundary buffered out by `hard_km` (smoothed) - the outer wall / trim ring.
Plus the shared internal LINE where two adjacent zones touch (e.g. the RO/MD Prut line).

Everything is built in Minecraft BLOCK space, which is already metric (1 block = 1/scale metres),
so a band of `km` km = km*1000*scale blocks. Coordinates are origin-anchored exactly like the rest
of Meld (coords.block_x/block_z), so they are absolute world coords - the spec's OFFSET_X/Z is baked
in (no separate offset needed). Exports: per-ring point files (x,z,lon,lat), a WorldGuard
regions.yml (poly2d, per-zone flags + owners/members), and lat/lon polylines for the map preview.

shapely does the buffering / simplification / shared-line intersection.
"""
from __future__ import annotations

import json
import math
import os
from functools import lru_cache
from pathlib import Path

from shapely.geometry import shape, Polygon, MultiPolygon, LineString, MultiLineString
from shapely.ops import unary_union, linemerge

from .constants import METERS_PER_DEG_LAT
from .coords import mpd_lon

_ASSETS = Path(__file__).resolve().parent.parent / "assets" / "countries.geojson"


# ---------------------------------------------------------------- country data
@lru_cache(maxsize=1)
def _countries() -> dict:
    """name(lower) -> shapely geometry (lon/lat). Loaded once."""
    data = json.load(open(_ASSETS, encoding="utf8"))
    out = {}
    for f in data["features"]:
        nm = (f["properties"].get("name") or "").strip()
        if nm:
            out[nm.lower()] = shape(f["geometry"])
    return out


def list_countries() -> list[str]:
    data = json.load(open(_ASSETS, encoding="utf8"))
    return sorted(f["properties"].get("name", "") for f in data["features"] if f["properties"].get("name"))


# OSM admin_level=2 boundaries: far more accurate than the bundled Natural Earth shapes
# (NE is generalized small-scale data — land borders can be off by kilometres). Fetched
# once per country from Overpass, simplified to ~50 m fidelity, cached on disk forever.
_OSM_CACHE = Path(__file__).resolve().parent.parent / "cache" / "osm-borders"
_OVERPASS = ["https://overpass-api.de/api/interpreter",
             "https://overpass.kumi.systems/api/interpreter"]

def _osm_country_lonlat(name: str):
    """OSM admin_level=2 boundary of `name` as a shapely (Multi)Polygon (lon/lat)."""
    from shapely.geometry import mapping
    from shapely.ops import polygonize
    key = name.strip().lower().replace(" ", "_")
    cf = _OSM_CACHE / f"{key}.json"
    if cf.is_file():
        return shape(json.loads(cf.read_text(encoding="utf8")))
    import urllib.parse
    import urllib.request
    q = ('[out:json][timeout:180];'
         f'relation["boundary"="administrative"]["admin_level"="2"]["name:en"="{name.strip()}"];'
         'out geom;')
    data = None
    for url in _OVERPASS:
        try:
            req = urllib.request.Request(url, data=urllib.parse.urlencode({"data": q}).encode(),
                                         headers={"User-Agent": "Meld/1.5 (border zones)"})
            with urllib.request.urlopen(req, timeout=240) as r:
                data = json.load(r)
            if data.get("elements"):
                break
        except Exception:  # noqa: BLE001 - try the next mirror; caller falls back to NE
            data = None
    if not data or not data.get("elements"):
        raise RuntimeError(f"overpass returned no admin_level=2 boundary for {name!r}")
    lines = []
    for el in data["elements"]:
        for m in el.get("members", []):
            if m.get("type") == "way" and m.get("role") in ("outer", "") and m.get("geometry"):
                lines.append(LineString([(p["lon"], p["lat"]) for p in m["geometry"]]))
    polys = [p for p in polygonize(lines) if p.is_valid]
    if not polys:
        raise RuntimeError(f"could not assemble OSM boundary rings for {name!r}")
    # ~0.0005 deg = ~50 m real fidelity; keeps the cache and downstream geometry light
    geom = unary_union(polys).simplify(0.0005, preserve_topology=True)
    _OSM_CACHE.mkdir(parents=True, exist_ok=True)
    cf.write_text(json.dumps(mapping(geom)), encoding="utf8")
    return geom


@lru_cache(maxsize=4)
def _land_clip(margin_deg: float):
    """Union of ALL Natural Earth land, buffered by ~margin. Used to clip OSM country
    shapes back to the coastline: OSM admin_level=2 relations include TERRITORIAL
    WATERS (12 nautical miles of sea), so a coastal border otherwise runs far offshore.
    Clipping against the all-land union leaves land borders untouched (the neighbour's
    land extends the union across them) and only trims the open-sea side to
    shoreline + margin."""
    return unary_union(list(_countries().values())).buffer(margin_deg, resolution=8)


# Default sea margin = 1/3 of the 12-nautical-mile territorial waters OSM includes
# (22.2 km / 3): a generous coastal strip, still far inside the legal maritime line.
_COAST_MARGIN_KM = 22.224 / 3.0


def _zone_lonlat(country_names: list[str], source: str = "osm", coast_margin_km: float = _COAST_MARGIN_KM):
    """Union of the named countries as one lon/lat geometry. source='osm' uses exact
    OSM admin boundaries (cached, coast clipped to shoreline + coast_margin_km of sea)
    with Natural Earth as a silent per-country fallback; source='ne' forces the bundled
    Natural Earth shapes."""
    cs = _countries()
    geoms = []
    for n in country_names:
        n = n.strip()
        if not n:
            continue
        g = None
        if source == "osm":
            try:
                g = _osm_country_lonlat(n)
                # ~1 deg latitude = 111 km; the margin needs no better precision
                clipped = g.intersection(_land_clip(round(max(0.0, coast_margin_km) / 111.0, 4)))
                if not clipped.is_empty:
                    g = clipped
            except Exception:  # noqa: BLE001 - offline / unknown name -> NE shape
                g = None
        if g is None:
            g = cs.get(n.lower())
        if g is not None:
            geoms.append(g)
    if not geoms:
        raise ValueError(f"no known countries in {country_names!r}")
    return unary_union(geoms)


# ------------------------------------------------------- coordinate transforms
def _fwd(lon: float, lat: float, o_lat: float, o_lon: float, scale: float) -> tuple[float, float]:
    """lon/lat -> absolute world block (x, z) as floats (origin-anchored, +Z = south)."""
    x = (lon - o_lon) * mpd_lon(o_lat) * scale
    z = (o_lat - lat) * METERS_PER_DEG_LAT * scale
    return x, z


def _inv(x: float, z: float, o_lat: float, o_lon: float, scale: float) -> tuple[float, float]:
    """world block (x, z) -> lon/lat (for the map preview)."""
    lon = o_lon + x / (mpd_lon(o_lat) * scale)
    lat = o_lat - z / (METERS_PER_DEG_LAT * scale)
    return lon, lat


def _to_blocks(geom, o_lat, o_lon, scale):
    """Re-project a lon/lat shapely geometry into block space (keeps polygon structure)."""
    def tx(coords):
        return [_fwd(lon, lat, o_lat, o_lon, scale) for lon, lat in coords]

    if isinstance(geom, Polygon):
        return Polygon(tx(geom.exterior.coords), [tx(r.coords) for r in geom.interiors])
    if isinstance(geom, MultiPolygon):
        return MultiPolygon([Polygon(tx(p.exterior.coords),
                                     [tx(r.coords) for r in p.interiors]) for p in geom.geoms])
    raise TypeError(type(geom))


# --------------------------------------------------------------- geometry ops
def _largest_polygon(geom):
    if isinstance(geom, MultiPolygon):
        return max(geom.geoms, key=lambda p: p.area)
    return geom


def _simplify_to(ring_coords: list, target: int) -> list:
    """Douglas-Peucker a coordinate ring down to ~target points (binary search on tolerance)."""
    line = LineString(ring_coords)
    if len(ring_coords) <= target:
        return ring_coords
    lo, hi = 0.0, 200000.0
    for _ in range(28):
        mid = (lo + hi) / 2
        n = len(line.simplify(mid, preserve_topology=False).coords)
        if n > target:
            lo = mid
        else:
            hi = mid
    return list(line.simplify(hi, preserve_topology=False).coords)


def _ring_xz(poly, target: int) -> list[tuple[int, int]]:
    """Exterior ring of `poly` (largest if multi), simplified to ~target integer (x,z) points."""
    ext = list(_largest_polygon(poly).exterior.coords)
    simp = _simplify_to(ext, target)
    pts = [(int(round(x)), int(round(z))) for x, z in simp]
    # drop the duplicate closing point; WorldGuard closes poly2d itself
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return pts


def _shared_line_xz(geom_a_block, geom_b_block, target: int, eps_blocks: float) -> list[tuple[int, int]]:
    """The internal line where two zones touch (e.g. RO/MD Prut), as ~target (x,z) points.

    Adjacent country polygons rarely share EXACT vertices, so a plain boundary intersection misses
    the shared edge. Instead take the part of A's boundary lying within `eps_blocks` of B - that is
    the coincident border - then merge the pieces into one polyline."""
    shared = geom_a_block.boundary.intersection(geom_b_block.buffer(eps_blocks))
    lines = []
    if isinstance(shared, LineString):
        lines = [shared]
    elif hasattr(shared, "geoms"):
        lines = [g for g in shared.geoms if isinstance(g, LineString)]
    lines = [l for l in lines if l.length > eps_blocks]
    if not lines:
        return []
    merged = linemerge(lines) if len(lines) > 1 else lines[0]
    if isinstance(merged, MultiLineString):
        merged = max(merged.geoms, key=lambda l: l.length)
    simp = _simplify_to(list(merged.coords), target)
    return [(int(round(x)), int(round(z))) for x, z in simp]


# --------------------------------------------------------------- public build
def build(spec: dict, origin: dict, scale: float) -> dict:
    """spec = {zones:[{name, countries[], owners[], members[], color_actual, flags_actual}],
               soft_km, hard_km, points:{actual,soft,hard}, shared_lines:[[i,j]], shared_points}.

    Each ZONE keeps its own ACTUAL border (identity). The soft + hard buffers are built on the
    UNION of all zones - ONE ring each around the whole clump - so the wall never runs BETWEEN two
    adjacent countries, only around the combined outer edge."""
    o_lat = float(origin["lat"])
    o_lon = float(origin["lon"])
    soft_km = float(spec.get("soft_km", 5))
    hard_km = float(spec.get("hard_km", 10))
    pts = spec.get("points", {}) or {}
    p_actual = max(3, min(5000, int(pts.get("actual", 700))))
    p_soft = max(3, min(5000, int(pts.get("soft", p_actual))))
    p_hard = max(3, min(5000, int(pts.get("hard", p_actual))))
    source = (spec.get("source") or "osm").strip().lower()
    coast_km = float(spec.get("coast_margin_km", _COAST_MARGIN_KM))

    zones, geoms = [], []
    for z in spec.get("zones", []):
        geom = _to_blocks(_zone_lonlat(z.get("countries", []), source, coast_km), o_lat, o_lon, scale)
        geoms.append(geom)
        xz = _ring_xz(geom, p_actual)
        zones.append({"spec": z, "geom_block": geom, "xz": xz,
                      "ll": [list(_inv(x, zz, o_lat, o_lon, scale)) for x, zz in xz]})
    if not geoms:
        raise ValueError("no zones")

    clump = unary_union(geoms)
    # Offsets in BLOCKS take precedence (the compact band design: soft ~32 blocks =
    # the no-build damage band, hard ~64 = the bounce wall); km inputs remain for
    # wide-band setups. Stored back as km so previews/labels stay consistent.
    soft_b, hard_b = soft_km * 1000.0 * scale, hard_km * 1000.0 * scale
    if spec.get("soft_blocks"):
        soft_b = max(1.0, float(spec["soft_blocks"]))
        soft_km = soft_b / (1000.0 * scale)
    if spec.get("hard_blocks"):
        hard_b = max(soft_b + 1.0, float(spec["hard_blocks"]))
        hard_km = hard_b / (1000.0 * scale)
    # Round joins (resolution 32) for SMOOTH buffers; no pre-simplify - _ring_xz reduces to the
    # target point count, so the soft/hard rings follow the count instead of getting cornery.
    soft = clump.buffer(soft_b, join_style=1, resolution=32) if soft_b > 0 else clump
    hard = clump.buffer(hard_b, join_style=1, resolution=32) if hard_b > 0 else clump

    def _ring(poly, n):
        xz = _ring_xz(poly, n)
        return xz, [list(_inv(x, zz, o_lat, o_lon, scale)) for x, zz in xz]

    soft_xz, soft_ll = _ring(soft, p_soft)
    hard_xz, hard_ll = _ring(hard, p_hard)
    # Trim ring = the hard wall buffered out by a margin. Generation fills to HERE, so terrain
    # continues past the wall and the void edge is never visible from the playable area (the wall
    # flings the player back well before it). 0 = trim exactly at the hard wall.
    trim_km = float(spec.get("trim_margin_km", 5))
    trim = hard.buffer(trim_km * 1000.0 * scale, join_style=1, resolution=16) if trim_km > 0 else hard
    trim_xz, trim_ll = _ring(trim, p_hard)

    shared = []
    eps = max(8.0, 800.0 * scale)
    for pair in spec.get("shared_lines", []):
        i, j = pair
        if 0 <= i < len(zones) and 0 <= j < len(zones):
            # High default: the shared line is the ONLY visible border between adjacent
            # countries, so it must follow the OSM geometry closely — a coarse polyline
            # cuts corners and visibly diverges from the real border.
            xz = _shared_line_xz(zones[i]["geom_block"], zones[j]["geom_block"],
                                 max(3, min(6000, int(spec.get("shared_points", 2000)))), eps)
            shared.append({"between": [zones[i]["spec"].get("name"), zones[j]["spec"].get("name")],
                           "xz": xz, "ll": [list(_inv(x, zz, o_lat, o_lon, scale)) for x, zz in xz]})

    return {"zones": zones,
            "clump": {"soft_xz": soft_xz, "soft_ll": soft_ll, "hard_xz": hard_xz, "hard_ll": hard_ll,
                      "trim_xz": trim_xz, "trim_ll": trim_ll,
                      "soft_km": soft_km, "hard_km": hard_km, "trim_km": trim_km},
            "enforce": {"damage_hearts": max(0.0, float(spec.get("damage_hearts", 2))),
                        "damage_delay_s": max(1, int(spec.get("damage_delay_s", 10)))},
            "shared": shared, "scale": scale, "origin": {"lat": o_lat, "lon": o_lon}}


# ------------------------------------------------------------------- exporters
COLORS = {"actual": "#00e5ff", "soft": "#ff9800", "hard": "#ffeb3b", "shared": "#76ff03"}
# Branding prefix for every player-facing border message (WG flags + Skript action bars).
_MSG_PREFIX = "&8[&6★&8] "


def preview(result: dict) -> dict:
    """Map payload: per-zone ACTUAL borders + ONE clump soft + ONE clump hard + shared lines."""
    cl = result["clump"]
    zones = [{"name": z["spec"].get("name", "zone"), "ll": z["ll"], "count": len(z["xz"]),
              "color": z["spec"].get("color_actual", COLORS["actual"]),
              "label": (z["spec"].get("name", "zone") + " border")} for z in result["zones"]]
    _scale = result.get("scale", 1.0)
    _soft_bl = cl["soft_km"] * 1000.0 * _scale
    _hard_bl = cl["hard_km"] * 1000.0 * _scale
    clump = [
        {"key": "soft", "ll": cl["soft_ll"], "count": len(cl["soft_xz"]), "color": COLORS["soft"],
         "label": f"soft +{_soft_bl:.0f} blocks - damage band (no build, periodic damage)"},
        {"key": "hard", "ll": cl["hard_ll"], "count": len(cl["hard_xz"]), "color": COLORS["hard"],
         "label": f"hard +{_hard_bl:.0f} blocks - the wall (players bounced back)"},
        {"key": "trim", "ll": cl["trim_ll"], "count": len(cl["trim_xz"]), "color": "#9aa0a6",
         "label": f"trim edge +{cl['trim_km']:g} km past wall - terrain ends here (hidden; flung back at the wall)"},
    ]
    shared = [{"between": s["between"], "ll": s["ll"], "color": COLORS["shared"],
               "label": "internal border"} for s in result["shared"]]
    return {"zones": zones, "clump": clump, "shared": shared, "colors": COLORS}


def _rid(name: str) -> str:
    return (name or "zone").strip().lower().replace(" ", "_")


def _yaml_points(pts) -> str:
    return "[" + ", ".join("{x: %d, z: %d}" % (x, z) for x, z in pts) + "]"


def _flag_block(flags: dict) -> str:
    if not flags:
        return "{}"
    return "{" + ", ".join(f"{k}: {v}" for k, v in flags.items()) + "}"


def _region(rid, min_y, max_y, pri, pts, flags, owners, members):
    own = "{players: [%s]}" % ", ".join(f'"{o}"' for o in owners) if owners else "{}"
    mem = "{players: [%s]}" % ", ".join(f'"{m}"' for m in members) if members else "{}"
    return [f"{rid}:", "    type: poly2d", f"    min-y: {min_y}", f"    max-y: {max_y}",
            f"    priority: {pri}", f"    points: {_yaml_points(pts)}",
            f"    flags: {_flag_block(flags)}", f"    owners: {own}", f"    members: {mem}", ""]


def write_regions_yml(result: dict, min_y: int, max_y: int) -> str:
    """Two nested disks, all enforcement native WorldGuard (no Skript addon exists with
    region events for current servers):
      - zone regions (priority 12, the countries): build ALLOWED, heal 0 — these override
        the band flags everywhere inside the country.
      - border_hard (priority 5, country + band): the whole strip between the country
        line and the wall is no-build + periodic damage (negative WG heal flags), and
        `exit: deny` bounces players trying to cross the outer edge. Nested disks mean
        entry/greeting flags can't mark the band (players inside the country are already
        inside this disk); the zone-priority override is what scopes the flags to it."""
    cl = result["clump"]
    enf = result.get("enforce", {})
    dmg_hp = enf.get("damage_hearts", 2) * 2.0     # hearts -> HP (half-hearts)
    dmg_s = enf.get("damage_delay_s", 10)
    px = _MSG_PREFIX
    # __global__ needs the full field set — WorldGuard's parser NPEs (and drops the
    # region) when priority/owners/members are absent, even though they look optional.
    L = ["__global__:", "    type: global", "    priority: 0",
         f'    flags: {{block-break: deny, block-place: deny, deny-message: "{px}&cThe world ends here! You cannot build beyond the border."}}',
         "    owners: {}", "    members: {}", ""]
    # ONE enforcement region: the hard disk (country + band). Everything between the
    # country line and the wall is no-build + periodic damage (WG heal flags negative);
    # the zone regions override both inside the country. exit-deny-message is the flag
    # WG actually shows when `exit: deny` blocks the move; the generic deny-message
    # covers build denials in the band. No border_soft region — the band starts at the
    # country line itself.
    hard_flags = {"block-break": "deny", "block-place": "deny", "exit": "deny",
                  "exit-deny-message": f'"{px}&cYou have reached the border of the world! Turn back!"',
                  "deny-message": f'"{px}&cYou cannot build in the border zone!"'}
    if dmg_hp > 0:
        hard_flags.update({"heal-delay": dmg_s, "heal-amount": int(-dmg_hp)})
    L += _region("border_hard", min_y, max_y, 5, cl["hard_xz"], hard_flags, [], [])
    # Shared-border corridor (priority 10, between the band and the zones): the zone
    # polygons are resampled independently, so along a shared border (RO/MD) slivers can
    # belong to NEITHER zone and fall through to the band's no-build + damage. A ~48
    # block corridor around each shared line keeps the riverbanks buildable and safe.
    for sh in result.get("shared", []):
        pts = sh.get("xz") or []
        if len(pts) < 2:
            continue
        corridor = LineString(pts).buffer(48.0, cap_style=2, join_style=1, resolution=8)
        cor_xz = _ring_xz(corridor, 1500)
        a, b = sh.get("between") or ["a", "b"]
        L += _region(_rid(f"border_shared_{a}_{b}"), min_y, max_y, 10, cor_xz,
                     {"block-break": "allow", "block-place": "allow",
                      "heal-delay": 0, "heal-amount": 0}, [], [])
    for z in result["zones"]:
        s = z["spec"]
        # No WG greeting/farewell/titles: Entering/Leaving is shown on the ACTION BAR by
        # border.sk's geometric crossing detector. Zones keep build allow + heal 0 so the
        # interior overrides the damage band's flags.
        zflags = {"block-break": "allow", "block-place": "allow",
                  "heal-delay": 0, "heal-amount": 0}
        zflags.update(s.get("flags_actual", {}))
        L += _region(_rid(s.get("name")), min_y, max_y, 12, z["xz"],
                     zflags, s.get("owners", []) or [], s.get("members", []) or [])
    # WorldGuard only reads regions nested under a top-level `regions:` key — a bare
    # region list parses as valid YAML but loads as zero regions.
    body = "\n".join(("    " + ln if ln else ln) for ln in L)
    return "regions:\n" + body + "\n"


def _write_points(path, xz, ll):
    with open(path, "w", encoding="utf8") as fh:
        fh.write("x,z,lon,lat\n")
        for (x, zz), (lon, lat) in zip(xz, ll):
            fh.write(f"{x},{zz},{lon:.6f},{lat:.6f}\n")


def write_exports(result, outdir, min_y, max_y, skript_opts=None) -> dict:
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for z in result["zones"]:
        nm = _rid(z["spec"].get("name"))
        _write_points(out / f"{nm}_actual.txt", z["xz"], z["ll"])
        written.append(f"{nm}_actual.txt")
    cl = result["clump"]
    _write_points(out / "border_soft.txt", cl["soft_xz"], cl["soft_ll"])
    _write_points(out / "border_hard.txt", cl["hard_xz"], cl["hard_ll"])
    written += ["border_soft.txt", "border_hard.txt"]
    for sh in result["shared"]:
        a, b = sh["between"]
        fn = ("shared_%s_%s.txt" % (a, b)).lower().replace(" ", "_")
        _write_points(out / fn, sh["xz"], sh["ll"])
        written.append(fn)
    (out / "regions.yml").write_text(write_regions_yml(result, min_y, max_y), encoding="utf8")
    written.append("regions.yml")
    if not skript_opts or skript_opts.get("generate", True):
        (out / "border.sk").write_text(write_skript(result, skript_opts or {}), encoding="utf8")
        written.append("border.sk")
    return {"dir": str(out), "files": written}


# --------------------------------------------------------- v2: Skript generator

_WALL_CELL = 128        # spatial bucket size (blocks); must stay >= the render radius
_WALL_SEG = 24.0        # target max segment length before draw-time interpolation
_WALL_CAP = 3000        # soft cap on stored segments per ring group (spacing grows past this)
# Segments are bucketed by their MIDPOINT, so any segment longer than a cell can end up
# invisible to players standing near its ends (their 3x3 cell scan misses the midpoint
# bucket) — walls develop gaps and appear displaced. Never split coarser than this.
_WALL_SEG_MAX = 120.0


def _split_ring_segments(rings: list, max_seg: float, cap: int) -> list:
    """Closed ring(s) of (x,z) points -> list of ((ax,az),(bx,bz)) segments no longer
    than max_seg blocks (re-split coarser if the cap would be exceeded, but never past
    _WALL_SEG_MAX — bucket correctness beats the cap)."""
    raw = []
    for ring in rings:
        n = len(ring)
        for i in range(n):
            a, b = ring[i], ring[(i + 1) % n]
            if a != b:
                raw.append((a, b))
    total = sum(math.dist(a, b) for a, b in raw)
    if total <= 0:
        return []
    seg = max(max_seg, min(_WALL_SEG_MAX, total / max(1, cap)))
    out = []
    for (ax, az), (bx, bz) in raw:
        d = math.dist((ax, az), (bx, bz))
        steps = max(1, int(math.ceil(d / seg)))
        for i in range(steps):
            t0, t1 = i / steps, (i + 1) / steps
            out.append(((ax + (bx - ax) * t0, az + (bz - az) * t0),
                        (ax + (bx - ax) * t1, az + (bz - az) * t1)))
    return out


def _wall_var_lines(tree: str, rings: list, with_sign: bool = False) -> tuple[str, int, str]:
    """Skript `set` lines that embed a ring group as two parallel vector list-vars
    ({tree::cX_Z::i} = segment start, {tree}b = segment end), bucketed by the
    segment midpoint into _WALL_CELL cells for cheap near-player lookup. with_sign
    additionally emits {tree}s = the ring's inside-side sign (see _zone_var_lines),
    so border.sk can tell which side of the wall a player is on.
    Returns (lines, segment count, one existing probe variable) — the probe lets the
    diagnostics test data presence directly, because Skript's `{list::*}` skips branch
    nodes that hold no value of their own, so `size of {tree::*}` always reads 0."""
    from shapely.geometry import LinearRing
    counters: dict = {}
    lines = []
    probe = ""
    total = 0
    for ring in rings:
        sign = 1 if LinearRing(ring).is_ccw else -1
        for (ax, az), (bx, bz) in _split_ring_segments([ring], _WALL_SEG, _WALL_CAP):
            cx = math.floor((ax + bx) / 2 / _WALL_CELL)
            cz = math.floor((az + bz) / 2 / _WALL_CELL)
            key = f"c{cx}_{cz}"
            counters[key] = counters.get(key, 0) + 1
            i = counters[key]
            if not probe:
                probe = f"{{{tree}::{key}::1}}"
            lines.append(f"    set {{{tree}::{key}::{i}}} to vector({ax:.0f}, 0, {az:.0f})")
            lines.append(f"    set {{{tree}b::{key}::{i}}} to vector({bx:.0f}, 0, {bz:.0f})")
            if with_sign:
                lines.append(f"    set {{{tree}s::{key}::{i}}} to {sign}")
            total += 1
    return "\n".join(lines), total, probe or f"{{{tree}::none::1}}"


def _split_open_segments(pts: list, max_seg: float, cap: int) -> list:
    """Open polyline -> segments (like _split_ring_segments but WITHOUT the closing
    wrap segment, which would draw a bogus straight line end-to-end)."""
    raw = [(a, b) for a, b in zip(pts, pts[1:]) if a != b]
    total = sum(math.dist(a, b) for a, b in raw)
    if total <= 0:
        return []
    seg = max(max_seg, min(_WALL_SEG_MAX, total / max(1, cap)))
    out = []
    for (ax, az), (bx, bz) in raw:
        steps = max(1, int(math.ceil(math.dist((ax, az), (bx, bz)) / seg)))
        for i in range(steps):
            t0, t1 = i / steps, (i + 1) / steps
            out.append(((ax + (bx - ax) * t0, az + (bz - az) * t0),
                        (ax + (bx - ax) * t1, az + (bz - az) * t1)))
    return out


def _seg_var_lines(tree: str, segs: list) -> tuple[str, int, str]:
    """Bucketed start/end var lines for a prebuilt segment list (any wall group)."""
    counters: dict = {}
    lines = []
    probe = ""
    for (ax, az), (bx, bz) in segs:
        cx = math.floor((ax + bx) / 2 / _WALL_CELL)
        cz = math.floor((az + bz) / 2 / _WALL_CELL)
        key = f"c{cx}_{cz}"
        counters[key] = counters.get(key, 0) + 1
        i = counters[key]
        if not probe:
            probe = f"{{{tree}::{key}::1}}"
        lines.append(f"    set {{{tree}::{key}::{i}}} to vector({ax:.0f}, 0, {az:.0f})")
        lines.append(f"    set {{{tree}b::{key}::{i}}} to vector({bx:.0f}, 0, {bz:.0f})")
    return "\n".join(lines), len(segs), probe or f"{{{tree}::none::1}}"


def _zone_var_lines(named_rings: list) -> tuple[str, int, str]:
    """Like _wall_var_lines for the ZONE group, but each segment also carries the zone's
    display name ({bzonen}) and the ring's inside-side sign ({bzones}): the sign of the
    2D cross product (B-A)x(P-A) that means P is INSIDE the ring, decided by the ring's
    winding. border.sk uses both for the geometric Entering/Leaving action-bar detector."""
    from shapely.geometry import LinearRing
    counters: dict = {}
    lines = []
    probe = ""
    total = 0
    for name, xz in named_rings:
        ring_sign = 1 if LinearRing(xz).is_ccw else -1
        disp = (name or "zone").strip().title()
        for (ax, az), (bx, bz) in _split_ring_segments([xz], _WALL_SEG, _WALL_CAP):
            cx = math.floor((ax + bx) / 2 / _WALL_CELL)
            cz = math.floor((az + bz) / 2 / _WALL_CELL)
            key = f"c{cx}_{cz}"
            counters[key] = counters.get(key, 0) + 1
            i = counters[key]
            if not probe:
                probe = f"{{bzone::{key}::1}}"
            lines.append(f"    set {{bzone::{key}::{i}}} to vector({ax:.0f}, 0, {az:.0f})")
            lines.append(f"    set {{bzoneb::{key}::{i}}} to vector({bx:.0f}, 0, {bz:.0f})")
            lines.append(f'    set {{bzonen::{key}::{i}}} to "{disp}"')
            lines.append(f"    set {{bzones::{key}::{i}}} to {ring_sign}")
            total += 1
    return "\n".join(lines), total, probe or "{bzone::none::1}"


def _wall_draw_block(tree: str, color: str) -> str:
    """The per-ring draw pass: scan the player's 3x3 bucket neighbourhood, interpolate
    each in-radius segment at ~4-block steps, stack SkBee dust from -wall-h..+wall-h.
    Emitted directly under `loop all players:` (2 levels deep).

    Loop references MUST be numbered by counting every enclosing loop from the outermost,
    INCLUDING the `loop all players:` these blocks sit under (players=1, buckets=2,
    segments=3, interpolation=4, vertical=5). The original code was numbered for a
    standalone trigger, so %loop-value-1% resolved to the PLAYER and the walls never
    drew; unnumbered refs are no better — Skript rejects them as ambiguous inside
    nested loops ("multiple loops match loop-index")."""
    base = " " * 8
    body = f"""loop {{_k::*}}:
    loop {{{tree}::%loop-value-2%::*}}:
        set {{_a}} to loop-value-3
        set {{_dx}} to (x of {{_a}}) - {{_px}}
        set {{_dz}} to (z of {{_a}}) - {{_pz}}
        if ({{_dx}} * {{_dx}}) + ({{_dz}} * {{_dz}}) <= {{@radius}} * {{@radius}}:
            set {{_b}} to {{{tree}b::%loop-value-2%::%loop-index-2%}}
            set {{_ax}} to x of {{_a}}
            set {{_az}} to z of {{_a}}
            set {{_bx}} to x of {{_b}}
            set {{_bz}} to z of {{_b}}
            set {{_n}} to ceil(sqrt(({{_bx}} - {{_ax}}) ^ 2 + ({{_bz}} - {{_az}}) ^ 2) / 4)
            if {{_n}} < 1:
                set {{_n}} to 1
            loop integers from 0 to {{_n}}:
                set {{_t}} to loop-value-4 / {{_n}}
                set {{_x}} to {{_ax}} + ({{_bx}} - {{_ax}}) * {{_t}}
                set {{_z}} to {{_az}} + ({{_bz}} - {{_az}}) * {{_t}}
                loop integers from -{{@wall-h}} to {{@wall-h}}:
                    make 1 of dust using dustOption({color}, 2.2) at location({{_x}}, {{_py}} + loop-value-5, {{_z}}, {{_w}})"""
    return "\n".join(base + ln if ln.strip() else ln for ln in body.split("\n"))
def write_skript(result: dict, opts: dict) -> str:
    """Generate a server-side border.sk. The Skript owns ONLY the packet-particle walls (SkBee
    dust) plus the geometric fling-back and Entering/Leaving action bars: enforcement (no-build,
    damage band, exit bounce) is native WorldGuard flags in the exported regions.yml — no
    WorldGuard Skript addon exists for current server versions with region events, so nothing
    here may reference regions. Pure geometry + vanilla Skript + SkBee."""
    # radius must stay within one bucket cell so the 3x3 neighbourhood scan covers it
    radius = max(16, min(_WALL_CELL - 8, int(opts.get("render_radius", 120))))
    wallh = int(opts.get("wall_height", 10))
    ticks = max(1, int(opts.get("update_ticks", 8)))
    zone_ids = [_rid(z["spec"].get("name")) for z in result["zones"]]
    cl = result["clump"]
    hard_lines, hard_n, hard_probe = _wall_var_lines("bhard", [cl["hard_xz"]], with_sign=True)
    # bzone = FULL country rings, detection only (crossing action bars + fling aim).
    zone_lines, zone_n, zone_probe = _zone_var_lines(
        [((z["spec"].get("name") or "zone"), z["xz"]) for z in result["zones"]])
    # Shared internal borders (e.g. the RO/MD Prut line) draw as ONE lime line; the
    # light-gray country walls are suppressed along it so adjacent countries don't show
    # coincident gray walls on top of the lime one.
    shared_pts = [sh["xz"] for sh in result.get("shared", []) if len(sh.get("xz") or []) >= 2]
    share_segs = []
    for pts in shared_pts:
        share_segs += _split_open_segments(pts, _WALL_SEG, _WALL_CAP)
    share_lines, share_n, share_probe = _seg_var_lines("bshare", share_segs)
    # Suppression criterion: distance from the segment midpoint to the OTHER zones'
    # AREA — a ring piece on a shared border touches the neighbour (distance ~0, only
    # independent-resampling jitter). Testing against the simplified lime polyline
    # instead leaves gray survivors wherever that polyline cuts a corner (the original
    # double-line bug); the area test is exact regardless of the lime line's fidelity.
    from shapely.geometry import Point as _Pt
    zone_geoms = [z["geom_block"] for z in result["zones"]]
    zdraw_segs = []
    for zi, z in enumerate(result["zones"]):
        others = [g for gi, g in enumerate(zone_geoms) if gi != zi]
        for s in _split_ring_segments([z["xz"]], _WALL_SEG, _WALL_CAP):
            if others:
                mid = _Pt((s[0][0] + s[1][0]) / 2, (s[0][1] + s[1][1]) / 2)
                if min(g.distance(mid) for g in others) <= 32:
                    continue
            zdraw_segs.append(s)
    zdraw_lines, zdraw_n, _ = _seg_var_lines("bzoned", zdraw_segs)
    # Walls: outer = vanilla world-border cyan, country line = light gray, shared = lime.
    # The old orange "soft" ring has no wall and no region — the entire country-line ->
    # wall strip is the damage band now.
    draw_hard = _wall_draw_block("bhard", "aqua")
    draw_zone = _wall_draw_block("bzoned", "light grey")
    draw_share = _wall_draw_block("bshare", "lime")
    return f"""# border.sk  -  generated by Meld (Border & zones, v3).
# Packet-particle border walls ONLY. Border enforcement (no-build + damage band between the
# country line and the wall, exit bounce at the wall) is handled natively by WorldGuard
# through the exported regions.yml. Zones: {", ".join(zone_ids) or "<zones>"}.
# This file needs ONLY Skript + SkBee (dust particles). The wall geometry is EMBEDDED below,
# no point files, no WorldGuard Skript addon, no region syntax anywhere.
# SETUP:
#   1. /rg reload   (after copying the exported regions.yml into the world's WorldGuard data)
#   2. /sk reload border
# Colors: outer wall = cyan (vanilla world-border look), country line = light gray.

options:
    radius: {radius}          # particle render radius (blocks)
    wall-h: {wallh}           # wall height above/below the player
    ticks: {ticks}            # particle update interval (ticks)

# ---- packet-particle walls (SkBee dust, near segments only) ----
# Segment endpoints are baked into bucketed list variables on load ({_WALL_CELL}-block cells), so
# every {{@ticks}} ticks each player only scans the 3x3 cells around them, interpolates the
# in-radius segments at ~4-block steps and gets a dust curtain from y-{{@wall-h}} to y+{{@wall-h}}
# broadcast to everyone near it (packet particles, no world lag). Players far from every ring hit
# nothing but nine empty list lookups, so no world/region gate is needed.
on load:
    delete {{bhard::*}}
    delete {{bhardb::*}}
    delete {{bhards::*}}
    delete {{bsoft::*}}
    delete {{bsoftb::*}}
    delete {{bzone::*}}
    delete {{bzoneb::*}}
    delete {{bzonen::*}}
    delete {{bzones::*}}
    delete {{bzoned::*}}
    delete {{bzonedb::*}}
    delete {{bshare::*}}
    delete {{bshareb::*}}
{hard_lines}
{zone_lines}
{zdraw_lines}
{share_lines}

every {{@ticks}} ticks:
    loop all players:
        set {{_px}} to x-coordinate of loop-player
        set {{_py}} to y-coordinate of loop-player
        set {{_pz}} to z-coordinate of loop-player
        set {{_w}} to world of loop-player
        set {{_cx}} to floor({{_px}} / {_WALL_CELL})
        set {{_cz}} to floor({{_pz}} / {_WALL_CELL})
        delete {{_k::*}}
        set {{_k::1}} to "c%{{_cx}} - 1%_%{{_cz}} - 1%"
        set {{_k::2}} to "c%{{_cx}}%_%{{_cz}} - 1%"
        set {{_k::3}} to "c%{{_cx}} + 1%_%{{_cz}} - 1%"
        set {{_k::4}} to "c%{{_cx}} - 1%_%{{_cz}}%"
        set {{_k::5}} to "c%{{_cx}}%_%{{_cz}}%"
        set {{_k::6}} to "c%{{_cx}} + 1%_%{{_cz}}%"
        set {{_k::7}} to "c%{{_cx}} - 1%_%{{_cz}} + 1%"
        set {{_k::8}} to "c%{{_cx}}%_%{{_cz}} + 1%"
        set {{_k::9}} to "c%{{_cx}} + 1%_%{{_cz}} + 1%"
{draw_hard}
{draw_zone}
{draw_share}
        # ---- fling-back + outlaw layer at the hard wall ----
        # exact point-to-segment distance vs every hard segment in the 3x3 buckets; the
        # nearest segment also tells WHICH SIDE of the wall the player is on (cross
        # product x the ring's inside-sign). Within ~2.8 blocks: fling back inland (WG's
        # own setback just re-places you, this gives the intended bounce). OUTSIDE the
        # wall at any distance (blast-through at high fly speed, teleports, op bypass):
        # heavy repeating damage + a fresh fling every pass until they are back in.
        set {{_uu}} to "%uuid of loop-player%"
        set {{_best}} to 999999
        loop {{_k::*}}:
            loop {{bhard::%loop-value-2%::*}}:
                set {{_a}} to loop-value-3
                set {{_b}} to {{bhardb::%loop-value-2%::%loop-index-2%}}
                set {{_abx}} to (x of {{_b}}) - (x of {{_a}})
                set {{_abz}} to (z of {{_b}}) - (z of {{_a}})
                set {{_len2}} to {{_abx}} * {{_abx}} + {{_abz}} * {{_abz}}
                if {{_len2}} > 0:
                    set {{_t}} to (({{_px}} - (x of {{_a}})) * {{_abx}} + ({{_pz}} - (z of {{_a}})) * {{_abz}}) / {{_len2}}
                    if {{_t}} < 0:
                        set {{_t}} to 0
                    if {{_t}} > 1:
                        set {{_t}} to 1
                    set {{_ddx}} to {{_px}} - ((x of {{_a}}) + {{_abx}} * {{_t}})
                    set {{_ddz}} to {{_pz}} - ((z of {{_a}}) + {{_abz}} * {{_t}})
                    set {{_d2}} to {{_ddx}} * {{_ddx}} + {{_ddz}} * {{_ddz}}
                    if {{_d2}} < {{_best}}:
                        set {{_best}} to {{_d2}}
                        set {{_ddx2}} to {{_ddx}}
                        set {{_ddz2}} to {{_ddz}}
                        set {{_hcross}} to {{_abx}} * ({{_pz}} - (z of {{_a}})) - {{_abz}} * ({{_px}} - (x of {{_a}}))
                        set {{_hsgn}} to {{bhards::%loop-value-2%::%loop-index-2%}}
        if {{_best}} <= 16384:
            if {{_hcross}} * {{_hsgn}} < 0:
                # OUTSIDE the wall (up to ~128 blocks tracked): outlaw treatment.
                # TELEPORT snap-back 8 blocks inside the nearest wall point (works on
                # creative and spectator too, unlike damage/velocity) + a 5x wall push.
                set {{bout::%{{_uu}}%}} to true
                set {{_ok}} to true
                if {{bod::%{{_uu}}%}} is set:
                    if difference between {{bod::%{{_uu}}%}} and now < 1 second:
                        set {{_ok}} to false
                if {{_ok}} is true:
                    damage loop-player by 4 hearts
                    set {{bod::%{{_uu}}%}} to now
                    send action bar "&8[&6★&8] &4Outside the border! Get back!" to loop-player
                set {{_nrm}} to sqrt({{_best}})
                if {{_nrm}} > 0:
                    set {{_tx}} to {{_px}} - {{_ddx2}} - ({{_ddx2}} / {{_nrm}}) * 8
                    set {{_tz}} to {{_pz}} - {{_ddz2}} - ({{_ddz2}} / {{_nrm}}) * 8
                    set {{_tl}} to location({{_tx}}, {{_py}}, {{_tz}}, {{_w}})
                    set {{_hy}} to y-coordinate of highest block at {{_tl}}
                    if {{_py}} < {{_hy}} + 1:
                        set {{_tl}} to location({{_tx}}, {{_hy}} + 1, {{_tz}}, {{_w}})
                    teleport loop-player to {{_tl}}
                    set velocity of loop-player to vector(0 - ({{_ddx2}} / {{_nrm}}) * 7, 0.6, 0 - ({{_ddz2}} / {{_nrm}}) * 7)
            else:
                # inside and near the wall: remember this spot as the safe anchor for
                # the unlimited-range backstop, and clear any outlaw state
                set {{blast::%{{_uu}}%}} to location of loop-player
                delete {{bout::%{{_uu}}%}}
        else:
            # no wall segment in tracking range at all. If the outlaw flag is still set,
            # the player blinked PAST the tracked strip (extreme speed / teleport) —
            # yank them back to their last known inside-the-border position.
            if {{bout::%{{_uu}}%}} is set:
                if {{blast::%{{_uu}}%}} is set:
                    teleport loop-player to {{blast::%{{_uu}}%}}
                    send action bar "&8[&6★&8] &4Outside the border! Get back!" to loop-player
        if {{_best}} <= 7.84:
            set {{_bz2}} to 999999
            loop {{_k::*}}:
                loop {{bzone::%loop-value-2%::*}}:
                    set {{_zv}} to loop-value-3
                    set {{_zdx}} to (x of {{_zv}}) - {{_px}}
                    set {{_zdz}} to (z of {{_zv}}) - {{_pz}}
                    set {{_zd2}} to {{_zdx}} * {{_zdx}} + {{_zdz}} * {{_zdz}}
                    if {{_zd2}} < {{_bz2}}:
                        set {{_bz2}} to {{_zd2}}
                        set {{_ix}} to {{_zdx}}
                        set {{_iz}} to {{_zdz}}
            if {{_bz2}} < 999999:
                set {{_nrm}} to sqrt({{_ix}} * {{_ix}} + {{_iz}} * {{_iz}})
                if {{_nrm}} > 0:
                    set velocity of loop-player to vector({{_ix}} / {{_nrm}} * 1.4, 0.55, {{_iz}} / {{_nrm}} * 1.4)
                    send action bar "&8[&6★&8] &cTurn back!" to loop-player
        # ---- Entering/Leaving on the ACTION BAR (geometric, no WG events) ----
        # nearest country-line segment within 24 blocks decides which SIDE of the line
        # the player is on (cross product x its ring's inside-sign); when the remembered
        # side for that zone flips, the player just crossed the line.
        set {{_zbest}} to 576
        loop {{_k::*}}:
            loop {{bzone::%loop-value-2%::*}}:
                set {{_a}} to loop-value-3
                set {{_b}} to {{bzoneb::%loop-value-2%::%loop-index-2%}}
                set {{_abx}} to (x of {{_b}}) - (x of {{_a}})
                set {{_abz}} to (z of {{_b}}) - (z of {{_a}})
                set {{_len2}} to {{_abx}} * {{_abx}} + {{_abz}} * {{_abz}}
                if {{_len2}} > 0:
                    set {{_t}} to (({{_px}} - (x of {{_a}})) * {{_abx}} + ({{_pz}} - (z of {{_a}})) * {{_abz}}) / {{_len2}}
                    if {{_t}} < 0:
                        set {{_t}} to 0
                    if {{_t}} > 1:
                        set {{_t}} to 1
                    set {{_ddx}} to {{_px}} - ((x of {{_a}}) + {{_abx}} * {{_t}})
                    set {{_ddz}} to {{_pz}} - ((z of {{_a}}) + {{_abz}} * {{_t}})
                    set {{_d2}} to {{_ddx}} * {{_ddx}} + {{_ddz}} * {{_ddz}}
                    if {{_d2}} < {{_zbest}}:
                        set {{_zbest}} to {{_d2}}
                        set {{_cross}} to {{_abx}} * ({{_pz}} - (z of {{_a}})) - {{_abz}} * ({{_px}} - (x of {{_a}}))
                        set {{_zn}} to {{bzonen::%loop-value-2%::%loop-index-2%}}
                        set {{_zs}} to {{bzones::%loop-value-2%::%loop-index-2%}}
        if {{_zbest}} < 576:
            if {{_cross}} * {{_zs}} > 0:
                set {{_side}} to 1
            else:
                set {{_side}} to -1
            set {{_uu}} to "%uuid of loop-player%"
            # Announce ENTERING only — never "leaving" (a country-to-country crossing
            # then produces exactly one message: the destination's). A short cooldown
            # stops the action bar re-firing while a player hovers on the line.
            if {{bmside::%{{_uu}}%::%{{_zn}}%}} is set:
                if {{bmside::%{{_uu}}%::%{{_zn}}%}} is not {{_side}}:
                    if {{_side}} = 1:
                        set {{_ok}} to true
                        if {{bmt::%{{_uu}}%::%{{_zn}}%}} is set:
                            if difference between {{bmt::%{{_uu}}%::%{{_zn}}%}} and now < 4 seconds:
                                set {{_ok}} to false
                        if {{_ok}} is true:
                            send action bar "&8[&6★&8] &bEntering %{{_zn}}%" to loop-player
                            set {{bmt::%{{_uu}}%::%{{_zn}}%}} to now
            set {{bmside::%{{_uu}}%::%{{_zn}}%}} to {{_side}}

# the outlaw flag must not survive death/rejoin — a respawned player would be yanked
# back to the border from spawn by the out-of-range backstop
on death of player:
    delete {{bout::%uuid of victim%}}

on quit:
    delete {{bout::%uuid of player%}}

# ---- diagnostics ----
# /borderstats (console or player): proves the wall data loaded by probing one known
# segment variable per ring (list sizes can't be used — Skript's {{list::*}} skips
# branch nodes that only hold children). /bordertest (player): draws a particle cross
# at your feet, proving the SkBee dust pipeline works regardless of where you stand.
command /borderstats:
    trigger:
        if {hard_probe} is set:
            send "hard wall data: OK ({hard_n} segments expected)"
        else:
            send "hard wall data: MISSING (expected {hard_n} segments - the on-load section did not run; try /sk reload border)"
        if {share_probe} is set:
            send "shared border line data: OK ({share_n} segments expected)"
        else:
            send "shared border line data: MISSING (fine if zones share no border)"
        if {zone_probe} is set:
            send "zone wall data: OK ({zone_n} segments expected)"
        else:
            send "zone wall data: MISSING"
        send "render radius {{@radius}} blocks, update every {{@ticks}} ticks, wall height +/-{{@wall-h}}"

command /bordertest:
    executable by: players
    trigger:
        loop integers from -6 to 6:
            loop integers from 0 to 3:
                make 1 of dust using dustOption(yellow, 2.2) at location((x-coordinate of player) + loop-value-1, (y-coordinate of player) + loop-value-2, (z-coordinate of player), world of player)
                make 1 of dust using dustOption(orange, 2.2) at location((x-coordinate of player), (y-coordinate of player) + loop-value-2, (z-coordinate of player) + loop-value-1, world of player)
        send "&8[&6★&8] &eIf two crossing particle curtains appeared around you, the wall renderer works."

# end border.sk
"""
