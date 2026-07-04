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


def _zone_lonlat(country_names: list[str]):
    """Union of the named countries as one lon/lat geometry."""
    cs = _countries()
    geoms = [cs[n.strip().lower()] for n in country_names if n.strip().lower() in cs]
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
    p_actual = max(3, min(1000, int(pts.get("actual", 700))))
    p_soft = max(3, min(1000, int(pts.get("soft", p_actual))))
    p_hard = max(3, min(1000, int(pts.get("hard", p_actual))))

    zones, geoms = [], []
    for z in spec.get("zones", []):
        geom = _to_blocks(_zone_lonlat(z.get("countries", [])), o_lat, o_lon, scale)
        geoms.append(geom)
        xz = _ring_xz(geom, p_actual)
        zones.append({"spec": z, "geom_block": geom, "xz": xz,
                      "ll": [list(_inv(x, zz, o_lat, o_lon, scale)) for x, zz in xz]})
    if not geoms:
        raise ValueError("no zones")

    clump = unary_union(geoms)
    soft_b, hard_b = soft_km * 1000.0 * scale, hard_km * 1000.0 * scale
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
            xz = _shared_line_xz(zones[i]["geom_block"], zones[j]["geom_block"],
                                 int(spec.get("shared_points", 20)), eps)
            shared.append({"between": [zones[i]["spec"].get("name"), zones[j]["spec"].get("name")],
                           "xz": xz, "ll": [list(_inv(x, zz, o_lat, o_lon, scale)) for x, zz in xz]})

    return {"zones": zones,
            "clump": {"soft_xz": soft_xz, "soft_ll": soft_ll, "hard_xz": hard_xz, "hard_ll": hard_ll,
                      "trim_xz": trim_xz, "trim_ll": trim_ll,
                      "soft_km": soft_km, "hard_km": hard_km, "trim_km": trim_km},
            "shared": shared, "scale": scale, "origin": {"lat": o_lat, "lon": o_lon}}


# ------------------------------------------------------------------- exporters
COLORS = {"actual": "#00e5ff", "soft": "#ff9800", "hard": "#ffeb3b", "shared": "#76ff03"}


def preview(result: dict) -> dict:
    """Map payload: per-zone ACTUAL borders + ONE clump soft + ONE clump hard + shared lines."""
    cl = result["clump"]
    zones = [{"name": z["spec"].get("name", "zone"), "ll": z["ll"], "count": len(z["xz"]),
              "color": z["spec"].get("color_actual", COLORS["actual"]),
              "label": (z["spec"].get("name", "zone") + " border")} for z in result["zones"]]
    clump = [
        {"key": "soft", "ll": cl["soft_ll"], "count": len(cl["soft_xz"]), "color": COLORS["soft"],
         "label": f"soft +{cl['soft_km']:g} km - build OK to here (safe edge)"},
        {"key": "hard", "ll": cl["hard_ll"], "count": len(cl["hard_xz"]), "color": COLORS["hard"],
         "label": f"hard +{cl['hard_km']:g} km - no-build + kill-zone wall (soft->hard)"},
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
    """Build is ALLOWED inside the soft ring (the country + a few-km safe margin) and DENIED in the
    soft->hard band (the no-build kill-zone) and outside (global deny). So `border_soft` carries the
    allow (priority 8), `border_hard` the deny (priority 5, the outer wall), and each zone's own
    ACTUAL region (priority 12, no build flag) is identity only (titles + owners/members)."""
    cl = result["clump"]
    L = ["__global__:", "    type: global",
         "    flags: {block-break: deny, block-place: deny}", ""]
    L += _region("border_hard", min_y, max_y, 5, cl["hard_xz"],
                 {"block-break": "deny", "block-place": "deny"}, [], [])
    L += _region("border_soft", min_y, max_y, 8, cl["soft_xz"],
                 {"block-break": "allow", "block-place": "allow"}, [], [])
    for z in result["zones"]:
        s = z["spec"]
        L += _region(_rid(s.get("name")), min_y, max_y, 12, z["xz"],
                     s.get("flags_actual", {}), s.get("owners", []) or [], s.get("members", []) or [])
    return "\n".join(L) + "\n"


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
_WALL_CAP = 1200        # max stored segments per ring group (spacing grows past this)


def _split_ring_segments(rings: list, max_seg: float, cap: int) -> list:
    """Closed ring(s) of (x,z) points -> list of ((ax,az),(bx,bz)) segments no longer
    than max_seg blocks (re-split coarser if the cap would be exceeded)."""
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
    seg = max(max_seg, total / max(1, cap))
    out = []
    for (ax, az), (bx, bz) in raw:
        d = math.dist((ax, az), (bx, bz))
        steps = max(1, int(math.ceil(d / seg)))
        for i in range(steps):
            t0, t1 = i / steps, (i + 1) / steps
            out.append(((ax + (bx - ax) * t0, az + (bz - az) * t0),
                        (ax + (bx - ax) * t1, az + (bz - az) * t1)))
    return out


def _wall_var_lines(tree: str, rings: list) -> str:
    """Skript `set` lines that embed a ring group as two parallel vector list-vars
    ({tree::cX_Z::i} = segment start, {tree}b = segment end), bucketed by the
    segment midpoint into _WALL_CELL cells for cheap near-player lookup."""
    segs = _split_ring_segments(rings, _WALL_SEG, _WALL_CAP)
    counters: dict = {}
    lines = []
    for (ax, az), (bx, bz) in segs:
        cx = math.floor((ax + bx) / 2 / _WALL_CELL)
        cz = math.floor((az + bz) / 2 / _WALL_CELL)
        key = f"c{cx}_{cz}"
        counters[key] = counters.get(key, 0) + 1
        i = counters[key]
        lines.append(f"    set {{{tree}::{key}::{i}}} to vector({ax:.0f}, 0, {az:.0f})")
        lines.append(f"    set {{{tree}b::{key}::{i}}} to vector({bx:.0f}, 0, {bz:.0f})")
    return "\n".join(lines)


def _wall_draw_block(tree: str, color: str) -> str:
    """The per-ring draw pass: scan the player's 3x3 bucket neighbourhood, interpolate
    each in-radius segment at ~4-block steps, stack SkBee dust from -wall-h..+wall-h."""
    base = " " * 12
    body = f"""loop {{_k::*}}:
    loop {{{tree}::%loop-value-1%::*}}:
        set {{_a}} to loop-value-2
        set {{_dx}} to (x of {{_a}}) - {{_px}}
        set {{_dz}} to (z of {{_a}}) - {{_pz}}
        if ({{_dx}} * {{_dx}}) + ({{_dz}} * {{_dz}}) <= {{@radius}} * {{@radius}}:
            set {{_b}} to {{{tree}b::%loop-value-1%::%loop-index-2%}}
            set {{_ax}} to x of {{_a}}
            set {{_az}} to z of {{_a}}
            set {{_bx}} to x of {{_b}}
            set {{_bz}} to z of {{_b}}
            set {{_n}} to ceil(sqrt(({{_bx}} - {{_ax}}) ^ 2 + ({{_bz}} - {{_az}}) ^ 2) / 4)
            if {{_n}} < 1:
                set {{_n}} to 1
            loop integers from 0 to {{_n}}:
                set {{_t}} to loop-value-3 / {{_n}}
                set {{_x}} to {{_ax}} + ({{_bx}} - {{_ax}}) * {{_t}}
                set {{_z}} to {{_az}} + ({{_bz}} - {{_az}}) * {{_t}}
                loop integers from -{{@wall-h}} to {{@wall-h}}:
                    make 1 of dust using dustOption({color}, 1.5) at location({{_x}}, {{_py}} + loop-value-4, {{_z}}, {{_w}}) to loop-player"""
    return "\n".join(base + ln if ln.strip() else ln for ln in body.split("\n"))
def write_skript(result: dict, opts: dict) -> str:
    """Generate a server-side border.sk: country titles, the soft->hard escalating kill-zone, the
    hard fling-back wall, and packet-particle walls (skript-particle). Parameterised by the UI opts.
    The Skript references the regions Meld emits (border_hard/border_soft + the zone names) and reads
    the exported point files for the particle walls."""
    interval = max(1, int(opts.get("interval", 60)))
    base = max(1, int(opts.get("base", 1)))
    curve = (opts.get("curve") or "double").lower()
    knock = float(opts.get("knockback", 1.8))
    # radius must stay within one bucket cell so the 3x3 neighbourhood scan covers it
    radius = max(16, min(_WALL_CELL - 8, int(opts.get("render_radius", 56))))
    wallh = int(opts.get("wall_height", 4))
    ticks = max(1, int(opts.get("update_ticks", 8)))
    zone_ids = [_rid(z["spec"].get("name")) for z in result["zones"]]
    zone_titles = {_rid(z["spec"].get("name")): (z["spec"].get("name") or "zone") for z in result["zones"]}
    dmg = "{@base} * 2 ^ ({_t} - 1)" if curve == "double" else "{@base} * {_t}"
    title_lines = "\n".join(
        f'    else if "{zid}" is in the regions at the player:\n'
        f'        send title "&bEntering {zone_titles[zid]}" with subtitle "" to the player for 2 seconds'
        for zid in zone_ids)
    cl = result["clump"]
    hard_lines = _wall_var_lines("bhard", [cl["hard_xz"]])
    soft_lines = _wall_var_lines("bsoft", [cl["soft_xz"]])
    zone_lines = _wall_var_lines("bzone", [z["xz"] for z in result["zones"]])
    draw_hard = _wall_draw_block("bhard", "yellow")
    draw_soft = _wall_draw_block("bsoft", "orange")
    draw_zone = _wall_draw_block("bzone", "aqua")
    return f"""# border.sk  -  generated by Meld (Border & zones, v2).
# Server runtime for the country-border system. Meld owns geometry; this owns titles, the kill-zone,
# the fling-back wall, and the packet-particle walls.
#
# REQUIRES (Meld's server setup installs all of these automatically):
#   WorldGuard, WorldEdit, Skript, skworldguard (region enter/exit events),
#   SkBee (dust particles for the walls — the wall geometry is EMBEDDED below,
#   no point files or other addons needed).
# SETUP:
#   1. /rg load   (after copying the exported regions.yml into the world's WorldGuard data)
#   2. /sk reload border
#
# Regions used (from regions.yml): border_hard (outer wall, build allowed), border_soft (safe edge),
#   {", ".join(zone_ids) or "<zones>"} (country identity).

options:
    interval: {interval}      # seconds per kill-zone damage step
    base: {base}              # hearts at step 1 ({curve} curve)
    knockback: {knock}
    radius: {radius}          # particle render radius (blocks)
    wall-h: {wallh}           # wall height above/below the player
    ticks: {ticks}            # particle update interval (ticks)

# ---- country titles + safe-zone notice (region events, never test polygons) ----
on region entered:
    set {{_r}} to "%name of event-region%"
    if {{_r}} is "border_soft":
        # crossed inward to safe -> nothing
    else if {{_r}} is "border_hard":
        # entered the outer ring from outside is impossible (void), ignore
    else:
{title_lines if title_lines else '        # (no zones)'}

on region exited:
    set {{_r}} to "%name of event-region%"
    if {{_r}} is "border_soft":
        # left the safe zone into the kill-zone (still inside border_hard)
        if "border_hard" is in the regions at the player:
            set {{kz_start::%uuid of player%}} to now
            set {{kz_tick::%uuid of player%}} to 0
            send title "&eYou left the safe zone" with subtitle "&cget back or it gets worse" to the player for 2 seconds
    else if {{_r}} is "border_hard":
        # crossed the OUTER wall -> fling back
        push the player in the horizontal facing of the player on z-axis at speed {{@knockback}} * -1
        damage the player by 4 hearts
        send title "&cYou went too far" with subtitle "&7turn back" to the player for 2 seconds
        if {{last_in::%uuid of player%}} is set:
            teleport the player to {{last_in::%uuid of player%}}

# ---- per-second: kill-zone escalation + last-inside backstop ----
every 1 second:
    loop all players:
        if "border_hard" is in the regions at loop-player:
            set {{last_in::%uuid of loop-player%}} to location of loop-player
            if "border_soft" is not in the regions at loop-player:
                # in the soft->hard band = the kill-zone
                set {{_t}} to round((difference between {{kz_start::%uuid of loop-player%}} and now) / {{@interval}} seconds, floor) + 1
                if {{_t}} > {{kz_tick::%uuid of loop-player%}}:
                    set {{kz_tick::%uuid of loop-player%}} to {{_t}}
                    set {{_dmg}} to {dmg}
                    damage loop-player by {{_dmg}} hearts
                    send action bar "&cOutside the border: &e%{{_dmg}}% hearts &c- get back" to loop-player
            else:
                delete {{kz_start::%uuid of loop-player%}}
                delete {{kz_tick::%uuid of loop-player%}}
        else:
            delete {{kz_start::%uuid of loop-player%}}
            delete {{kz_tick::%uuid of loop-player%}}

on death of player:
    delete {{kz_start::%uuid of victim%}}
    delete {{kz_tick::%uuid of victim%}}

# ---- packet-particle walls (SkBee dust, per-player, near segments only) ----
# Wall geometry is EMBEDDED below: segment endpoints are baked into bucketed list variables on
# load ({_WALL_CELL}-block cells), so every {{@ticks}} ticks each in-border player only scans the
# 3x3 cells around them, interpolates the in-radius segments at ~4-block steps and gets a dust
# curtain from y-{{@wall-h}} to y+{{@wall-h}} sent ONLY to them (packet particles, no world lag).
# Colors: hard wall = yellow, soft (safe edge) = orange, country borders = aqua.
# If your SkBee build rejects the trailing "to loop-player", delete that tail — the wall then
# renders for everyone near it instead of per-player (same visual, slightly more packets).
on load:
    delete {{bhard::*}}
    delete {{bhardb::*}}
    delete {{bsoft::*}}
    delete {{bsoftb::*}}
    delete {{bzone::*}}
    delete {{bzoneb::*}}
{hard_lines}
{soft_lines}
{zone_lines}

every {{@ticks}} ticks:
    loop all players:
        # region gate = right world AND inside the playable area, one cheap check
        if "border_hard" is in the regions at loop-player:
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
{draw_soft}
{draw_zone}

# end border.sk
"""
