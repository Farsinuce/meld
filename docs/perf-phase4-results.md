# Phase 4 - results and verification

Branch `perf/speed-to-worldgen-phase4`. Two deliverables: the contention measurement (see
`perf-phase4-contention.md`) and one code change, plus the comparison that matters most -
Meld against bare arnis on identical ground.

## Meld against bare arnis - the number the whole project is for

Same 16.4 km Bucharest bbox, same warm caches, same settings, same machine. "Bare arnis" is
one arnis process given the whole box (21 rayon threads, 12 flush threads, stream-to-disk),
which is what the upstream tool does with a large area.

| | bare arnis | Meld (phase 4) |
|---|---|---|
| wall | 181.2 s | **138.4 s** |
| canonical regions | 961 | 1296 |
| **region files/min** | **318** | **562** |
| CPU actually busy | 1867.7 cpu-s / 181.2 s = **10.3 of 24 cores (43%)** | ~76-78% |
| peak RSS | **14.65 GB, one process** | ~1 GB per worker, RAM-gated |

**Meld delivers 1.77x bare arnis on identical ground.**

The reason is now measured rather than asserted: **bare arnis leaves more than half the
machine idle even when handed 21 threads.** A single process cannot fill 24 cores because of
the serial fronts inside one render - fetch, parse, and the barriers between passes. Meld's
tiling runs those serial sections of one cell against the parallel sections of another, which
is exactly what the 43% -> 77% CPU difference is.

The memory figure matters as much as the speed one. Bare arnis peaked at 14.65 GB for this
area, and that scales with the area - double the bbox and it does not fit. Meld's footprint is
per-cell and RAM-gated, so the same machine renders an arbitrarily large area at constant
memory.

Two caveats, stated because they cut the other way:

* **Bare arnis is more CPU-efficient per region** (1.94 cpu-s/region against roughly 2.1),
  because one bbox has one halo ring while 81 cells have 81. Meld trades duplicated halo work
  for occupancy, and wins on wall clock because occupancy is worth more.
* **On a small area Meld does not win.** On a 16x16-region square: bare arnis 40.7 s for 256
  regions (377/min), Meld 54.3 s for 320 regions (354/min) with CPU at only 48%, because 20
  cells is too few for the governor to ramp. The crossover is somewhere above 20 cells.

## The code change: flat-layout Gaussian blur

The vertical pass of the built-up blur built each column with
`after_h.iter().map(|row| row[x])` - pointer-chasing `h` independent heap allocations per
column. One cell reads about 24.6 GB through that path; sixteen concurrent cells demand
roughly 393 GB against roughly 100 GB/s of DDR5.

Replaced with a single 32x32 blocked transpose into a flat column-major buffer, so each column
is a contiguous slice and every cache line is reused.

**Verification:**

| gate | result |
|---|---|
| D2 bit-exact test (reference arm + committed digest) | pass |
| `block_hash` on a real cell | `54e7c9becb2f8f80`, unchanged |
| arnis golden hashes | 5/5 |
| cargo test | 499 passed |
| clippy `--all-targets --all-features -D warnings` | clean |
| cargo fmt --check | clean |

**Measured effect:**

| | before | after |
|---|---|---|
| `elev_landcover_repair`, 1 cell at 2 threads | 1501 ms | 1458-1471 ms |
| wall at 16-way contention | 24.19 s | 23.59 s |
| CPU at 16-way contention | 33.66 cpu-s | 32.51 cpu-s |
| throughput at 16-way contention | 39.7 cells/min | **40.7 cells/min** |

**The hypothesis behind it was only weakly supported, and that is worth recording.** The
gather was expected to hurt disproportionately under contention, because phase 4 identified
memory bandwidth as the binding resource. In fact the gain is about the same at one cell
(-3%) as at sixteen (-3.4%), so the gather was not a significant contention source. The 46%
CPU inflation phase 4 measured comes from somewhere else - most plausibly
`element_placement`, which is 45% of a cell and writes to large block arrays.

The change is kept because it is free: bit-exact, no configuration, no quality risk, and a
permanent 2-3%.

## Full benchmark, end to end

81-cell Bucharest cs4, warm cache, governor auto, canonical regions on:

| run | wall | cells/min | region files/min | CPU | RAM |
|---|---|---|---|---|---|
| 1 | 138.4 s | 35.12 | **562** | 76% | 54% |
| 2 | 142.0 s | 34.23 | 548 | 78% | 52% |

Against the ladder: **230.3 s stock -> 138.4 s, 1.66x**; against a hand-tuned 1.9.7,
**1.26x**; against bare arnis on the same ground, **1.77x**. Every run merged every cell.

## What phase 4 settled

* The contention is the memory system, not I/O or locks (uniform 2.05x inflation across three
  unrelated phases; CPU-seconds inflate, which waiting would not do).
* The system runs at **93% of the honest ceiling** at its operating point. The "40% scheduling
  gap" chased across two phases was an arithmetic error - the ceiling had been computed from
  an uncontended per-cell cost that cannot occur at 16 concurrent cells.
* Cell size is already optimal at 4 (399 / 537 / 393 regions per minute at cs2 / cs4 / cs8).
* The blur gather was not the contention source. `element_placement` is the remaining suspect
  and has never been profiled for memory behaviour.

## What is left, honestly

| lever | est | confidence | note |
|---|---|---|---|
| OSM decode dedup | 5-8% | high | 8.20 GB decoded per run vs 262 MB distinct; quality-neutral |
| GPU blur / elevation | up to 9% | medium | mechanism proven (-28% on caves, device 98% idle) but ships as an approximate mode; the paths hash differently |
| `element_placement` memory behaviour | unknown | - | 45% of a cell, never profiled for cache/bandwidth; the last place the 46% inflation can be hiding |

Nothing else has evidence behind it. Scheduling, worker counts, thread splits, cell size and
merge offload are all settled or measured dead.
