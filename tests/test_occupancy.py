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
    OccupancyTracker,
    damped_step,
    suggest_workers,
)

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
