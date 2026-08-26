"""Phase-2 measurement + correctness tasks: I1 timers, H2 schema/4, C2 frozen master, C5 log tail.

What each test here would have caught:

* I1 - the merge/prune/health/meta cost was only ever inferred from cell-log mtimes. If the four
  timers stop reaching the report, N6 ("<= 7 s of post-arnis tail per 81-cell run") goes back to
  being unharvestable and any future regression in the merge path is invisible.
* H2 - schema/4 is ADDITIVE. A reader written against schema/3 must still work, so a test that
  only checks "the new keys exist" is half a test: the schema/3 key set has to survive too.
* C2 - the master world path used to be re-resolved per cell, inside the merge retry loop. That
  is a live wrong-world-merge race (project.py's `_read` swallows exceptions and returns the
  DEFAULT save location, and `subworld_number` rewrites project.json non-atomically once per
  cell). It has to be resolved once per run and carried in the job.
* C5 - `_scan_cell_health` read the entire cell log, and a cs8 log is megabytes. The bounded read
  that replaces it must still see markers that are printed near the START of the run - a plain
  `[-6000:]` tail does not, which is the trap this pins down.
"""
from __future__ import annotations

import ast
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
from src import runreport  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

#: Every key schema/3 put in `summary`. schema/4 may ADD to this; it may not drop or rename one.
SCHEMA3_SUMMARY_KEYS = {
    "started", "ended", "elapsed_s", "total", "merged", "failed", "incomplete", "regions",
    "on_disk_mb", "workers_peak", "workers_setting", "retries", "cell_fastest_s",
    "cell_slowest_s", "cell_median_s", "cell_avg_s", "cpu_avg", "cpu_peak", "ram_peak",
    "cores", "scale", "cell_size", "buildings", "overture_failed_cells",
}
#: Every key schema/3 put on a cell row.
SCHEMA3_CELL_KEYS = {
    "cell", "status", "worker", "queued", "started", "ended", "duration_s", "attempts", "reason",
}
TIMER_KEYS = ("merge_s", "prune_s", "health_s", "meta_s")


def _build(timing: dict, *, started: float = 1000.0, ended: float = 1060.0,
           grid: dict | None = None) -> dict:
    return runreport.build_report(
        world_name="W", meld_version="9.9.9",
        run={"started": started, "ended": ended, "total": len(timing), "est_regions": 16},
        timing=timing, timeline=[], grid=grid or {}, prefetch_timings={},
        settings={"scale": 1.0, "job_size_regions": 4, "max_workers": 4,
                  "min_threads_per_worker": 2, "phase2_timers": True},
        actual_mb=None, max_workers=4, machine={"cores": 8})


def _cell(dur: float, timers: dict | None = None, status: str = "merged") -> dict:
    t = {"status": status, "worker": 0, "queued": 1000.0, "started": 1000.0, "ended": 1000.0 + dur,
         "duration": dur, "attempts": 1}
    if timers is not None:
        t["timers"] = timers
    return t


# ── I1: the four timers, per cell and summed ───────────────────────────────────────────────

def test_timers_reach_the_report_per_cell_and_summed():
    rep = _build({
        "0,0,4": _cell(30.0, {"merge_s": 0.05, "prune_s": 0.02, "health_s": 0.01, "meta_s": 0.004}),
        "1,0,4": _cell(30.0, {"merge_s": 0.15, "prune_s": 0.03, "health_s": 0.02, "meta_s": 0.006}),
    })
    by_cell = {c["cell"]: c["timers"] for c in rep["cells"]}
    assert by_cell["0,0,4"] == {"merge_s": 0.05, "prune_s": 0.02, "health_s": 0.01, "meta_s": 0.004}
    assert rep["summary"]["timers"] == {"merge_s": 0.2, "prune_s": 0.05,
                                        "health_s": 0.03, "meta_s": 0.01}


def test_every_cell_row_carries_all_four_timer_keys():
    """Absent-safe by construction: a consumer summing cells[].timers must never hit a KeyError,
    whether the cell ran, never ran, or came from a run recorded before the timers existed."""
    rep = _build({"0,0,4": _cell(30.0), "1,0,4": _cell(30.0, {"merge_s": 0.5})},
                 grid={"9,9,4": "planned"})
    assert len(rep["cells"]) == 3
    for c in rep["cells"]:
        assert set(c["timers"]) == set(TIMER_KEYS), c["cell"]
        assert all(isinstance(v, float) for v in c["timers"].values())
    # A run with no timers at all sums to zeros, not to None and not to a missing block.
    assert _build({"0,0,4": _cell(30.0)})["summary"]["timers"] == {k: 0.0 for k in TIMER_KEYS}


def test_timer_block_survives_junk():
    rep = _build({"0,0,4": _cell(30.0, {"merge_s": "x", "prune_s": None, "health_s": 3})})
    assert rep["cells"][0]["timers"] == {"merge_s": 0.0, "prune_s": 0.0,
                                         "health_s": 3.0, "meta_s": 0.0}


def test_timing_timers_records_last_attempt_only():
    server._timing_reset()
    try:
        server._timing_timers("0,0,4", {"merge_s": 9.0, "prune_s": 1.0})
        server._timing_timers("0,0,4", {"merge_s": 0.5, "prune_s": 0.25,
                                        "health_s": 0.1, "meta_s": 0.05})
        with server._RUN_TIMING_LOCK:
            got = dict(server._CELL_TIMING["0,0,4"]["timers"])
    finally:
        server._timing_reset()
    assert got == {"merge_s": 0.5, "prune_s": 0.25, "health_s": 0.1, "meta_s": 0.05}


def test_timer_log_line_is_gated_but_the_report_half_is_not():
    """The log line is noise for someone not benchmarking, so it is gated on phase2_timers; the
    report fields are unconditional, because N6 has to be harvestable from any run."""
    src = REPO.joinpath("server.py").read_text(encoding="utf-8")
    fn = _func_source(src, "_runner")
    i_record = fn.index("_timing_timers(cell_key, _timers)")
    i_gate = fn.index('settings.get("phase2_timers"')
    i_log = fn.index("[Timers]")
    assert i_record < i_gate < i_log, "the unconditional record must precede the gated log line"
    # ...and the gate must not also be wrapping the record.
    assert "phase2_timers" not in fn[:i_record]


def _func_source(src: str, name: str) -> str:
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"{name} not found in server.py")


# ── H2: schema/4 is additive ───────────────────────────────────────────────────────────────

def test_schema_is_4():
    assert runreport.SCHEMA == "meld-run-report/4"
    assert _build({"0,0,4": _cell(30.0)})["schema"] == "meld-run-report/4"


def test_schema3_reader_still_works():
    rep = _build({"0,0,4": _cell(30.0, {"merge_s": 0.1})})
    assert SCHEMA3_SUMMARY_KEYS <= set(rep["summary"]), \
        SCHEMA3_SUMMARY_KEYS - set(rep["summary"])
    assert SCHEMA3_CELL_KEYS <= set(rep["cells"][0]), \
        SCHEMA3_CELL_KEYS - set(rep["cells"][0])
    for key in ("machine", "config", "prefetch", "cells", "timeline", "world", "meld_version",
                "generated_at"):
        assert key in rep, key
    # The only additions at the top of summary are the two schema/4 fields.
    assert set(rep["summary"]) - SCHEMA3_SUMMARY_KEYS == {"cells_per_min", "timers"}


def test_cells_per_min_counts_merged_cells_over_elapsed():
    # 4 merged + 1 failed in 60 s => 4 cells/min, not 5.
    timing = {f"{i},0,4": _cell(30.0) for i in range(4)}
    timing["9,0,4"] = _cell(5.0, status="failed")
    rep = _build(timing, started=1000.0, ended=1060.0)
    assert rep["summary"]["merged"] == 4
    assert rep["summary"]["cells_per_min"] == 4.0


def test_cells_per_min_is_a_float_on_a_zero_length_run():
    rep = _build({}, started=1000.0, ended=1000.0)
    assert rep["summary"]["cells_per_min"] == 0.0
    assert isinstance(rep["summary"]["cells_per_min"], float)


def test_html_renders_with_the_new_fields():
    rep = _build({"0,0,4": _cell(30.0, {"merge_s": 0.5, "prune_s": 0.25,
                                        "health_s": 0.1, "meta_s": 0.05})})
    html = runreport.render_html(rep)
    assert "cells/min" in html
    assert "Post-arnis tail" in html
    # A run that recorded no timers must not grow a row of zeros.
    assert "Post-arnis tail" not in runreport.render_html(_build({"0,0,4": _cell(30.0)}))


def test_phase2_settings_are_captured_in_the_report_config():
    rep = runreport.build_report(
        world_name="W", meld_version="9", run={"started": 1.0, "ended": 2.0},
        timing={}, timeline=[], grid={}, prefetch_timings={},
        settings={"canonical_regions": True, "parse_fast_json": True, "phase2_timers": False},
        actual_mb=None, max_workers=4)
    assert rep["config"]["canonical_regions"] is True
    assert rep["config"]["parse_fast_json"] is True
    assert rep["config"]["phase2_timers"] is False


# ── C2: the master world path is a frozen run invariant ────────────────────────────────────

class _StubProject:
    def __init__(self, root: Path):
        self.root = root
        self.cells_dir = root / "cells"
        self.statuses: dict[str, str] = {}
        self._settings = {"scale": 1.0, "job_size_regions": 4, "prune_cell_after_merge": True}

    def settings(self):
        return dict(self._settings)

    def origin(self):
        return {"lat": 44.0, "lon": 26.0}

    def elevation(self):
        return {"seed": 1}

    def load(self):
        return {"name": "Meld World"}

    def set_cell_status(self, ck, status):
        self.statuses[ck] = status


@pytest.fixture()
def submit_harness(tmp_path, monkeypatch):
    """Run _submit_cells against stubs and hand back the jobs it queued."""
    jobs: list[dict] = []
    resolved: list[Path] = []

    def fake_master(create: bool = True) -> Path:
        p = tmp_path / "worlds" / "World A"
        resolved.append(p)
        return p

    pool = types.SimpleNamespace(submit=jobs.append)
    monkeypatch.setattr(server, "master_world_path", fake_master)
    monkeypatch.setattr(server, "POOL", pool)
    monkeypatch.setattr(server, "PROJECT", _StubProject(tmp_path))
    monkeypatch.setattr(server, "power", types.SimpleNamespace(acquire=lambda: None))
    monkeypatch.setattr(server, "_reset_export_status", lambda: None)
    server._timing_reset()
    yield jobs, resolved
    server._timing_reset()


def test_master_is_resolved_once_per_run_and_frozen_into_every_job(submit_harness):
    jobs, resolved = submit_harness
    cells = [{"cell_key": f"{i},0,4", "bbox": [0, 0, 1, 1]} for i in range(9)]
    server._submit_cells(cells, settings={"scale": 1.0}, origin={"lat": 44.0, "lon": 26.0})
    assert len(jobs) == 9
    # ONE resolution for the whole run - not one per cell, and not one per merge attempt.
    assert len(resolved) == 1
    frozen = {j["master"] for j in jobs}
    assert frozen == {str(resolved[0])}


def test_a_project_switch_mid_run_cannot_redirect_queued_cells(submit_harness, tmp_path):
    """The race C2 closes: cells already queued keep merging into the world the run started in."""
    jobs, _ = submit_harness
    server._submit_cells([{"cell_key": "0,0,4", "bbox": [0, 0, 1, 1]}],
                         settings={"scale": 1.0}, origin={"lat": 44.0, "lon": 26.0})
    before = jobs[0]["master"]
    # Somebody switches project (or project.json is read mid-rewrite and defaults).
    server.master_world_path = lambda create=True: tmp_path / "OTHER"
    assert jobs[0]["master"] == before
    assert "World A" in before


def test_runner_reads_the_frozen_master_and_not_the_live_one():
    """Structural, because _runner needs a live arnis to execute: the point of C2 is that
    master_world_path() is no longer called inside the merge retry loop."""
    src = REPO.joinpath("server.py").read_text(encoding="utf-8")
    fn = _func_source(src, "_runner")
    assert 'master = job.get("master") or str(master_world_path())' in fn
    calls = [n for n in ast.walk(ast.parse(fn))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "master_world_path"]
    assert len(calls) == 1, "one fallback resolution only, at the top"
    i_master = fn.index('master = job.get("master")')
    i_merge = fn.index("merge_cell_into_master(")
    i_loop = fn.index("for _attempt in range(3):")
    assert i_master < i_loop < i_merge, "must be resolved before the retry loop, not inside it"


def test_submit_cells_freezes_master_next_to_the_other_world_invariants(submit_harness):
    jobs, _ = submit_harness
    server._submit_cells([{"cell_key": "0,0,4", "bbox": [0, 0, 1, 1]}],
                         settings={"scale": 1.0}, origin={"lat": 44.0, "lon": 26.0})
    job = jobs[0]
    for k in ("settings", "origin", "elevation", "world_name", "master"):
        assert k in job, k


# ── C5: the cell-log read is bounded but not truncated ─────────────────────────────────────

def test_log_markers_finds_a_marker_a_tail_read_would_miss(tmp_path):
    """The exact regression a naive `[-6000:]` would introduce: every health marker is printed in
    the elevation / land-cover phase near the start of the run, and the placement + save output
    that follows it is what makes a cs8 log megabytes."""
    p = tmp_path / "cell-0_0_8.log"
    p.write_text("Fetching ESA tile\nFailed to read ESA tile\n" + ("noise\n" * 200_000),
                 encoding="utf-8")
    assert p.stat().st_size > 1_000_000
    assert "Failed to read ESA tile" in server._log_markers(p)
    assert "Failed to read ESA tile" not in p.read_text(encoding="utf-8")[-6000:]


def test_log_markers_spans_a_chunk_boundary(tmp_path):
    p = tmp_path / "cell.log"
    marker = "Failed to read ESA tile"
    for split in (1, len(marker) // 2, len(marker) - 1):
        pad = 64 - split          # tiny chunk size, so the marker straddles a read boundary
        p.write_text("x" * pad + marker + "y" * 500, encoding="utf-8")
        assert marker in server._log_markers(p, (marker,), chunk=64), split


def test_log_markers_on_a_missing_file_is_empty_not_an_error(tmp_path):
    assert server._log_markers(tmp_path / "nope.log") == set()


def test_scan_cell_health_flags_and_clears(tmp_path, monkeypatch):
    # _save_cell_health persists to PROJECT.root; point it at tmp_path so the test cannot write
    # into the developer's real project folder.
    monkeypatch.setattr(server, "PROJECT", types.SimpleNamespace(root=tmp_path))
    monkeypatch.setattr(server, "_CELL_HEALTH", {})
    # `out` is the cell OUTPUT dir (<project>/cells/<tag>), so the log lives two levels up.
    out = tmp_path / "cells" / "0_0_4"
    (tmp_path / "logs").mkdir(parents=True)
    out.mkdir(parents=True)
    log = tmp_path / "logs" / "cell-0_0_4.log"
    # Marker up front, a megabyte of ordinary output after it - exactly the real shape.
    log.write_text("elevation tile is too small; Re-downloading\n" + ("place\n" * 200_000),
                   encoding="utf-8")
    server._scan_cell_health("0,0,4", str(out))
    with server._CELL_HEALTH_LOCK:
        entry = dict(server._CELL_HEALTH.get("0,0,4") or {})
    assert entry.get("suspect") is True
    assert entry.get("reasons") == ["terrain-tile-retry"]

    log.write_text("all fine\n", encoding="utf-8")
    server._scan_cell_health("0,0,4", str(out))
    with server._CELL_HEALTH_LOCK:
        assert "0,0,4" not in server._CELL_HEALTH


# ── M1 (server half): the three switches are coerced and never travel with a world ─────────

def test_phase2_settings_never_travel_in_world_meta():
    for k in ("canonical_regions", "parse_fast_json", "phase2_timers"):
        assert k in server._META_SKIP_SETTINGS, k


@pytest.mark.parametrize("sent,want", [
    (True, True), (False, False), (1, True), (0, False),
    ("true", True), ("false", False), ("off", False), ("", False), ("yes", True),
])
def test_api_settings_coerces_the_phase2_switches(monkeypatch, sent, want):
    """A kill switch stored as the string "false" is truthy, and would arm the very optimisation
    it exists to disable."""
    captured: dict = {}

    class _P:
        def settings(self):
            return {}

        def update_settings(self, patch):
            captured.update(patch)
            return {"max_workers": 4, **patch}

        def elevation(self):
            return {"seed": 1}

    monkeypatch.setattr(server, "PROJECT", _P())
    monkeypatch.setattr(server, "POOL", types.SimpleNamespace(
        admit_cb=None, set_max_workers=lambda n: None,
        stagger_seconds=0.0, stagger_adaptive=True))
    client = server.app.test_client()
    body = {k: sent for k in ("canonical_regions", "parse_fast_json", "phase2_timers")}
    resp = client.post("/api/settings", json=body)
    assert resp.status_code == 200
    for k in body:
        assert captured[k] is want, (k, sent)


def test_api_settings_leaves_the_phase2_switches_alone_when_not_sent(monkeypatch):
    captured: dict = {}

    class _P:
        def settings(self):
            return {}

        def update_settings(self, patch):
            captured.update(patch)
            return {"max_workers": 4}

        def elevation(self):
            return {"seed": 1}

    monkeypatch.setattr(server, "PROJECT", _P())
    monkeypatch.setattr(server, "POOL", types.SimpleNamespace(
        admit_cb=None, set_max_workers=lambda n: None,
        stagger_seconds=0.0, stagger_adaptive=True))
    server.app.test_client().post("/api/settings", json={"map_item": True})
    assert "canonical_regions" not in captured
    assert "parse_fast_json" not in captured
    assert "phase2_timers" not in captured
