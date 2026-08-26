# Performance work - branch map and phase 3 proposal

## Branch map

All local, none pushed. Two repos move together: `meld-triagefix` (Meld) and
`arnis-triagefix` (the generator). A third worktree, `c:/tmp/meld-ab-baseline`, is pinned at
Meld `main` and exists only so the benchmark has an untouched 1.9.7 to measure against.

```
main (Meld 1.9.7 + arnis 3.1.7)                <- the shipped release, and the A/B baseline
 |
 +-- perf/speed-to-worldgen           PHASE 1   adaptive governor
 |     meld  2d6b017  adaptive governor, exact telemetry, Stop fix
 |     meld  8c1e2c3  relative gain threshold + strike-based stopping
 |     meld  bdaad54  count delivery off the clock, RAM envelope on small grids
 |     meld  629c28d  jump while evidence is strong, then bisect the knee
 |     arnis a1143f70 stdout protocol v1, deterministic fill budget, safer statics
 |      |
 |      +-- perf/speed-to-worldgen-phase2       PHASE 2   cpu-second deletion
 |            meld  9ffec00  phase 2 plan + research annex
 |            meld  ab8f953  open-polygon seam proposal (recorded, not built)
 |            meld  8adf464  the >85% GO set + the measurement it was missing
 |            meld  7a20228  pass each cell's canonical rectangle to arnis
 |            meld  15e9ac7  emit --canonical-regions=VALUE; fail runs with failed cells
 |            meld  3b2a913  idle-machine guard must PREVENT a stop, not hasten one
 |            meld  28b7a10  phase 2 measured results
 |            meld  d7975e8  stock-default baseline + the full ladder
 |            meld  3ae3a6a  phase 3 notes: units, the CPU ceiling
 |            meld  c43e7e7  measured the GPU; the contention claim was wrong
 |            arnis 6b6b0990 region-write counters, golden-hash rebuild, blur bit-exact gate
 |            arnis 5621116b --canonical-regions: never write the halo ring
 |            arnis 59f545df 3.1.8
 |            arnis 71fe56ca allow a negative rectangle
 |             |
 |             +-- perf/speed-to-worldgen-phase3   PHASE 3   <- proposed, this document
 |
 +-- (root repo) perf/adaptive-scheduler 29b8912   the original research plan + annex, docs only
```

Each phase branches from the previous tip, so phase 1 and phase 2 results stay reproducible
and comparable while phase 3 moves.

## Where phase 3 starts from (all measured, 81-cell Bucharest cs4, warm cache)

| configuration | wall | cells/min | region files/min | vs stock | vs hand-tuned |
|---|---|---|---|---|---|
| stock 1.9.7, 4 workers | 230.3 s | 21.10 | 338 | 1.00x | 0.76x |
| 1.9.7 hand-tuned, 16 workers | 174.9 s | 27.79 | 445 | 1.32x | 1.00x |
| phase 1, warm | 160.3 s | 30.32 | 485 | 1.44x | 1.09x |
| phase 2, warm (best) | 137.6 s | 35.32 | 565 | 1.67x | 1.27x |

A cs4 cell is 16 region files, so the third column is the second one times 16.

Phase 2 reached **64.2% of its own CPU-conservation ceiling** (35.32 of 55.0 cells/min at the
measured 26.188 cpu-s per cell). Two independent levers remain: raise the fraction achieved
(scheduling), and lower the cpu-seconds (GPU, decode dedup).

## Phase 3 proposal

### Contents, in the order they should be attempted

| # | item | what it does | basis | est |
|---|---|---|---|---|
| 1 | **Cold-start convergence** | cold runs settle at 6-12 workers and ~60% CPU; warm runs reach 14-18 and 79% on identical code | measured gap, both arms on the same branch | +8-12% |
| 2 | **Elevation phase split** | separate decode / interpolation / blur; only the last two are GPU-shaped and the split is unmeasured | prerequisite for item 4, no gain of its own | 0% |
| 3 | **A1 - OSM decode dedup** | the tile set is decoded once per cell: 8.20 GB per run against 262 MB distinct on disk | phase-2 research, 84% confidence | +3-6% |
| 4 | **GPU: built-up Gaussian blur** | 3.07 G taps/cell of content-independent dense grid math, already isolated behind `gaussian_blur_grid` and already covered by the D2 bit-exact gate | measured: elevation is 18.7% of a cell | +8-13% |
| 5 | **GPU: elevation interpolation** | the rest of the elevation phase, if item 2 says it is worth it | conditional on item 2 | +4-8% |
| 6 | **`place` phase profile** | 31% of a cell and scales only 4.5x on 10.5x threads - the worst scaling of any large phase, and unprofiled | unknown, investigation only | ? |

### Why the GPU items are credible now

The phase-2 research rejected GPU work on a contention argument. That argument was tested
against the wgpu cave path arnis already ships, and it is false:

| | wall | CPU | GPU busy |
|---|---|---|---|
| caves, GPU off, 1 process | 32.27 s | 54.22 cpu-s | - |
| caves, GPU on, 1 process | 23.22 s | 39.11 cpu-s | 70 ms |
| caves, GPU off, 8 concurrent | 37.46 s median | 65.69 cpu-s | - |
| caves, GPU on, 8 concurrent | 26.99 s median | 45.24 cpu-s | 441 ms total |

-28.0% at one process, -27.9% at eight. The gain does not decay, and the device is ~98%
idle. 70 ms of GPU time replaced 15.1 cpu-seconds.

The toggle also already exists end to end: `gpu_accel` (`off | auto | dgpu | igpu`) is a Meld
setting that already reaches arnis as `--gpu`. Phase 3 widens what it governs; it does not
invent a mechanism. Hardware here is an RTX 5080 Laptop (Vulkan) plus an Intel iGPU, and
`--gpu igpu` exists for machines without a discrete card.

### The determinism decision that has to be made deliberately

Measured on one cell: `--gpu off` hashes `7d3ac20b32e5788a`, `--gpu auto` hashes
`79cce91095795787`. The paths genuinely disagree - f32 on the GPU against f64 on the CPU,
exactly as the cave module documents (~0.0005% of blocks).

So a GPU elevation kernel most likely ships as an **approximate mode**, like caves already
does: invisible in play, but it breaks any hash-based comparison and needs its own golden
baseline. Every determinism gate in both phases has run on the CPU path and must keep doing
so, with the CPU path staying the reference. This is a product call, and it should be made
before the kernel is written rather than discovered after.

### Projected results

Held to the same 81-cell Bucharest cs4 benchmark. "eff" is the fraction of the
CPU-conservation ceiling actually achieved; phase 2 reached 64%, and 75% is the assumption
behind every scheduler-improved row - it is an assumption, not a measurement.

| scenario | cpu-s/cell | eff | wall | cells/min | regions/min | vs stock | vs tuned |
|---|---|---|---|---|---|---|---|
| phase 2 today (best warm) | 26.2 | 64% | 138 s | 35.3 | 565 | 1.67x | 1.27x |
| + cold-start convergence | 26.2 | 75% | 118 s | 41.2 | 660 | 1.95x | 1.48x |
| + A1 osm dedup | 25.1 | 75% | 113 s | 43.0 | 687 | **2.04x** | 1.55x |
| + GPU blur | 22.6 | 75% | 102 s | 47.7 | 764 | 2.26x | 1.72x |
| + GPU all elevation | 20.4 | 75% | 92 s | 53.0 | 849 | 2.51x | 1.91x |
| stretch, eff 82% as well | 20.4 | 82% | 84 s | 58.0 | 928 | **2.75x** | 2.09x |

**2x stock arrives at item 3**, before any GPU work, and needs only the scheduler fix plus
the decode dedup. The GPU items are what carry it past that, and they are also what finally
put **60 cells/min** within argument's reach - the stretch row lands at 58.0, so 60 remains a
stretch target rather than a promise.

Confidence, honestly: items 1 and 3 are well-understood and previously scoped (75-85%). Item
4's mechanism is now measured but its size is derived from elevation's wall-clock share, not
from a profile of the blur itself - which is exactly what item 2 exists to fix, and why item
2 comes first. Treat the last three rows as a direction, not a forecast.

### What is deliberately not in phase 3

* **Merge offload** - measured dead in phase 2 at 0.27% of worker time.
* **Sparse overflow patches at the seam** - saves the same cpu-seconds the write filter
  already took, while moving work into the merge path and adding a second merge
  implementation. See the phase-3 candidate section of `perf-phase2-plan.md`.
* **Open polygons at the cell seam** - a real quality defect (Sutherland-Hodgman re-closes a
  clipped building along the bbox line, and the wall renderer draws that edge) but a
  correctness item, not a performance one. Recorded in `perf-phase2-plan.md`.
