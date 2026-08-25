"""Worker sizing from measured occupancy (src/occupancy.py).

The numbers in these tests are real: they come from timing arnis with the process's
own `TotalProcessorTime` on a 24-core box, and they are the reason this module
exists. A 1:20 cell and a 1:1 cell differ by 7.6x in how many cores they keep busy,
so no single `max_workers` can be right for both.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.occupancy import (  # noqa: E402
    RECENT_WINDOW,
    RSS_WINDOW,
    THROUGHPUT_WINDOW,
    THROUGHPUT_WINDOW_S,
    OccupancyTracker,
    damped_step,
    suggest_workers,
)


class FakeClock:
    """A clock the test drives by hand, so throughput is measured without sleeping."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

# Measured, cached tiles, terrain + caves + baked lighting, 24 cores.
CORES_1TO20 = 4.13 / 4.05    # ~1.02 - a Romania cell is nearly serial
CORES_1TO1 = 904.5 / 116.7   # ~7.75 - a 224-region cell parallelises well
CORES = 24


class TestTracker:
    def test_reports_nothing_until_it_has_evidence(self):
        t = OccupancyTracker()
        assert t.cores_per_cell is None
        t.record(cpu_seconds=4.13, wall_seconds=4.05)
        assert t.cores_per_cell is None, "one sample is not enough to resize a pool"

    def test_median_of_recorded_cells(self):
        t = OccupancyTracker()
        for _ in range(4):
            t.record(cpu_seconds=4.13, wall_seconds=4.05)
        assert t.cores_per_cell == pytest_approx(CORES_1TO20)

    def test_ignores_runs_too_short_to_mean_anything(self):
        t = OccupancyTracker()
        t.record(cpu_seconds=0.2, wall_seconds=0.3)   # a failure, or an all-cache read
        t.record(cpu_seconds=0.0, wall_seconds=9.0)   # no CPU recorded at all
        assert t.cores_per_cell is None

    def test_one_slow_cell_does_not_move_the_estimate(self):
        # Median, not mean: a cold-cache first cell should not steer the whole project.
        t = OccupancyTracker()
        t.record(cpu_seconds=40.0, wall_seconds=4.0)   # outlier, 10 cores
        for _ in range(4):
            t.record(cpu_seconds=4.13, wall_seconds=4.05)
        assert t.cores_per_cell == pytest_approx(CORES_1TO20)

    def test_follows_a_change_of_scale(self):
        # A project that switches from 1:20 to 1:1 cells must re-converge.
        t = OccupancyTracker()
        for _ in range(8):
            t.record(cpu_seconds=4.13, wall_seconds=4.05)
        for _ in range(8):
            t.record(cpu_seconds=904.5, wall_seconds=116.7)
        assert t.cores_per_cell == pytest_approx(CORES_1TO1)


class TestSuggestWorkers:
    def test_romania_1to20_wants_far_more_than_the_default_four(self):
        # 24 x 0.9 / 1.02 = 21.2 -> 21. The stored default is 4.
        assert suggest_workers(CORES_1TO20, CORES, 90.0) == 21

    def test_a_1to1_cell_saturates_the_box_almost_alone(self):
        # 24 x 0.9 / 7.75 = 2.8 -> 2. Floors deliberately: one worker too many costs
        # more than one too few.
        assert suggest_workers(CORES_1TO1, CORES, 90.0) == 2

    def test_ram_wins_when_it_is_tighter_than_cpu(self):
        # A 1:1 cell peaks ~4.15 GB under eviction; 12 GB free fits two, whatever the
        # CPU says. RAM is a wall, CPU is only a slowdown.
        assert (
            suggest_workers(
                CORES_1TO20, CORES, 90.0,
                ram_available_mb=12_000, ram_per_cell_mb=4_150,
            )
            == 2
        )

    def test_cpu_wins_when_ram_is_plentiful(self):
        assert (
            suggest_workers(
                CORES_1TO20, CORES, 90.0,
                ram_available_mb=64_000, ram_per_cell_mb=1_200,
            )
            == 21
        )

    def test_never_returns_zero(self):
        # A cell heavier than the whole budget still has to run, one at a time.
        assert suggest_workers(100.0, CORES, 90.0) == 1
        assert suggest_workers(CORES_1TO20, CORES, 0.0) == 1
        assert suggest_workers(CORES_1TO20, CORES, 90.0,
                               ram_available_mb=100, ram_per_cell_mb=4_000) == 1

    def test_respects_the_hard_cap(self):
        assert suggest_workers(0.01, CORES, 90.0, hard_cap=64) == 64

    def test_rejects_nonsense_inputs_instead_of_dividing_by_zero(self):
        assert suggest_workers(0.0, CORES, 90.0) == 1
        assert suggest_workers(CORES_1TO20, 0, 90.0) == 1

    def test_budget_percentage_is_honoured(self):
        full = suggest_workers(CORES_1TO20, CORES, 100.0)
        half = suggest_workers(CORES_1TO20, CORES, 50.0)
        assert half < full


class TestDampedStep:
    def test_walks_toward_the_target_rather_than_jumping(self):
        # Occupancy was measured UNDER the current worker count, so 4 -> 21 in one
        # move would be acting on an extrapolation. Step, then re-measure.
        assert damped_step(4, 21) == 6
        assert damped_step(6, 21) == 8

    def test_steps_down_as_well(self):
        assert damped_step(12, 3) == 10

    def test_lands_exactly_and_then_stays(self):
        assert damped_step(20, 21) == 21
        assert damped_step(21, 21) == 21

    def test_converges(self):
        current = 4
        for _ in range(20):
            current = damped_step(current, 21)
        assert current == 21


def pytest_approx(value):
    import pytest

    return pytest.approx(value, rel=1e-6)


class TestGpuBudget:
    """The third budget: workers share ONE adapter, so per-worker busy fractions
    stack. Numbers from the measured 1:1 cell: the 5080 was ~1% busy per worker,
    so this clamp exists for weaker adapters and future heavier kernels, not for
    today's hardware."""

    def test_gpu_fraction_is_tracked_alongside_cpu(self):
        t = OccupancyTracker()
        # measured shape: 54.7 s wall, ~0.6 s of GPU dispatches
        for _ in range(4):
            t.record(cpu_seconds=623.0, wall_seconds=54.7, gpu_seconds=0.6)
        assert t.gpu_fraction_per_cell == pytest_approx(0.6 / 54.7)

    def test_gpu_off_reports_zero_not_none_confusion(self):
        t = OccupancyTracker()
        for _ in range(3):
            t.record(cpu_seconds=623.0, wall_seconds=54.7)
        assert t.gpu_fraction_per_cell == 0.0

    def test_todays_hardware_is_not_gpu_limited(self):
        # 1% busy per worker: the 95% budget fits 95 workers - CPU and RAM clamp
        # long before the GPU does.
        assert (
            suggest_workers(
                11.4, 24, 90.0,
                gpu_fraction_per_cell=0.011, gpu_target_pct=95.0,
            )
            == 1
        )  # CPU is the binding budget here, not the GPU

    def test_a_saturating_kernel_clamps_workers(self):
        # A hypothetical adapter kept 40% busy per worker: only 2 fit under 95%.
        assert (
            suggest_workers(
                1.0, 24, 90.0,
                gpu_fraction_per_cell=0.40, gpu_target_pct=95.0,
            )
            == 2
        )

    def test_zero_fraction_never_divides(self):
        assert suggest_workers(1.02, 24, 90.0, gpu_fraction_per_cell=0.0) == 21
        assert suggest_workers(1.02, 24, 90.0, gpu_fraction_per_cell=None) == 21

    def test_tightest_budget_wins_across_all_three(self):
        # CPU says 21, RAM says 5, GPU says 3 -> 3.
        assert (
            suggest_workers(
                1.02, 24, 90.0,
                ram_available_mb=6_000, ram_per_cell_mb=1_200,
                gpu_fraction_per_cell=0.30, gpu_target_pct=95.0,
            )
            == 3
        )


class TestReset:
    """A run start, or a change of scale bucket, must not inherit the last run's
    window: 1:20 cells and 1:1 cells differ by 7.6x, and their median describes no
    cell that exists."""

    def test_clears_every_window(self):
        clock = FakeClock()
        t = OccupancyTracker(clock=clock)
        for _ in range(4):
            t.record(cpu_seconds=904.5, wall_seconds=116.7,
                     gpu_seconds=0.6, peak_rss_mb=4_150)
            clock.advance(120.0)
        assert t.cores_per_cell is not None
        assert t.rss_p95 is not None
        assert t.cells_per_min is not None

        t.reset()
        assert t.cores_per_cell is None
        assert t.cores_per_cell_recent is None
        assert t.gpu_fraction_per_cell is None
        assert t.rss_p95 is None
        assert t.rss_median is None
        assert t.cells_per_min is None

    def test_a_reset_bucket_converges_immediately_not_over_a_window(self):
        # Without reset the 1:20 history sits in the median for eight more cells and
        # the pool sizes itself for the average of two different workloads.
        t = OccupancyTracker()
        for _ in range(8):
            t.record(cpu_seconds=4.13, wall_seconds=4.05)
        t.reset()
        for _ in range(2):
            t.record(cpu_seconds=904.5, wall_seconds=116.7)
        assert t.cores_per_cell == pytest_approx(CORES_1TO1)

    def test_reset_on_a_fresh_tracker_is_harmless(self):
        t = OccupancyTracker()
        t.reset()
        t.reset()
        assert t.cores_per_cell is None


class TestRamSamples:
    """RAM is the wall the Berlin runs kept walking into: ram_peak 88% at 16 workers,
    93% at cell_size=8. The tracker has to carry the number the governor fences
    admissions with."""

    def test_peak_rss_is_optional_and_old_calls_still_work(self):
        t = OccupancyTracker()
        t.record(4.13, 4.05)                 # positional, as server.py calls it
        t.record(4.13, 4.05, gpu_seconds=0.1)
        assert t.cores_per_cell == pytest_approx(CORES_1TO20)
        assert t.rss_p95 is None, "no RSS reported means no RSS opinion"
        assert t.rss_median is None

    def test_p95_answers_on_the_very_first_sample(self):
        # Unlike the CPU properties: one observed 4 GB cell is already reason not to
        # admit a worker into 2 GB of headroom.
        t = OccupancyTracker()
        t.record(904.5, 116.7, peak_rss_mb=4_150)
        assert t.rss_p95 == pytest_approx(4_150.0)
        assert t.rss_median == pytest_approx(4_150.0)

    def test_p95_is_nearest_rank_so_it_does_not_hide_the_peak(self):
        t = OccupancyTracker()
        for mb in [1_000, 1_100, 1_050, 1_020, 4_150]:
            t.record(904.5, 116.7, peak_rss_mb=mb)
        # An interpolated p95 would land ~3.5 GB; the wall has to be the real peak.
        assert t.rss_p95 == pytest_approx(4_150.0)
        assert t.rss_median == pytest_approx(1_050.0)

    def test_a_full_window_puts_the_rank_one_below_the_top(self):
        # ceil(0.95 * 32) = 31, so with 32 samples the single heaviest cell sits
        # ABOVE the p95 by definition -- one freak cell in 32 is not the wall.
        t = OccupancyTracker()
        for _ in range(RSS_WINDOW - 1):
            t.record(904.5, 116.7, peak_rss_mb=1_000)
        t.record(904.5, 116.7, peak_rss_mb=9_000)
        assert t.rss_p95 == pytest_approx(1_000.0)
        assert t.rss_median == pytest_approx(1_000.0)

        # Two heavy cells out of 32 and the wall moves: that is a pattern, not noise.
        t2 = OccupancyTracker()
        for _ in range(RSS_WINDOW - 2):
            t2.record(904.5, 116.7, peak_rss_mb=1_000)
        for _ in range(2):
            t2.record(904.5, 116.7, peak_rss_mb=9_000)
        assert t2.rss_p95 == pytest_approx(9_000.0)

    def test_window_is_bounded(self):
        t = OccupancyTracker()
        for i in range(RSS_WINDOW * 3):
            t.record(904.5, 116.7, peak_rss_mb=1_000 + i)
        assert len(t.rss_samples) == RSS_WINDOW
        assert min(t.rss_samples) == 1_000 + RSS_WINDOW * 2

    def test_short_and_missing_samples_do_not_pollute_the_wall(self):
        t = OccupancyTracker()
        t.record(0.2, 0.3, peak_rss_mb=80)      # cached read: not steady-state work
        t.record(904.5, 116.7, peak_rss_mb=None)
        t.record(904.5, 116.7, peak_rss_mb=0)
        assert t.rss_p95 is None

    def test_a_missing_cpu_reading_does_not_discard_a_known_peak(self):
        # Callers coerce an absent cpu_s to 0.0 (governor.py: `float(cpu_s or 0.0)`).
        # Such a cell says nothing about occupancy, but its 4 GB peak is still the
        # wall the next admission has to clear, so the two gates are independent.
        t = OccupancyTracker()
        t.record(0.0, 116.7, peak_rss_mb=4_150)
        assert t.cores_per_cell is None, "no CPU time means no occupancy opinion"
        assert t.gpu_fraction_per_cell is None
        assert t.rss_p95 == pytest_approx(4_150.0)
        assert t.rss_median == pytest_approx(4_150.0)

    def test_a_short_cell_is_still_excluded_whatever_its_cpu(self):
        # The wall gate is the one that filters cached reads, and it still binds:
        # an 80 MB tile read is not evidence about a real cell's footprint.
        t = OccupancyTracker()
        t.record(1.0, 0.3, peak_rss_mb=80)
        assert t.rss_p95 is None
        assert len(t.completions) == 1, "it is still a finished cell for throughput"

    def test_it_feeds_the_existing_ram_budget(self):
        # 12 GB free, a measured 4.15 GB p95 -> two workers, whatever the CPU says.
        t = OccupancyTracker()
        for _ in range(3):
            t.record(904.5, 116.7, peak_rss_mb=4_150)
        assert (
            suggest_workers(
                CORES_1TO20, CORES, 90.0,
                ram_available_mb=12_000, ram_per_cell_mb=t.rss_p95,
            )
            == 2
        )


class TestThroughput:
    """Flat throughput with rising per-cell time IS the knee: Berlin held 21-23
    cells/min at every worker count from 8 to 24 while the median cell went 21.1 s
    to 63.5 s. Without this number a governor reads only the per-cell time and
    concludes, wrongly, that it is losing."""

    def test_none_until_two_cells_have_landed(self):
        clock = FakeClock()
        t = OccupancyTracker(clock=clock)
        assert t.cells_per_min is None
        t.record(904.5, 116.7)
        assert t.cells_per_min is None

    def test_rate_over_the_wall_between_completions(self):
        clock = FakeClock()
        t = OccupancyTracker(clock=clock)
        for _ in range(3):
            t.record(904.5, 116.7)
            clock.advance(30.0)
        # three completions, 60 s from first to last -> 2 per minute
        assert t.cells_per_min == pytest_approx(2.0)

    def test_reproduces_the_measured_berlin_rate(self):
        # 400 cells in 1221 s = 19.7 cells/min (Bucuresti, 16 workers).
        clock = FakeClock()
        t = OccupancyTracker(clock=clock)
        gap = 1221.0 / 399.0
        for i in range(60):
            t.record(70.0, 60.0)
            if i < 59:
                clock.advance(gap)
        assert t.cells_per_min == pytest_approx(60.0 / gap)

    def test_cached_cells_count_as_throughput(self):
        # They are excluded from occupancy (they say nothing about cores) but they
        # are real finished cells; hiding them would read slow exactly when the run
        # is fastest.
        clock = FakeClock()
        t = OccupancyTracker(clock=clock)
        for _ in range(4):
            t.record(0.2, 0.3)     # below MIN_SAMPLE_WALL_S
            clock.advance(20.0)
        assert t.cores_per_cell is None
        assert t.cells_per_min == pytest_approx(3.0)

    def test_only_the_rolling_window_counts(self):
        clock = FakeClock()
        t = OccupancyTracker(clock=clock)
        t.record(904.5, 116.7)                       # ancient
        clock.advance(10.0)
        t.record(904.5, 116.7)                       # ancient
        clock.advance(THROUGHPUT_WINDOW_S * 2)
        t.record(904.5, 116.7)
        clock.advance(30.0)
        t.record(904.5, 116.7)
        assert t.cells_per_min == pytest_approx(2.0)  # the old pair is out of window

    def test_a_stalled_clock_does_not_divide_by_zero(self):
        t = OccupancyTracker(clock=FakeClock())      # never advanced
        for _ in range(4):
            t.record(904.5, 116.7)
        assert t.cells_per_min is None

    def test_window_is_bounded(self):
        clock = FakeClock()
        t = OccupancyTracker(clock=clock)
        for _ in range(THROUGHPUT_WINDOW * 2):
            t.record(0.2, 0.3)
            clock.advance(0.5)
        assert len(t.completions) == THROUGHPUT_WINDOW

    def test_default_clock_is_real_and_monotonic(self):
        t = OccupancyTracker()
        t.record(904.5, 116.7)
        t.record(904.5, 116.7)
        assert len(t.completions) == 2
        assert t.completions[1] >= t.completions[0]


class TestRecentOccupancy:
    """Contention has to be visible before the eight-cell window turns over, or the
    governor keeps climbing on a reading taken at the old worker count."""

    def test_none_until_two_cells(self):
        t = OccupancyTracker()
        assert t.cores_per_cell_recent is None
        t.record(904.5, 116.7)
        assert t.cores_per_cell_recent is None

    def test_matches_the_lifetime_median_when_nothing_changes(self):
        t = OccupancyTracker()
        for _ in range(8):
            t.record(4.13, 4.05)
        assert t.cores_per_cell_recent == pytest_approx(CORES_1TO20)
        assert t.cores_per_cell_recent == pytest_approx(t.cores_per_cell)

    def test_sees_contention_the_long_window_still_hides(self):
        # Five uncontended cells at 7.75 cores, then a resize halves per-cell
        # occupancy. The eight-cell median still reads 7.75; the short one has
        # already moved.
        t = OccupancyTracker()
        for _ in range(5):
            t.record(cpu_seconds=904.5, wall_seconds=116.7)
        for _ in range(RECENT_WINDOW):
            t.record(cpu_seconds=904.5 / 2, wall_seconds=116.7)
        assert t.cores_per_cell == pytest_approx(CORES_1TO1)
        assert t.cores_per_cell_recent == pytest_approx(CORES_1TO1 / 2)
        assert t.cores_per_cell_recent < t.cores_per_cell

    def test_reads_only_the_last_few_cells(self):
        t = OccupancyTracker()
        t.record(40.0, 4.0)                 # 10 cores, long gone
        for _ in range(RECENT_WINDOW):
            t.record(4.13, 4.05)
        assert t.cores_per_cell_recent == pytest_approx(CORES_1TO20)


class TestPurity:
    """The module has to stay a pure function of recorded samples: server.py keeps
    one process-wide tracker and governor.py keeps its own, and neither can afford
    the other's state or a flask import."""

    def test_two_trackers_share_no_state(self):
        # The mutable windows are dataclass fields with default_factory; a shared
        # mutable default would make every tracker in the process the same tracker.
        a, b = OccupancyTracker(), OccupancyTracker()
        for _ in range(2):
            a.record(904.5, 116.7, gpu_seconds=0.6, peak_rss_mb=4_150)
        assert b.cores_per_cell is None
        assert b.rss_p95 is None
        assert b.cells_per_min is None
        assert len(b.completions) == 0

    def test_imports_nothing_from_the_app(self):
        import src.occupancy as occ

        source = Path(occ.__file__).read_text(encoding="utf-8")
        for forbidden in ("import flask", "from flask", "import server"):
            assert forbidden not in source

    def test_reset_is_reusable_across_many_buckets(self):
        # A project that walks 1:20 -> 1:1 -> 1:20 resets twice; nothing may leak.
        clock = FakeClock()
        t = OccupancyTracker(clock=clock)
        for cpu, wall in [(4.13, 4.05), (904.5, 116.7), (4.13, 4.05)]:
            t.reset()
            for _ in range(3):
                t.record(cpu, wall, peak_rss_mb=1_000)
                clock.advance(30.0)
            assert t.cores_per_cell == pytest_approx(cpu / wall)
            assert len(t.completions) == 3

    def test_recording_after_a_reset_still_feeds_every_window(self):
        clock = FakeClock()
        t = OccupancyTracker(clock=clock)
        t.record(904.5, 116.7, gpu_seconds=0.6, peak_rss_mb=4_150)
        t.reset()
        for _ in range(2):
            t.record(4.13, 4.05, gpu_seconds=0.1, peak_rss_mb=1_200)
            clock.advance(30.0)
        assert t.cores_per_cell == pytest_approx(CORES_1TO20)
        assert t.cores_per_cell_recent == pytest_approx(CORES_1TO20)
        assert t.gpu_fraction_per_cell == pytest_approx(0.1 / 4.05)
        assert t.rss_p95 == pytest_approx(1_200.0)
        assert t.cells_per_min == pytest_approx(2.0)


class TestTheBerlinKnee:
    """The measured run this module exists to stop: workers were raised live 8 -> 24
    on a 1:1 cell_size=4 Berlin project. Throughput stayed flat at 21-23 cells/min
    the whole way while the median cell went 21.1 s -> 63.5 s. Fed those numbers the
    tracker must make the knee VISIBLE -- flat rate, falling per-cell occupancy -- so
    a governor stops climbing at 8-12 instead of running to 24."""

    #: (workers, median cell seconds), straight off the real meld-report.json.
    BERLIN = [(8, 21.1), (12, 33.9), (16, 41.6), (20, 56.5), (24, 63.5)]

    @staticmethod
    def _leg(workers, median_s, clock, t):
        """Run one worker-count leg: `workers` cells in flight, each taking
        `median_s`, so the machine retires one every median_s/workers."""
        # Core-seconds per cell fall as workers rise: the box has 24 cores at a
        # measured 79% average, and the cells are sharing them, so each added
        # worker buys every cell fewer cores over a longer wall.
        cores_each = 24 * 0.79 / workers
        for _ in range(workers):
            t.record(cores_each * median_s, median_s, peak_rss_mb=1_100)
            clock.advance(median_s / workers)

    def test_throughput_stays_flat_while_per_cell_time_inflates(self):
        rates, occupancies = [], []
        for workers, median_s in self.BERLIN:
            clock = FakeClock()
            t = OccupancyTracker(clock=clock)     # each leg measured on its own
            self._leg(workers, median_s, clock, t)
            rates.append(t.cells_per_min)
            occupancies.append(t.cores_per_cell)

        # Flat rate: every leg lands in the measured 21-23 cells/min band.
        for (workers, _), rate in zip(self.BERLIN, rates):
            assert 20.0 <= rate <= 24.0, f"{workers}w -> {rate:.1f} cells/min"
        assert max(rates) - min(rates) < 3.0, "throughput must read as FLAT"

        # Falling occupancy: each added worker gets a thinner slice of the box.
        assert occupancies == sorted(occupancies, reverse=True)
        assert occupancies[-1] < occupancies[0] / 2

    def test_the_knee_is_at_8_to_12_workers_not_24(self):
        # THE regression this module is for. suggest_workers is fed the occupancy
        # measured AT 8 workers -- the reading a governor actually holds when it is
        # deciding whether to climb -- and must not answer 24.
        clock = FakeClock()
        t = OccupancyTracker(clock=clock)
        self._leg(8, 21.1, clock, t)

        target = suggest_workers(
            t.cores_per_cell, CORES, 90.0,
            ram_available_mb=31_400 * 0.5, ram_per_cell_mb=t.rss_p95,
        )
        assert 8 <= target <= 12, f"knee should be 8-12, got {target}"

    def _mid_resize(self):
        """One continuous run settled at 8 workers, then resized to 24 -- stopped
        exactly RECENT_WINDOW cells later, which is the moment a governor is next
        asked whether to keep climbing."""
        clock = FakeClock()
        t = OccupancyTracker(clock=clock)
        self._leg(8, 21.1, clock, t)
        settled = t.cores_per_cell
        for _ in range(RECENT_WINDOW):
            t.record(24 * 0.79 / 24 * 63.5, 63.5, peak_rss_mb=1_100)
            clock.advance(63.5 / 24)
        return t, settled

    def test_recent_occupancy_leads_the_long_window_after_a_resize(self):
        t, settled = self._mid_resize()
        # The eight-cell median is still mostly pre-resize cells, so it reports the
        # old, uncontended world; the three-cell one has already collapsed.
        assert t.cores_per_cell == pytest_approx(settled)
        assert t.cores_per_cell_recent < settled / 2
        assert t.cores_per_cell_recent < t.cores_per_cell, (
            "the short window must lead the long one on the way down")

    def test_falling_occupancy_alone_would_argue_for_MORE_workers(self):
        # The trap, and the reason cells_per_min exists. Contention pushes measured
        # cores-per-cell DOWN, and the CPU budget reads a smaller cell as "more of
        # these fit" -- so a governor watching occupancy alone accelerates into the
        # knee. Here it would sanction 24+ workers at the exact moment the machine
        # has stopped paying for them.
        t, settled = self._mid_resize()
        naive = suggest_workers(t.cores_per_cell_recent, CORES, 90.0)
        assert naive > suggest_workers(settled, CORES, 90.0)
        assert naive >= 24, "occupancy alone really does argue for the bad answer"

        # Throughput is the field that refuses: it never moved off the measured band
        # across the whole 8 -> 24 climb, so the extra workers bought nothing.
        assert 20.0 <= t.cells_per_min <= 24.0

    def test_the_whole_climb_never_improves_the_rate(self):
        clock = FakeClock()
        t = OccupancyTracker(clock=clock)
        for workers, median_s in self.BERLIN:
            self._leg(workers, median_s, clock, t)
        assert 20.0 <= t.cells_per_min <= 24.0
        # Ending at 24 workers, both windows agree again -- and agree on a cell that
        # keeps under one core busy, a third of what it held at 8.
        assert t.cores_per_cell == pytest_approx(t.cores_per_cell_recent)
        assert t.cores_per_cell < 1.0
