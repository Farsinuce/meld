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

**60 cells/min sits above the absolute ceiling** *at today's per-cell cost* - but see the GPU
section below, which is the one measured way to lower that cost enough to reach it. It is not a scheduling problem and no
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

## GPU: I was wrong about contention. Measured, on this machine.

The phase-2 research rejected GPU work partly on the argument that 16-20 arnis processes
would contend for one device. **That argument is false, and the experiment that disproves it
takes ten minutes.** arnis already ships a wgpu compute path for cave density, so it can be
measured rather than argued about.

Machine: NVIDIA GeForce RTX 5080 Laptop (Vulkan), plus an Intel iGPU. Same Bucharest ring-3
cell, caves on, 2 rayon threads, `--canonical-regions` on.

| | wall | CPU | GPU busy |
|---|---|---|---|
| caves, `--gpu off`, 1 process | 32.27 s | 54.22 cpu-s | - |
| caves, `--gpu auto`, 1 process | **23.22 s** | **39.11 cpu-s** | 70 ms |
| caves, `--gpu off`, **8 concurrent** | 37.46 s median | 65.69 cpu-s | - |
| caves, `--gpu auto`, **8 concurrent** | **26.99 s** median | **45.24 cpu-s** | 441 ms *total* |

Single process: **-28.0%** wall. Eight concurrent processes sharing one device: **-27.9%**.
The gain is unchanged. 70 ms of GPU time replaced 15.1 cpu-seconds, and across eight
concurrent cells the device was busy for 441 ms in total - roughly 98% idle. Contention is
not the blocker; the device is nowhere near saturated, and per-process wgpu device creation
is cheap enough to disappear into a 23-second cell.

Two of the four original objections survive, and one is now the whole story.

**Survives - determinism.** Measured on the same cell: `--gpu off` gives
`block_hash=7d3ac20b32e5788a`, `--gpu auto` gives `79cce91095795787`. The paths genuinely
disagree, exactly as the cave module documents (f32 on the GPU against f64 on the CPU). Any
GPU kernel that feeds block choices needs an off-by-default flag and its own hash baseline,
and cannot be compared against the CPU one.

**Survives, but smaller than claimed - the obvious kernel already scales.** `elevation` is
3.53 s of an 18.84 s cell (18.7%) at 2 threads and 0.57 s at 21, so it does scale on CPU.
But the production scheduler deliberately gives each cell ~2 threads, so in the shape that
actually runs, elevation really is ~19% of a cell.

**Dead - "it removes less CPU than the write filter."** That was arithmetic on the wrong
baseline. Elevation is ~4.9 cpu-s of the post-filter 26.188 cpu-s cell.

## What a GPU elevation kernel would be worth

Using the measured 26.188 cpu-s cell and elevation's measured 18.7% share:

| moved to GPU | cpu-s/cell | absolute ceiling | at 85% efficiency |
|---|---|---|---|
| nothing (today) | 26.19 | 55.0 cells/min | 46.7 |
| half of elevation | 23.7 | 60.7 | 51.6 |
| 70% of elevation | 22.8 | 63.3 | 53.8 |
| all of elevation | 21.3 | 67.7 | 57.5 |

**This is what moves 60 cells/min from "above the ceiling" to "plausible".** It is the only
lever identified so far that raises the ceiling that far, because it deletes cpu-seconds
outright rather than redistributing them - the GPU is a second processor, not a rearrangement
of the first.

The built-up Gaussian blur is the natural first kernel: it is already isolated behind
`gaussian_blur_grid`, it already has a bit-exact test (task D2, shipped in phase 2), it is
content-independent dense grid math (2049^2 x 183 taps x 2 passes x 2 blurs = 3.07 G taps per
cell), and D2 means a GPU version can be held to the CPU result or explicitly declared
approximate.

## Proposed phase 3: extend the toggle that already exists

`gpu_accel` is already a Meld setting (`off | auto | dgpu | igpu`) and already reaches arnis
as `--gpu`. It governs cave density alone. Phase 3 is to widen what it governs, not to invent
a mechanism:

1. **Measure first.** Instrument the elevation phase to separate tile decode, interpolation
   and the blur. Only the last two are GPU-shaped, and the split is currently unmeasured -
   the table above assumes elevation is homogeneous, which it is not.
2. **Port the blur.** Reuse `caves/gpu.rs` device setup verbatim. Gate on `gpu_accel`, default
   off. Hold it to D2's bit-exact test if f32 permits; if not, declare the tolerance and give
   it a separate golden baseline, as caves has.
3. **Then interpolation**, if step 1 says it is worth it.
4. **Keep the CPU path as the reference.** Every determinism gate in both phases has run on
   the CPU path and must keep doing so.

Honest risk: f32 will probably not reproduce the f64 blur bit-exactly, so this most likely
ships as an approximate mode like caves. That is a product decision - a cell that differs by
~0.0005% of blocks is invisible in play but breaks any hash-based comparison - and it should
be made deliberately rather than discovered late.

## Better next targets, in order

| target | why | est |
|---|---|---|
| Cold-start convergence | warm hits 79% CPU, cold 60%; same code | 8-12% |
| A1, OSM decode duplication | 31x re-decode, 80-150 cpu-s/run | 3-6% |
| `place` phase | 31% of a cell and scales only 4.5x on 10.5x threads - the worst scaling of any large phase | unknown, needs a profile |
| `parse` / `fetch` | 2.1x and 1.0x scaling; Amdahl fronts inside every cell | modest |
| **GPU elevation/blur** | the only lever measured that can raise the ceiling past 60 cells/min; contention disproved | **15-20% cpu-s** |
