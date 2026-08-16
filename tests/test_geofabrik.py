"""Finding and fetching the right Geofabrik extract, without trusting anything unverified.

Two realities these tests pin down. First: index-v1-nogeom.json sounds like the right index and
carries NO extent of any kind (verified 2026-08-16 - id, parent, name, urls, iso codes, nothing
else), so suggestions must come from index-v1.json's cutting polygons, and "smallest" from
polygon area because neither index carries file sizes. Second: a half-downloaded .pbf that wears
the final name scans as a real extract and the bake then "helpfully" skips it as corrupt - holes
in the world as the only symptom - so a download must live under .part until the last byte.

All network is mocked through geofabrik._open_url, the module's single seam to urllib.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import geofabrik as gf  # noqa: E402
from src import osm_pack as op  # noqa: E402


# ── a fake index, shaped exactly like the real one (verified schema) ──────────────────────────

def _feat(fid, parent, box, url=None):
    """One index feature: box = (west, south, east, north) as a single-ring MultiPolygon."""
    w, s, e, n = box
    return {"type": "Feature",
            "properties": {"id": fid, "parent": parent, "name": fid.replace("-", " ").title(),
                           "urls": {"pbf": url or f"https://dl.example/{fid}-latest.osm.pbf"}},
            "geometry": {"type": "MultiPolygon",
                         "coordinates": [[[[w, s], [e, s], [e, n], [w, n], [w, s]]]]}}


NESTED = {"features": [
    _feat("continent", None, (0, 0, 40, 40)),
    _feat("alpha", "continent", (0, 0, 20, 20)),
    _feat("alpha-south", "alpha", (0, 0, 20, 10)),
]}

BORDER = {"features": [
    _feat("continent", None, (0, 0, 20, 20)),
    _feat("west", "continent", (0, 0, 10, 20)),
    _feat("east", "continent", (10, 0, 20, 20)),
]}


class _Resp:
    """File-ish HTTP response: read() drains everything, read(n) hands out one chunk per call."""

    def __init__(self, chunks, total=None):
        self._chunks = list(chunks)
        self.headers = {} if total is None else {"Content-Length": str(total)}

    def read(self, n=-1):
        if n == -1:
            body, self._chunks = b"".join(self._chunks), []
            return body
        return self._chunks.pop(0) if self._chunks else b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ── suggest: the smallest file that does the job ──────────────────────────────────────────────

def test_suggest_picks_the_smallest_containing_extract():
    """Three nested extracts all contain the selection; the answer is the state-sized one, not
    the continent - the difference the planner measured is 19 GB vs 0.07 GB for the same tiles."""
    sel = {"south": 2, "west": 2, "north": 8, "east": 8}
    sugg = gf.suggest(sel, index=NESTED)
    assert [e["id"] for e in sugg] == ["alpha-south", "alpha", "continent"]
    assert sugg[0]["role"] == "contains" and sugg[0]["leaf"] is True
    assert all(e["role"] == "contains" for e in sugg), "a containing leaf needs no cover set"


def test_suggest_entries_carry_the_ui_contract():
    sel = {"south": 2, "west": 2, "north": 8, "east": 8}
    e = gf.suggest(sel, index=NESTED)[0]
    assert e["name"] == "Alpha South"
    assert e["url"] == "https://dl.example/alpha-south-latest.osm.pbf"
    assert e["parent"] == "alpha"
    assert e["covered_pct"] == 100.0
    assert e["area_deg2"] > 0


def test_cross_border_selection_returns_multiple_leaves():
    """A selection across a border has no single leaf; the bake merges .pbf seam-correctly, so
    the honest suggestion is BOTH country files, with the containing parent as the alternative."""
    sel = {"south": 8, "west": 8, "north": 12, "east": 12}
    sugg = gf.suggest(sel, index=BORDER)
    contains = [e["id"] for e in sugg if e["role"] == "contains"]
    cover = {e["id"] for e in sugg if e["role"] == "cover"}
    assert contains == ["continent"]
    assert cover == {"west", "east"}
    assert all(e["leaf"] for e in sugg if e["role"] == "cover")


def test_open_sea_outside_every_polygon_is_not_held_against_coverage():
    """Points no extract covers bake as empty tiles anyway; demanding an extract for them would
    make every coastal selection unanswerable."""
    coastal = {"features": [_feat("island", None, (0, 0, 20, 10))]}
    sel = {"south": 2, "west": 2, "north": 8, "east": 50}   # runs far offshore of the island
    sugg = gf.suggest(sel, index=coastal)
    assert any(e["id"] == "island" and e["role"] == "contains" for e in sugg)


def test_an_entry_without_geometry_inherits_the_parents_extent():
    """Today's index always has geometry; the day one entry doesn't, inheriting the parent can
    only OVER-cover (an extract never exceeds its parent), so it suggests a bigger file, never
    a hole."""
    idx = {"features": [
        _feat("continent", None, (0, 0, 40, 40)),
        {"type": "Feature",
         "properties": {"id": "orphan", "parent": "continent", "name": "Orphan",
                        "urls": {"pbf": "https://dl.example/orphan-latest.osm.pbf"}}},
    ]}
    sel = {"south": 2, "west": 2, "north": 8, "east": 8}
    assert {e["id"] for e in gf.suggest(sel, index=idx)} == {"continent", "orphan"}


# ── download: .part until proven complete ─────────────────────────────────────────────────────

def test_download_streams_to_part_then_renames(tmp_path, monkeypatch):
    chunks = [b"a" * 100, b"b" * 100, b"c" * 50]
    monkeypatch.setattr(gf, "_open_url", lambda url, timeout=120: _Resp(chunks, total=250))
    final = tmp_path / "x-latest.osm.pbf"
    part = tmp_path / "x-latest.osm.pbf.part"
    mid = []

    def prog(done, total):
        mid.append((part.exists(), final.exists(), done, total))

    res = gf.download("https://dl.example/x-latest.osm.pbf", tmp_path, on_progress=prog)
    assert res["ok"] is True and res["bytes"] == 250
    assert final.read_bytes() == b"".join(chunks)
    assert not part.exists()
    # Mid-transfer the bytes lived ONLY under .part - the scan globs *.pbf and must never see a
    # half file wearing the final name.
    assert mid and all(p and not f for p, f, _, _ in mid)
    assert mid[-1][2:] == (250, 250)


def test_cancelled_download_leaves_no_pbf(tmp_path, monkeypatch):
    monkeypatch.setattr(gf, "_open_url",
                        lambda url, timeout=120: _Resp([b"a" * 100] * 10, total=1000))
    calls = [0]

    def stop():
        calls[0] += 1
        return calls[0] > 1     # let one chunk land, then cancel

    res = gf.download("https://dl.example/x-latest.osm.pbf", tmp_path, should_stop=stop)
    assert res["ok"] is False and res["stopped"] is True
    assert list(tmp_path.iterdir()) == [], "a cancelled download must leave nothing behind"


def test_connection_drop_is_not_published_as_a_finished_file(tmp_path, monkeypatch):
    """A dropped connection surfaces as clean EOF, not an exception - without the length check
    the half file would be renamed as if the transfer had finished."""
    monkeypatch.setattr(gf, "_open_url", lambda url, timeout=120: _Resp([b"a" * 50], total=100))
    res = gf.download("https://dl.example/x-latest.osm.pbf", tmp_path)
    assert res["ok"] is False and res["stopped"] is False
    assert list(tmp_path.iterdir()) == []


# ── the index cache: 7 days, and stale beats nothing ──────────────────────────────────────────

def test_fresh_index_cache_never_touches_the_network(tmp_path, monkeypatch):
    monkeypatch.setattr(gf, "pbf_dir", lambda: tmp_path)
    (tmp_path / ".geofabrik-index.json").write_text(json.dumps(NESTED), encoding="utf-8")

    def boom(url, timeout=120):
        raise AssertionError("the index changes rarely; a fresh cache must not spend a request")
    monkeypatch.setattr(gf, "_open_url", boom)
    assert gf.fetch_index()["features"][0]["properties"]["id"] == "continent"


def test_stale_cache_is_served_when_the_network_fails(tmp_path, monkeypatch):
    """Last month's borders beat no suggestions at all for a user offline mid-project."""
    monkeypatch.setattr(gf, "pbf_dir", lambda: tmp_path)
    p = tmp_path / ".geofabrik-index.json"
    p.write_text(json.dumps(BORDER), encoding="utf-8")
    old = time.time() - 30 * 86400
    os.utime(p, (old, old))

    def down(url, timeout=120):
        raise OSError("no route to host")
    monkeypatch.setattr(gf, "_open_url", down)
    assert gf.fetch_index()["features"][1]["properties"]["id"] == "west"


def test_missing_cache_downloads_and_writes_it(tmp_path, monkeypatch):
    monkeypatch.setattr(gf, "pbf_dir", lambda: tmp_path)
    body = json.dumps(NESTED).encode("utf-8")
    monkeypatch.setattr(gf, "_open_url", lambda url, timeout=120: _Resp([body]))
    idx = gf.fetch_index()
    assert len(idx["features"]) == 3
    assert json.loads((tmp_path / ".geofabrik-index.json").read_text(encoding="utf-8")) == NESTED


# ── freshness: inform, never refuse ───────────────────────────────────────────────────────────

def test_stale_detection_from_mtime(tmp_path):
    old = tmp_path / "old-latest.osm.pbf"
    old.write_bytes(b"x")
    t = time.time() - 400 * 86400
    os.utime(old, (t, t))
    fresh = tmp_path / "fresh-latest.osm.pbf"
    fresh.write_bytes(b"x")

    scan = op.scan_pbf_folder(str(tmp_path))
    ages = {f["name"]: f["age_days"] for f in scan["files"]}
    assert ages["old-latest.osm.pbf"] > 399
    assert ages["fresh-latest.osm.pbf"] < 1
    assert op.stale_files(scan["files"]) == ["old-latest.osm.pbf"]


def test_a_dated_filename_outranks_a_fresh_mtime(tmp_path):
    """Copying a dated snapshot resets its mtime, so the file looks a day old while its DATA
    carries last year's roads. When the name says older, believe the name."""
    stamp = (datetime.now() - timedelta(days=365)).strftime("%y%m%d")
    p = tmp_path / f"danube-{stamp}.osm.pbf"
    p.write_bytes(b"x")     # mtime = now

    scan = op.scan_pbf_folder(str(tmp_path))
    assert scan["files"][0]["age_days"] >= 364
    assert op.stale_files(scan["files"]) == [p.name]


def test_the_ram_constant_matches_the_bake_planner():
    """geofabrik duplicates RAM_GB_PER_PBF_GB to avoid a circular import; if the two ever
    drift, the download warning and the bake refusal would disagree about the same file."""
    from src import geofabrik as gf, osm_pack as op
    assert gf.RAM_GB_PER_PBF_GB == op.RAM_GB_PER_PBF_GB


def test_size_enrichment_flags_an_oversized_candidate(monkeypatch):
    from src import geofabrik as gf

    class R:
        headers = {"Content-Length": str(int(20e9))}      # a 20 GB continent file
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(gf, "_open_url", lambda url, timeout=15, method="GET": R())

    import psutil
    class VM:
        available = int(20e9)                              # 20 GB free -> cannot bake 44 GB
    monkeypatch.setattr(psutil, "virtual_memory", lambda: VM)

    gf._SIZE_CACHE.clear()
    cand = [{"id": "europe", "url": "https://x/europe.osm.pbf", "role": "contains"}]
    gf.enrich_sizes(cand)
    assert cand[0]["size_bytes"] == int(20e9)
    assert cand[0]["ram_ok"] is False


def test_a_failed_head_hides_nothing(monkeypatch):
    """No size must degrade to "show the candidate without numbers", never to hiding it."""
    from src import geofabrik as gf
    def boom(url, timeout=15, method="GET"):
        raise OSError("blocked")
    monkeypatch.setattr(gf, "_open_url", boom)
    gf._SIZE_CACHE.clear()
    cand = [{"id": "x", "url": "https://x/x.osm.pbf", "role": "contains"}]
    gf.enrich_sizes(cand)
    assert "size_bytes" not in cand[0] and "ram_ok" not in cand[0]
