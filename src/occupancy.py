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

The tracker records four things per finished cell: CPU occupancy, GPU busy share,
peak resident set, and the moment it finished. CPU alone cannot see the two ways a
run actually goes wrong -- RAM crossing into swap (measured: 93% of 31.4 GB at 1:1
cell_size=8) and throughput going flat while per-cell time inflates (measured on
Berlin: 21-23 cells/min identical at 8 and at 24 workers, while the median cell went
21.1 s -> 63.5 s). The RSS and throughput windows exist so a governor can see both.

Everything here is a pure function of recorded samples so it can be tested without
running a render.
"""
from __future__ import annotations

import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

# Never recommend more than this regardless of the arithmetic; matches WorkerPool.
DEFAULT_HARD_CAP = 64

# Ignore samples shorter than this: a cell that failed in a second, or one that was
# almost entirely a cached-tile read, says nothing about steady-state occupancy.
MIN_SAMPLE_WALL_S = 1.5

# How many recent cells the estimate is based on. Short enough to follow a change of
# scale mid-project, long enough that one slow disk moment does not move it.
WINDOW = 8

# The short window. Contention from a worker-count change shows up in the very next
# cells; three is the fewest that still has a median rather than a coin flip.
RECENT_WINDOW = 3

# Peak-RSS history. Longer than WINDOW because the p95 it feeds is an admission
# wall: it should remember the heaviest cell of the last while, not of the last few.
RSS_WINDOW = 32

# Completion timestamps kept, and the rolling wall they are read over. Five minutes
# spans several 1:1 cells (median 21-124 s) yet is short enough that a rate change
# after a resize shows up within a minute or two.
THROUGHPUT_WINDOW = 256
THROUGHPUT_WINDOW_S = 300.0


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile of a non-empty list.

    Nearest-rank, not interpolated: with 8-32 samples an interpolated p95 lands
    between the top two readings and so understates the peak this exists to fence.
    """
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = -(-len(ordered) * round(pct * 100) // 10000)  # ceil, no float rounding
    return ordered[min(max(rank, 1), len(ordered)) - 1]


@dataclass
class OccupancyTracker:
    """Rolling record of what each finished cell actually used: CPU cores, the
    fraction of its wall the shared GPU was busy on its behalf, its peak resident
    set, and when it finished."""

    samples: list[float] = field(default_factory=list)
    gpu_samples: list[float] = field(default_factory=list)
    rss_samples: deque[float] = field(
        default_factory=lambda: deque(maxlen=RSS_WINDOW)
    )
    completions: deque[float] = field(
        default_factory=lambda: deque(maxlen=THROUGHPUT_WINDOW)
    )
    # Injectable so throughput can be tested without sleeping.
    clock: Callable[[], float] = time.monotonic

    def reset(self) -> None:
        """Forget everything measured so far.

        Call this on run start and whenever the scale bucket changes. Without it the
        window carries 1:20 cells (~1.02 cores each) into a 1:1 run (~7.75), and the
        median between the two describes no cell that exists -- the pool then sizes
        itself for the average of two different machines' worth of work.
        """
        self.samples.clear()
        self.gpu_samples.clear()
        self.rss_samples.clear()
        self.completions.clear()

    def record(
        self,
        cpu_seconds: float,
        wall_seconds: float,
        gpu_seconds: float = 0.0,
        peak_rss_mb: float | None = None,
    ) -> None:
        """Record one finished cell. `cpu_seconds` is total processor time across all
        of that process's threads, so `cpu/wall` is the mean number of busy cores.
        `gpu_seconds` is the wall the cell spent inside GPU dispatches (arnis reports
        it; 0 when the GPU is off), so `gpu/wall` is that worker's share of the ONE
        shared adapter. `peak_rss_mb` is that process's peak working set, if known.

        The completion timestamp is taken for EVERY call, including cells too short
        to say anything about occupancy: a cached cell that finished in a second is
        still throughput, and dropping it would make cells_per_min read low exactly
        when the run is going fastest. The CPU, GPU and RSS windows keep the
        MIN_SAMPLE_WALL_S gate, because those describe steady-state work.
        """
        self.completions.append(self.clock())

        if wall_seconds < MIN_SAMPLE_WALL_S:
            return

        # RAM is recorded independently of CPU time, and deliberately BEFORE the
        # `cpu_seconds <= 0` gate. A cell that reported no processor time says
        # nothing about occupancy, but its peak working set was still a real 4 GB
        # that the next admission has to fit beside -- and cpu_seconds is the
        # likelier of the two to come back missing, since callers coerce an absent
        # reading to 0.0. Dropping a known RSS because an unrelated field was
        # unavailable would silently lower the only wall that matters.
        if peak_rss_mb is not None and peak_rss_mb > 0:
            self.rss_samples.append(float(peak_rss_mb))

        if cpu_seconds <= 0:
            return
        self.samples.append(cpu_seconds / wall_seconds)
        del self.samples[:-WINDOW]
        self.gpu_samples.append(max(0.0, gpu_seconds) / wall_seconds)
        del self.gpu_samples[:-WINDOW]

    @property
    def cores_per_cell(self) -> float | None:
        """Median cores a cell keeps busy, or None until there is enough to say.

        Median rather than mean: the first cell of a run pays cold caches and would
        drag a mean down for the rest of the project.
        """
        if len(self.samples) < 2:
            return None
        return statistics.median(self.samples)

    @property
    def cores_per_cell_recent(self) -> float | None:
        """The same median over only the last RECENT_WINDOW cells, or None.

        `cores_per_cell` averages over eight cells, so after a resize it keeps
        reporting the old contention for most of a window. This one turns over in
        three: when it sits well below `cores_per_cell`, the added workers are
        taking cores from each other rather than finding new work.
        """
        recent = self.samples[-RECENT_WINDOW:]
        if len(recent) < 2:
            return None
        return statistics.median(recent)

    @property
    def gpu_fraction_per_cell(self) -> float | None:
        """Median share of one worker's wall spent on the GPU, or None."""
        if len(self.gpu_samples) < 2:
            return None
        return statistics.median(self.gpu_samples)

    @property
    def rss_median_mb(self) -> float | None:
        """Median peak working set of a cell, in MB, or None.

        This is the one to multiply by a worker count when asking "would another N
        workers fit"; the p95 is the one to fence a single admission against.
        """
        if not self.rss_samples:
            return None
        return statistics.median(self.rss_samples)

    @property
    def rss_p95_mb(self) -> float | None:
        """95th-percentile peak working set of a cell, in MB, or None.

        Answers on the FIRST sample rather than waiting for two like the CPU
        properties do. RAM is a wall, not a slowdown: one observed 4 GB cell is
        already reason enough not to admit a worker into 2 GB of headroom, whereas
        one observed occupancy reading is not reason enough to resize a pool.
        """
        if not self.rss_samples:
            return None
        return _percentile(list(self.rss_samples), 95.0)

    # Short aliases; callers read the tracker as `.rss_p95` / `.rss_median` while the
    # governor snapshot spells the same numbers with their unit.
    @property
    def rss_p95(self) -> float | None:
        return self.rss_p95_mb

    @property
    def rss_median(self) -> float | None:
        return self.rss_median_mb

    @property
    def cells_per_min(self) -> float | None:
        """Finished cells per minute over the last THROUGHPUT_WINDOW_S, or None.

        Measured as (n-1) completions across the wall between the first and the last
        of them, not as n across the wall since the run began: the first form is the
        rate the machine is holding now, the second is dragged down forever by a slow
        first cell. None until two completions land inside the window.

        This is the number that says whether a resize did anything. On Berlin it sat
        at 21-23 cells/min for every worker count from 8 to 24 while the median cell
        time tripled -- flat throughput with rising per-cell time IS the knee.
        """
        if len(self.completions) < 2:
            return None
        cutoff = self.completions[-1] - THROUGHPUT_WINDOW_S
        recent = [t for t in self.completions if t >= cutoff]
        if len(recent) < 2:
            return None
        span = recent[-1] - recent[0]
        if span <= 0:
            return None
        return (len(recent) - 1) / span * 60.0


def suggest_workers(
    cores_per_cell: float,
    cores_total: int,
    cpu_target_pct: float = 90.0,
    ram_available_mb: float | None = None,
    ram_per_cell_mb: float | None = None,
    hard_cap: int = DEFAULT_HARD_CAP,
    gpu_fraction_per_cell: float | None = None,
    gpu_target_pct: float = 95.0,
) -> int:
    """How many cells to run at once, from measured occupancy and headroom.

    Three independent budgets; the tightest wins:
    - CPU:  cores_total * cpu_target_pct / cores_per_cell
    - RAM:  a hard wall where CPU is only a slowdown, so it always clamps
    - GPU:  workers share ONE adapter, so each worker's measured busy fraction
            stacks; gpu_target_pct / fraction caps how many fit. Measured today a
            1:1 cell keeps the 5080 ~1% busy, so this clamp is a safety valve for
            weaker adapters, not a daily limiter.

    Floors rather than rounds: going over a budget oversubscribes, and the penalty
    for one worker too many is larger than the gain from one too few.
    """
    if cores_per_cell <= 0 or cores_total <= 0:
        return 1
    budget = cores_total * max(0.0, cpu_target_pct) / 100.0
    limit = int(budget // cores_per_cell)

    if ram_available_mb is not None and ram_per_cell_mb and ram_per_cell_mb > 0:
        limit = min(limit, int(ram_available_mb // ram_per_cell_mb))

    if gpu_fraction_per_cell is not None and gpu_fraction_per_cell > 0.0:
        limit = min(limit, int((gpu_target_pct / 100.0) / gpu_fraction_per_cell))

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
