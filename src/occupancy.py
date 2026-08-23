"""Worker sizing from MEASURED CPU occupancy, not from assumed thread counts.

Meld's existing budget divides `cores * cpu_target_pct/100` by `max_workers` and
hands each cell that many rayon threads. That assumes a cell can *use* them. Measured
on a 24-core box with `TotalProcessorTime`, it often cannot:

    ~1 region at scale 0.05 (a Romania 1:20 cell)   4.13 core-seconds over 4.05 s  ->  1.02 cores
    224 regions at scale 1.0                      904.5 core-seconds over 116.7 s  ->  7.75 cores

A cell's parallelism depends on its SIZE, because most of arnis's phases scale with
the world's volume while several (OSM parse, the serial half of the post passes) do
not. So at 1:20 four workers leave the machine ~83% idle, and at 1:1 three workers
already saturate it. One number cannot be right for both, which is why this derives
it instead.

Everything here is a pure function of recorded samples so it can be tested without
running a render.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

# Never recommend more than this regardless of the arithmetic; matches WorkerPool.
DEFAULT_HARD_CAP = 64

# Ignore samples shorter than this: a cell that failed in a second, or one that was
# almost entirely a cached-tile read, says nothing about steady-state occupancy.
MIN_SAMPLE_WALL_S = 1.5

# How many recent cells the estimate is based on. Short enough to follow a change of
# scale mid-project, long enough that one slow disk moment does not move it.
WINDOW = 8


@dataclass
class OccupancyTracker:
    """Rolling record of how many cores each finished cell actually used."""

    samples: list[float] = field(default_factory=list)

    def record(self, cpu_seconds: float, wall_seconds: float) -> None:
        """Record one finished cell. `cpu_seconds` is total processor time across all
        of that process's threads, so `cpu/wall` is the mean number of busy cores."""
        if wall_seconds < MIN_SAMPLE_WALL_S or cpu_seconds <= 0:
            return
        self.samples.append(cpu_seconds / wall_seconds)
        del self.samples[:-WINDOW]

    @property
    def cores_per_cell(self) -> float | None:
        """Median cores a cell keeps busy, or None until there is enough to say.

        Median rather than mean: the first cell of a run pays cold caches and would
        drag a mean down for the rest of the project.
        """
        if len(self.samples) < 2:
            return None
        return statistics.median(self.samples)


def suggest_workers(
    cores_per_cell: float,
    cores_total: int,
    cpu_target_pct: float = 90.0,
    ram_available_mb: float | None = None,
    ram_per_cell_mb: float | None = None,
    hard_cap: int = DEFAULT_HARD_CAP,
) -> int:
    """How many cells to run at once, from measured occupancy and RAM headroom.

    Floors rather than rounds: going over the core budget oversubscribes, and the
    penalty for one worker too many is larger than the gain from one too few.
    """
    if cores_per_cell <= 0 or cores_total <= 0:
        return 1
    budget = cores_total * max(0.0, cpu_target_pct) / 100.0
    by_cpu = int(budget // cores_per_cell)

    limit = by_cpu
    if ram_available_mb is not None and ram_per_cell_mb and ram_per_cell_mb > 0:
        # RAM is a hard wall where CPU is only a slowdown, so it always wins.
        limit = min(limit, int(ram_available_mb // ram_per_cell_mb))

    return max(1, min(limit, hard_cap))


def damped_step(current: int, target: int, max_step: int = 2) -> int:
    """Move `current` toward `target` a little at a time.

    Occupancy is measured UNDER the worker count in force, so a reading taken with
    four workers does not prove what twelve would do -- adding workers adds
    contention and pushes per-cell occupancy down. Stepping means the next batch of
    samples re-measures reality before the next move, instead of committing to an
    extrapolation.
    """
    if target == current:
        return current
    step = min(max_step, abs(target - current))
    return current + step if target > current else current - step
