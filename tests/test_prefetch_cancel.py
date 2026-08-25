"""Prefetch cancellation is UNIFORM across every entry point.

A stop press has to stop the download, not just the UI. Before this, run_terrain_prefetch was
the only fetcher that could be interrupted: run_prefetch would keep pulling Overpass tiles (and
sleeping through retry backoffs) for minutes after the user stopped the build. The contract is
the same everywhere now — `should_stop` polled between units, and a clean RETURN of the partial
result, never an exception, because "stopped" is a normal outcome and the cells simply fall back
to fetching live.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import prefetch  # noqa: E402

ENTRY_POINTS = ("run_prefetch", "run_terrain_prefetch", "run_mapterhorn_bake", "purge_small_tiles")
ORIGIN = {"lat": 44.43, "lon": 26.10}
SETTINGS = {"scale": 1.0, "seam_buffer_chunks": 8, "prefetch_margin_m": 256,
            "prefetch_concurrency": 1}          # serial, so the call count below is deterministic
# Two cells ~1000 km apart, so they land on different z11 grid tiles (and different clumps).
CELLS = [{"cell_key": "0,0,4"}, {"cell_key": "500,0,4"}]


def test_every_entry_point_accepts_should_stop():
    missing = [n for n in ENTRY_POINTS
               if "should_stop" not in inspect.signature(getattr(prefetch, n)).parameters]
    assert not missing, f"not cancellable: {missing}"


def test_run_prefetch_returns_empty_when_already_stopped(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("downloaded after the stop flag was already up")
    monkeypatch.setattr(prefetch, "_download_one", boom)
    out = prefetch.run_prefetch(CELLS, ORIGIN, SETTINGS, "arnis.exe", tmp_path,
                                lambda s: None, lambda c: None, should_stop=lambda: True)
    assert out == {}                            # clean return, no exception


def test_run_prefetch_stops_between_tiles(tmp_path, monkeypatch):
    """The flag flips during the FIRST tile; nothing after it may be fetched."""
    state = {"stop": False, "calls": 0}

    def fake_dl(exe, bbox, out_json, urls, log, retries=3, should_stop=None):
        state["calls"] += 1
        state["stop"] = True
        return False, "stopped"

    monkeypatch.setattr(prefetch, "_download_one", fake_dl)
    chunks = []
    out = prefetch.run_prefetch(CELLS, ORIGIN, SETTINGS, "arnis.exe", tmp_path,
                                lambda s: None, chunks.append,
                                should_stop=lambda: state["stop"])
    assert state["calls"] == 1
    assert out == {}                            # both cells fall back to live
    assert all(c["state"] != "done" for c in chunks)


def test_run_prefetch_still_works_without_a_stop_callback(tmp_path, monkeypatch):
    """Default None = the old behaviour, byte for byte: every tile attempted."""
    seen = []

    def fake_dl(exe, bbox, out_json, urls, log, retries=3, should_stop=None):
        seen.append(bbox)
        return False, "exit 1"

    monkeypatch.setattr(prefetch, "_download_one", fake_dl)
    out = prefetch.run_prefetch(CELLS, ORIGIN, SETTINGS, "arnis.exe", tmp_path,
                                lambda s: None, lambda c: None)
    assert len(seen) >= 2                       # kept going past the first failure
    assert out == {}


def test_a_raising_stop_callback_does_not_stop_the_fetch():
    def bad():
        raise RuntimeError("flag exploded")
    assert prefetch._stop_requested(bad) is False
    assert prefetch._stop_requested(None) is False
    assert prefetch._stop_requested(lambda: True) is True


def test_terrain_and_mapterhorn_stop_before_the_first_sweep(monkeypatch):
    import subprocess

    def boom(*a, **k):
        raise AssertionError("spawned arnis after the stop flag was up")
    monkeypatch.setattr(subprocess, "run", boom)
    bboxes = [{"south": 44.4, "west": 26.0, "north": 44.5, "east": 26.2}] * 3
    r1 = prefetch.run_terrain_prefetch(bboxes, "arnis.exe", lambda s: None, should_stop=lambda: True)
    r2 = prefetch.run_mapterhorn_bake(bboxes, "arnis.exe", lambda s: None, should_stop=lambda: True)
    assert r1["ok"] == 0 and r2["ok"] == 0
