# Phase 3 - notes and estimates

Written after phase 2 landed. Nothing here is built. Numbers are measured where marked and
derived where marked; the derivations are shown so they can be checked rather than trusted.

## What a "cell" is, and what "cells/min" means

A **cell** is one Meld job: one `arnis.exe` process rendering one square of regions. At
`job_size_regions = 4` a cell owns **4x4 = 16 region files**, and the 81-cell Bucharest
benchmark writes **1296 regions** (confirmed in every run report: `regions: 1296`).

So the throughput figures convert as:

| | cells/min | region files/min | blocks/min (approx) |
|---|---|---|---|
| stock 1.9.7, 4 workers | 21.10 | 338 | 88.6 G |
| phase 2, warm (best) | 35.32 | 565 | 148.3 G |

One region is 512x512 blocks across the build height, so "region files/min" is the number to
quote when comparing against anything that measures Anvil output rather than Meld jobs.

## Can we reach 60 cells/min? No - not at today's per-cell cost

The hard bound is CPU conservation: a 24-core machine delivers 24 cpu-seconds per wall
second, so `cells/min = 24 * 60 / cpu_seconds_per_cell`. Per-cell CPU is **measured**
(Bucharest ring-3 cell, 2 threads, uncontended): 29.859 cpu-s before the write filter,
**26.188 cpu-s after**.

| per-cell CPU | absolute ceiling | at 85% efficiency | at 90% |
|---|---|---|---|
| 29.859 cpu-s (pre-phase-2) | 48.2 cells/min | 41.0 | 43.4 |
| **26.188 cpu-s (today)** | **55.0 cells/min** | **46.7** | 49.5 |

**60 cells/min sits above the absolute ceiling.** It is not a scheduling problem and no
amount of worker tuning reaches it - it would need per-cell CPU at or under 24 cpu-s *and*
100% efficiency, which is not a thing. Reaching 60 realistically means cutting per-cell CPU
by roughly a further 25%, to ~20 cpu-s.

Caveat on the ceiling, stated because it flatters the model: 26.188 cpu-s is measured
**uncontended**. Sixteen concurrent cells cost more CPU each than one cell alone (cache and
memory-bandwidth pressure), which is why the run sits at 35.3 cells/min while the model
predicts 43.4 at its measured 79% CPU. The true ceiling is therefore somewhat below 55.

## Is 2x stock reachable? Yes, and probably without a GPU

2x stock = **42.2 cells/min = 115 s** for the 81-cell render. That is under the 46.7
cells/min the model allows at 85% efficiency, so it is not blocked by physics. Today's best
is 35.32 cells/min (137.6 s, 1.67x). The gap is 22 seconds, and there are two known,
already-scoped sources for it:

1. **Cold-start convergence** (scheduling). Warm runs reach 14-18 workers at 79% CPU; cold
   runs settle at 6-12 and ~60%. Fixing the climb mainly rescues cold runs, but the same
   under-spend caps warm runs too - 79% is not 90%. Worth an estimated 8-12%.
2. **A1, the OSM re-deserialisation** (cpu-seconds). The tile set is decoded once per cell:
   8.20 GB decoded across a run against 262 MB distinct on disk, a 31x duplication.
   Estimated 80-150 cpu-s per run recoverable, i.e. 3-6%.

Together those plausibly land 115-125 s, which is **1.85-2.0x stock**. Neither needs a GPU.

## Why there is no GPU work, and what the catches are

GPU offload was designed and scored during phase-2 research. It did not clear the confidence
gate and was not built. The reasons are specific, not squeamishness:

**1. The process model is the wrong shape for it.** Meld runs *one arnis process per cell*,
16-20 of them at once. Each would create its own wgpu device, upload its own buffers and
queue its own dispatches against a single physical GPU. Device creation alone is tens to
hundreds of milliseconds, and the queue becomes a contended global resource exactly when the
scheduler is trying to run many cells in parallel. The existing cave GPU path was measured on
*one* process; nothing about that result transfers to sixteen.

**2. The obvious kernel is already parallel on the CPU.** The `elevation` phase is the
GPU-shaped candidate - dense uniform grid math. But it is 3.82 s at 2 threads and 0.57 s at
21: it already scales 6.7x. It is 20% of a cell only because each cell is deliberately given
few threads. Moving it to a contended GPU trades good CPU scaling for a queue.

**3. Determinism.** arnis's existing GPU path is documented as *approximate by contract*: f32
on the GPU shifts roughly 0.0005% of blocks versus f64 on the CPU. That is acceptable for
cave density, which nothing else keys off. Elevation feeds block heights, so an f32 shift
moves terrain, changes block choices, and breaks the golden-hash gate that every other change
in both phases has had to pass. It would need an off-by-default flag and its own hash
baseline - i.e. an output that cannot be compared against the CPU one.

**4. It removes less CPU than what already shipped, for far more risk.** Elevation is ~3.8
cpu-s of a 28.8 cpu-s cell - about 13%. The region write filter removed **12.3%** with an
*identical* block_hash, a 40-line change and no new dependency. GPU work would be a large,
risky change competing for the same order of magnitude.

**When it would become worth revisiting:** if per-cell CPU is driven low enough that the
remaining cost is dominated by dense float grids, or if the process model changes so one
process renders many cells and can own the GPU for the whole run. Neither is true today.

## Better next targets, in order

| target | why | est |
|---|---|---|
| Cold-start convergence | warm hits 79% CPU, cold 60%; same code | 8-12% |
| A1, OSM decode duplication | 31x re-decode, 80-150 cpu-s/run | 3-6% |
| `place` phase | 31% of a cell and scales only 4.5x on 10.5x threads - the worst scaling of any large phase | unknown, needs a profile |
| `parse` / `fetch` | 2.1x and 1.0x scaling; Amdahl fronts inside every cell | modest |
| GPU | see above | deferred |
