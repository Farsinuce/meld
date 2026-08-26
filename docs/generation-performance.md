# Generation performance

How Meld decides how hard to drive your PC while it builds a world, what you can
change, and how the machinery works underneath.

**Branch:** `perf/speed-to-worldgen-phase2` (Meld and the arnis fork, both), which
continues `perf/speed-to-worldgen`.
**Default behaviour is unchanged.** Everything described here is off until you turn
it on: `governor_mode` ships as `"off"`, which reproduces the pre-governor
scheduling formulas exactly, and every arnis-side addition is gated behind an
environment variable that is unset by default.

Part A is for people building worlds. Part B is for people changing this code.

---

# Part A. For people building worlds

## The short version

Meld builds many cells at once, each one its own generator process. How many run
side by side is the single biggest lever on a build, and it is not a "more is
better" lever. On the reference machine, at 1:1 with cell size 4, going from 8
workers to 24 delivered **the same number of cells per minute** while making each
individual cell **three times slower** and pushing RAM from 81% to the edge of
swapping. That is measured, not modelled.

So the new **Generation performance** panel does two things: it exposes the limits
you actually care about (CPU share, free RAM, disk write threads), and it can find
the worker count that is genuinely fastest on your machine by timing your run
instead of guessing from your core count.

## Where it lives

Settings card, the **Generation performance** block, between the OSM source drawer
and Seed. Everything is live: changing a number mid-run reaches the next cell a
worker picks up. The world-shaping inputs (scale, origin, seed, elevation lock,
bbox) stay frozen for the whole run, so tuning performance mid-build can never
desync the world.

## The three modes

| Mode | UI label | What it does |
|---|---|---|
| **Off** | "Off, I set them" | Manual. Your numbers below are used exactly as typed, all run long. Identical to how every previous Meld release behaved. |
| **Advise** | "Advise, suggest only" | Meld times every cell and shows you what it would change. It changes nothing by itself. |
| **Auto** | "Auto, Meld tunes it" | Meld starts low, times every cell, and moves the worker count to whatever is actually fastest on this PC. It never goes past the limits you set. |

**Off is the default.** Advise is the safe way to find out whether Auto would help
you: run a build, watch the readout, then decide.

Auto still obeys you. Your **Workers** number becomes a ceiling it may not pass,
your **CPU limit** and **RAM headroom** are hard fences, and **Freeze** stops it
moving at all. The only knob Auto genuinely takes over is Threads/task, which is
why that row greys out and says so.

Two things Auto deliberately does not do:

- **It skips grids under 32 cells.** A short run would still be calibrating when it
  finished, so it just uses your stored worker count and stays out of the way.
- **It remembers per scale band and cell size, not per project.** The second time
  you build at 1:1 with cell size 4 on this machine, it warm-starts at the count
  the last run settled on, re-checks it for a dozen cells, and only re-measures if
  reality has changed.

## The knobs

| Control | Setting key | Range | Default | When to change it |
|---|---|---|---|---|
| **Workers** | `max_workers` | 1..64 | 4 | The main lever. How many cells generate at once. In Auto it is the ceiling. See the recommended numbers below. |
| **Threads / task** | `min_threads_per_worker` | 1..8 | 4 | Manual mode only. CPU threads each single cell gets for its own tile build. Keep `workers x threads` at or under your core count. In Auto, Meld sets this per cell from measured occupancy and the row is disabled. |
| **CPU limit** | `cpu_target_pct` | 10..95% | 90 | Lower it to keep the machine usable for other work. It stops at 95 on purpose so the OS and the disk-writing step never starve. |
| **RAM headroom** | `ram_headroom_mb` | 512..8192 MB | 2048 | Free memory Meld refuses to eat into. Before starting another cell it checks this much would still be free afterwards, and waits instead of starting if it would not. Raise it if the machine swaps or workers die mid-save. Lower it to squeeze one more worker out of a small-RAM PC, at the risk of a failed cell. |
| **Flush threads** | `flush_threads_cap` | 1..24 | 12 | Ceiling on threads writing finished regions to disk at once, shared across all workers. Raise on NVMe, lower on a slow or external drive. It is a limit, not a target. |
| **Stagger starts / step / adaptive** | `cpu_stagger_*` | on, 1..4 s | on, 2 s | Manual mode only, and these rows hide themselves in Advise/Auto. It delays the **first** cell of each worker so they do not all hit the network, CPU and disk in the same instant. It does nothing to later cells. In Advise/Auto the admission gate does this job properly, by measuring instead of waiting a fixed time. |

Two of these are new on this branch: **RAM headroom** and **Flush threads**. Flush
threads replaces a hardcoded value of 6 that used to be invisible; on the reference
machine, on a single-process 1:1 benchmark, raising it from 6 to 12 took a cell
from 65.6 s to 57.1 s (measured, on the earlier performance branch).

### Why "workers x threads" matters

Each worker asks the generator for its own pool of CPU threads. Multiply them and
that is the real load on your cores. The old formula had a floor of 4 threads per
worker, so 12 workers asked for 48 threads on a 24-core machine, and 24 workers
asked for 96. The cores do not multiply; the OS just time-slices harder and every
cell gets slower.

In Advise/Auto the per-worker share is the senior bound and the floor is 1. On a
24-core box with CPU limit 90% (a budget of 21 cores) the per-cell thread count
comes out as `floor(21 / workers)`:

| workers | 4 | 8 | 10 | 12 | 16 | 24 |
|---|---|---|---|---|---|---|
| threads per cell | 5 | 2 | 2 | 1 | 1 | 1 |
| flush threads | 12 | 6 | 4 | 4 | 3 | 2 |

One thread per cell at 12 workers looks alarming and is not: twelve cells each
using one core is twelve cores of real work, where the old formula's 48 threads on
24 cores was twelve cells fighting over the same cores and delivering the same
throughput more slowly.

## The live readout

Visible in Advise and Auto, under the knobs.

| Line | What it is telling you |
|---|---|
| **State chip** | `CALIBRATE` measuring this machine at this cell size. `CONVERGE` climbing while it is still getting faster. `STEADY` settled, it stays here unless something changes. `RECAL` re-measuring from scratch. `FROZEN` held by you. `OFF` not running. |
| **limited by** | What stopped it climbing: `throughput` (the honest one, another worker stopped paying), `ram` (headroom blocks the next step), `ceiling` (your Workers number), `contention` (added workers were mostly waiting on each other), `history` (warm-started from a previous run). |
| **Workers now to target** | What is running against what it wants. In Advise, the target is the recommendation it is not applying. |
| **Threads / cell, flush** | What the next cell will be given. |
| **The meter** | `workers x threads` against your cores. It turns red when oversubscribed. |
| **Measured cores / cell** | How much CPU a cell really uses, averaged over recent cells. About 7.75 at 1:1, about 1.02 at 1:20 (measured). This number is the whole reason the panel exists: the old scheduler assumed it. |
| **Cells / min** | Delivered throughput. **This is the number to judge a change by**, not CPU%. |
| **RAM p95 / worker** | Peak memory a cell actually takes, 95th percentile. Multiply by workers, add your headroom, compare against your free RAM. |
| **Cells measured** | How much evidence is behind all of the above. Under about 6, treat it as noise. |

**Recalibrate** throws away what it learned and walks the curve again from where it
is. Use it after a real change: a different cell size, a different save drive, or
something heavy starting up on the PC.

**Freeze** stops it deciding anything and holds the current counts. Measurement
continues, so the readout stays live and the run still contributes what it learned.

## Recommended settings

All rows marked **measured** come from real `meld-report.json` files on the
reference machine: 24 logical cores (8P + 16E, Intel Core Ultra 9 275HX), 31.4 GB
RAM, NVMe, Windows 11. Rows marked **estimate** are arithmetic from measured
per-cell cost, not from a sweep.

| Scale | Cell size | Workers | Basis |
|---|---|---|---|
| **1:1** | 4 | **10 to 12** | **measured.** Throughput was flat at 21-23 cells/min across every worker count from 8 to 24, while median cell time went 21.1 s (8w), 33.9 s (12w), 41.6 s (16w), 56.5 s (20w), 63.5 s (24w). RAM peaked at 81%. A separate 400-cell Bucuresti run at 16 workers delivered 19.7 cells/min at 88% RAM. Every worker past about 12 bought nothing and cost latency and memory. |
| **1:1** | 8 | **6 to 8** | **estimate**, anchored on a measurement: 15 workers at cell size 8 hit **93% RAM peak**, which is one cell away from swapping, with a 123.9 s median cell. Cell size 8 is roughly four times the area of cell size 4, so halve the worker count and watch the RAM p95 line. |
| **1:2 to 1:9** | 4 | start at 12, let Auto climb | **estimate.** Between the two measured bands. This is exactly the case Auto is for. |
| **1:10 and smaller** | 4 | **16 to 20** | **estimate** from documented per-cell cost (about 1.02 cores and about 1.2 GB per cell at 1:20, measured previously, not re-measured on this branch). CPU: 21 core budget / 1.02 = about 20. RAM: (31.4 GB - 2 GB headroom) / 1.2 GB = about 24. CPU binds first, so about 20. |

Everything else, all scales: **CPU limit 90**, **RAM headroom 2048 MB**,
**Flush threads 12** on NVMe (drop to 4 or less on an external or spinning drive).

### On a different machine

Two ceilings, take the lower, then stop at the throughput knee:

```
cpu_ceiling = floor(cores * cpu_limit_pct / 100) / measured_cores_per_cell
ram_ceiling = (free_MB - ram_headroom_MB) / measured_rss_p95_per_worker_MB
```

Both inputs are on the live readout, so run one build in **Advise** and read them
off. The knee is the part arithmetic cannot give you, which is why Auto walks to it
instead of computing it.

## My run is slow. What do I check?

In order. The first three catch most of it.

1. **Are cells slow, or is the run slow?** Open the readout. If **cells/min** is
   fine but each cell feels slow, that is normal for a parallel build and nothing
   is wrong. If **cells/min is flat while you add workers**, you are past the knee:
   lower Workers, or switch to Auto and let it find the number.

2. **What does "limited by" say?**
   - `ram` with a large RAM p95: too many workers for this cell size. Lower Workers,
     or use a smaller cell size, or raise RAM headroom and accept fewer workers.
   - `ceiling`: it wants more workers than you allowed. Raise Workers if the meter
     and RAM p95 have room.
   - `contention`: added workers were waiting on each other, not working. Nothing to
     fix; this is the governor correctly refusing to climb.
   - `throughput`: it found the knee. This is the healthy answer.

3. **Check the meter.** If `workers x threads` is over your core count and red, you
   are oversubscribed. In manual mode lower Threads/task or Workers. In Auto this
   should not happen; if it does, it is a bug worth reporting.

4. **RAM peak over 90% in the run report** means the next run is one cell away from
   swapping, and a swapping run is not slow, it is stopped. Lower Workers first,
   raise RAM headroom second.

5. **Is the map data cached?** With no local source the generator calls Overpass
   over the network. On one measured bbox that was **13192 ms against 1157 ms** from
   the local cache: the difference between a cell that is mostly waiting and one
   that is mostly computing. Meld passes a local tile directory when it has one, so
   bake the OSM pack for your area before a big build.

6. **Is the elevation baked?** An unbaked area makes every cell fetch its own
   terrain tiles. Bake it once from the data pack card.

7. **Small grid?** Under 32 cells Auto deliberately does not ramp. That is not a
   fault; short runs cannot repay the measurement.

8. **Slow save drive?** If cells finish but the run crawls at the end, lower Flush
   threads. Too many parallel writes on a slow drive make every save slower and
   spike RAM.

9. **Stagger is not the problem.** It only delays the first cell of each worker,
   once per run, and only in manual mode. It never makes a run longer overall.

10. **Still slow?** Run in **Advise** for one build and read `measured cores/cell`
    and `RAM p95/worker` off the panel. Those two numbers plus your core count and
    free RAM explain almost every case, and they are the numbers to quote in a bug
    report.

---

# Part B. For developers

## Map of the change

| File | Role |
|---|---|
| `src/governor.py` | The control loop. Pure module: no flask, no I/O beyond two swappable psutil probes. |
| `src/occupancy.py` | Measurement primitives: cores/cell, RSS p95, cells/min, plus the pre-existing `suggest_workers` envelope kept as the advisory view. |
| `src/workers.py` | Pool. Stop flag, per-run stagger epoch, and the `admit_cb` hook. |
| `src/arnis_cmd.py` | Spawns the generator; parses the stdout protocol; prefers exact generator-reported CPU/RSS over the psutil sampler. |
| `src/project.py` | Settings defaults and the `worker_autoscale` migration. |
| `src/presets.py` | `_MACHINE_KEYS`, so governor state never travels in a preset. |
| `src/statusbar.py` | The `gov` segment and the two new worker stages. |
| `server.py` | Wiring, validation clamps, the three routes, `/api/status` and `/api/mini` additions. |
| `web/index.html` | The Generation performance panel. |
| `bench/` | Side-by-side harness: legacy scheduling against the governor, with a determinism gate. |
| arnis `src/meld_telemetry.rs` | Stdout protocol v1. |
| arnis `src/floodfill.rs` | Opt-in deterministic fill budget replacing the wall-clock timeout. |

## The governor

### Why throughput and not CPU%

`occupancy.py` answers "how many cells fit in the CPU/RAM/GPU budget" from measured
core occupancy. That is a budget, not a schedule: it assumes a cell's occupancy
stays constant however many cells run beside it, which the measured Berlin data
falsifies. A 24-worker run pinned 79% of the CPU and delivered the same 22
cells/min as 8 workers at a third of the per-cell latency. CPU% would have called
that a success. The only honest control variable is delivered cells/min, and the
only honest way to find the knee is to walk to it.

The envelope survives as `Governor.advice()` and is surfaced on `/api/governor`. It
is shown, never silently applied. It is also documented as unsafe to apply directly:
at 1:1 with measured 7.75 cores/cell it recommends 2 workers, while 8 really do
deliver more.

### States

```
                    small grid (<32 cells)  --> STEADY (static)
                    advise mode             --> STEADY (static)
                    history hit             --> STEADY (+ RECHECK_CELLS budget)
begin_run --> CALIBRATE --> CONVERGE --> STEADY --> RECAL --> CONVERGE --> ...
                                            |
                              freeze()  --> FROZEN
                              mode off  --> OFF
```

- **CALIBRATE** opens at `CALIBRATE_START = 4` workers, takes `STEP_SAMPLES = 3`
  cells, then hands over. Low on purpose: climbing is cheap, unwinding is not.
- **CONVERGE** is the hill climb. One `+CLIMB_STEP = 2` move per 3 samples. It stops
  and settles when: the marginal gain falls under the threshold; the gain goes
  negative (it unwinds exactly one step); measured cores/cell collapses below
  `CONTENTION_RATIO = 0.6` of the previous step's; RAM headroom cannot hold the
  added workers; or it reaches the ceiling.
- **STEADY** keeps measuring. It re-opens as `RECAL` only when throughput sits more
  than `DRIFT_PCT = 25%` off its baseline for `DRIFT_CELLS = 5` consecutive cells,
  never on one cell.
- **RECAL** behaves as CALIBRATE from the current count.
- **FROZEN** and **OFF** decide nothing; FROZEN still records samples.

**Naming caveat worth knowing.** The design contract listed a `WARMSTART` state. The
implementation collapses it into `STEADY` with `binding="history"` and a
`RECHECK_CELLS = 12` re-check budget, because a warm start is a hypothesis being
tested, not a distinct ramp phase. `governor.py` never emits the string `WARMSTART`;
`web/index.html` and `src/statusbar.py` both carry a defensive entry for it, so if a
future change does emit it, both surfaces already render it.

### Tunables (`src/governor.py`)

| Name | Value | Meaning |
|---|---|---|
| `MIN_SAMPLE_WALL_S` | 2.0 | Cells faster than this say nothing about steady state. Higher than `occupancy.MIN_SAMPLE_WALL_S` (1.5) on purpose: a throughput decision is worth more evidence than an occupancy estimate. |
| `STEP_SAMPLES` | 3 | Cells required at a worker level before its throughput is believed. |
| `WINDOW_CELLS` | 6 | Rolling window behind cells/min and the drift check. |
| `SMALL_GRID_CELLS` | 32 | Under this, no ramp. |
| `CLIMB_STEP` | 2 | Hill-climb step size. |
| `GAIN_MIN_SMALL` / `GAIN_MIN_LARGE` | 0.75 / 0.5 | Marginal cells/min a step must buy. Lower above `GAIN_TAPER_WORKERS = 8` because past the 8 P-cores added workers land on E-cores and the honest gain per step is smaller. |
| `CONTENTION_RATIO` | 0.6 | Cores/cell collapse that means the new workers are waiting, not working. |
| `DRIFT_PCT` / `DRIFT_CELLS` | 0.25 / 5 | STEADY re-open trigger. |
| `RECHECK_CELLS` | 12 | Warm-start re-check budget. A drifting cell does not spend it, so a collapse that begins inside the window cannot re-anchor the baseline onto itself and hide. |
| `ADMIT_TIMEOUT_S` / `ADMIT_POLL_S` | 3.0 / 0.25 | Admission gate. RAM only - there is no CPU gate. |
| `CALIBRATE_START` | 4 | Opening worker count. |

### What it measures, and how

- **cores/cell** comes from `OccupancyTracker` over a window of 8 (`cores_per_cell`),
  with a 3-cell recent view (`cores_per_cell_recent`) alongside it.
- **RSS p95** is nearest-rank, not interpolated: an interpolated p95 over 32 samples
  lands between the top two readings and understates the exact peak this is meant to
  fence. It answers on the first sample, unlike the CPU properties which need two,
  because RAM is a wall rather than a slowdown.
- **cells/min** is derived as `workers * 60 / median(recent walls)`, not counted off
  the clock. That makes it immune to how long the caller sat between cells, and stops
  one stalled worker masquerading as a collapse in all of them. (`occupancy.py` also
  offers a clock-based `cells_per_min` over a 300 s window; the snapshot uses the
  governor's median-derived one.)
- Failed cells and cells under `MIN_SAMPLE_WALL_S` are dropped. A crash at three
  seconds is evidence about the generator, not about how many should run at once.
- `threads_for_next_cell(workers=)` is **also how the governor learns the pool's real
  size**. It must be called with the count actually in force. When that changes,
  every sample window is re-anchored rather than averaged across two contention
  regimes.

### Thread and flush allocation

Clamp precedence, senior first:

```
core_budget = max(1, floor(cores * cpu_target_pct / 100))
rayon_upper = max(1, core_budget // workers)                    # SENIOR
rayon       = clamp(ceil(1.25 * cores_per_cell), 1, rayon_upper)
              or clamp(core_budget // workers, 1, 4) with no measurement yet
flush       = clamp(flush_threads_cap // max(1, workers // 4), 1, flush_threads_cap)
```

The 1.25 is deliberate slack over measured occupancy: a cell alternates parallel and
serial phases, so asking for exactly its mean core count starves the parallel ones.
**The floor is 1.** The old floor of 4 is what produced 96 rayon threads across 24
workers on 24 cores.

In `off` mode the same method returns the legacy formulas byte for byte:

```
rayon = max(min_threads_per_worker, core_budget // workers)      # floor 4
flush = max(2, min(6, rayon // 2))
```

This was verified exhaustively: 13,824 combinations of cores x cpu_pct x
min_threads x workers, governor-off output bit-identical to a transcription of the
deleted `server.py` expressions, 0 mismatches. Off mode also never gates admission
and never resizes the pool. Two edges differ, both only reachable by hand-editing
`project.json` past the API clamps, and both are the safer behaviour:
`min_threads_per_worker: 0` gave 1 and now gives 4; `cpu_target_pct: 200` was
honoured and now clamps to 95.

### Admission versus stagger

They are the same job done two ways, and they are mutually exclusive.

**Stagger** (legacy, manual mode) delays the first job of each worker by a fixed or
adaptive step so starts do not collide. It is open-loop: it waits a guessed time
whether or not the machine is busy. Two bugs were fixed on this branch: the
"first job" flag was per-thread-forever rather than per-run, so runs 2..N started
every worker in lockstep, and the adaptive EWMA leaked across runs, projects and
scales. Both are now keyed to a pool-level run epoch that `server.py` bumps at run
start. The delay arithmetic itself is unchanged.

**Admission** (Auto only) is closed-loop. `POOL.admit_cb` is armed only when
`governor_mode == "auto"`, and when it is armed the stagger sleep is skipped entirely,
because admission is the pacing. `Governor.admit()` holds a worker while free RAM is
below `rss_p95 + ram_headroom_mb`. There is **no CPU gate**: a near-100% CPU is the
goal of a CPU-bound render, not a reason to wait. The seconds a worker spends held are
charged into that cell's sample, so the throughput the governor optimises can see the
cost of its own gate. Guarantees that matter:

- It **never returns later than `timeout_s`** (default 3 s), returning
  `"go(timeout)"`. A governor that can stall the pool is worse than one that guesses.
- `active == 0` is **always** admitted. The machine must make progress even when the
  RAM probe is unhappy.
- No psutil means never gate.
- STEADY only ever waits on RAM, never on CPU.
- The callback is invoked **outside** the pool lock, so a blocking governor cannot
  wedge the queue or its peers, and exceptions from it are swallowed so a broken
  governor cannot stop cells running.

The worker's stage string while held is `"waiting for admission"`, a real state
distinct from `queued`: the cell has been taken off the queue and is waiting on
headroom, not on a free slot.

### One integration trap, found by an end-to-end test

`begin_run()` *chooses* an opening worker count (the calibrate start, or a warm
start) but does not apply it. If the caller does not push that onto the pool, the
pool keeps `max_workers`, the first `threads_for_next_cell()` re-anchors the
governor to that number, and a run told to calibrate from 4 calibrates from 24: the
exact runaway the Berlin data forbids. `server.py` now applies
`GOVERNOR.snapshot().target` to the pool immediately after `begin_run()`. Any other
caller must do the same.

## The arnis side

### Stdout protocol v1

Emitted only when `ARNIS_PHASE_MARKERS` is set to exactly `1`. Unset, every entry
point returns before any clock read or formatting, so a default run is byte-identical
to one built without the module.

Grammar, one line each, stdout, flushed:

```
[meld] v=1 phase=<name> t=<ms_since_process_start>
[meld] v=1 phase=done wall_s=<f.3> cpu_s=<f.3> peak_mb=<f.1> gpu_ms=<u64>
```

Phase names, in pipeline order: `fetch`, `elevation`, `parse`, `overture`, `place`,
`merge`, `ground`, `post`, `save`, then the terminal `done`.

Rules a consumer must respect:

- Markers are emitted at the **start** of each phase. A phase's duration is
  `t(next) - t(this)`; the last phase's is `wall_s * 1000 - t(last)`.
- Not every phase fires. `overture` only when the Overture fetch actually runs;
  `merge` only on the parallel-tile path (the sequential path has no merge).
- On the parallel path, placement and per-batch merges **interleave**, so `place` to
  `merge` is the combined loop and `merge` to `ground` is a short teardown. The
  generator's own `bench` output keeps the true `element_placement` / `tile_merge`
  split; v1 has no field for it.
- `cpu_s` and `peak_mb` come straight from Win32 process counters (`GetProcessTimes`,
  `K32GetProcessMemoryInfo`, both kernel32 exports, so no new crate dependency and no
  Cargo.toml change). On non-Windows targets they are `-1.000` / `-1.0`, kept in the
  same float shape so one parser handles both, and Meld's psutil sampler covers that
  case.
- `done` is emitted at the very end of a successful run, after level.dat and spawn
  writes, so `wall_s` covers the whole process. It reads the same GPU counter the
  `[gpu] busy_ms=` line reports; that line is untouched and prints earlier.
- Unknown `k=v` tokens are ignored by the Meld parser so the protocol can grow without
  a version bump. `is_marker_line()` is version-agnostic: a future `v=2` binary is
  swallowed, never misread. Meld's own `[meld] arnis exited with code ...` diagnostic
  carries no `v=` and is unaffected.

Meld side (`src/arnis_cmd.py`): marker lines are consumed **before** `on_line()`, so
they never reach the user log or `parse_progress()`. This matters concretely, because
`parse_progress("[meld] v=1 phase=ground t=880")` would otherwise return 35 and the
`done` line would return 100. When the `done` line carries both `cpu_s` and `wall_s`
they win over the sampler and are tagged `source="arnis"`; otherwise
`source="sampler"`, with the roughly 0.5 s terminal undercount a 0.5 s poll implies.
`on_stats` extras are delivered by signature inspection, so the pre-existing
two-argument callback keeps being called with exactly two arguments.

`server.py` sets `ARNIS_PHASE_MARKERS=1` whenever `governor_mode != "off"`. There is no
capability probe: an older binary ignores an unknown environment variable and simply
lands on the sampler path, so probing bought nothing and its `--help` grep matched a
string clap never prints, which silently disabled the whole protocol.

### Environment variables

| Variable | Owner | Default | Effect |
|---|---|---|---|
| `MELD_GOVERNOR` | Meld process | unset | `off`/`advise`/`auto`. Last word over settings. `MELD_GOVERNOR=off` pins a run to legacy scheduling without editing a project. |
| `RAYON_NUM_THREADS` | per arnis child | from the formula above | Tile parallelism inside one cell. |
| `ARNIS_FLUSH_THREADS` | per arnis child | from the formula above | Region write pool. The fork's own default is `cores/4` clamped 2..6 **per process**, correct for a lone generator and oversubscribed the moment several run. |
| `ARNIS_PHASE_MARKERS` | per arnis child | unset | Protocol v1. Exactly `1`. |
| `ARNIS_STREAM_TO_DISK` | per arnis child | set for cell size >= 8 | Region eviction. Pin it when benchmarking, or the run picks its path from whatever RAM happens to be free. |
| `ARNIS_BLOCK_HASH` | bench server env | unset | Makes each child print `[BENCHMARK] block_hash=<16 hex>`. The determinism gate reads this. |
| `ARNIS_FILL_BUDGET` | arnis | **set to `1` by Meld on every cell, in every mode** | Deterministic flood-fill budget, replacing the wall-clock limit - the one path whose output depended on machine load. Accepts `1|true|yes|on`, case-insensitive. Golden hashes verified identical with it on. |

### The fill budget, and why it exists

`--timeout` was enforced with `Instant::now()` inside the fill loop, which makes
output a function of machine load and worker count rather than of input. Two runs of
the same cell on a busier box could truncate different polygons. With
`ARNIS_FILL_BUDGET=1` the same limit is expressed in **work units**:

```
budget_units = timeout_secs * BUDGET_UNITS_PER_SECOND      // 2_000_000
```

A work unit is one point-in-polygon decision plus its bookkeeping. The constant takes
the pessimistic end of a reasoned 2-4 M units/s/core range **deliberately**: erring low
means the budget can only bind sooner than the wall clock would on an idle machine,
never later, so budget mode can never let a polygon that used to be truncated run away.
In practice it does not bind at all: Meld passes `--timeout 600..1200`, so 1.2-2.4
billion units against a worst case of about 125 M. Like the wall-clock timeout, it is
a hang guard, not a routine truncation. The 2-4 M figure is reasoned from the code's
structure, not from an instrumented micro-benchmark.

Verified: the default path is byte-identical (`scripts/golden_hash.sh`, all 5
fixtures), and the same 5 fixtures re-run live with `--timeout 600` produced identical
hashes with the env var set and unset. **Nothing sets it yet.** Flipping it on is a
Meld-side decision.

Caveat inherited from the legacy shape: the budget, like the timeout, is only consulted
between seed fronts, so one enormous connected component still floods to completion
once seeded. Tightening that would change output shape.

### Process-global statics

Four `set_*` functions that write process-global config now assert on a **conflicting**
re-set instead of silently taking the first value or the last:
`set_world_bounds` (`world_editor/common.rs`), `set_data_version` (`world_editor/java.rs`),
`set_noise_seed` (`ground_generation.rs`), `set_biome_amounts` (`caves/decoration.rs`).

No assert can fire today: every call site runs once per process. They exist to make
the one-cell-per-process assumption explicit and loud, so anyone who later runs two
cells in one process (a `--serve` mode, an in-process batch) hits a named panic instead
of blocks clamped to the wrong world height or chunks stamped with mismatched
DataVersions. `set_biome_amounts` required widening `BiomeAmounts` to derive
`Debug, PartialEq`, which is derive-only. Signatures and call sites are unchanged.

## Settings keys

All of them live in `src/project.py` `default_settings()`, are validated in `server.py`
`/api/settings`, and **must** appear in **both** `presets._MACHINE_KEYS` and
`server.py`'s `_META_SKIP_SETTINGS`. The first block is the governor's six keys plus
the retired `worker_autoscale`; the second is the three keys phase 2 added.

| Key | Type | Default | Clamp |
|---|---|---|---|
| `governor_mode` | str | `"off"` | enum `off`/`advise`/`auto`; anything else falls back to `off` |
| `governor_history` | dict | `{}` | not user-editable; written by `end_run()` |
| `cpu_target_pct` | int | 90 | 10..95 (pre-existing; the 100-vs-90 fallback disagreement is now unified on 90 across `project.py`, `server.py` and `governor.py`) |
| `ram_headroom_mb` | int | 2048 | 512..8192 |
| `flush_threads_cap` | int | 12 | 1..24 |
| `governor_max_workers` | int | 0 | 0..64. 0 means "use `max_workers` as the ceiling"; > 0 is an explicit ceiling independent of it. |
| `worker_autoscale` | bool | False | **Legacy, read-only for one release.** `project.migrate_governor_settings()` returns a patch mapping `True` onto `governor_mode="auto"` and always clears the flag, so it is idempotent and never resurrects a mode the user turned off. Called at boot and on project switch. |

Phase 2 adds three more, in the same four places:

| Key | Type | Default | Clamp |
|---|---|---|---|
| `phase2_timers` | bool | `True` | coerced to bool (a string `"false"`/`"0"`/`"off"` is read as false rather than stored as a truthy string). Gates the per-cell `[Timers]` log line **only**; `summary.timers` and `cells[].timers` are written either way. On by default because it is measurement, not behaviour: four `time.monotonic()` reads per cell, and nothing about what is written changes. |
| `canonical_regions` | bool | `False` | coerced to bool. **Declared, not wired.** Would gate emitting arnis's `--canonical-regions`; `False` = arnis writes all 36 regions and Meld deletes the surplus, as today. |
| `parse_fast_json` | bool | `False` | coerced to bool. **Declared, not wired.** Would gate the faster OSM decode path; `False` = the fork parses tiles exactly as today. |

`canonical_regions` and `parse_fast_json` are kill switches for work that has **not
landed**. Nothing in the code reads them today: no `--canonical-regions` flag is
emitted, and no JSON parser is swapped. They exist now, defaulting to today's
behaviour, so that the tasks behind them (the region-write filter and the OSM parse
change, both HOLD in `perf-phase2-plan.md`) can be flag-gated the day they land
without a settings migration, a preset re-save, or a world-meta rewrite. If you are
reading this and neither has landed, they are dead keys and setting them does
nothing. Do not document them to users as features.

`governor_history` is keyed by `f"{scale_bucket}/{cell_size}"` where `scale_bucket` is
`"1:1"` for scale >= 0.5, `"1:2..1:9"` for >= 0.1, else `"1:10+"`. Each entry holds
`workers, threads, flush, cores_per_cell, rss_p95_mb, cells_per_min, ts`.

**Why both key lists matter.** `_MACHINE_KEYS` keeps these out of shared presets, and
`governor_history` is the sharpest case: it carries one box's measured cores/cell, RSS
p95 and cells/min, which would warm-start a laptop at a 24-core desktop's knee.
`_META_SKIP_SETTINGS` stops a world-meta import overwriting local scheduling.
`gpu_accel` was found leaking the same way and was added to `_MACHINE_KEYS` here.

Nothing is persisted from a run that never chose anything: `end_run()` returns `None`
for advise mode, static/small grids, and runs that ended before `STEP_SAMPLES` samples,
because a warm start reads history back as if it were a converged answer. `/api/stop`
can persist history from a stopped run, but only cells that actually completed
(`ok=True`, at least 2 s) contributed samples, so the data is honest.

## HTTP API

All additive. No existing field was removed or renamed.

| Route | Shape |
|---|---|
| `GET /api/status` | gains `"governor"`: the 12-field snapshot (`mode, state, workers, target, threads, flush, cores_per_cell, rss_p95_mb, cells_per_min, binding, samples, note`). **Always present** (mode `off` / state `OFF` when idle) so the UI can distinguish "old server" from "governor idle". |
| `GET /api/mini` | gains `"gov": {"state", "w", "target"}`. Three fields, not the snapshot: this route is polled once a second by the status bar and its entire reason to exist is being tiny. |
| `GET /api/governor` | the snapshot plus `history`, `advice`, `cores`, `ceiling`, `pool_workers`, `admission_armed`. Its own route because the Settings card polls it only while open, every few seconds. `admission_armed` is the honest answer to "is the governor pacing this run": mode is what was asked for, this is what is in force. |
| `POST /api/governor/recalibrate` | `{ok: true, state: ...}`. A no-op in off mode, which is why it reports the resulting state rather than asserting it took. |
| `POST /api/governor/freeze` | `{ok: true, state: ...}`. |

Worker stages gained `"waiting for admission"` and `"finishing merges"`;
`WORKER_STAGES` is now 10 entries and every one has a distinct colour in
`statusbar.stage_style`. `_PHASE_STAGE` maps the 10 protocol phases onto them, and
`state["phase"]` is preferred over the prose scan (`post` and `place` name no keyword
the scan knows, and would otherwise be read off the percentage). `on_phase` writes
`state["phase"]`, deliberately **not** `state["message"]`: overwriting the message
would trade "Generating tile 3/16" for "place". An unrecognised phase falls through to
the prose scan rather than inventing a stage the UI has no colour for.

Known cosmetic gap: `web/index.html`'s `GOV_BINDING_TXT` maps
`cpu/ram/gpu/ceiling/disk/none` to prose and falls back to the raw string for the
governor's other binding values (`throughput`, `history`, `contention`, `frozen`), so
those render unprosed. Harmless, one dictionary away from fixed.

## Running the bench

`bench/bench_scheduler.py` renders the same area twice with only the scheduling knobs
moved, and answers two questions: is the governor faster, and did it change the world.
Full documentation is in `bench/README.md`.

```bash
python bench/bench_scheduler.py --dry-run     # validate the matrix, render nothing. Start here.
python bench/bench_scheduler.py --selftest    # harness logic only. No server, no binary, no network.
python bench/bench_scheduler.py --only smoke  # 4x4 = 16 one-region cells at 1:1, both arms
python bench/bench_scheduler.py               # the full sweep: legacy w4/w12/w16 vs governor auto<=20
python bench/bench_scheduler.py --port 5799 --data-dir C:/tmp/benchdata --only smoke
```

Three things to get right before trusting a sweep:

1. **The binary it measures.** Meld resolves the repo folder, then its parent, then
   `arnis-source/target/release`, then `bin/`. Drop the build you want benched where
   Meld looks **first**, or the sweep will faithfully measure the old one. The spawned
   server prints `arnis binary: <path>` into `bench/results/<label>/server.log`; check
   that line before trusting a run.
2. **Read the right columns.** Judge the governor on `median s`, `p95 s`, `ram peak`
   and `w peak`, **not on wall time alone**. A governor arm that ends at `w peak` 8-12
   with the same `cells/min` as `legacy-w16` and a lower median is the win, even though
   the wall-time delta reads about 0%.
3. **The determinism gate is not advisory.** Every run in a group must produce an
   identical `{cell: block_hash}` vector. A strong mismatch exits `3` and voids every
   timing above it. The region-file fallback (used when `ARNIS_BLOCK_HASH` was not set,
   for instance under `--attach`) is reported as `weak` and is non-fatal by default,
   because `.mca` bytes depend on chunk write order and zlib output, so a mismatch
   there is "look closer", not "the scheduler is broken". `--strict-fallback` makes it
   fatal anyway.

The harness locks the elevation range once and feeds the same range to every run,
bakes elevation tiles and pre-warms Overture before measuring, and never sets
`ARNIS_OFFLINE`: forcing offline mode would change what the generator produces, and
this harness is not allowed to change what the generator produces.

## What a run measures, and where it lands

Phase 1 measured the generator. Everything after the generator exited was untimed,
and the run report had no throughput field at all, so every cells/min figure in this
document was computed by hand from `elapsed_s` and a cell count. Phase 2 closes both
gaps. It adds instruments only: nothing here changes what is generated, merged or
written.

### The post-generator timers

`_runner` in `server.py` now wraps four spans with `time.monotonic()` and reports
them per cell:

| Timer | Covers |
|---|---|
| `merge_s` | the `merge.py` call that copies the cell's regions into the master world |
| `prune_s` | deleting the cell's scratch world after a successful merge |
| `health_s` | `_scan_cell_health` over the cell's log |
| `meta_s` | the project/world metadata writes that follow the merge |

They are recorded per cell and summed over the run, and a cell that merged prints
them after its `MERGE` line (with the `[Prune]` line in between whenever `prune_cell_after_merge` is on, which is the default):

```
MERGE 3,-2: +16 regions, -20 seam, level.dat=copied
  [Timers] 3,-2: merge 0.04s prune 0.01s health 0.00s meta 0.00s
```

**`phase2_timers` (default `True`) gates only that log line, not the measurement.**
The report fields are written either way, because N6 has to be harvestable from any
run; a line per cell is just noise for someone who is not benchmarking. Turning the
setting off therefore costs you nothing except the log line, and turning it on
changes nothing about the world.

`server.TIMER_KEYS` is the single list every consumer reads - the log line, the
per-cell report block and the summed run block - so a fifth span is added in exactly
one place.

**Read them as a tripwire, not as a target.** **Derived** from the phase-1 reports,
the four together come to **4.24-6.54 worker-seconds out of 2401.7, about 0.27% of
worker time** on an 81-cell cs4 run - roughly twelve times below the bench harness's
own noise floor. That number was an estimate; these timers are what turn it into a
measurement, and the pass criterion on it is a ceiling (`<= 7 s` per run), not a
target. They are here so a future change to the post-generator tail is caught getting
worse, not because there is time in there to win.

### The run report: `meld-run-report/4`

`src/runreport.py` bumps `SCHEMA` to `meld-run-report/4`. The bump is **additive
only** - every schema/3 field keeps its name and its meaning, and a schema/3 reader
still parses a schema/4 report because the new keys are absent-safe.

| New field | Type | Meaning |
|---|---|---|
| `summary.cells_per_min` | float | `len(merged cells) / elapsed_s * 60`, computed when the report is built. The number the governor optimises, finally written down instead of recomputed by hand in every write-up. |
| `summary.timers` | object | `{"merge_s", "prune_s", "health_s", "meta_s"}`, floats, summed over the run. |
| `cells[].timers` | object | the same four keys for one cell. |

H2 also stops the harness lying about what it ran. `matrix.json` now declares what
the measured arms actually used, and after every settings apply the harness compares
the live `/api/settings` against `DEFAULT_ASSERT_SETTINGS` - `buildings`, `interior`,
`bake_lighting`, `native_region_format`, `native_blinear_level`, `overture` and
`stream_to_disk` - and **aborts** on a mismatch instead of quietly measuring a
different world. `stream_to_disk` is in that list because it decides which of the two
region-write paths below carries the traffic. Both report schemas are accepted by
the reader (`REPORT_SCHEMAS`), so an old result on disk still harvests.

`summary.timers` is what makes the 0.27% figure above harvestable by the harness
rather than something you grep out of a log by hand.

### The arnis region-count split

A cs4 cell hands arnis a bbox widened by the seam buffer, so the generator writes
6x6 = 36 region files and `merge.py` keeps only the canonical 4x4 = 16. Those 20
discarded files leave the process by one of two paths, never both, and which one
carries them depends on whether the run streamed to disk:

- `flush_region_via` (`src/world_editor/mod.rs`) - the eviction path, taken under
  `ARNIS_STREAM_TO_DISK`, inside the `place` span.
- `save_java` (`src/world_editor/java.rs`) - the end-of-run write of whatever is still
  resident in RAM, inside `save`.

These two are the only callers of `write_region_to_disk`, so their sum is the complete
count of region files a cell wrote. `world_editor::region_stats` counts both and prints
one line per cell, on every exit path of `save_java` including its two early returns
(which is exactly the shape of a fully streamed run, the case that matters most):

```
[regions] flushed=31 saved=5 canonical=16 discarded=20 flushed_discarded=17 saved_discarded=3
```
(shape only - those are illustrative counts, not a measurement.)

`flushed_discarded` versus `saved_discarded` is the whole point: it says which of the
two region-write changes in the plan would actually recover the discarded work. The
prefix is deliberately `[regions]` and not `[meld]`, so it stays a plain diagnostic in
the cell log, is never mistaken for a protocol v1 record, and carries no `N/M` pair or
`parse_progress` keyword that could move the progress bar. It counts; it does not
change which regions are written.

### What is still not measured

- **Per-run CPU seconds.** The per-cell `cpu_s` on the `done` line exists (phase 1,
  `GetProcessTimes`), but it is **not** summed into the run report. Until it is, the
  only run-level CPU figure available is the `timeline[].cpu` integral, and that is
  **not an instrument independent of wall time**: 10 samples at 20 s, each a mean of
  about four `cpu_percent` readings clamped at 100, pinned at 100 through the middle
  of the run. With the cores pinned it collapses to about `elapsed x cores`, so it is
  a *floor on demand* and a "CPU seconds went down" claim built on it is really just
  "wall time went down" restated.
- **The phase split inside `parse` and `place`.** The v1 marker set still reports
  `parse` and `place` as single spans, and the `bench.mark` labels the generator can
  produce are not reachable from a Meld-driven run (nothing in Meld passes
  `--benchmark`).

### Two corrections phase 2 made to phase 1's arithmetic

Both of these contradict figures phase 1 left in the write-ups. They are recorded
here because re-deriving them from the old numbers leads to the wrong plan.

**1. The per-cell profile behind phase 2 was taken on the cheapest cell in the
grid.** Cell `(0,0)` is the NW-corner cell, not the centre. It covers exactly **one**
z11 tile - the smallest of the four, 18.8 MB - where a typical cell covers **four**,
and it ran **12.4 s against a 27.2 s run median**. Ring 0-1 cells (9 of 81) run
11.5-16.8 s; rings 2-4 run 29-32 s. Multiplying that cell's 27.7 cpu-s by 81 is what
produced the phase-1 headroom figure, and it is wrong:

| | phase 1 claimed | corrected |
|---|---|---|
| CPU demand, 81-cell cs4 run | 2244 cpu-s | **>= 3395 cpu-s** |
| CPU-conservation floor on 24 cores | 93.5 s | **~141 s** |
| Warm-run efficiency against that floor | 58% | **~88%** (160.3 s against a 141.5 s floor) |
| Headroom from scheduling alone | ~1.7x | **~1.11-1.15x** |

The corrected column comes from the machine-level `timeline[].cpu` integral in the
real reports, and it is a **floor on demand**, not a measurement of CPU seconds: see
"What is still not measured" above for why that integral cannot be used to claim a
cpu-second win. It is good enough for this correction because it is a *lower* bound
and it already exceeds the old estimate by 51-69%.

The practical consequence: the warm run is already close to the CPU floor, so there
is very little left for a scheduler to find. Further wall-time gains have to come
from removing CPU work, not from arranging it better - which is exactly why the
phase-2 tasks that could deliver seconds are the ones held for review.

**2. Merge offload is dead as a performance idea.** See the timer numbers above:
merge + prune + health + meta together derive to 4.24-6.54 worker-s per 81-cell run,
about 0.27% of worker time, in a mid-run window with no core headroom to overlap into. A
`MergePool` would touch about twenty correctness-critical consumers - run-end,
auto-export, the render-queue driver that rebinds the global `PROJECT`, and the Stop
guarantee that currently holds *by construction* - to chase a quarter of a percent.
It is not deferred pending a better design; it is priced and rejected. Re-open it
only if per-cell CPU work falls far enough that tens of milliseconds are a real
share, and re-measure with these timers first.

Full evidence, file:line citations and the confidence gate for every phase-2 task
are in [`perf-phase2-plan.md`](./perf-phase2-plan.md).

## Determinism rules any future change must respect

1. **Per-cell generator output must not change by default.** Every behavioural change
   in arnis is env-gated off. `scripts/golden_hash.sh` over the 5 fixtures is the gate,
   and it must be run against a **release** build: it does not rebuild for you.
2. **`governor_mode="off"` reproduces the legacy formulas exactly**, including the
   flush cap of 6. The literal 6 is gone from `server.py`, but `flush_threads_cap`
   governs only in advise/auto. Deviating from this is a contract break, not a tuning
   choice. (The measured 65.6 s to 57.1 s flush win is reachable without pool resizing
   by setting `governor_mode="advise"`.)
3. **Existing stdout strings Meld greps must stay byte-identical**: `[gpu] busy_ms=`,
   `[BENCHMARK] block_hash=`, and the phase banners `parse_progress()` keys off. New
   machine output goes in the `[meld] v=1` namespace and is consumed before `on_line()`.
4. **Do not change how Meld spawns the generator.** `arnis.exe` is a GUI-subsystem
   binary on Windows; the current spawn is correct and load-bearing.
5. **One cell per process.** The four asserts above enforce it. Any in-process batching
   must make that config per-editor first.
6. **New settings keys go in four places**: `project.default_settings()`,
   `presets._MACHINE_KEYS`, `server._META_SKIP_SETTINGS`, and a clamp in
   `/api/settings`. Miss the second and a preset carries one machine's tuning onto
   another; miss the third and a world-meta import overwrites local scheduling. This
   holds for a key that nothing reads yet: `canonical_regions` and `parse_fast_json`
   are in all four places today precisely so the feature behind them is a code change
   and not also a settings migration.
7. **Label measured numbers as measured and estimates as estimates.** The
   recommendation table above distinguishes them; keep it that way when you extend it.
8. **A kill switch defaults to today's behaviour, and an unwired one stays `False`.**
   `phase2_timers` defaults `True` only because it is measurement and changes nothing
   that is written. Anything that changes what is generated or written defaults off.
9. **Do not build a per-cell number into a per-run number.** The phase-1 headroom
   figure was one cell's cpu-seconds times 81, and the cell was the cheapest in the
   grid; it overstated available headroom by about 1.5x. Multiply by a measured
   distribution or measure the run.

## Explicitly not done

Named so nobody re-derives them as new ideas.

- **Merge offload - now measured, and rejected on the measurement.** Merging is
  Meld-side and runs after the generator exits, so the governor still measures
  generator wall time and does not see it; `_governor_cell_done` still reports
  `ok=True` for a cell whose merge later fails, which remains a deliberate choice.
  What changed is that the tail is no longer unmeasured: merge + prune + health +
  meta derive to **4.24-6.54 worker-s per 81-cell run, about 0.27% of worker time**,
  and I1's four spans now measure it directly, in every run. Merge still neither parallelises across cells nor overlaps the
  next generation, and on these numbers it should not: see "What a run measures"
  above for why a `MergePool` is priced and rejected rather than deferred.
- **Streaming prefetch.** OSM and elevation prefetch still run as a phase before the
  run rather than overlapping it. Cancellation was fixed on this branch (every entry
  point takes `should_stop`, retry backoff sleeps in 1 s slices instead of up to 6, and
  Stop during a prefetch no longer starts the run minutes later), but the phase is
  still serial.
- **Parallel Overture decode.** Overture was previously measured at about 93% of
  per-cell time when not cached. It is gated on `args.buildings` and disk-cached, but
  the decode itself is still single-threaded per cell.
- **The GPU experiment is done, and it is not the answer.** Measured on the reference
  machine: 1.18x on a 1:1 cell wall (dGPU), 1.10x (iGPU), 1.37x on fleet core-seconds,
  kernel parity 0.0005%, under 200 MB of VRAM. The surprise was that the dGPU and the
  iGPU finished within about 5 s of each other: GPU speed was never the constraint, the
  offloadable share was. Do not expect scheduling wins here.
- **The 1:1 shared-resource wall has not been named.** This is the open question worth
  the most. At 1:1 throughput is flat from 8 to 24 workers while per-cell time triples,
  which means added workers contend on something that is not raw CPU: CPU averaged 79%
  at 24 workers, so there was headroom. Candidates, in rough order of suspicion: memory
  bandwidth and last-level cache, since each cell holds a multi-GB world model; NVMe
  write pressure during region flush; the Windows heap under concurrent NBT allocation
  (a known past offender in this codebase, fixed once with mimalloc in the B_Linear
  converter); and P-core versus E-core placement past 8 workers. **The governor routes
  around this wall by measuring it. It does not explain it.** Whoever names it gets the
  next real speedup, and the instruments now exist: a worker sweep under the bench
  harness with phase markers on should show which phase inflates.
- **Not verified end to end.** No live full render was done with `ARNIS_PHASE_MARKERS`
  on through Meld's own runner: the arnis side proved the protocol with its own live
  runs, the Meld side proved consumption by unit test and signature check. The
  `overture` marker specifically has never fired in a live run, only by construction.
  Only a debug arnis build was smoke-tested for the markers.
