"""
geofabrik.py — "get the data for me": find and download the right Geofabrik extract(s).

WHY
    The .pbf bake made offline OSM possible, but it left the user the hardest step: knowing
    download.geofabrik.de exists, navigating its continent → country → state tree, and picking
    a file whose invisible cutting polygon actually covers their selection. The reported
    failure mode is a user downloading eight continent extracts (75 GB) to render one US state
    — see osm_pack.plan_bake. This module answers "which file(s)?" from the drawn selection and
    streams them into the default drop folder, so the zero-config path is suggest → fetch →
    scan → bake with no path typed and no browsing done.

THE INDEX, AS IT ACTUALLY IS (verified with a live fetch, 2026-08-16)
    Geofabrik publishes two machine-readable indexes. index-v1-nogeom.json sounds like the
    right one and is useless here: its 555 entries carry ONLY id, parent, name, urls and iso
    codes — no bbox, no extent of any kind. Only index-v1.json (~3.8 MB) carries each extract's
    cutting polygon (a GeoJSON MultiPolygon), so that is what we fetch and extents are computed
    from the geometry. Neither index carries file sizes, so "smallest" is ranked by polygon
    area — the honest proxy available without spending a HEAD request per candidate.

CONTAINMENT IS POLYGON, NOT BBOX
    An extract's bbox lies. Italy's bbox contains most of the Adriatic; its cutting polygon
    does not. Suggesting by bbox would hand the user a file that silently lacks half their
    selection, and the hole would surface as an empty half-world after an hours-long bake. So
    candidates are tested by sampling a grid of points across the selection against the real
    MultiPolygon. Points NO extract covers (open sea beyond every cutting polygon) are dropped
    from the universe instead of making coverage unreachable — those tiles bake empty anyway.

Stdlib only. All network goes through _open_url() so tests can replace one function.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .paths import data_dir

INDEX_URL = "https://download.geofabrik.de/index-v1.json"
# Extract boundaries change on the order of years, not days; asking the server per suggestion
# would spend requests to learn nothing new.
INDEX_TTL_S = 7 * 86400
# 5x5 sample points across the selection. Enough to catch a cutting polygon that dodges the
# middle of the bbox, cheap enough that testing all overlapping candidates stays instant.
_GRID_N = 5


def pbf_dir() -> Path:
    """The default .pbf drop folder (data/pbf), created lazily. One well-known folder so the
    downloader and the bake agree on a location without the user ever typing a path."""
    d = data_dir() / "pbf"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _open_url(url: str, timeout: int = 120, method: str = "GET"):
    """The single seam between this module and the network — tests monkeypatch this one name
    instead of reaching into urllib. HEAD rides through the same seam: a second seam would be a
    second thing tests forget to patch."""
    req = urllib.request.Request(url, headers={"User-Agent": "Meld (geofabrik fetcher)"},
                                 method=method)
    return urllib.request.urlopen(req, timeout=timeout)


# ── the index ─────────────────────────────────────────────────────────────────
def index_cache_path() -> Path:
    return pbf_dir() / ".geofabrik-index.json"


def fetch_index(force: bool = False) -> dict:
    """The Geofabrik index, from a 7-day disk cache when possible.

    A failed refresh falls back to whatever cached copy exists, however old: last month's
    borders beat no suggestions at all when the user is offline mid-project, and an extract
    boundary that moved within a month is a curiosity, not a hole in a world.
    """
    p = index_cache_path()
    try:
        if not force and time.time() - p.stat().st_mtime < INDEX_TTL_S:
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    try:
        with _open_url(INDEX_URL) as r:
            raw = r.read()
        idx = json.loads(raw.decode("utf-8"))
        # tmp → replace, same discipline as the tile writers: a crash mid-write must not leave
        # a truncated cache that parses as "no features" and kills suggestions for a week.
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, p)
        return idx
    except Exception as ex:  # noqa: BLE001
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            raise RuntimeError(
                f"could not fetch the Geofabrik index and no cached copy exists ({ex})")


def pbf_name(url: str) -> str:
    """Filename an extract url downloads as (the path's last segment)."""
    return Path(urllib.parse.urlsplit(url).path).name


def resolve_pbf_url(ident: str, index: dict | None = None) -> str | None:
    """Index id (e.g. "romania") → its .pbf url, or None if the id is unknown."""
    idx = index if index is not None else fetch_index()
    for f in idx.get("features", []):
        props = f.get("properties") or {}
        if props.get("id") == ident:
            return (props.get("urls") or {}).get("pbf")
    return None


# ── geometry (even-odd over all rings: outer rings and holes need no bookkeeping) ──
def _rings(geom: dict | None) -> list:
    """Flatten a GeoJSON (Multi)Polygon to a flat list of rings. The even-odd test below treats
    outers and holes identically — crossing an outer puts a point in, crossing a hole takes it
    back out — so flattening loses nothing."""
    if not geom:
        return []
    co = geom.get("coordinates") or []
    if geom.get("type") == "Polygon":
        return [r for r in co if r]
    if geom.get("type") == "MultiPolygon":
        return [r for poly in co for r in poly if r]
    return []


def _point_in_rings(lon: float, lat: float, rings: list) -> bool:
    """Even-odd ray cast (eastward) across every ring."""
    inside = False
    for ring in rings:
        j = len(ring) - 1
        for i in range(len(ring)):
            y1, y2 = ring[j][1], ring[i][1]
            if (y1 > lat) != (y2 > lat):
                x_at = ring[j][0] + (lat - y1) * (ring[i][0] - ring[j][0]) / (y2 - y1)
                if x_at > lon:
                    inside = not inside
            j = i
    return inside


def _seg_crosses_bbox(x1, y1, x2, y2, b: dict) -> bool:
    """Does the segment (x1,y1)-(x2,y2) touch the axis-aligned box `b`? Liang-Barsky clip."""
    dx, dy = x2 - x1, y2 - y1
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x1 - b["west"]), (dx, b["east"] - x1),
                 (-dy, y1 - b["south"]), (dy, b["north"] - y1)):
        if p == 0:
            if q < 0:
                return False             # parallel to this edge and outside it
            continue
        r = q / p
        if p < 0:
            if r > t1:
                return False
            t0 = max(t0, r)
        else:
            if r < t0:
                return False
            t1 = min(t1, r)
    return t0 <= t1


def rings_intersect_bbox(rings: list, b: dict) -> bool:
    """Does a polygon (list of rings) touch an axis-aligned lat/lon box?

    Exact, not sampled. Sampling a box against a country polygon is unsafe in exactly the case
    that matters here: a country reaching into the box as a strip narrower than the sample
    spacing scores zero hits and gets dropped, and dropping an extract means a hole in somebody's
    world. Three tests cover every way a simple polygon and a rectangle can meet - a vertex
    inside the box, a box corner inside the polygon, or an edge crossing - and the caller is
    expected to bbox-reject first, since this is per-vertex work.
    """
    if not rings or not b:
        return True                      # unknown geometry: never exclude on a guess
    for ring in rings:
        for lon, lat in ring:
            if b["west"] <= lon <= b["east"] and b["south"] <= lat <= b["north"]:
                return True
    for lon, lat in ((b["west"], b["south"]), (b["east"], b["south"]),
                     (b["west"], b["north"]), (b["east"], b["north"])):
        if _point_in_rings(lon, lat, rings):
            return True
    for ring in rings:
        for i in range(len(ring)):
            x1, y1 = ring[i]
            x2, y2 = ring[(i + 1) % len(ring)]
            if _seg_crosses_bbox(x1, y1, x2, y2, b):
                return True
    return False


def rings_for_pbf_name(name: str, index: dict | None = None) -> list | None:
    """Local .pbf filename -> that extract's border polygon, or None if we cannot place it.

    None means "unknown", and every caller must treat unknown as "keep the file". Filenames in
    the wild carry a date or `-latest` that the index id does not: germany-260814.osm.pbf and
    germany-latest.osm.pbf are both the `germany` feature.
    """
    stem = str(name or "").strip()
    if not stem:
        return None
    stem = Path(stem).name
    for suffix in (".osm.pbf", ".pbf"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    stem = re.sub(r"-(?:latest|\d{6,8})$", "", stem)
    if not stem:
        return None
    try:
        idx = index if index is not None else fetch_index()
    except Exception:  # noqa: BLE001
        return None                      # offline / unreadable index: unknown, so keep the file
    feats = idx.get("features", [])
    by_id = {f["properties"]["id"]: f for f in feats
             if (f.get("properties") or {}).get("id")}
    f = by_id.get(stem)
    if f is None:
        # Fall back to matching on the filename the index itself would download as, so a
        # renamed-but-recognisable file still resolves.
        for cand in feats:
            url = ((cand.get("properties") or {}).get("urls") or {}).get("pbf")
            if url and pbf_name(url).startswith(stem):
                f = cand
                break
    if f is None:
        return None
    rings = _feature_rings(f, by_id)
    return rings or None


def _rings_bbox(rings: list) -> dict:
    xs = [p[0] for r in rings for p in r]
    ys = [p[1] for r in rings for p in r]
    return {"south": min(ys), "west": min(xs), "north": max(ys), "east": max(xs)}


def _rings_area(rings: list) -> float:
    """Σ |shoelace| per ring, in deg². Ranking key, not a measurement: the bbox area would rank
    france (whose extract spans overseas territories, so its bbox spans oceans) as larger than
    europe. Ring areas don't. Holes count positive — Geofabrik cutting polygons don't use them."""
    total = 0.0
    for ring in rings:
        a = 0.0
        j = len(ring) - 1
        for i in range(len(ring)):
            a += (ring[j][0] + ring[i][0]) * (ring[j][1] - ring[i][1])
            j = i
        total += abs(a) / 2.0
    return total


def _feature_rings(f: dict, by_id: dict) -> list:
    """A feature's cutting polygon, inheriting the parent's when its own is missing. Today's
    index always carries geometry; the fallback exists for the day an entry doesn't, because a
    parent's polygon can only OVER-cover — an extract never exceeds its parent — so inheriting
    suggests a bigger file, never a hole."""
    seen: set = set()
    while f is not None:
        r = _rings(f.get("geometry"))
        if r:
            return r
        pid = (f.get("properties") or {}).get("parent")
        if not pid or pid in seen:
            return []
        seen.add(pid)
        f = by_id.get(pid)
    return []


# ── the suggestion ────────────────────────────────────────────────────────────
def suggest(bbox: dict, index: dict | None = None, grid_n: int = _GRID_N,
            max_singles: int = 3) -> list[dict]:
    """Candidate extracts for `bbox`: the smallest extracts whose polygon contains it (role
    "contains", smallest-area first, up to `max_singles`), plus — when no single LEAF contains
    it — a greedy minimal set of leaf extracts that jointly cover it (role "cover"). The bake
    already merges multiple .pbf seam-correctly, so a cross-border cover set is a first-class
    answer, not a fallback.

    Each entry: {id, name, parent, url, leaf, role, extent, area_deg2, covered_pct}. No size:
    the index carries none (verified), and area_deg2 is the honest stand-in.
    """
    idx = index if index is not None else fetch_index()
    feats = [f for f in idx.get("features", [])
             if ((f.get("properties") or {}).get("urls") or {}).get("pbf")]
    by_id = {f["properties"]["id"]: f for f in feats if f["properties"].get("id")}
    # A leaf is an id nobody names as their parent — the index has no explicit flag for it.
    parent_ids = {f["properties"].get("parent") for f in feats} - {None}

    n = max(2, int(grid_n))
    pts = []
    for i in range(n):
        for j in range(n):
            pts.append((bbox["west"] + (bbox["east"] - bbox["west"]) * j / (n - 1),
                        bbox["south"] + (bbox["north"] - bbox["south"]) * i / (n - 1)))

    cand = []
    for f in feats:
        rings = _feature_rings(f, by_id)
        if not rings:
            continue
        fb = _rings_bbox(rings)
        # bbox quick-reject first: the polygon test is per-vertex work and most of the 555
        # extracts are nowhere near the selection.
        if (fb["north"] < bbox["south"] or fb["south"] > bbox["north"]
                or fb["east"] < bbox["west"] or fb["west"] > bbox["east"]):
            continue
        mask = frozenset(k for k, (lon, lat) in enumerate(pts)
                         if _point_in_rings(lon, lat, rings))
        if mask:
            cand.append((f, rings, fb, mask))
    if not cand:
        return []

    universe: set = set()
    for _, _, _, mask in cand:
        universe |= mask

    def _entry(f, rings, fb, mask, role):
        props = f["properties"]
        return {"id": props.get("id"), "name": props.get("name"),
                "parent": props.get("parent"),
                "url": (props.get("urls") or {}).get("pbf"),
                "leaf": props.get("id") not in parent_ids, "role": role,
                "extent": fb, "area_deg2": round(_rings_area(rings), 2),
                "covered_pct": round(100.0 * len(mask & universe) / len(universe), 1)}

    out = []
    singles = [(f, r, fb, m) for f, r, fb, m in cand if universe <= m]
    singles.sort(key=lambda t: _rings_area(t[1]))
    for f, r, fb, m in singles[:max_singles]:
        out.append(_entry(f, r, fb, m, "contains"))

    if not any(e["leaf"] for e in out):
        # No single leaf holds the whole selection (a border was crossed): greedy set cover
        # over the leaves. Greedy, not optimal — set cover is NP-hard and a country border
        # crossing involves 2-4 files, where greedy IS optimal in practice.
        leaves = [(f, r, fb, m) for f, r, fb, m in cand
                  if f["properties"].get("id") not in parent_ids]
        remaining = set(universe)
        while remaining and leaves:
            f, r, fb, m = max(leaves, key=lambda t: (len(t[3] & remaining), -_rings_area(t[1])))
            if not (m & remaining):
                break
            out.append(_entry(f, r, fb, m, "cover"))
            remaining -= m
            leaves = [t for t in leaves if t[0] is not f]
    return out


# ── the download ──────────────────────────────────────────────────────────────
def download(url: str, dest_folder, on_progress=None, should_stop=None,
             chunk_bytes: int = 1 << 18) -> dict:
    """Stream `url` into dest_folder as <name>.part, publishing <name> with one os.replace.

    The scan globs *.pbf, so a half-downloaded file must never wear the final name: it would
    scan as a real extract and the bake's corrupt-.pbf skip would then "helpfully" drop it —
    holes in the world as the only symptom. The .part suffix also keeps an in-flight download
    invisible to a concurrently running scan. Any exit but success removes the .part.

    on_progress(bytes_done, bytes_total) — total is 0 when the server sends no Content-Length.
    should_stop() → True aborts cleanly: {ok: False, stopped: True}.
    """
    name = pbf_name(url)
    if not name.endswith(".pbf"):
        raise ValueError(f"not a .pbf url: {url}")
    dest = Path(dest_folder)
    dest.mkdir(parents=True, exist_ok=True)
    final, part = dest / name, dest / (name + ".part")
    done = total = 0
    try:
        with _open_url(url) as r, open(part, "wb") as f:
            try:
                total = int(r.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                total = 0
            while True:
                if should_stop and should_stop():
                    return {"ok": False, "stopped": True, "path": "", "bytes": done,
                            "total": total}
                b = r.read(chunk_bytes)
                if not b:
                    break
                f.write(b)
                done += len(b)
                if on_progress:
                    on_progress(done, total)
        # A dropped connection surfaces as a clean EOF, not an exception — without this check a
        # half file would be published as if the transfer had finished.
        if total and done < total:
            return {"ok": False, "stopped": False, "path": "", "bytes": done, "total": total,
                    "error": f"connection dropped at {done}/{total} bytes"}
        os.replace(part, final)
        return {"ok": True, "stopped": False, "path": str(final), "bytes": done, "total": total}
    finally:
        try:
            part.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


# Peak bake RAM per GB of .pbf, per worker - measured (pyosmium 4.3.1, ukraine-latest:
# 871 MB -> 1910 MB peak). Duplicated from osm_pack to avoid a circular import; the two are
# asserted equal in tests.
RAM_GB_PER_PBF_GB = 2.2

_SIZE_CACHE: dict[str, int] = {}


def enrich_sizes(suggestions: list[dict], limit: int = 5) -> None:
    """Fill in size_bytes / ram_gb / ram_ok for the candidates a user will actually see.

    The index carries no file sizes at all, and the download size is the one number that decides
    whether a candidate is a 70 MB two-minute bake or a 19 GB machine-eater - so the first few
    candidates get one HEAD request each (Content-Length, cached per URL for the session).
    Best-effort by design: a failed HEAD leaves the fields absent rather than failing the
    suggestion, because "no size known" must degrade to showing the candidate, not hiding it.

    ram_ok compares the measured 2.2 GB-per-GB bake cost of THIS extract against the memory
    actually free right now - the warning the 68 GB-machine / 190 GB-pagefile report asked for.
    """
    try:
        import psutil
        avail_gb = psutil.virtual_memory().available / 1e9
    except Exception:                                          # noqa: BLE001
        avail_gb = None
    for cand in suggestions[:limit]:
        url = cand.get("url")
        if not url:
            continue
        if url not in _SIZE_CACHE:
            try:
                with _open_url(url, timeout=15, method="HEAD") as r:
                    _SIZE_CACHE[url] = int(r.headers.get("Content-Length") or 0)
            except Exception:                                  # noqa: BLE001
                _SIZE_CACHE[url] = 0
        size = _SIZE_CACHE[url]
        if size:
            ram = size / 1e9 * RAM_GB_PER_PBF_GB
            cand["size_bytes"] = size
            cand["ram_gb"] = round(ram, 1)
            if avail_gb is not None:
                # 0.6: same headroom factor the bake planner budgets with - the OS, the browser
                # and any running render need the rest.
                cand["ram_ok"] = ram <= avail_gb * 0.6
