"""Worker/thread scheduling that converges on MEASURED DELIVERED THROUGHPUT.

`occupancy.py` answers "how many cells FIT in the CPU/RAM/GPU budget" from measured
core occupancy. That is a budget, not a schedule: it assumes a cell's occupancy stays
the same however many cells run beside it. A real 1:1 run says otherwise.

Measured, Berlin, scale 1:1, cell_size 4, 24 logical cores (8P+16E), workers raised
live mid-run:

    workers      8      12      16      20      24
    median cell  21.1s  33.9s   41.6s   56.5s   63.5s
    cells/min    ~22.7  ~21.2   ~23.1   ~21.2   ~22.7

Throughput is FLAT from 8 to 24 workers. Every worker added past ~8-12 bought nothing
and inflated each cell by up to 3x - a project that looks like it is doing more while
delivering the same, with ram_peak walking up to 93% (one eviction storm from swap).
So the only honest control variable is delivered cells/min, and the only honest way to
find the knee is to walk to it and measure, which is what this module does.

Three jobs:

  * threads_for_next_cell - split the core budget per cell. The old formula
    `max(min_threads_per_worker, core_budget // workers)` had a FLOOR of 4, so 24
    workers each asked rayon for 4 threads: 96 threads on 24 cores. Here the
    per-worker share is the SENIOR bound and the floor is 2 - never 1, because a
    single-threaded arnis is a different (much slower) machine, and the whole point
    of the governor is that adding workers past the knee buys nothing.
  * admit - hold a worker at the gate while RAM is tight, so the pool cannot outrun
    the memory. RAM ONLY: a near-100% CPU is the GOAL of a CPU-bound render, not a
    reason to wait. Bounded: never longer than timeout_s (3 s), the first worker is
    never held, and the seconds waited are reported back so the caller can charge
    them into the cell sample - otherwise the gate's own cost is invisible to the
    metric the governor optimises.
  * on_cell_complete - the state machine: CALIBRATE low, CONVERGE by +2 while the
    marginal gain is worth it, then STEADY, and re-open (RECAL) if reality drifts.
    Samples carry the worker level they LAUNCHED under; a completion from the
    previous level is dropped rather than credited to the new one.

Everything is a pure function of recorded samples plus one optional psutil probe
(available RAM, swappable on the instance for tests), so the whole machine is testable
without running a render. No flask, no I/O beyond the probe and the settings read.

Byte determinism: governor_mode="off" (the default) returns the LEGACY thread
formula, never gates admission, and never resizes the pool. governor_mode="advise"
returns the SAME legacy pair - it observes and reports, so the child environment it
produces is byte-identical to "off". Only "auto" schedules. Nothing here changes what
arnis writes; it only changes how many arnis processes run and with what env.

Thread safety: N worker threads call admit/threads_for_next_cell/on_cell_complete
while the flask request thread calls snapshot/advice/freeze/recalibrate. One RLock
guards every read-modify-write of the sample windows and the state machine. It is
NEVER held across the admit() sleep.
"""
from __future__ import annotations

import math
import os
import statistics
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .occupancy import (
    DEFAULT_HARD_CAP,
    OccupancyTracker,
    damped_step,
    suggest_workers,
)

# ---------------------------------------------------------------- tunables ----

#: Cells shorter than this say nothing about steady state (a failure, an all-cache
#: read). Higher than occupancy.MIN_SAMPLE_WALL_S on purpose: a throughput decision
#: is worth more evidence than an occupancy estimate.
MIN_SAMPLE_WALL_S = 2.0

#: Completed cells required at a worker level before its throughput is believed.
STEP_SAMPLES = 3

#: Rolling window (cells) behind cells_per_min and the STEADY drift check.
WINDOW_CELLS = 6

#: Under this many cells the ramp costs more than it can repay - a 20-cell grid is
#: over before CALIBRATE finishes. Use the static/persisted worker count.
SMALL_GRID_CELLS = 32

#: Hill-climb step. Matches occupancy.damped_step's default: measure, then move.
CLIMB_STEP = 2

#: Fraction of current throughput a +2 step must buy to justify the next one.
#: RELATIVE, not absolute: the first Bucharest A/B settled at 6 workers because each
#: step bought ~0.44 cells/min - under the old absolute 0.5 floor - while the climb
#: from 6 to 16 was still worth +19% in total. A percentage cannot mistake "small
#: steps on a curve that is still rising" for a plateau the way a flat number does.
GAIN_MIN_FRAC_SMALL = 0.03
GAIN_MIN_FRAC_LARGE = 0.02
GAIN_TAPER_WORKERS = 8

#: An absolute floor under the relative test, so a near-zero-throughput start cannot
#: make every step look significant.
GAIN_MIN_ABS = 0.15

#: Consecutive non-paying steps required before the climb stops. One sample is noise:
#: the same A/B stepped 4 -> 6 -> 8 and unwound to 6 on a single bad measurement.
STOP_STRIKES = 2

#: While the CPU budget is this far from spent, a step that merely fails to pay is a
#: strike, never an immediate stop. At 71% CPU and 51% RAM nothing should have settled
#: at 6 of 24 cores - there was budget left and the stop rules did not know it.
BUDGET_SPENT_CPU_PCT = 88.0

#: Contention signature: if a step drops measured cores-per-cell below this fraction
#: of the previous step's, the new workers are mostly waiting on each other.
CONTENTION_RATIO = 0.6

#: STEADY re-opens when throughput sits this far off its baseline this many cells.
DRIFT_PCT = 0.25
DRIFT_CELLS = 5

#: Warm-start re-check budget: cells judged against the PERSISTED throughput before
#: the baseline is re-anchored to what this run is actually delivering.
RECHECK_CELLS = 12

#: Admission gates on RAM ONLY. There is deliberately no CPU gate: a render that has
#: pinned the CPU is a render that is working, and holding workers back because the
#: machine is busy is how the governor used to pay ~35% of its throughput for a metric
#: that could not see the bill (the wait happened before the cell's own clock started).
#: The timeout is short for the same reason - 3 s is long enough for a flush to land,
#: short enough that a mis-read probe costs a rounding error rather than a run.
ADMIT_POLL_S = 0.25
ADMIT_TIMEOUT_S = 3.0

#: Settings cache lifetime, in the sense of "revalidate against this stamp". See _cfg.
CFG_STAMP_NONE = object()

#: Where CALIBRATE starts. Low on purpose: climbing is cheap, unwinding is not.
CALIBRATE_START = 4

MODES = ("off", "advise", "auto")
#: The states in which the hill climb is still moving. Reporting only: admission no
#: longer treats them differently (there is nothing left to treat differently - the
#: only gate is RAM, and RAM binds the same whatever the state machine is doing).
RAMP_STATES = ("CALIBRATE", "CONVERGE", "RECAL")


# ---------------------------------------------------------------- helpers -----

def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def _p95(values: list[float]) -> float | None:
    """95th percentile, nearest-rank. Peak RSS is what decides whether a worker fits,
    and the mean of peaks is not a peak."""
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil(0.95 * len(ordered))
    return ordered[max(0, rank - 1)]


def bucket_key(scale: float, cell_size: int) -> str:
    """History key: what a cell COSTS is set by its scale band and its size, not by
    where on Earth it is. 1:1/4 and 1:10+/4 are different machines' worth of work."""
    if scale >= 0.5:
        band = "1:1"
    elif scale >= 0.1:
        band = "1:2..1:9"
    else:
        band = "1:10+"
    return f"{band}/{int(cell_size)}"


def _psutil():
    try:  # optional dependency: absent = degrade to "never gate"
        import psutil  # noqa: PLC0415

        return psutil
    except Exception:  # noqa: BLE001
        return None


def _available_mb() -> float | None:
    ps = _psutil()
    if ps is None:
        return None
    try:
        return float(ps.virtual_memory().available) / (1024.0 * 1024.0)
    except Exception:  # noqa: BLE001
        return None


class Admission(str):
    """An admission verdict that also carries what it cost.

    It IS a str ("go", "go(steady)", "go(timeout)"), so every existing consumer -
    comparisons, logging, the `Callable[[int, int], str]` on the pool - keeps working
    unchanged. `.gate_s` is the seconds the worker actually spent held at the gate,
    which the caller charges into that cell's sample so the optimised metric can see
    the gate's own cost. Callers that discard the return value can read the same
    number back later with `Governor.take_gate_s(worker_id)`.
    """

    gate_s: float = 0.0

    def __new__(cls, verdict: str, gate_s: float = 0.0) -> "Admission":
        self = super().__new__(cls, verdict)
        self.gate_s = float(gate_s)
        return self


@dataclass
class GovernorSnapshot:
    """One flat, JSON-safe view of the governor for /api/status and /api/governor."""

    mode: str
    state: str
    workers: int
    target: int
    threads: int
    flush: int
    cores_per_cell: float | None
    rss_p95_mb: float | None
    cells_per_min: float | None
    binding: str
    samples: int
    note: str


class Governor:
    """Sizes the worker pool and each worker's threads from what cells actually deliver.

    Lifecycle, per render run::

        gov.begin_run(total_cells=N, scale=s, cell_size=c, ceiling=POOL.max_workers)
        POOL.set_max_workers(gov.snapshot().target)      # apply the opening move
        ...per cell:
            gov.admit(worker_id=i, active=POOL.active)
            rayon, flush = gov.threads_for_next_cell(workers=POOL.max_workers)
            new = gov.on_cell_complete(...)
            if new: POOL.set_max_workers(new)             # auto mode only
        persist = gov.end_run()

    `threads_for_next_cell` is also how the governor learns the pool's REAL size: it
    is called with the count in force, so an unapplied advisory target never corrupts
    the measurements (samples are re-anchored whenever the real count changes).
    """

    def __init__(
        self,
        cores: int,
        get_settings: Callable[[], dict],
        log: Callable[[str], None] = lambda s: None,
        settings_stamp: Callable[[], object] | None = None,
    ) -> None:
        """`settings_stamp`, when given, is a cheap "have the settings changed" probe -
        in Meld, `lambda: os.path.getmtime(PROJECT.json_path)`. With it, `_cfg()`
        serves a cached dict until the stamp moves, which is what keeps the admission
        loop and the per-cell reads off the JSON file. Without it nothing is cached
        and every read is live, exactly as before."""
        self.cores = max(1, int(cores or 1))
        self._get_settings = get_settings
        self._settings_stamp = settings_stamp
        self._log = log
        # One lock for the whole state machine and every sample window. RLock because
        # snapshot() -> cells_per_min -> _tp all want it and re-entering is normal.
        self._lock = threading.RLock()
        self._cfg_cache: dict | None = None
        self._cfg_stamp: object = CFG_STAMP_NONE
        self._gate_s: dict[int, float] = {}
        # Test seams: swapped wholesale in tests so no probe, sleep or clock is real.
        self._available_mb_probe: Callable[[], float | None] = _available_mb
        self._sleep: Callable[[float], None] = time.sleep
        self._now: Callable[[], float] = time.monotonic
        self._wallclock: Callable[[], float] = time.time
        self.mode = "off"
        self.state = "OFF"
        self.bucket = ""
        self.workers = 1
        self.target = 1
        self.ceiling = 1
        self._reset_run()

    # ------------------------------------------------------------- settings ---

    def _cfg(self) -> dict:
        """The live project settings, cached behind the stamp probe when there is one.

        PROJECT.settings() is a JSON read off disk. The admission poll loop used to
        call it once per 0.25 s poll - ~1150 reads per blocked cell at 24 workers -
        and a read that lands mid-write falls back to defaults, which made the
        governor briefly see governor_mode="off". admit() now reads it ONCE per call
        and passes the dict down; this cache removes the rest.
        """
        if self._settings_stamp is not None:
            try:
                stamp = self._settings_stamp()
            except Exception:  # noqa: BLE001
                stamp = CFG_STAMP_NONE
            with self._lock:
                if (self._cfg_cache is not None and stamp is not CFG_STAMP_NONE
                        and stamp == self._cfg_stamp):
                    return self._cfg_cache
            fresh = self._read_settings()
            with self._lock:
                self._cfg_cache, self._cfg_stamp = fresh, stamp
            return fresh
        return self._read_settings()

    def _read_settings(self) -> dict:
        try:
            return self._get_settings() or {}
        except Exception:  # noqa: BLE001 - a broken settings read must not stop a render
            return {}

    def _cpu_target_pct(self, cfg: dict, *, clamped: bool = True) -> float:
        """Unified fallback: 90, not 100. Two call sites used to disagree, and the 100
        one handed out a core budget the OS never actually had spare.

        `clamped=False` is the LEGACY read, used only by governor_mode="off"/"advise".
        The pre-governor code took this number raw and documented >100 as deliberate
        oversubscription, so clamping it to 95 would have quietly re-tuned a
        governor-OFF install (cpu_target_pct 120 gave 28 threads at one worker, and
        the clamp turns that into 22). The governed paths keep the 10..95 clamp.
        """
        try:
            pct = float(cfg.get("cpu_target_pct") or 90)
        except (TypeError, ValueError):
            pct = 90.0
        if not clamped:
            return pct
        return float(_clamp(int(pct), 10, 95))

    def _ram_headroom_mb(self, cfg: dict) -> int:
        try:
            return _clamp(int(cfg.get("ram_headroom_mb") or 2048), 512, 8192)
        except (TypeError, ValueError):
            return 2048

    def _flush_cap(self, cfg: dict) -> int:
        try:
            return _clamp(int(cfg.get("flush_threads_cap") or 12), 1, 24)
        except (TypeError, ValueError):
            return 12

    def _governor_max_workers(self, cfg: dict) -> int:
        try:
            return _clamp(int(cfg.get("governor_max_workers") or 0), 0, DEFAULT_HARD_CAP)
        except (TypeError, ValueError):
            return 0

    def _resolve_mode(self, cfg: dict) -> str:
        """settings -> mode, with MELD_GOVERNOR as the last word.

        The env var exists so a run can be pinned to legacy scheduling without editing
        a project's settings (MELD_GOVERNOR=off); any valid value overrides.
        `worker_autoscale` migrates only when `governor_mode` was never written: an
        explicit "off" in settings beats a legacy opt-in.
        """
        if "governor_mode" in cfg:
            mode = str(cfg.get("governor_mode") or "off").strip().lower()
        else:
            mode = "auto" if cfg.get("worker_autoscale") else "off"
        if mode not in MODES:
            mode = "off"
        env = str(os.environ.get("MELD_GOVERNOR", "")).strip().lower()
        if env in MODES:
            mode = env
        return mode

    # ------------------------------------------------------------ run state ---

    def _reset_run(self) -> None:
        self._occ = OccupancyTracker()
        self._walls: list[float] = []        # rolling, for cells_per_min + drift
        self._step_walls: list[float] = []   # since the current worker level began
        self._rss: list[float] = []
        self._samples = 0
        self._stale_samples = 0     # completions dropped as belonging to an old level
        self._gate_s.clear()
        self._static = False
        self._prev_tp: float | None = None
        self._prev_workers = 0
        self._prev_cpc: float | None = None
        self._strikes = 0
        self._steady_tp: float | None = None
        self._drift_run = 0
        self._recheck_left = 0
        self._binding = "none"
        self._note = ""
        self._total_cells = 0

    def begin_run(self, *, total_cells: int, scale: float, cell_size: int, ceiling: int) -> None:
        cfg = self._cfg()
        with self._lock:
            self._begin_run_locked(cfg, total_cells=total_cells, scale=scale,
                                   cell_size=cell_size, ceiling=ceiling)

    def _begin_run_locked(self, cfg: dict, *, total_cells: int, scale: float,
                          cell_size: int, ceiling: int) -> None:
        self._reset_run()
        self.mode = self._resolve_mode(cfg)
        self.bucket = bucket_key(float(scale or 1.0), int(cell_size or 1))
        self._total_cells = max(0, int(total_cells or 0))

        explicit = self._governor_max_workers(cfg)
        base = max(1, int(ceiling or 1))
        self.ceiling = _clamp(explicit or base, 1, DEFAULT_HARD_CAP)

        if self.mode == "off":
            self.state = "OFF"
            self.workers = self.target = base
            self._note = "legacy scheduling (governor off)"
            return

        if self.mode == "advise":
            # Watch and report only. The pool keeps whatever the user set; the
            # recommendation shows up in snapshot().target and advice().
            self.state = "STEADY"
            self._static = True
            self.workers = self.target = _clamp(base, 1, self.ceiling)
            self._note = "advisory: measuring, not resizing"
            return

        warm = self._warm_start(cfg)
        if warm is not None:
            self.workers = self.target = warm
            self.state = "STEADY"
            self._recheck_left = RECHECK_CELLS
            self._binding = "history"
            self._note = f"warm start {warm}w from {self.bucket}"
            self._log(f"  [Governor] warm start {warm} workers from history {self.bucket}")
            return

        if self._total_cells < SMALL_GRID_CELLS:
            # Too short to pay for a ramp: a 4-cell grid would still be calibrating
            # when it finished. Run the stored count and stay out of the way.
            self.state = "STEADY"
            self._static = True
            self.workers = self.target = _clamp(base, 1, self.ceiling)
            self._note = f"small grid ({self._total_cells} cells): static {self.workers}w"
            return

        self.state = "CALIBRATE"
        self.workers = self.target = _clamp(min(CALIBRATE_START, self.ceiling), 1, self.ceiling)
        self._note = f"calibrating from {self.workers}w"
        self._log(f"  [Governor] calibrating from {self.workers} workers (ceiling {self.ceiling})")

    def _warm_start(self, cfg: dict) -> int | None:
        """Last run's converged count for this bucket, clamped to the ceiling and
        shrunk until this machine's CURRENT free RAM actually holds it.

        The RAM check is the total form (w * rss_p95 + headroom), not the incremental
        one admit() uses: at t=0 nothing is resident yet, so the whole pool has to fit
        in what is free now.
        """
        history = cfg.get("governor_history") or {}
        entry = history.get(self.bucket) if isinstance(history, dict) else None
        if not isinstance(entry, dict):
            return None
        try:
            w = int(entry.get("workers") or 0)
        except (TypeError, ValueError):
            return None
        if w <= 0:
            return None
        w = _clamp(w, 1, self.ceiling)
        try:
            rss = float(entry.get("rss_p95_mb") or 0.0)
        except (TypeError, ValueError):
            rss = 0.0
        avail = self._available_mb_probe()
        if avail is not None and rss > 0:
            headroom = self._ram_headroom_mb(cfg)
            while w > 1 and avail < (w * rss + headroom):
                w -= 1
        try:
            self._steady_tp = float(entry.get("cells_per_min") or 0.0) or None
        except (TypeError, ValueError):
            self._steady_tp = None
        return w

    # --------------------------------------------------------------- threads --

    def threads_for_next_cell(self, *, workers: int) -> tuple[int, int]:
        """(RAYON_NUM_THREADS, ARNIS_FLUSH_THREADS) for the next cell.

        Clamp precedence, senior first::

            core_budget = max(1, floor(cores * cpu_target_pct / 100))
            rayon_upper = max(2, round(core_budget / workers))     # SENIOR
            rayon       = clamp(ceil(1.25 * cores_per_cell), 1, rayon_upper)
                          or clamp(core_budget // workers, 1, 4) with no measurement
            flush       = clamp(ceil(flush_threads_cap / workers), 1, cap)

        The 1.25 is deliberate slack over measured occupancy: a cell alternates
        parallel and serial phases, so asking for exactly its mean core count starves
        the parallel ones.

        rayon_upper never goes below 2. `core_budget // workers` collapsed to 1 the
        moment the pool passed the budget (21 // 11 == 1), so every cell from 11
        workers up ran single-threaded rayon - at exactly the 8..12 knee the governor
        converges on. It is a share, so it rounds rather than truncating.

        flush is a TOTAL, not a per-worker allowance: `cap // (workers // 4)` handed
        every worker the whole cap below 8 workers, i.e. 4 x 12 = 48 compression
        threads on 24 cores during the first calibration cells. Dividing by the worker
        count keeps the sum near the cap instead of multiplying by it.

        Exact saturation points on this box (24 cores, cpu_target_pct 90 -> core
        budget floor(24 * 0.9) = 21, flush cap 12):

            workers      1   2   4   6   8  10  12  14  15  21  24
            rayon_upper 21  10   5   4   3   2   2   2   2   2   2
            flush       12   6   3   2   2   2   1   1   1   1   1

        rayon_upper reaches its floor of 2 at 15 workers (round(21/15) = 1, so the
        floor binds); from 9 workers up it is already 2 by arithmetic. flush reaches
        its floor of 1 at 12 workers, i.e. at `workers >= cap`.
        """
        workers = max(1, int(workers or 1))
        if self.mode != "off":
            with self._lock:
                self._observe_workers(workers)
        return self._threads(workers)

    def _threads(self, workers: int, cfg: dict | None = None) -> tuple[int, int]:
        cfg = self._cfg() if cfg is None else cfg

        if self.mode != "auto":
            # Legacy formula, byte-for-byte with server.py's pre-governor block, for
            # BOTH "off" and "advise": advise observes and reports, it does not
            # schedule, so the child env it produces has to be identical to off's.
            # cpu_target_pct is read RAW here - the legacy code documented >100 as
            # deliberate oversubscription and never clamped it.
            legacy_budget = max(1, int(self.cores * self._cpu_target_pct(cfg, clamped=False) / 100.0))
            raw = cfg.get("min_threads_per_worker", 4)
            try:
                # `or 1`, not `or 4`: legacy turned a falsy/blank setting into 1.
                min_threads = max(1, int(raw) if raw else 1)
            except (TypeError, ValueError):
                min_threads = 1
            rayon = max(min_threads, legacy_budget // max(1, workers))
            return rayon, max(2, min(6, rayon // 2))

        pct = self._cpu_target_pct(cfg)
        core_budget = max(1, int(self.cores * pct / 100.0))
        rayon_upper = max(2, int(round(core_budget / workers)))
        cpc = self.cores_per_cell
        if cpc:
            rayon = _clamp(int(math.ceil(1.25 * cpc)), 1, rayon_upper)
        else:
            rayon = _clamp(core_budget // workers, 2, min(4, rayon_upper))

        cap = self._flush_cap(cfg)
        flush = _clamp(int(math.ceil(cap / max(1, workers))), 1, cap)
        return rayon, flush

    def _observe_workers(self, workers: int) -> None:
        """The pool is the source of truth for how many cells are in flight.

        When it changes - because auto mode applied a target, or because the user
        moved the slider mid-run - every sample taken under the old count stops
        describing the present, so the windows are re-anchored rather than averaged
        across two different machines' worth of contention.
        """
        if workers == self.workers:
            return
        self.workers = workers
        if self.state in ("OFF", "STEADY"):
            self.target = workers
        self._occ = OccupancyTracker()
        self._walls = []
        self._step_walls = []
        self._drift_run = 0

    # ------------------------------------------------------------- admission --

    def _gate(self, cfg: dict) -> tuple[bool, str]:
        """(may a worker start now, what is holding it). RAM, and only RAM.

        There is no CPU gate. A render that has pinned the CPU is a render that is
        doing its job; holding workers out because the machine is busy made a 1:1 run
        - which sits at ~100% CPU by definition - wait the full timeout on EVERY cell.
        """
        avail = self._available_mb_probe()
        if avail is None:
            return True, ""          # no psutil: never invent a reason to wait
        with self._lock:
            need = (self.rss_p95_mb or 0.0) + self._ram_headroom_mb(cfg)
        if avail < need:
            return False, "ram"
        return True, ""

    def take_gate_s(self, worker_id: int) -> float:
        """Seconds worker `worker_id` last spent held at the gate, and clear it.

        For callers that cannot use the `Admission.gate_s` on admit()'s return value
        (the pool's admit_cb discards it). Charge the number into that worker's next
        on_cell_complete(gate_s=...) so the throughput metric pays for its own gate.
        """
        with self._lock:
            return float(self._gate_s.pop(int(worker_id), 0.0))

    def admit(self, *, worker_id: int, active: int,
              timeout_s: float = ADMIT_TIMEOUT_S) -> Admission:
        """Hold a worker at the gate, briefly, when starting it now would OOM.

        Returns an Admission (a str: "go", "go(steady)" = pass-through, or
        "go(timeout)") whose `.gate_s` is how long the worker was actually held.
        NEVER returns later than timeout_s: a governor that can stall the pool is
        worse than a governor that guesses. active == 0 is always admitted - the
        machine must make progress even when the RAM probe is unhappy. Only "auto"
        ever gates: "advise" watches, it does not touch the run.

        The settings are read ONCE here and passed into every poll. The loop must
        never hold the lock across a sleep.
        """
        if self.mode != "auto" or self.state in ("OFF", "FROZEN"):
            return self._admitted(worker_id, "go(steady)", 0.0)
        if int(active or 0) <= 0:
            return self._admitted(worker_id, "go", 0.0)
        cfg = self._cfg()
        started = self._now()
        ok, why = self._gate(cfg)
        if ok:
            return self._admitted(
                worker_id, "go(steady)" if self.state == "STEADY" else "go", 0.0)
        deadline = started + max(0.0, float(timeout_s))
        while True:
            remaining = deadline - self._now()
            if remaining <= 0:
                waited = max(0.0, self._now() - started)
                with self._lock:
                    self._note = f"worker {worker_id} admitted on timeout ({why})"
                return self._admitted(worker_id, "go(timeout)", waited)
            self._sleep(min(ADMIT_POLL_S, remaining))
            ok, why = self._gate(cfg)
            if ok:
                return self._admitted(worker_id, "go", max(0.0, self._now() - started))

    def _admitted(self, worker_id: int, verdict: str, waited: float) -> Admission:
        if waited > 0:
            with self._lock:
                self._gate_s[int(worker_id)] = float(waited)
        return Admission(verdict, waited)

    # ---------------------------------------------------------- measurement ---

    @property
    def cores_per_cell(self) -> float | None:
        with self._lock:
            return self._occ.cores_per_cell

    @property
    def rss_p95_mb(self) -> float | None:
        with self._lock:
            return _p95(list(self._rss))

    def _tp(self, walls: list[float]) -> float | None:
        """Delivered cells/min at the CURRENT worker count.

        Derived from the median cell wall rather than counted off the clock so it is
        immune to how long the caller sat between cells, and so a stall in one worker
        cannot masquerade as a throughput collapse in all of them.
        """
        if not walls:
            return None
        median = statistics.median(walls)
        if median <= 0:
            return None
        return self.workers * 60.0 / median

    @property
    def cells_per_min(self) -> float | None:
        with self._lock:
            return self._tp(list(self._walls))

    def _gain_threshold(self, workers: int, tp: float | None = None) -> float:
        """Relative to what the pool already delivers, with an absolute floor."""
        frac = (GAIN_MIN_FRAC_SMALL if workers <= GAIN_TAPER_WORKERS
                else GAIN_MIN_FRAC_LARGE)
        return max(GAIN_MIN_ABS, frac * (tp or 0.0))

    def _budget_spent(self) -> bool:
        """True when the machine is genuinely busy, so a weak step really is the knee.

        With CPU headroom left, a step that does not pay is far more likely to be a
        noisy sample than a wall, so it costs a strike instead of ending the climb."""
        pct = self._cpu_pct_now()
        return pct is not None and pct >= BUDGET_SPENT_CPU_PCT

    @staticmethod
    def _cpu_pct_now() -> float | None:
        try:
            import psutil
        except Exception:  # noqa: BLE001 - psutil is optional everywhere else too
            return None
        try:
            return float(psutil.cpu_percent(interval=None))
        except Exception:  # noqa: BLE001
            return None

    def on_cell_complete(
        self,
        *,
        wall_s: float,
        cpu_s: float,
        peak_rss_mb: float | None,
        gpu_s: float,
        ok: bool,
        gate_s: float = 0.0,
        launched_workers: int | None = None,
    ) -> int | None:
        """Record a finished cell; return a NEW worker target, or None to hold.

        Failed cells and cells under MIN_SAMPLE_WALL_S are dropped: a crash at three
        seconds is evidence about arnis, not about how many should run at once.

        `gate_s` is the time this worker spent held in admit() before the cell
        started. It is added to the wall the THROUGHPUT metric uses, because a gate
        that delays a cell has slowed delivery by exactly that much and the governor
        must be able to see the cost of its own gate. It is deliberately NOT added to
        the occupancy wall: cores_per_cell is cpu_s over the time the cell actually
        ran, and inflating it would make a gated run look like a contended one.

        `launched_workers` is the worker level the cell LAUNCHED under. When the pool
        was resized while this cell was in flight, its wall describes the old level,
        not the new one - crediting it to the new level made every step look like a
        win (an 8 -> 10 step scored 28.4 against 22.7 on the measured Berlin curve
        purely because the shorter 8-worker walls were divided by 10). Such a
        completion is dropped from every window rather than mis-attributed. None
        means "untagged", which is trusted as current.
        """
        if not ok or wall_s is None or float(wall_s) < MIN_SAMPLE_WALL_S:
            return None
        wall = float(wall_s)
        gate = max(0.0, float(gate_s or 0.0))
        with self._lock:
            if launched_workers is not None and int(launched_workers) != int(self.workers):
                # Old level: keep the RSS (a peak is a peak, whatever the level, and
                # the RAM gate needs it) and drop everything the decision is made on.
                if peak_rss_mb:
                    self._rss.append(float(peak_rss_mb))
                    del self._rss[:-WINDOW_CELLS * 4]
                self._stale_samples += 1
                return None
            self._occ.record(float(cpu_s or 0.0), wall, gpu_seconds=float(gpu_s or 0.0))
            if peak_rss_mb:
                self._rss.append(float(peak_rss_mb))
                del self._rss[:-WINDOW_CELLS * 4]
            self._walls.append(wall + gate)
            del self._walls[:-WINDOW_CELLS]
            self._step_walls.append(wall + gate)
            self._samples += 1

            if self.mode != "auto" or self.state in ("OFF", "FROZEN") or self._static:
                return None
            if self.state in ("CALIBRATE", "RECAL"):
                return self._on_calibrate()
            if self.state == "CONVERGE":
                return self._on_converge()
            return self._on_steady()

    # -------------------------------------------------------- state machine ---

    def _step_tp(self) -> float | None:
        return self._tp(self._step_walls)

    def _remember_step(self, tp: float | None) -> None:
        self._prev_tp = tp
        self._prev_workers = self.workers
        self._prev_cpc = self.cores_per_cell

    def _ram_fits(self, target: int) -> bool:
        """Room for the workers this step ADDS, on top of what is already resident."""
        avail = self._available_mb_probe()
        if avail is None:
            return True
        added = max(0, target - self.workers)
        need = added * (self.rss_p95_mb or 0.0) + self._ram_headroom_mb(self._cfg())
        return avail >= need

    def _apply(self, target: int) -> int | None:
        target = _clamp(target, 1, self.ceiling)
        self.target = target
        self._step_walls = []
        if target == self.workers:
            return None
        return target

    def _settle(self, binding: str, note: str, tp: float | None) -> None:
        self.state = "STEADY"
        self._binding = binding
        self._note = note
        self._steady_tp = tp
        self._drift_run = 0
        self._log(f"  [Governor] steady at {self.workers} workers ({note})")

    def _reset_strikes(self) -> None:
        self._strikes = 0

    def _on_calibrate(self) -> int | None:
        """Measure the opening level, then hand over to the hill climb."""
        if len(self._step_walls) < STEP_SAMPLES:
            return None
        tp = self._step_tp()
        self._remember_step(tp)
        if self.workers >= self.ceiling:
            self._settle("ceiling", f"at ceiling {self.ceiling}", tp)
            return None
        nxt = _clamp(damped_step(self.workers, self.workers + CLIMB_STEP, CLIMB_STEP), 1, self.ceiling)
        if not self._ram_fits(nxt):
            self._settle("ram", "RAM headroom blocks the ramp", tp)
            return None
        self.state = "CONVERGE"
        self._binding = "throughput"
        self._note = f"{tp:.1f} cells/min at {self.workers}w -> trying {nxt}w" if tp else ""
        return self._apply(nxt)

    def _on_converge(self) -> int | None:
        """One +2 step per measurement; stop the moment the step stops paying.

        The decision metric is DELIVERED cells/min, never cpu%. A 24-worker Berlin run
        pinned 79% of the CPU and delivered the same 22 cells/min as 8 workers at a
        third of the per-cell latency: cpu% would have called that a success.
        """
        if len(self._step_walls) < STEP_SAMPLES:
            return None
        tp = self._step_tp()
        prev = self._prev_tp
        if tp is None or prev is None:
            self._remember_step(tp)
            return None
        gain = tp - prev
        threshold = self._gain_threshold(self._prev_workers or self.workers, prev)

        # A step that fails to pay costs a strike; only STOP_STRIKES of them in a row
        # end the climb. A single weak sample used to settle the pool for the whole run.
        if gain < threshold:
            self._strikes += 1
            spent = self._budget_spent()
            last = self._strikes >= STOP_STRIKES or (gain < 0 and spent)
            if last:
                if gain < 0:
                    # Genuinely worse: unwind exactly one step. Whatever the last move
                    # bought, it cost more.
                    back = damped_step(self.workers, self._prev_workers or self.workers,
                                       CLIMB_STEP)
                    self._settle(
                        "throughput",
                        f"{gain:+.1f} cells/min at {self.workers}w after "
                        f"{self._strikes} strikes -> back to {back}w",
                        prev,
                    )
                    return self._apply(back)
                self._settle(
                    "throughput",
                    f"+{gain:.2f} < {threshold:.2f} cells/min for {self._strikes} "
                    f"steps - knee at {self.workers}w",
                    tp,
                )
                return None
            self._binding = "throughput"
            self._note = (f"strike {self._strikes}/{STOP_STRIKES} "
                          f"({gain:+.2f} cells/min, cpu spent={spent})")
            if gain < 0:
                # Worse, but only once. Re-measure THIS level rather than climbing
                # further into territory that just looked bad - keep the old baseline
                # so the retry is judged against the same number.
                self._step_walls = []
                return None
            # Flat-but-not-worse with CPU budget still free: far more likely noise than
            # the knee, so take one more step and let the next sample decide.
            self._remember_step(max(tp, prev))
        else:
            self._strikes = 0

        cpc, prev_cpc = self.cores_per_cell, self._prev_cpc
        if cpc and prev_cpc and cpc < CONTENTION_RATIO * prev_cpc:
            self._settle("contention", f"cores/cell {prev_cpc:.2f} -> {cpc:.2f}", tp)
            return None
        if self.workers >= self.ceiling:
            self._settle("ceiling", f"at ceiling {self.ceiling}", tp)
            return None
        nxt = _clamp(damped_step(self.workers, self.workers + CLIMB_STEP, CLIMB_STEP), 1, self.ceiling)
        if not self._ram_fits(nxt):
            self._settle("ram", "RAM headroom blocks the ramp", tp)
            return None
        self._remember_step(tp)
        self._binding = "throughput"
        self._note = f"+{gain:.2f} cells/min -> trying {nxt}w"
        return self._apply(nxt)

    def _on_steady(self) -> int | None:
        """Keep measuring. Re-open only on a sustained divergence, never on one cell."""
        tp = self.cells_per_min
        if tp is None:
            return None
        if self._steady_tp is None or self._steady_tp <= 0:
            self._steady_tp = tp
            return None
        drift = abs(tp - self._steady_tp) / self._steady_tp
        self._drift_run = self._drift_run + 1 if drift > DRIFT_PCT else 0
        if self._drift_run >= DRIFT_CELLS:
            self.state = "RECAL"
            self._step_walls = []
            self._drift_run = 0
            self._recheck_left = 0
            self._prev_tp = None
            self._strikes = 0
            self._binding = "throughput"
            self._note = f"throughput drifted {drift * 100:.0f}% - recalibrating"
            self._log(f"  [Governor] throughput drifted {drift * 100:.0f}%, recalibrating")
            return None
        if self._recheck_left > 0 and self._drift_run == 0:
            # A drifting cell does not spend the budget: otherwise a collapse that
            # started inside the re-check window would re-anchor the baseline ONTO
            # the collapse and hide itself.
            self._recheck_left -= 1
            if self._recheck_left == 0:
                # Warm-start budget spent and the persisted number held up: judge the
                # rest of the run against what it is really delivering.
                self._steady_tp = tp
                self._note = f"warm start confirmed at {self.workers}w"
        return None

    # ------------------------------------------------------------- controls ---

    def freeze(self) -> None:
        """Stop deciding anything. Samples still accrue for the UI and for history."""
        with self._lock:
            if self.mode == "off":
                return
            self.state = "FROZEN"
            self._binding = "frozen"
            self._note = f"frozen at {self.workers} workers"

    def recalibrate(self) -> None:
        """Throw away the convergence and walk the curve again from here."""
        with self._lock:
            if self.mode == "off":
                return
            self.state = "RECAL"
            self._step_walls = []
            self._prev_tp = None
            self._prev_workers = 0
            self._prev_cpc = None
            self._strikes = 0
            self._steady_tp = None
            self._drift_run = 0
            self._recheck_left = 0
            self._static = False
            self._binding = "none"
            self._note = "recalibrating on request"

    # ------------------------------------------------------------- reporting --

    def advice(self) -> dict:
        """What the occupancy envelope would pick, for /api/governor.

        This is the SAFETY view, not the control loop: suggest_workers assumes each
        cell keeps using the cores it used when measured, which contention makes
        false as the pool grows (measured 7.75 cores/cell at 1:1 -> it recommends 2,
        while 8 really do deliver more). Shown, never silently applied.
        """
        cfg = self._cfg()
        with self._lock:
            return self._advice_locked(cfg)

    def _advice_locked(self, cfg: dict) -> dict:
        cpc = self.cores_per_cell
        rss = self.rss_p95_mb
        if not cpc:
            return {
                "workers": None,
                "reason": "not enough samples yet",
                "cores_per_cell": None,
                "rss_p95_mb": round(rss, 1) if rss else None,
            }
        envelope = suggest_workers(
            cpc,
            self.cores,
            self._cpu_target_pct(cfg),
            ram_available_mb=self._available_mb_probe(),
            ram_per_cell_mb=rss,
            hard_cap=self.ceiling or DEFAULT_HARD_CAP,
            gpu_fraction_per_cell=self._occ.gpu_fraction_per_cell,
        )
        tp = self.cells_per_min
        return {
            "workers": envelope,
            "reason": "occupancy envelope (CPU/RAM/GPU budget)",
            "cores_per_cell": round(cpc, 3),
            "rss_p95_mb": round(rss, 1) if rss else None,
            "cells_per_min": round(tp, 2) if tp else None,
        }

    def snapshot(self) -> GovernorSnapshot:
        cfg = self._cfg()
        with self._lock:
            return self._snapshot_locked(cfg)

    def _snapshot_locked(self, cfg: dict) -> GovernorSnapshot:
        threads, flush = self._threads(max(1, self.workers), cfg)
        target = self.target
        if self.mode == "advise":
            # Nothing is applied in advise mode, so target carries the recommendation.
            target = self._advice_locked(cfg).get("workers") or self.workers
        cpc = self.cores_per_cell
        rss = self.rss_p95_mb
        tp = self.cells_per_min
        return GovernorSnapshot(
            mode=self.mode,
            state=self.state,
            workers=int(self.workers),
            target=int(target),
            threads=int(threads),
            flush=int(flush),
            cores_per_cell=round(cpc, 3) if cpc else None,
            rss_p95_mb=round(rss, 1) if rss else None,
            cells_per_min=round(tp, 2) if tp else None,
            binding=self._binding,
            samples=int(self._samples),
            note=self._note,
        )

    def end_run(self) -> tuple[str, dict] | None:
        """(bucket_key, history entry) worth persisting, or None - then PARK.

        Nothing is persisted from a run that never chose anything - an advisory run, a
        small static grid, or one that ended before it had evidence - because a warm
        start reads this back as if it were a converged answer.

        Parking matters as much as persisting: without it the governor kept reporting
        state STEADY at 12 workers for the rest of the process, so /api/status and the
        tray painted a finished run as a live one. After end_run the governor is idle
        (state "OFF"): it gates nothing, decides nothing, and says so. Whatever was
        persisted stays persisted - begin_run reads it back as a warm start.
        """
        with self._lock:
            entry = self._history_entry()
            self._park()
            return entry

    def _park(self) -> None:
        self.state = "OFF"
        self.target = self.workers
        # _static is deliberately NOT cleared: end_run() can be called twice (stop path +
        # the last in-flight cell), and clearing it would let the second call persist a
        # run the first one correctly refused as un-converged.
        self._binding = "none"
        self._note = f"run ended at {self.workers} worker(s)"

    def _history_entry(self) -> tuple[str, dict] | None:
        if self.mode != "auto" or self._static or self._samples < STEP_SAMPLES:
            return None
        threads, flush = self._threads(max(1, self.workers))
        cpc = self.cores_per_cell
        rss = self.rss_p95_mb
        tp = self.cells_per_min
        return self.bucket, {
            "workers": int(self.workers),
            "threads": int(threads),
            "flush": int(flush),
            "cores_per_cell": round(float(cpc), 3) if cpc else 0.0,
            "rss_p95_mb": round(float(rss), 1) if rss else 0.0,
            "cells_per_min": round(float(tp), 2) if tp else 0.0,
            "ts": float(self._wallclock()),
        }
