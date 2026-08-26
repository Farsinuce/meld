"""The throughput governor (src/governor.py).

The centrepiece is TestBerlinCurve: a replay of a REAL 1:1 run where the worker count
was raised live from 8 to 24 while a human watched cells/min refuse to move. Fed that
curve, the governor has to stop climbing where the measurements stop paying - in the
8..12 band - and must never walk to 24, because 24 workers delivered the same ~22
cells/min at 3x the per-cell latency and 93% RAM.

No renders, no network, no sleeping on a real clock except one deliberate 0.5 s
admission-timeout bound.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.governor import (  # noqa: E402
    CLIMB_STEP,
    ADMIT_POLL_S,
    ADMIT_TIMEOUT_S,
    Admission,
    Governor,
    GovernorSnapshot,
    bucket_key,
)

CORES = 24                    # the measured box: Core Ultra 9 275HX, 8P + 16E
CORE_BUDGET = 21              # floor(24 * 90 / 100)
FLUSH_CAP = 12                # the default flush_threads_cap


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    """MELD_GOVERNOR is the last word over settings, so a shell that happens to have
    it set must not decide these tests."""
    monkeypatch.delenv("MELD_GOVERNOR", raising=False)


def make_gov(cores: int = CORES, avail_mb: float | None = 20_000.0,
             **over) -> tuple[Governor, dict]:
    cfg = {
        "governor_mode": "auto",
        "cpu_target_pct": 90,
        "ram_headroom_mb": 2048,
        "flush_threads_cap": 12,
        "governor_max_workers": 0,
        "min_threads_per_worker": 4,
        "governor_history": {},
    }
    cfg.update(over)
    gov = Governor(cores=cores, get_settings=lambda: cfg)
    gov._available_mb_probe = lambda: avail_mb
    # Step decisions are now a wall-clock rate, so every driver needs a clock it can
    # advance. Real time would make the numbers depend on how fast the test machine is.
    gov._clock = VirtualClock()
    gov._now = gov._clock.now
    # admit() polls against a deadline, so the clock must also move when it sleeps -
    # otherwise a closed gate spins forever instead of timing out.
    gov._sleep = gov._clock.advance
    return gov, cfg


class VirtualClock:
    """A clock the drivers advance by hand, so a simulated pool delivers cells at a
    simulated rate. Deterministic: the same script always yields the same throughput."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += max(0.0, seconds)


class FakeClock:
    """Time only moves when the governor asks to sleep, so an admission wait is
    exact instead of flaky."""

    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += max(0.0, seconds)


def _cpc_at(w: int, cpc: dict[int, float] | None) -> float:
    return (cpc or {}).get(w, min(7.75, 19.2 / w))


def drive(gov: Governor, walls: dict[int, float], cells: int,
          cpc: dict[int, float] | None = None, rss_mb: float = 1500.0) -> set[int]:
    """Run `cells` completions, playing the pool: whatever the governor targets is
    what runs next. Returns every worker level that was actually visited.

    Zero pipeline depth - each cell finishes at the level it launched under - so the
    launch tag always matches and this driver isolates the state machine from the
    attribution question. drive_pipelined is the one that exercises the tag.
    """
    seen = {gov.workers}
    for _ in range(cells):
        gov.threads_for_next_cell(workers=gov.target)   # the pool applies the target
        w = gov.workers
        wall = walls[w]
        # W workers x `wall` seconds each => one completion every wall/W seconds.
        gov._clock.advance(wall / max(1, w))
        gov.on_cell_complete(wall_s=wall, cpu_s=wall * _cpc_at(w, cpc),
                             peak_rss_mb=rss_mb, gpu_s=0.0, ok=True,
                             launched_workers=w)
        seen.add(gov.workers)
    return seen


def drive_pipelined(gov: Governor, walls: dict[int, float], cells: int,
                    lag: int = 3, cpc: dict[int, float] | None = None,
                    rss_mb: float = 1500.0) -> set[int]:
    """The real pipeline: `lag` cells are in flight at all times, so the completions
    that arrive just after a resize are cells that LAUNCHED at the previous level.

    Each is reported with the level it launched under, which is the whole point of
    finding 10: without the tag those short old walls get divided by the new, higher
    worker count and every step scores as a win.
    """
    from collections import deque

    inflight: deque[int] = deque()
    seen = {gov.workers}
    for _ in range(cells):
        gov.threads_for_next_cell(workers=gov.target)
        launched = gov.workers
        seen.add(launched)
        inflight.append(launched)
        if len(inflight) > lag:
            done = inflight.popleft()
            wall = walls[done]
            gov._clock.advance(wall / max(1, gov.workers))
            gov.on_cell_complete(wall_s=wall, cpu_s=wall * _cpc_at(done, cpc),
                                 peak_rss_mb=rss_mb, gpu_s=0.0, ok=True,
                                 launched_workers=done)
        seen.add(gov.workers)
    return seen


# Measured medians at 8/12/16/20/24 (contract ground truth). 10/14/18/22 are the
# midpoints between measured neighbours; 4 and 6 are below the knee, where throughput
# still scales with workers - which is exactly the region the governor must walk up.
BERLIN_WALL_S = {
    4: 14.0, 6: 17.0,
    8: 21.1, 10: 27.5, 12: 33.9, 14: 37.75,
    16: 41.6, 18: 49.05, 20: 56.5, 22: 60.0, 24: 63.5,
}


class TestBerlinCurve:
    """Fed the real curve, stop at the knee."""

    def _converged(self):
        gov, _ = make_gov()
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        seen = drive(gov, BERLIN_WALL_S, cells=40)
        return gov, seen

    def test_stops_climbing_in_the_eight_to_twelve_band(self):
        gov, _ = self._converged()
        assert 8 <= gov.workers <= 12, (
            f"converged on {gov.workers} workers; the measured knee is 8-12")

    def test_never_walks_to_twenty_four(self):
        gov, seen = self._converged()
        assert max(seen) <= 12, f"visited {sorted(seen)} - past 12 buys nothing"
        assert 24 not in seen

    def test_ends_steady_and_stays_there(self):
        gov, _ = self._converged()
        assert gov.state == "STEADY"
        target = gov.target
        drive(gov, BERLIN_WALL_S, cells=20)
        assert gov.target == target, "a flat curve must not restart the climb"

    def test_it_did_climb_rather_than_sitting_at_the_start(self):
        # The failure mode on the other side: a governor that never leaves 4 workers
        # is not conservative, it is idle.
        _, seen = self._converged()
        assert max(seen) >= 8

    def test_throughput_is_the_metric_not_cpu_percent(self):
        # Berlin pinned cpu ~79% at every level. Reported cells/min must be the
        # measured delivery, ~21-23, at whatever level it settled on.
        gov, _ = self._converged()
        assert gov.cells_per_min is not None
        assert 18.0 <= gov.cells_per_min <= 26.0


class TestBucketKey:
    def test_scale_bands(self):
        assert bucket_key(1.0, 4) == "1:1/4"
        assert bucket_key(0.5, 8) == "1:1/8"
        assert bucket_key(0.25, 4) == "1:2..1:9/4"
        assert bucket_key(0.05, 4) == "1:10+/4"


class TestOffMode:
    def test_off_returns_the_legacy_thread_formula(self):
        gov, _ = make_gov(governor_mode="off")
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=8)
        # legacy: max(min_threads_per_worker=4, 21 // 8 = 2) = 4, flush max(2,min(6,2))=2
        assert gov.threads_for_next_cell(workers=8) == (4, 2)
        assert gov.threads_for_next_cell(workers=2) == (10, 5)

    def test_off_never_gates_or_resizes(self):
        gov, _ = make_gov(governor_mode="off")
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=8)
        assert gov.admit(worker_id=0, active=8) == "go(steady)"
        for _ in range(20):
            assert gov.on_cell_complete(wall_s=20.0, cpu_s=60.0, peak_rss_mb=1500.0,
                                        gpu_s=0.0, ok=True) is None
        assert gov.state == "OFF" and gov.target == 8
        assert gov.end_run() is None

    def test_env_forces_off_over_settings(self, monkeypatch):
        monkeypatch.setenv("MELD_GOVERNOR", "off")
        gov, _ = make_gov(governor_mode="auto")
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=8)
        assert gov.mode == "off" and gov.state == "OFF"
        assert gov.threads_for_next_cell(workers=8) == (4, 2)

    def test_legacy_worker_autoscale_migrates_only_when_mode_absent(self):
        cfg = {"worker_autoscale": True, "cpu_target_pct": 90}
        gov = Governor(cores=CORES, get_settings=lambda: cfg)
        gov._available_mb_probe = lambda: 20_000.0
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        assert gov.mode == "auto"
        cfg["governor_mode"] = "off"     # an explicit choice beats the legacy flag
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        assert gov.mode == "off"


class TestThreadClamps:
    """Inversion cases: the per-worker share is SENIOR to the occupancy estimate, and
    the floor is 1. The old floor of 4 is what put 96 rayon threads on 24 cores."""

    def _gov_with_occupancy(self, cores_per_cell: float | None):
        gov, _ = make_gov()
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        if cores_per_cell:
            for _ in range(4):
                gov._occ.record(cpu_seconds=cores_per_cell * 100.0, wall_seconds=100.0)
        return gov

    def _threads_at(self, gov: Governor, workers: int) -> tuple[int, int]:
        gov.workers = workers          # pretend the pool is already this size
        return gov.threads_for_next_cell(workers=workers)

    def test_per_worker_share_beats_a_hungry_cell(self):
        # 7.75 cores/cell * 1.25 = 10 threads wanted, but 8 workers get round(21/8) = 3.
        gov = self._gov_with_occupancy(7.75)
        assert self._threads_at(gov, 8)[0] == 3

    def test_rayon_floors_to_two_not_four_and_not_one(self):
        gov = self._gov_with_occupancy(7.75)
        assert self._threads_at(gov, 24)[0] == 2, "24 x 4 = 96 threads was the old bug"
        assert self._threads_at(gov, 22)[0] == 2
        assert self._threads_at(gov, CORE_BUDGET)[0] == 2   # one core each, still 2 threads

    def test_a_light_cell_takes_less_than_its_share(self):
        # 1:20 measured 1.02 cores/cell: ceil(1.25 * 1.02) = 2, not the 10 it could have.
        gov = self._gov_with_occupancy(1.02)
        assert self._threads_at(gov, 2)[0] == 2

    def test_without_a_measurement_the_share_is_capped_at_four(self):
        gov = self._gov_with_occupancy(None)
        assert self._threads_at(gov, 2)[0] == 4      # 21//2 = 10, capped
        assert self._threads_at(gov, 8)[0] == 2      # 21//8 = 2, under the cap
        # Floor of 2, same as the measured branch: the tracker is wiped on every resize, so
        # the wave a step is scored on always lands here. A floor of 1 measured that wave
        # single-threaded and made the knee an artifact of the governor's own grant.
        assert self._threads_at(gov, 30)[0] == 2     # 21//30 = 0, floored at 2

    def test_flush_scales_down_with_workers_and_floors_at_one(self):
        gov = self._gov_with_occupancy(2.0)
        assert self._threads_at(gov, 1)[1] == 12
        assert self._threads_at(gov, 2)[1] == 6
        assert self._threads_at(gov, 4)[1] == 3
        assert self._threads_at(gov, 8)[1] == 2
        assert self._threads_at(gov, 12)[1] == 1     # cap is where flush floors
        assert self._threads_at(gov, 24)[1] == 1
        assert self._threads_at(gov, 96)[1] == 1

    def test_flush_cap_setting_is_the_ceiling(self):
        gov, _ = make_gov(flush_threads_cap=6)
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        assert self._threads_at(gov, 1)[1] == 6
        assert self._threads_at(gov, 3)[1] == 2
        assert self._threads_at(gov, 8)[1] == 1

    def test_cpu_target_falls_back_to_ninety_not_a_hundred(self):
        gov, cfg = make_gov()
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        del cfg["cpu_target_pct"]
        assert self._threads_at(gov, 1)[0] == 4      # budget 21 -> capped at 4
        cfg["cpu_target_pct"] = 200                  # clamped to 95, never 200
        gov.workers = 1
        assert gov._threads(1)[0] == 4


class TestSmallGrid:
    # 16 x 4.15 GB + 2 GB headroom = ~68 GB, so the envelope has to be given room
    # before "skips the ramp" can be tested independently of "fits in RAM".
    ROOMY = 70_000.0

    def test_a_short_grid_skips_the_ramp_entirely(self):
        gov, _ = make_gov(avail_mb=self.ROOMY)
        gov.begin_run(total_cells=10, scale=1.0, cell_size=4, ceiling=16)
        assert gov.state == "STEADY" and gov.workers == 16
        seen = drive(gov, {16: 30.0}, cells=20)
        assert seen == {16}
        assert gov.target == 16

    def test_a_short_grid_persists_nothing(self):
        gov, _ = make_gov(avail_mb=self.ROOMY)
        gov.begin_run(total_cells=10, scale=1.0, cell_size=4, ceiling=16)
        drive(gov, {16: 30.0}, cells=10)
        assert gov.end_run() is None, "a static run is not a converged answer"

    def test_a_short_grid_still_obeys_the_ram_envelope(self):
        """Skipping calibration must not skip the RAM check.

        Measured: a 25-cell 8x8 run took the small-grid path, ran at the ceiling of
        20 with no calibration and drove RAM to 98% while delivering LESS than the
        16-worker baseline. 20 GB free at 1:1 holds (20000 - 2048) / 4150 = 4 cells.
        """
        gov, _ = make_gov(avail_mb=20_000.0)
        gov.begin_run(total_cells=25, scale=1.0, cell_size=8, ceiling=20)
        assert gov.state == "STEADY"
        assert gov.workers == 4, f"ceiling 20 should trim to 4, got {gov.workers}"
        assert gov._binding == "ram"

    def test_a_small_scale_short_grid_is_barely_trimmed(self):
        # 1:20 cells are ~1.2 GB, so the same 20 GB holds far more of them.
        gov, _ = make_gov(avail_mb=20_000.0)
        gov.begin_run(total_cells=10, scale=0.05, cell_size=4, ceiling=16)
        assert gov.workers == 14


class TestWarmStart:
    HISTORY = {"1:1/4": {"workers": 12, "threads": 2, "flush": 4,
                         "cores_per_cell": 2.4, "rss_p95_mb": 1000.0,
                         "cells_per_min": 21.2, "ts": 0.0}}

    def test_starts_at_the_remembered_count_and_goes_straight_to_steady(self):
        gov, _ = make_gov(governor_history=dict(self.HISTORY))
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        assert gov.state == "STEADY" and gov.workers == 12 and gov.target == 12

    def test_shrinks_until_the_ram_actually_holds_it(self):
        # 10 GB free, 1 GB/cell p95, 2 GB headroom: 7 workers fit (9.0 GB), 8 do not.
        gov, _ = make_gov(avail_mb=10_000.0, governor_history=dict(self.HISTORY))
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        assert gov.workers == 7

    def test_clamped_to_the_pool_ceiling(self):
        gov, _ = make_gov(governor_history=dict(self.HISTORY))
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=8)
        assert gov.workers == 8

    def test_a_different_bucket_does_not_warm_start(self):
        # 6 GB free keeps the calibrate opening well away from the remembered 12, so
        # "did not warm-start" cannot be confused with "opened where history said".
        gov, _ = make_gov(governor_history=dict(self.HISTORY), avail_mb=6_000.0)
        gov.begin_run(total_cells=400, scale=0.05, cell_size=4, ceiling=24)
        assert gov.state == "CALIBRATE", "a foreign bucket must be measured, not assumed"
        assert gov.workers != self.HISTORY["1:1/4"]["workers"]
        assert gov._steady_tp is None, "and it must not inherit the other bucket's rate"


class TestCalibrateAndConverge:
    def test_calibrate_starts_low(self):
        gov, _ = make_gov()
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        assert gov.state == "CALIBRATE" and gov.workers == 4
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=2)
        assert gov.workers == 2, "never open above the ceiling"

    def test_a_negative_step_backs_off_after_two_strikes(self):
        # A worse level must be seen twice before the pool unwinds: the first
        # Bucharest A/B settled the whole run on one bad sample.
        # Past 6 everything is worse, and the fast ramp can jump straight to 8 or
        # bisect through 10/12, so every reachable level has to be priced.
        walls = {4: 20.0, 6: 25.0, 8: 60.0, 10: 80.0, 12: 100.0, 14: 120.0, 16: 140.0}
        gov, _ = make_gov()
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        seen = drive(gov, walls, cells=40)
        assert max(seen) >= 8, "it had to try 8 to learn 8 was worse"
        assert gov.state == "STEADY"
        assert gov.target <= 8, "a losing level must not become the resting place"

    def test_a_marginal_gain_stops_the_climb(self):
        # +0.08 cells/min on a 12 cells/min pool is 0.7%, under GAIN_MIN_FRAC_SMALL,
        # so it is a strike; STOP_STRIKES of them settle the pool.
        walls = {4: 20.0, 6: 29.8, 8: 39.7, 10: 49.6, 12: 59.5}
        gov, _ = make_gov()
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        drive(gov, walls, cells=40)
        assert gov.state == "STEADY" and gov._binding == "throughput"

    def test_a_small_but_real_gain_keeps_climbing(self):
        """The regression the first Bucharest A/B exposed.

        Every +2 step buys ~0.44 cells/min - under the old absolute 0.5 floor, so the
        governor settled at 6 while 16 workers really delivered ~19% more. A relative
        threshold keeps the climb alive on a curve that is still rising.
        """
        walls = {w: w * 60.0 / (23.0 + (w - 4) * 0.44) for w in range(4, 26, 2)}
        gov, _ = make_gov()
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=16)
        seen = drive(gov, walls, cells=120)
        assert max(seen) >= 12, f"climb stalled early at {max(seen)}w"

    def test_the_gain_threshold_is_relative_with_an_absolute_floor(self):
        gov, _ = make_gov()
        # 3% under the taper, 2% above it, never below the absolute floor.
        assert gov._gain_threshold(8, 100.0) == pytest.approx(3.0)
        assert gov._gain_threshold(10, 100.0) == pytest.approx(2.0)
        assert gov._gain_threshold(8, 1.0) == pytest.approx(0.15)   # floor wins
        assert gov._gain_threshold(8, None) == pytest.approx(0.15)

    def test_the_ceiling_stops_the_climb(self):
        walls = {4: 20.0, 6: 13.0, 8: 10.0}   # still climbing when it runs out of room
        gov, _ = make_gov()
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=6)
        seen = drive(gov, walls, cells=15)
        assert seen == {4, 6} and gov._binding == "ceiling"

    def test_governor_max_workers_overrides_the_pool_ceiling(self):
        walls = {4: 20.0, 6: 13.0, 8: 10.0}
        gov, _ = make_gov(governor_max_workers=6)
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        assert gov.ceiling == 6
        seen = drive(gov, walls, cells=15)
        assert max(seen) == 6

    def test_ram_headroom_stops_the_climb(self):
        # 5 GB free, 1.5 GB/cell measured, 2 GB headroom. The opening is RAM-bounded too
        # now, so price every level the climb could reach and assert on the binding.
        gov, _ = make_gov(avail_mb=5_000.0)
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        opened = gov.workers
        drive(gov, {w: 20.0 for w in range(1, 26)}, cells=30)
        assert gov.state == "STEADY" and gov._binding == "ram"
        assert gov.target <= opened + CLIMB_STEP, "RAM must cap the climb almost at once"

    def test_the_contention_signature_stops_the_climb(self):
        # Throughput still rising, but each cell's measured occupancy collapsed 5 -> 1
        # core: the new workers are waiting on each other, not working.
        walls = {4: 20.0, 6: 16.0}
        gov, _ = make_gov()
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        drive(gov, walls, cells=8, cpc={4: 5.0, 6: 1.0})
        assert gov.state == "STEADY" and gov._binding == "contention"
        assert gov.target == 6

    def test_a_step_is_not_judged_on_fewer_than_three_cells(self):
        gov, _ = make_gov()
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        for _ in range(2):
            assert gov.on_cell_complete(wall_s=20.0, cpu_s=40.0, peak_rss_mb=1500.0,
                                        gpu_s=0.0, ok=True) is None
        assert gov.state == "CALIBRATE" and gov.target == 4


class TestSteadyAndRecal:
    HISTORY = {"1:1/4": {"workers": 8, "rss_p95_mb": 1000.0, "cells_per_min": 22.7}}

    def test_a_sustained_throughput_collapse_reopens_the_ramp(self):
        gov, _ = make_gov(governor_history=dict(self.HISTORY))
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        assert gov.state == "STEADY"
        drive(gov, {8: 21.1}, cells=6)          # matches the remembered 22.7 cells/min
        assert gov.state == "STEADY"
        for _ in range(20):                     # something else moved onto the box
            if gov.state != "STEADY":
                break
            gov.on_cell_complete(wall_s=60.0, cpu_s=120.0, peak_rss_mb=1500.0,
                                 gpu_s=0.0, ok=True)
        assert gov.state == "RECAL"

    def test_one_slow_cell_does_not_reopen_anything(self):
        gov, _ = make_gov(governor_history=dict(self.HISTORY))
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        drive(gov, {8: 21.1}, cells=6)
        gov.on_cell_complete(wall_s=200.0, cpu_s=400.0, peak_rss_mb=1500.0,
                             gpu_s=0.0, ok=True)
        assert gov.state == "STEADY"

    def test_recalibrate_walks_the_curve_again(self):
        gov, _ = make_gov()
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        drive(gov, BERLIN_WALL_S, cells=40)
        settled = gov.workers
        gov.recalibrate()
        assert gov.state == "RECAL"
        # Enough completions for the post-RECAL window to span MIN_RATE_SPAN_S; three
        # samples inside one burst are an interval, not a rate.
        drive(gov, BERLIN_WALL_S, cells=12)
        assert gov.state == "CONVERGE" and gov.target == settled + 2


class TestFreeze:
    def test_frozen_changes_nothing(self):
        gov, _ = make_gov()
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        drive(gov, BERLIN_WALL_S, cells=4)      # mid-ramp
        gov.freeze()
        target = gov.target
        for _ in range(20):
            assert gov.on_cell_complete(wall_s=63.5, cpu_s=63.5, peak_rss_mb=9000.0,
                                        gpu_s=0.0, ok=True) is None
        assert gov.state == "FROZEN" and gov.target == target

    def test_frozen_never_holds_a_worker(self):
        gov, _ = make_gov(avail_mb=10.0)        # RAM the gate would refuse
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        gov.freeze()
        assert gov.admit(worker_id=3, active=4) == "go(steady)"


class TestAdmission:
    def test_steady_with_ram_to_spare_is_pass_through(self):
        gov, _ = make_gov()
        gov.begin_run(total_cells=10, scale=1.0, cell_size=4, ceiling=8)
        assert gov.admit(worker_id=2, active=4) == "go(steady)"

    def test_the_first_worker_is_never_held(self):
        gov, _ = make_gov(avail_mb=10.0)
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        assert gov.admit(worker_id=0, active=0, timeout_s=99.0) == "go"

    def test_it_waits_then_admits_when_ram_frees_up(self):
        clock = FakeClock()
        gov, _ = make_gov(avail_mb=100.0)
        gov._now, gov._sleep = clock.now, clock.sleep
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        free = {"mb": 100.0}
        calls = {"n": 0}

        def probe():
            calls["n"] += 1
            if calls["n"] > 3:
                free["mb"] = 20_000.0
            return free["mb"]

        gov._available_mb_probe = probe
        assert gov.admit(worker_id=1, active=2, timeout_s=12.0) == "go"
        assert 0 < clock.t <= 12.0

    def test_it_never_waits_longer_than_the_timeout(self):
        clock = FakeClock()
        gov, _ = make_gov(avail_mb=100.0)       # never enough, whatever it does
        gov._now, gov._sleep = clock.now, clock.sleep
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        assert gov.admit(worker_id=1, active=2, timeout_s=12.0) == "go(timeout)"
        assert clock.t == pytest.approx(12.0, abs=ADMIT_POLL_S)

    def test_a_zero_timeout_returns_at_once(self):
        gov, _ = make_gov(avail_mb=100.0)
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        assert gov.admit(worker_id=1, active=2, timeout_s=0.0) == "go(timeout)"

    def test_a_real_wait_is_bounded_by_the_real_clock(self):
        import time as _time

        gov, _ = make_gov(avail_mb=100.0)
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        started = _time.monotonic()
        assert gov.admit(worker_id=1, active=2, timeout_s=0.5) == "go(timeout)"
        assert _time.monotonic() - started < 3.0

    def test_without_psutil_it_degrades_to_go(self):
        gov, _ = make_gov(avail_mb=None)
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        gov._sleep = lambda s: pytest.fail("must not sleep with no probe to wait on")
        assert gov.admit(worker_id=1, active=2) == "go"


class TestSamples:
    def test_failed_cells_and_stubs_are_ignored(self):
        gov, _ = make_gov()
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        gov.on_cell_complete(wall_s=30.0, cpu_s=60.0, peak_rss_mb=1500.0,
                             gpu_s=0.0, ok=False)
        gov.on_cell_complete(wall_s=1.2, cpu_s=1.0, peak_rss_mb=1500.0,
                             gpu_s=0.0, ok=True)
        assert gov.snapshot().samples == 0
        assert gov.state == "CALIBRATE"

    def test_a_pool_resize_re_anchors_the_windows(self):
        gov, _ = make_gov()
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        drive(gov, {4: 20.0, 6: 13.0}, cells=3)
        assert gov.target == 6
        gov.threads_for_next_cell(workers=6)
        assert gov.cells_per_min is None, "old-level samples do not describe 6 workers"


class TestReporting:
    def test_snapshot_carries_the_whole_contract(self):
        gov, _ = make_gov()
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        drive(gov, BERLIN_WALL_S, cells=40)
        snap = gov.snapshot()
        assert isinstance(snap, GovernorSnapshot)
        for field in ("mode", "state", "workers", "target", "threads", "flush",
                      "cores_per_cell", "rss_p95_mb", "cells_per_min", "binding",
                      "samples", "note"):
            assert hasattr(snap, field)
        assert snap.mode == "auto" and snap.state == "STEADY"
        assert snap.threads >= 1 and snap.flush >= 1
        assert snap.rss_p95_mb == 1500.0 and snap.samples == 40

    def test_end_run_returns_a_persistable_entry(self):
        gov, _ = make_gov()
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        drive(gov, BERLIN_WALL_S, cells=40)
        key, entry = gov.end_run()
        assert key == "1:1/4"
        assert set(entry) == {"workers", "threads", "flush", "cores_per_cell",
                              "rss_p95_mb", "cells_per_min", "ts"}
        assert 8 <= entry["workers"] <= 12
        assert entry["cells_per_min"] > 0 and entry["rss_p95_mb"] == 1500.0

    def test_a_run_with_no_evidence_persists_nothing(self):
        gov, _ = make_gov()
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        gov.on_cell_complete(wall_s=20.0, cpu_s=40.0, peak_rss_mb=1500.0,
                             gpu_s=0.0, ok=True)
        assert gov.end_run() is None

    def test_advise_mode_measures_but_never_resizes(self):
        gov, _ = make_gov(governor_mode="advise")
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=16)
        seen = drive(gov, {16: 41.6}, cells=20)
        assert seen == {16}, "advise mode holds the user's worker count"
        assert gov.snapshot().samples == 20
        assert gov.end_run() is None
        # The envelope's opinion is published instead of applied.
        assert gov.advice()["workers"] is not None

    def test_advice_is_empty_until_there_is_evidence(self):
        gov, _ = make_gov()
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        assert gov.advice()["workers"] is None


# ---------------------------------------------------------------------------
# Regression tests, one per defect from the adversarial review. Each is named for
# the finding it closes so a re-break is traceable to the decision that fixed it.
# ---------------------------------------------------------------------------

class TestFinding03RayonUpper:
    """rayon_upper = max(2, round(core_budget / workers)).

    `max(1, core_budget // workers)` collapsed to 1 at 11 workers (21 // 11 == 1),
    which is INSIDE the 8..12 band the governor converges on: the measured auto
    pairs were 8w(2,6) 11w(1,6) 12w(1,4) 24w(1,2) against off's (4,2).
    """

    def _measured(self, cpc: float = 7.75) -> Governor:
        gov, _ = make_gov()
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        for _ in range(4):
            gov._occ.record(cpu_seconds=cpc * 100.0, wall_seconds=100.0)
        return gov

    def _at(self, gov: Governor, workers: int) -> tuple[int, int]:
        # Pretend the pool is already this size: threads_for_next_cell re-anchors the
        # sample windows when the count CHANGES, which would throw the measurement away.
        gov.workers = workers
        return gov.threads_for_next_cell(workers=workers)

    def test_finding_03_rayon_never_below_two(self):
        gov = self._measured()
        for w in (11, 12, 14, 15, 21, 22, 24, 48):
            rayon = self._at(gov, w)[0]
            assert rayon >= 2, f"{w} workers got RAYON_NUM_THREADS={rayon}"

    def test_finding_03_the_share_is_still_senior_to_the_measurement(self):
        # A 7.75-core cell wants ceil(1.25 * 7.75) = 10; the share still caps it.
        gov = self._measured()
        assert self._at(gov, 4)[0] == 5     # round(21/4) = 5
        assert self._at(gov, 8)[0] == 3     # round(21/8) = 3

    def test_finding_03_the_documented_saturation_point_is_the_real_one(self):
        # The docstring's table, recomputed here: 24 cores, cpu_target_pct 90 ->
        # core budget 21. The floor of 2 first BINDS at 15 workers (round(21/15) = 1);
        # from 9 workers up the arithmetic already gives 2.
        expected = {1: 21, 2: 10, 4: 5, 6: 4, 8: 3, 10: 2, 12: 2, 14: 2, 15: 2, 24: 2}
        gov = self._measured(cpc=100.0)          # hungry enough to want the whole bound
        for w, upper in expected.items():
            assert max(2, round(CORE_BUDGET / w)) == upper
            assert self._at(gov, w)[0] == upper


class TestFinding04FlushTotal:
    """flush = clamp(ceil(cap / workers), 1, cap): a TOTAL, not a per-worker grant."""

    def _gov(self, **over) -> Governor:
        gov, _ = make_gov(**over)
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        for _ in range(4):
            gov._occ.record(cpu_seconds=200.0, wall_seconds=100.0)
        return gov

    def _at(self, gov: Governor, workers: int) -> tuple[int, int]:
        gov.workers = workers
        return gov.threads_for_next_cell(workers=workers)

    def test_finding_04_four_workers_do_not_get_the_whole_cap_each(self):
        gov = self._gov()
        # The bug: cap // max(1, 4 // 4) == 12, i.e. 4 x 12 = 48 compression threads
        # on 24 cores, on the very first calibration cells.
        assert self._at(gov, 4)[1] == 3

    def test_finding_04_the_total_stays_near_the_cap(self):
        gov = self._gov()
        for w in (1, 2, 3, 4, 5, 6, 8, 10, 12):
            flush = self._at(gov, w)[1]
            total = w * flush
            assert flush >= 1
            assert total <= 2 * FLUSH_CAP, f"{w}w x {flush} = {total} flush threads"
        # Past the cap flush is already 1 and the total IS the worker count - that is
        # the pool's size, not a multiplied allowance.
        for w in (16, 24, 48):
            assert self._at(gov, w)[1] == 1

    def test_finding_04_flush_saturates_at_one_when_workers_reach_the_cap(self):
        gov = self._gov()
        assert self._at(gov, FLUSH_CAP - 1)[1] == 2
        assert self._at(gov, FLUSH_CAP)[1] == 1
        assert self._at(gov, FLUSH_CAP + 1)[1] == 1

    def test_finding_04_a_smaller_cap_saturates_sooner(self):
        gov = self._gov(flush_threads_cap=6)
        assert self._at(gov, 6)[1] == 1
        assert self._at(gov, 3)[1] == 2


class TestFinding05Admission:
    """No CPU gate, a 3 s timeout, and the wait is charged to the cell that paid it."""

    def test_finding_05_there_is_no_cpu_gate_at_all(self):
        gov, _ = make_gov()
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        assert not hasattr(gov, "_cpu_pct_probe"), "the CPU gate is gone, probe included"
        gov._sleep = lambda s: pytest.fail("a pinned CPU is the goal, not a reason to wait")
        assert gov.state == "CALIBRATE"          # the state that used to gate on CPU
        assert gov.admit(worker_id=3, active=8) == "go"

    def test_finding_05_the_timeout_is_three_seconds(self):
        assert ADMIT_TIMEOUT_S == 3.0
        clock = FakeClock()
        gov, _ = make_gov(avail_mb=100.0)        # RAM never frees: full timeout
        gov._now, gov._sleep = clock.now, clock.sleep
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        assert gov.admit(worker_id=1, active=2) == "go(timeout)"
        assert clock.t == pytest.approx(3.0, abs=ADMIT_POLL_S)

    def test_finding_05_admit_reports_the_seconds_it_waited(self):
        clock = FakeClock()
        gov, _ = make_gov(avail_mb=100.0)
        gov._now, gov._sleep = clock.now, clock.sleep
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        verdict = gov.admit(worker_id=7, active=2)
        assert isinstance(verdict, Admission) and verdict == "go(timeout)"
        assert verdict.gate_s == pytest.approx(3.0, abs=ADMIT_POLL_S)
        # Same number for a caller that dropped the return value (the pool does).
        assert gov.take_gate_s(7) == pytest.approx(verdict.gate_s)
        assert gov.take_gate_s(7) == 0.0, "taking it clears it"

    def test_finding_05_a_pass_through_costs_nothing(self):
        gov, _ = make_gov()
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        assert gov.admit(worker_id=1, active=2).gate_s == 0.0
        assert gov.take_gate_s(1) == 0.0

    def test_finding_05_the_wait_is_charged_into_the_cell_sample(self):
        gov, _ = make_gov()
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        assert gov.workers == 4
        gov.on_cell_complete(wall_s=20.0, cpu_s=40.0, peak_rss_mb=1500.0,
                             gpu_s=0.0, ok=True, gate_s=10.0)
        # 4 workers, 30 s of wall per delivered cell -> 8.0, not the 12.0 the
        # ungated wall alone would have claimed.
        assert gov.cells_per_min == pytest.approx(8.0)

    def test_finding_05_the_gate_does_not_pollute_the_occupancy_estimate(self):
        gov, _ = make_gov()
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        for _ in range(3):
            gov.on_cell_complete(wall_s=20.0, cpu_s=40.0, peak_rss_mb=1500.0,
                                 gpu_s=0.0, ok=True, gate_s=10.0)
        # cores/cell is cpu over the time the cell RAN: 40 / 20, never 40 / 30.
        assert gov.cores_per_cell == pytest.approx(2.0)


class TestFinding06OffModeCpuTarget:
    """governor_mode="off" reads cpu_target_pct RAW, as the legacy code did."""

    def test_finding_06_off_mode_does_not_clamp_to_ninety_five(self):
        gov, _ = make_gov(governor_mode="off", cpu_target_pct=120)
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        # int(24 * 120 / 100) = 28 threads at one worker. The 10..95 clamp would
        # have said int(24 * 95 / 100) = 22 for a governor-OFF install.
        assert gov.threads_for_next_cell(workers=1) == (28, 6)
        assert gov.threads_for_next_cell(workers=1) != (22, 6)

    def test_finding_06_oversubscription_survives_at_a_hundred_and_fifty(self):
        gov, _ = make_gov(governor_mode="off", cpu_target_pct=150)
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        assert gov.threads_for_next_cell(workers=2)[0] == 18     # 36 // 2

    def test_finding_06_the_governed_paths_still_clamp(self):
        gov, cfg = make_gov(cpu_target_pct=120)
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        assert gov._cpu_target_pct(cfg) == 95.0
        assert gov._cpu_target_pct(cfg, clamped=False) == 120.0
        cfg["cpu_target_pct"] = 1
        assert gov._cpu_target_pct(cfg) == 10.0


class TestFinding08EndRunParks:
    def _finished(self):
        gov, _ = make_gov()
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        drive(gov, BERLIN_WALL_S, cells=40)
        assert gov.state == "STEADY"
        return gov, gov.end_run()

    def test_finding_08_state_goes_idle_after_a_run(self):
        gov, persisted = self._finished()
        assert persisted is not None, "parking must not cost the history entry"
        assert gov.state == "OFF"
        assert gov.snapshot().state == "OFF"
        assert gov.target == gov.workers, "no pending resize survives the run"

    def test_finding_08_a_parked_governor_decides_nothing(self):
        gov, _ = self._finished()
        gov._sleep = lambda s: pytest.fail("a finished run must not hold a worker")
        gov._available_mb_probe = lambda: 10.0
        assert gov.admit(worker_id=1, active=8) == "go(steady)"
        assert gov.on_cell_complete(wall_s=63.5, cpu_s=63.5, peak_rss_mb=9000.0,
                                    gpu_s=0.0, ok=True) is None

    def test_finding_08_history_already_persisted_stays_persisted(self):
        gov, persisted = self._finished()
        key, entry = persisted
        assert key == "1:1/4" and 8 <= entry["workers"] <= 12
        # And a warm start reads it back on the next run.
        gov2, _ = make_gov(governor_history={key: entry})
        gov2.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        assert gov2.state == "STEADY" and gov2.workers == entry["workers"]


class TestFinding09AdviseIsLegacy:
    """advise observes and logs. It must not change one byte of the child env."""

    def test_finding_09_advise_returns_the_legacy_thread_pair(self):
        adv, _ = make_gov(governor_mode="advise")
        off, _ = make_gov(governor_mode="off")
        for gov in (adv, off):
            gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        for w in (1, 2, 4, 8, 12, 24):
            assert adv.threads_for_next_cell(workers=w) == off.threads_for_next_cell(workers=w)
        assert adv.threads_for_next_cell(workers=8) == (4, 2)   # the measured off pair

    def test_finding_09_advise_stays_legacy_once_it_has_measurements(self):
        adv, _ = make_gov(governor_mode="advise")
        adv.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=8)
        drive(adv, {8: 21.1}, cells=10)
        assert adv.cores_per_cell is not None, "it is still measuring"
        assert adv.threads_for_next_cell(workers=8) == (4, 2)
        assert adv.snapshot().threads == 4

    def test_finding_09_advise_never_holds_a_worker(self):
        gov, _ = make_gov(governor_mode="advise", avail_mb=10.0)
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        gov._sleep = lambda s: pytest.fail("advise changes nothing, waits for nothing")
        assert gov.admit(worker_id=1, active=8) == "go(steady)"
        assert gov.admit(worker_id=1, active=8).gate_s == 0.0


class TestFinding10SampleAttribution:
    """A cell is evidence about the level it LAUNCHED under, and no other."""

    def test_finding_10_a_completion_from_the_previous_level_is_ignored(self):
        gov, _ = make_gov()
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        # 4 workers x 20 s cells => a completion every 5 s, so six of them span 25 s and the
        # window is long enough to be a rate rather than a burst interval.
        for _ in range(6):
            gov._clock.advance(5.0)
            gov.on_cell_complete(wall_s=20.0, cpu_s=40.0, peak_rss_mb=1500.0,
                                 gpu_s=0.0, ok=True, launched_workers=4)
        assert gov.target == 6 and gov.state == "CONVERGE"
        gov.threads_for_next_cell(workers=6)          # the pool applied it
        before = gov.snapshot().samples
        for _ in range(3):                            # cells that were already in flight
            gov._clock.advance(5.0)
            assert gov.on_cell_complete(wall_s=20.0, cpu_s=40.0, peak_rss_mb=1500.0,
                                        gpu_s=0.0, ok=True, launched_workers=4) is None
        assert gov.snapshot().samples == before, "4-worker walls are not evidence about 6"
        assert gov.cells_per_min is None
        assert gov.state == "CONVERGE", "and they cannot decide the step either"

    def test_finding_10_the_rss_of_a_stale_cell_is_still_kept(self):
        # A peak is a peak whatever the level, and the RAM gate is the one consumer
        # that must not forget it.
        gov, _ = make_gov()
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        gov.threads_for_next_cell(workers=6)
        gov.on_cell_complete(wall_s=20.0, cpu_s=40.0, peak_rss_mb=4321.0,
                             gpu_s=0.0, ok=True, launched_workers=4)
        assert gov.rss_p95_mb == 4321.0
        assert gov.snapshot().samples == 0

    def test_finding_10_an_untagged_completion_is_trusted_as_current(self):
        gov, _ = make_gov()
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        gov.on_cell_complete(wall_s=20.0, cpu_s=40.0, peak_rss_mb=1500.0,
                             gpu_s=0.0, ok=True)
        assert gov.snapshot().samples == 1

    @pytest.mark.parametrize("lag", [2, 3, 6, 8, 10])
    def test_finding_10_the_berlin_curve_still_stops_at_the_knee_with_a_pipeline(self, lag):
        # Measured counterfactual: strip the tag from this exact replay and the
        # governor walks to 10 at lag 2-4 and all the way to 24 from lag 6 up,
        # because each step's first `lag` completions are shorter, older-level walls
        # divided by the new, higher worker count.
        gov, _ = make_gov()
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        seen = drive_pipelined(gov, BERLIN_WALL_S, cells=120, lag=lag)
        assert 8 <= gov.workers <= 12, f"converged on {gov.workers} (lag {lag})"
        assert max(seen) <= 12, f"visited {sorted(seen)} (lag {lag})"
        assert 24 not in seen
        assert gov.state == "STEADY"


class SpyLock:
    """The real RLock, plus a record of who took it and how deep it is right now."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.acquires = 0
        self.depth = 0

    def __enter__(self):
        self.inner.acquire()
        self.acquires += 1
        self.depth += 1
        return None

    def __exit__(self, *exc) -> bool:
        self.depth -= 1
        self.inner.release()
        return False


class TestFinding11Locking:
    """One RLock over the state machine and every sample window, never held over a sleep.

    Note what these tests can and cannot prove. The lost-increment race on
    `self._samples += 1` is real but the GIL hides it in a stress run on this box
    (measured: 5 x 4 threads x 250 completions with the lock stubbed out lost nothing),
    so the structural assertions below - the windows are written INSIDE the lock, every
    entry point takes it - are the ones that actually fail if the locking is removed.
    """

    def test_finding_11_the_sample_windows_are_written_under_the_lock(self):
        gov, _ = make_gov()
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        spy = SpyLock(gov._lock)
        gov._lock = spy
        depths: list[int] = []
        real_record = gov._occ.record

        def record(*a, **kw):
            # _occ.record sits in the same critical section as the wall windows and
            # `self._samples += 1`, so its depth is the whole block's depth.
            depths.append(spy.depth)
            return real_record(*a, **kw)

        gov._occ.record = record
        gov.on_cell_complete(wall_s=20.0, cpu_s=40.0, peak_rss_mb=1500.0,
                             gpu_s=0.0, ok=True, launched_workers=4)
        assert depths == [1], f"sample recorded at lock depth {depths}"

    def test_finding_11_every_entry_point_takes_the_lock(self):
        gov, _ = make_gov()
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        spy = SpyLock(gov._lock)
        gov._lock = spy
        calls = {
            "begin_run": lambda: gov.begin_run(total_cells=400, scale=1.0,
                                               cell_size=4, ceiling=24),
            "threads_for_next_cell": lambda: gov.threads_for_next_cell(workers=6),
            "on_cell_complete": lambda: gov.on_cell_complete(
                wall_s=20.0, cpu_s=40.0, peak_rss_mb=1500.0, gpu_s=0.0, ok=True),
            "snapshot": gov.snapshot,
            "advice": gov.advice,
            "freeze": gov.freeze,
            "recalibrate": gov.recalibrate,
            "end_run": gov.end_run,
        }
        for name, call in calls.items():
            before = spy.acquires
            call()
            assert spy.acquires > before, f"{name} touched the state unsynchronised"
            assert spy.depth == 0, f"{name} left the lock held"

    def test_finding_11_a_concurrent_run_stays_consistent(self):
        # Smoke test rather than a race detector (see the class docstring): four
        # producers and two readers, no exception, and every completion counted once.
        import threading as _threading

        gov, _ = make_gov()
        gov.begin_run(total_cells=4000, scale=1.0, cell_size=4, ceiling=8)
        errors: list[BaseException] = []
        per_thread, threads_n = 250, 4

        def producer():
            try:
                for _ in range(per_thread):
                    gov.threads_for_next_cell(workers=gov.target)
                    gov.on_cell_complete(wall_s=20.0, cpu_s=40.0, peak_rss_mb=1500.0,
                                         gpu_s=0.0, ok=True)
            except BaseException as ex:                       # noqa: BLE001
                errors.append(ex)

        def reader():
            try:
                for _ in range(per_thread * 2):
                    gov.snapshot()
                    gov.advice()
                    _ = gov.cells_per_min, gov.rss_p95_mb, gov.cores_per_cell
            except BaseException as ex:                       # noqa: BLE001
                errors.append(ex)

        ts = ([_threading.Thread(target=producer) for _ in range(threads_n)]
              + [_threading.Thread(target=reader) for _ in range(2)])
        for t in ts:
            t.start()
        for t in ts:
            t.join(60)
        assert not errors, f"{errors[:3]}"
        assert gov.snapshot().samples == per_thread * threads_n

    def test_finding_11_the_lock_is_never_held_across_the_admit_sleep(self):
        clock = FakeClock()
        gov, _ = make_gov(avail_mb=100.0)
        gov._now = clock.now
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        held: list[bool] = []

        def sleeping(seconds: float) -> None:
            # A second thread must be able to take the lock while this one waits.
            import threading as _threading

            got = _threading.Event()

            def grab():
                with gov._lock:
                    got.set()

            t = _threading.Thread(target=grab)
            t.start()
            held.append(got.wait(5.0))
            t.join(5.0)
            clock.sleep(seconds)

        gov._sleep = sleeping
        assert gov.admit(worker_id=1, active=2) == "go(timeout)"
        assert held and all(held), "a blocked worker must not freeze the flask thread"


class TestFinding12SettingsReads:
    def test_finding_12_admit_reads_the_settings_once_per_call(self):
        reads = {"n": 0}
        cfg = {"governor_mode": "auto", "cpu_target_pct": 90, "ram_headroom_mb": 2048}

        def get_settings():
            reads["n"] += 1
            return cfg

        clock = FakeClock()
        gov = Governor(cores=CORES, get_settings=get_settings)
        gov._available_mb_probe = lambda: 100.0          # never enough: full timeout
        gov._now, gov._sleep = clock.now, clock.sleep
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        reads["n"] = 0
        assert gov.admit(worker_id=1, active=2) == "go(timeout)"
        assert len(clock.slept) >= 10, "it really did poll"
        assert reads["n"] == 1, f"{reads['n']} settings reads for one admit()"

    def test_finding_12_the_settings_are_cached_behind_the_stamp(self):
        reads = {"n": 0}
        stamp = {"v": 1.0}
        cfg = {"governor_mode": "auto", "cpu_target_pct": 90}

        def get_settings():
            reads["n"] += 1
            return dict(cfg)

        gov = Governor(cores=CORES, get_settings=get_settings,
                       settings_stamp=lambda: stamp["v"])
        for _ in range(20):
            gov._cfg()
        assert reads["n"] == 1
        stamp["v"] = 2.0                                  # the file was written
        gov._cfg()
        assert reads["n"] == 2

    def test_finding_12_without_a_stamp_nothing_is_cached(self):
        reads = {"n": 0}

        def get_settings():
            reads["n"] += 1
            return {"governor_mode": "auto"}

        gov = Governor(cores=CORES, get_settings=get_settings)
        gov._cfg()
        gov._cfg()
        assert reads["n"] == 2, "no stamp, no staleness: every read is live"


class TestFinding16MinThreadsFallback:
    def test_finding_16_a_falsy_min_threads_setting_falls_back_to_one(self):
        for falsy in (0, None, ""):
            gov, _ = make_gov(governor_mode="off", min_threads_per_worker=falsy)
            gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
            # legacy `or 1`: 21 // 8 = 2 wins, and the answer is 2 - not the 4 a
            # hand-edited blob started getting when the fallback became 4.
            assert gov.threads_for_next_cell(workers=8) == (2, 2), f"min_threads={falsy!r}"

    def test_finding_16_a_real_setting_is_still_the_floor(self):
        gov, _ = make_gov(governor_mode="off", min_threads_per_worker=4)
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        assert gov.threads_for_next_cell(workers=8) == (4, 2)

    def test_finding_16_an_absent_key_keeps_the_shipped_default(self):
        gov, cfg = make_gov(governor_mode="off")
        del cfg["min_threads_per_worker"]
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        assert gov.threads_for_next_cell(workers=8) == (4, 2)


class TestIdleMachineKeepsClimbing:
    """Measured on the phase-2 branch: with the region-write filter on, three cs4 runs all
    converged at 8 workers with CPU at 53-60% - sixteen cores doing nothing - and lost 8%
    against the same build with the filter off. Cheaper cells make each +2 step a smaller
    FRACTION of throughput, so the relative threshold is reached while real headroom
    remains. A stop is only trustworthy once the machine is actually busy.
    """

    @staticmethod
    def _walls(tp: dict[int, float]) -> dict[int, float]:
        return {w: w * 60.0 / t for w, t in tp.items()}

    # Gains of ~2% a step: under the relative threshold, but the curve is still rising.
    RISING = {4: 21.0, 6: 21.4, 8: 21.9, 10: 22.3, 12: 22.8, 14: 23.2, 16: 23.7,
              18: 24.1, 20: 24.6, 22: 25.0, 24: 25.5}

    def test_an_idle_machine_climbs_past_the_first_weak_steps(self):
        gov, _ = make_gov()
        gov._budget_spent = lambda: False          # cores to spare
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=20)
        seen = drive(gov, self._walls(self.RISING), cells=200)
        assert max(seen) > 8, (
            f"settled at {max(seen)}w with the machine idle - this is the measured 8-worker "
            f"regression"
        )

    def test_a_busy_machine_still_settles_promptly(self):
        gov, _ = make_gov()
        gov._budget_spent = lambda: True           # genuinely saturated
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=20)
        seen = drive(gov, self._walls(self.RISING), cells=200)
        assert gov.state == "STEADY"
        assert max(seen) <= 20

    def test_the_idle_allowance_is_strictly_more_patient(self):
        from src.governor import STOP_STRIKES, STOP_STRIKES_IDLE
        assert STOP_STRIKES_IDLE > STOP_STRIKES


class TestRateNeedsARealWindow:
    """Completions arrive in bursts - workers start together, so they finish together.

    A real cold run on this branch produced steps of "+40.4", "-58.0" and "-176.9"
    cells/min and then "throughput drifted 78%, recalibrating" on a loop, thrashing
    4 -> 6 -> 12 -> 8 -> 6 and settling at 6 of 24 cores. Throughput cannot swing by 176
    cells/min; the estimator was reading the gap between two neighbours inside one batch.
    """

    def test_a_burst_is_not_a_rate(self):
        gov, _ = make_gov()
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        for _ in range(3):                      # three completions 0.7 s apart
            gov._clock.advance(0.7)
            gov.on_cell_complete(wall_s=20.0, cpu_s=30.0, peak_rss_mb=1000.0,
                                 gpu_s=0.0, ok=True, launched_workers=gov.workers)
        # 2 intervals over 1.4 s would read as 85.7 cells/min. It must refuse instead.
        assert gov._rate_tp() is None, "a 1.4 s window is an interval, not a rate"

    def test_a_long_enough_window_is_believed(self):
        from src.governor import MIN_RATE_SPAN_S
        gov, _ = make_gov()
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=24)
        # Two completions, spaced past the floor: the window is long enough to be a rate,
        # but STEP_SAMPLES has not been reached, so no decision fires and clears it.
        step = MIN_RATE_SPAN_S + 1.0
        for _ in range(2):
            gov._clock.advance(step)
            gov.on_cell_complete(wall_s=20.0, cpu_s=30.0, peak_rss_mb=1000.0,
                                 gpu_s=0.0, ok=True, launched_workers=gov.workers)
        rate = gov._rate_tp()
        assert rate is not None, "a window past the floor is a rate"
        assert rate == pytest.approx(60.0 / step, rel=1e-6)   # 1 interval over `step` seconds

    def test_the_climb_does_not_thrash_on_bursty_arrivals(self):
        """The end-to-end shape of the bug: a rising curve delivered in bursts.

        Every level's completions land in a tight clump, which is exactly what the real
        pool does. The climb must still climb instead of oscillating.
        """
        gov, _ = make_gov()
        gov._budget_spent = lambda: False
        gov.begin_run(total_cells=400, scale=1.0, cell_size=4, ceiling=20)
        tp = {w: 20.0 + w * 0.6 for w in range(2, 26, 2)}      # genuinely rising
        seen = {gov.workers}
        for _ in range(300):
            gov.threads_for_next_cell(workers=gov.target)
            w = gov.workers
            gov._clock.advance(60.0 / tp[w])                   # steady delivery at this level
            gov.on_cell_complete(wall_s=w * 60.0 / tp[w], cpu_s=30.0, peak_rss_mb=1000.0,
                                 gpu_s=0.0, ok=True, launched_workers=w)
            seen.add(gov.workers)
        assert max(seen) >= 12, f"climb stalled at {max(seen)}w on a rising curve"
