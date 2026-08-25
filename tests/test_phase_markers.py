"""Arnis stdout protocol v1: parsing, consumption, and the stats it replaces.

The generator prints machine lines only when ARNIS_PHASE_MARKERS=1 is set:

    [meld] v=1 phase=<name> t=<ms_since_process_start>
    [meld] v=1 phase=done wall_s=<f.3> cpu_s=<f.3> peak_mb=<f.1> gpu_ms=<u64>

Two things must hold. They must never reach the user-visible log or parse_progress()'s
keyword scraping (a line containing "saving" or "done" would jump the progress bar), and
the `done` line - measured inside the process at exit - must beat the psutil sampler, which
polls every 0.5 s and therefore always undercounts the tail of a run.

No subprocess is started anywhere here: run_arnis is driven with a fake Popen whose stdout
is a list of synthetic lines.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import arnis_cmd  # noqa: E402


# --- pure parser ---------------------------------------------------------------------

def test_phase_line_parses_name_and_time():
    assert arnis_cmd.parse_phase_marker("[meld] v=1 phase=elevation t=1234") == {
        "phase": "elevation", "t_ms": 1234}


def test_done_line_parses_every_field():
    m = arnis_cmd.parse_phase_marker(
        "[meld] v=1 phase=done wall_s=41.625 cpu_s=310.500 peak_mb=1873.4 gpu_ms=902")
    assert m == {"phase": "done", "wall_s": 41.625, "cpu_s": 310.5,
                 "peak_mb": 1873.4, "gpu_ms": 902}


def test_done_line_omits_fields_it_cannot_measure():
    """A generator with no GPU and no RSS reading just leaves the keys out."""
    m = arnis_cmd.parse_phase_marker("[meld] v=1 phase=done wall_s=9.0 cpu_s=17.0")
    assert m == {"phase": "done", "wall_s": 9.0, "cpu_s": 17.0}
    assert "peak_mb" not in m and "gpu_ms" not in m


def test_unparseable_numbers_are_dropped_not_fatal():
    m = arnis_cmd.parse_phase_marker(
        "[meld] v=1 phase=done wall_s=nope cpu_s=2.0 peak_mb=NaN gpu_ms=x")
    assert m == {"phase": "done", "cpu_s": 2.0}


def test_unknown_tokens_are_ignored_so_the_protocol_can_grow():
    assert arnis_cmd.parse_phase_marker("[meld] v=1 phase=save t=7 shards=12 io=nvme") == {
        "phase": "save", "t_ms": 7}


def test_phase_line_without_t_is_still_a_phase():
    assert arnis_cmd.parse_phase_marker("[meld] v=1 phase=merge") == {
        "phase": "merge", "t_ms": 0}


@pytest.mark.parametrize("line", [
    "Fetching data...",
    "[meld] arnis exited with code 101 after 12 line(s) of output",
    "> Generating 4/9 done",
    "",
])
def test_ordinary_output_is_not_a_marker(line):
    assert arnis_cmd.is_marker_line(line) is False
    assert arnis_cmd.parse_phase_marker(line) is None


def test_future_protocol_versions_are_swallowed_but_not_interpreted():
    """A v=2 generator against today's Meld: consumed (never logged), never misread."""
    line = "[meld] v=2 phase=whatever payload=..."
    assert arnis_cmd.is_marker_line(line) is True
    assert arnis_cmd.parse_phase_marker(line) is None


def test_marker_lines_would_have_moved_the_progress_bar():
    """Why consumption matters: these strings hit parse_progress()'s keywords."""
    assert arnis_cmd.parse_progress("[meld] v=1 phase=ground t=880", 0) == 35
    assert arnis_cmd.parse_progress("[meld] v=1 phase=done wall_s=1 cpu_s=1", 0) == 100


# --- run_arnis wiring (fake Popen, no subprocess) ------------------------------------

class _FakeProc:
    def __init__(self, lines, returncode=0):
        self.stdout = iter(lines)
        self.returncode = returncode
        self.pid = -1

    def wait(self):
        return self.returncode

    def kill(self):
        pass


def _run(monkeypatch, lines, *, returncode=0, on_line=None, on_stats=None, on_phase=None):
    monkeypatch.setattr(arnis_cmd.subprocess, "Popen",
                        lambda *a, **k: _FakeProc(lines, returncode))
    return arnis_cmd.run_arnis(["arnis.exe"], cwd=".", on_line=on_line,
                               on_stats=on_stats, on_phase=on_phase)


LINES = [
    "Fetching data...\n",
    "[meld] v=1 phase=fetch t=12\n",
    "[meld] v=1 phase=ground t=880\n",
    "Generating terrain 3/9\n",
    "[meld] v=1 phase=done wall_s=41.625 cpu_s=310.500 peak_mb=1873.4 gpu_ms=902\n",
]


def test_markers_never_reach_on_line(monkeypatch):
    seen, phases = [], []
    ok = _run(monkeypatch, LINES, on_line=seen.append,
              on_phase=lambda name, t: phases.append((name, t)))
    assert ok is True
    assert seen == ["Fetching data...", "Generating terrain 3/9"]
    assert phases == [("fetch", 12), ("ground", 880)]


def test_done_line_is_consumed_and_not_delivered_as_a_phase(monkeypatch):
    phases = []
    _run(monkeypatch, LINES, on_phase=lambda name, t: phases.append(name), on_stats=lambda *a, **k: None)
    assert "done" not in phases


def test_arnis_numbers_win_over_the_sampler(monkeypatch):
    got = {}

    def on_stats(cpu_seconds, wall_seconds, peak_rss_mb=None, source="sampler", **extra):
        got.update(cpu=cpu_seconds, wall=wall_seconds, peak=peak_rss_mb,
                   source=source, extra=extra)

    _run(monkeypatch, LINES, on_stats=on_stats)
    assert got["cpu"] == 310.5
    assert got["wall"] == 41.625
    assert got["peak"] == 1873.4
    assert got["source"] == "arnis"
    assert got["extra"] == {"gpu_ms": 902}


def test_no_done_line_falls_back_to_the_sampler(monkeypatch):
    """Old binary, or markers disabled: the psutil path still reports, flagged as such."""
    got = {}

    def on_stats(cpu_seconds, wall_seconds, peak_rss_mb=None, source="?"):
        got.update(cpu=cpu_seconds, wall=wall_seconds, peak=peak_rss_mb, source=source)

    _run(monkeypatch, ["Fetching data...\n", "Done! 1/1\n"], on_stats=on_stats)
    assert got["source"] == "sampler"
    assert got["cpu"] >= 0.0          # the fake pid samples nothing; 0.0 is the floor
    assert got["wall"] >= 0.0
    assert got["peak"] is None        # nothing to sample from a pid that does not exist


def test_legacy_two_argument_callback_still_works(monkeypatch):
    """server.py's current `def on_stats(cpu_seconds, wall_seconds)` must not break."""
    calls = []

    def on_stats(cpu_seconds, wall_seconds):
        calls.append((cpu_seconds, wall_seconds))

    _run(monkeypatch, LINES, on_stats=on_stats)
    assert calls == [(310.5, 41.625)]


def test_kwargs_callback_receives_every_extra(monkeypatch):
    seen = {}

    def on_stats(cpu, wall, **kw):
        seen.update(kw)

    _run(monkeypatch, LINES, on_stats=on_stats)
    assert seen == {"peak_rss_mb": 1873.4, "source": "arnis", "gpu_ms": 902}


def test_a_raising_callback_never_fails_the_cell(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("telemetry blew up")

    assert _run(monkeypatch, LINES, on_stats=boom, on_phase=boom) is True


def test_running_without_callbacks_is_fine(monkeypatch):
    assert _run(monkeypatch, LINES) is True


def test_marker_lines_still_count_as_output_in_the_exit_message(monkeypatch):
    """The failure line reports lines of OUTPUT; markers are output, silence is the signal."""
    seen = []
    ok = _run(monkeypatch, LINES, returncode=101, on_line=seen.append)
    assert ok is False
    assert seen[-1] == "[meld] arnis exited with code 101 after 5 line(s) of output"


# --- the psutil fallback, exercised with a fake psutil --------------------------------
#
# The env var is now set UNCONDITIONALLY by the caller (there is no --help probe: clap
# prints neither ARNIS_PHASE_MARKERS nor a --phase-markers, so any such grep answers
# False forever). An old binary ignores the unknown var and prints no `done` line, so
# the fallback below is what a pre-protocol generator actually gets - it has to produce
# a real peak_rss_mb, not just a source label.

@pytest.fixture(autouse=True)
def _clear_help_cache():
    arnis_cmd._HELP_CACHE.clear()
    yield
    arnis_cmd._HELP_CACHE.clear()


def test_the_probe_is_gone():
    """D1: no capability probe may come back. Setting the env var on an old binary is
    harmless, and a --help grep for an ENV VAR is always False, so the probe could only
    ever disable the protocol it was meant to detect."""
    assert not hasattr(arnis_cmd, "supports_phase_markers")
    assert not hasattr(arnis_cmd, "_PHASE_MARKER_TOKENS")


def test_forget_probe_still_reopens_the_flag_question(monkeypatch):
    """forget_probe() survives the probe deletion: arnis_supports() gates --overture and
    friends, and an in-place generator update must not keep answering from the old binary."""
    arnis_cmd._HELP_CACHE["arnis.exe"] = "Usage: arnis ...\n"
    assert arnis_cmd.arnis_supports("arnis.exe", "--overture") is False
    arnis_cmd.forget_probe("arnis.exe")
    monkeypatch.setattr(arnis_cmd.subprocess, "run",
                        lambda *a, **k: type("R", (), {"stdout": "--overture",
                                                       "stderr": ""})())
    assert arnis_cmd.arnis_supports("arnis.exe", "--overture") is True


class _FakePsutil:
    """Just enough psutil for the sampler thread. `sampled` fires on the first read so the
    test can hold stdout open until at least one poll has landed (the poller waits 0.5 s
    before its first read, and a fake proc's stdout would otherwise be drained instantly)."""

    def __init__(self, sampled):
        self._sampled = sampled

        class _Times:
            user, system = 3.0, 1.0

        class _Mem:
            rss = 512 * 1024 * 1024        # no peak_wset: the non-Windows running-max path

        outer = self

        class Process:
            def __init__(self, pid):
                self.pid = pid

            def cpu_times(self):
                outer._sampled.set()
                return _Times()

            def memory_info(self):
                return _Mem()

        self.Process = Process


def test_sampler_fallback_populates_peak_rss_when_no_done_line(monkeypatch):
    """No `phase=done` (old binary / markers ignored): the psutil poller must still report
    a real peak_rss_mb, not None, and flag itself as source="sampler"."""
    import threading as _t
    sampled = _t.Event()
    monkeypatch.setitem(sys.modules, "psutil", _FakePsutil(sampled))

    def _lines():
        yield "Fetching data...\n"
        sampled.wait(5.0)          # hold stdout open until one poll has landed
        yield "Done! 1/1\n"

    got = {}

    def on_stats(cpu_seconds, wall_seconds, peak_rss_mb=None, source="?", **kw):
        got.update(cpu=cpu_seconds, wall=wall_seconds, peak=peak_rss_mb,
                   source=source, extra=kw)

    assert _run(monkeypatch, _lines(), on_stats=on_stats) is True
    assert sampled.is_set(), "the sampler never polled; the assertions below are vacuous"
    assert got["source"] == "sampler"
    assert got["peak"] == pytest.approx(512.0)
    assert got["cpu"] == pytest.approx(4.0)       # user 3.0 + system 1.0
    assert got["wall"] > 0.0
    assert got["extra"] == {}                     # no gpu_ms without a done line


def test_done_line_still_beats_a_working_sampler(monkeypatch):
    """Both sources available: the in-process numbers win, sampler's 512 MB is discarded."""
    import threading as _t
    sampled = _t.Event()
    monkeypatch.setitem(sys.modules, "psutil", _FakePsutil(sampled))

    def _lines():
        yield "Fetching data...\n"
        sampled.wait(5.0)
        yield ("[meld] v=1 phase=done wall_s=41.625 cpu_s=310.500 "
               "peak_mb=1873.4 gpu_ms=902\n")

    got = {}

    def on_stats(cpu_seconds, wall_seconds, peak_rss_mb=None, source="?", **kw):
        got.update(cpu=cpu_seconds, peak=peak_rss_mb, source=source)

    _run(monkeypatch, _lines(), on_stats=on_stats)
    assert got == {"cpu": 310.5, "peak": 1873.4, "source": "arnis"}


def test_a_done_line_without_peak_mb_falls_back_to_the_sampled_peak(monkeypatch):
    """Mixed case: the generator times itself but cannot read its own RSS. cpu/wall come
    from arnis, peak_rss_mb from the poller."""
    import threading as _t
    sampled = _t.Event()
    monkeypatch.setitem(sys.modules, "psutil", _FakePsutil(sampled))

    def _lines():
        yield "Fetching data...\n"
        sampled.wait(5.0)
        yield "[meld] v=1 phase=done wall_s=9.0 cpu_s=17.0\n"

    got = {}

    def on_stats(cpu_seconds, wall_seconds, peak_rss_mb=None, source="?", **kw):
        got.update(cpu=cpu_seconds, wall=wall_seconds, peak=peak_rss_mb, source=source)

    _run(monkeypatch, _lines(), on_stats=on_stats)
    assert got["source"] == "arnis" and got["cpu"] == 17.0 and got["wall"] == 9.0
    assert got["peak"] == pytest.approx(512.0)


def test_markers_are_consumed_even_with_no_on_phase_callback(monkeypatch):
    """Consumption is unconditional: with on_phase=None the marker lines must still be
    swallowed rather than falling through to on_line() and moving the progress bar."""
    seen = []
    _run(monkeypatch, LINES, on_line=seen.append, on_phase=None)
    assert seen == ["Fetching data...", "Generating terrain 3/9"]
    assert not any(arnis_cmd.is_marker_line(s) for s in seen)
