# Phase 3 - measured results

Branch `perf/speed-to-worldgen-phase3`, both repos. Same benchmark as phases 1 and 2:
81-cell Bucharest cs4, 16.4 km bbox, 1:1 scale, blinear + stream-to-disk, warm shared cache,
`bench/ab_bucharest.py`. Baseline is stock Meld 1.9.7 + arnis 3.1.7.

## Headline: phase 3 raised the floor, not the ceiling

| configuration | cold | warm | cells/min | region files/min |
|---|---|---|---|---|
| stock 1.9.7, 4 workers | 230.3 s | - | 21.10 | 338 |
| 1.9.7 hand-tuned, 16 workers | 174.9 s | - | 27.79 | 445 |
| phase 1 | 171.0 s | 160.3 s | 30.32 | 485 |
| phase 2 | 173.9 s median | 137.6-149.2 s | 35.32 best | 565 |
| **phase 3** | **144.8 s median** | **139.1-143.9 s** | **34.95** | **559** |

Phase 3 cold repeats: 145.4 / 144.3 / 144.8 s. Warm: 143.9 / 139.1 s.

The best single number barely moved (137.6 s in phase 2, 139.1 s here). What changed is that
**every** run is now that fast. Phase 2 ranged 137.6-186.4 s depending on how the governor's
climb happened to go; phase 3 lands 139-145 s regardless. Cold runs improved **173.9 s ->
144.8 s (1.20x)** and CPU utilisation went from ~60% to 76-80%.

Against the ladder: **1.59x stock cold, 1.66x warm; 1.21x and 1.26x against the hand-tuned
baseline.** Consistency is the deliverable here - a first render of a new project is no
longer materially worse than a repeat.

## What was wrong, found by reading the governor's own log

Two defects, both in phase 1/2 code, both invisible from the outside because the runs
completed successfully.

**The delivered-rate estimator was measuring bursts, not throughput.** Workers start
together, so they finish together. Three completions 0.7 s apart read as 171 cells/min. The
decision log showed the consequences directly:

```
4 -> 6 workers   (21.3 cells/min)
6 -> 12 workers  (31.0 cells/min)
12 -> 8 workers  (40.4 cells/min)      <- the best reading, and it stepped DOWN
steady at 8 workers (-58.0 cells/min after 4 strikes -> back to 6w)
throughput drifted 78%, recalibrating
... and again, and again
```

A swing of -176.9 cells/min appears later in the same log. Throughput cannot move like that;
the estimator was reporting the gap between two neighbours inside one batch. A reading is now
believed only once its window covers `MIN_RATE_SPAN_S` (10 s) of wall clock. Until then the
level is held and the window grows - `None` means "keep measuring", never "decide on
nothing".

That alone fixed convergence: the pool now reaches 18-20 workers instead of settling at 6.

**Opening the climb at a flat 4 wasted half the run.** With convergence fixed the pool
arrived at the right answer but the wall time barely moved, because an 81-cell run spent more
cells climbing than working: eight `+2` steps, each waiting 10 s for an honest reading. The
opening is now scaled off the machine (half its cores) and then passed through the RAM
envelope.

**The RAM envelope was 4x too pessimistic whenever streaming was on.** It used server.py's
non-streaming figure of 4.15 GB per 1:1 cell. But `stream_to_disk` exists precisely to cap
how many regions stay resident, and the measured peak on this branch is **1008-1085 MB**.
With 20 GB free that admitted only 4 workers, which then capped the opening above. The
estimate now reads `stream_to_disk`; a real p95 replaces it after the first few cells either
way.

## Portability - the part that has to work on hardware that is not this desktop

Nothing in the opening encodes 24 cores or 31 GB. It is `cores x fraction`, bounded by the
machine's own free RAM and by the user's ceiling, and it is covered by tests that assert the
shape rather than the number:

| machine | free RAM | opens at |
|---|---|---|
| 24 cores | 20 GB | 12 |
| 24 cores | 12 GB | 7 |
| 24 cores | 6 GB | 3 |
| 16 cores | 16 GB | 8 |
| 8 cores | 16 GB | 4 |
| 8 cores | 4 GB | 1 |
| 4 cores | 8 GB | 4 (its ceiling) |
| 2 cores | 3 GB | 1 |

`TestPortableAcrossMachines` pins: never above the ceiling for any (cores, ceiling) pair;
a memory-poor machine opens low and RAM beats cores; every machine opens at least 1 so a run
can always start; opening is monotonic in cores when RAM allows; streaming admits more than
non-streaming; and a missing psutil probe degrades instead of stopping the run.

## Elevation phase breakdown (item 2 - the measurement the GPU work needed)

Measured with `--benchmark` on the Bucharest ring-3 cell, 2 threads:

| stage | ms | share of elevation |
|---|---|---|
| `elev_landcover_repair` (contains the built-up Gaussian blur) | 1501 | 52.5% |
| `elev_raw_fetch` (I/O) | 634 | 22.2% |
| `elev_repair_anomalies` | 502 | 17.6% |
| `elev_landcover_fetch` | 199 | 7.0% |
| everything else | 20 | 0.7% |
| **terrain_total** | **2860** | 100% |

The cell was 15.275 s, so elevation is **18.7%** of it - independently matching the
phase-marker measurement taken a different way. The GPU-shaped candidate is therefore bounded
by `elev_landcover_repair`: **1501 ms, 9.8% of a whole cell**, and the blur is only part of
that stage. This is what an honest GPU estimate has to be built on, and it is smaller than
the "move all of elevation" row in the phase-3 proposal assumed.

## Generation output is unaffected

* arnis golden hashes 5/5 identical, against a binary the script rebuilds itself.
* Every phase-3 change is Meld-side scheduling except the elevation measurement, which used
  the pre-existing `--benchmark` flag and changed no code.
* All benchmark runs merged every cell with zero failures (81/81 and 25/25).
* Meld 533 tests, arnis 499 tests, `clippy --all-targets --all-features -- -D warnings` clean.

## Where the remaining time is, and what 1000 regions/min would need

At 34.95 cells/min we are at **559 region files/min**. 1000 region files/min is **62.5
cells/min**, which needs the per-cell CPU cost to fall to about 23 cpu-s *and* an efficiency
near 90% - or a bigger cut with today's efficiency.

Measured per-cell CPU is 26.188 cpu-s, so the CPU-conservation ceiling on 24 cores is 55.0
cells/min = 880 regions/min. **1000 regions/min is above the ceiling at today's per-cell
cost**; reaching it requires deleting cpu-seconds, not scheduling them better:

| lever | measured basis | est |
|---|---|---|
| A1 OSM decode dedup | 8.20 GB decoded per run vs 262 MB distinct (31x) | 3-6% |
| GPU: the blur inside `elev_landcover_repair` | bounded at 1501 ms = 9.8% of a cell | up to 9% |
| `place` phase | 31% of a cell, scales only 4.5x on 10.5x threads, still unprofiled | unknown |

`place` is the largest unexamined block and the only one that could plausibly move the
ceiling far enough on its own. It should be profiled before any more GPU design work.
