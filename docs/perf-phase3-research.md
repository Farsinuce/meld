# Phase 3 - what the tests say about going faster

Everything here is measured on this machine (24 cores, 31.4 GB, RTX 5080 Laptop, NVMe) on the
Bucharest ring-3 cell or the 81-cell cs4 benchmark. Where a number is derived rather than
measured it says so.

## The full cell breakdown, measured

Ring-3 Bucharest cell, 2 threads, `--benchmark`, canonical regions on. Cell wall 15.275 s.

| stage | ms | share of cell |
|---|---|---|
| **element_placement** | **6939** | **45.4%** |
| terrain_total (elevation) | 2860 | 18.7% |
| save | 2678 | 17.5% |
| osm_fetch | 946 | 6.2% |
| parse_osm | 646 | 4.2% |
| landcover_osm_repair | 481 | 3.1% |
| post_passes | 257 | 1.7% |
| tile_merge | 217 | 1.4% |
| precompute | 140 | 0.9% |

Elevation subdivides as: `elev_landcover_repair` 1501 ms (52.5% of elevation),
`elev_raw_fetch` 634, `elev_repair_anomalies` 502, `elev_landcover_fetch` 199.

## Thread scaling - threads are free up to 8, then they are not

Same cell, sweeping `RAYON_NUM_THREADS` and `ARNIS_FLUSH_THREADS` together:

| threads | place_ms | save_ms | elev_ms | wall | **cpu_s** |
|---|---|---|---|---|---|
| 1 | 11616 | 4929 | 4992 | 24.69 s | 24.27 |
| 2 | 6550 | 2561 | 2748 | 14.64 s | 24.64 |
| 4 | 3828 | 1347 | 1756 | 9.44 s | 24.06 |
| 8 | 2045 | 705 | 1050 | 6.27 s | 24.80 |
| 16 | 1315 | 429 | 861 | 5.01 s | 28.25 |
| 21 | 1153 | 414 | 853 | 4.83 s | 29.61 |

Two things matter here.

**Total CPU per cell is flat at ~24.1-24.8 cpu-s for 1, 2, 4 and 8 threads.** Parallelism
inside a cell is genuinely free in that range - it converts wall time without inventing work.
Above 8 it costs 17-23% more CPU for progressively less wall.

**`element_placement` scales far better than previously reported.** 11616 -> 1153 ms is
**10.1x on 21 threads**. An earlier note in this repo claimed 4.5x; that figure came from
comparing phase-marker spans that also contained the tile merge, and it was wrong. Placement
is not a scaling problem - it is simply the largest block of real work.

## A hypothesis the measurements produced, and the test that killed it

If per-cell CPU is flat in thread count, then machine throughput ought to be
`workers x 60 / cell_wall` with `workers = 24/threads`, which predicts:

| threads | workers | predicted cells/min |
|---|---|---|
| 1 | 24 | 58.3 |
| 2 | 12 | 49.2 |
| 4 | 6 | 38.1 |
| 8 | 3 | 28.7 |

Fewer threads per cell should win outright. Tested on the real 81-cell benchmark:

| configuration | wall | cells/min |
|---|---|---|
| 20 workers x 1 thread | 178.3 s | 27.25 |
| 12 workers x 2 threads | 167.6 s | 28.99 |
| **governor (adaptive)** | **144.8 s** | **33.42** |

**The prediction was wrong and 1 thread is the worst of the three.** Single-cell timings
scaled linearly ignore contention: twenty concurrent cells do not each run at the speed one
cell runs at alone. The lesson is that per-cell microbenchmarks cannot be extrapolated to
machine throughput on this workload, and any future claim of the form "N workers x M threads
would be faster" has to be run, not derived.

Worth stating plainly: **the adaptive governor beat both hand-configured arms.** Whatever is
left to win is not in the workers-x-threads split.

## Where the remaining time actually is, and what it would cost to take it

Ranked by measured size, with the resource each one would spend.

### 1. OSM decode duplication - CPU, best ratio of gain to risk

`osm_fetch` 946 ms + `parse_osm` 646 ms = **1592 ms, 10.4% of a cell**. The phase-2 research
measured the underlying waste: 8.20 GB of JSON decoded across an 81-cell run against 262 MB
distinct on disk, a **31x duplication**, because each cell is a separate process that decodes
the same z11 tiles from scratch.

An in-process cache cannot help across processes. What can: a compact pre-decoded artifact
that Meld produces once per run and every cell memory-maps, or a binary cache format next to
the tile dir. Estimated **5-8% of the cell**, quality-neutral by construction (same elements,
same order - the artifact must be keyed so a stale entry cannot change output).

### 2. The built-up Gaussian blur - GPU, bounded and now measured

`elev_landcover_repair` is 1501 ms, **9.8% of a cell**, and the blur is a part of it. The GPU
mechanism is proven on this machine: the existing cave kernel gives -28.0% wall at one
process and **-27.9% at eight concurrent processes**, with the device ~98% idle (441 ms busy
across eight cells). Contention is not the obstacle.

The obstacle is determinism, and it is confirmed rather than assumed: the same cell hashes
`7d3ac20b32e5788a` on CPU and `79cce91095795787` on GPU. f32 against f64. So this ships as an
opt-in approximate mode like caves, or not at all. Estimated up to **9%**, and it is the only
lever that spends the GPU rather than the CPU.

### 3. `save` - RAM and disk, already well parallelised

2678 ms, 17.5%, scaling 6.5-12x. The write filter shipped in phase 2 already removed 20 of 36
region files. What is left is real output. Compression level is a CPU-vs-disk dial
(`native_blinear_level`, currently 6) that has not been swept - a cheap experiment, but the
gain is bounded by save being 17.5% and already scaling.

### 4. `element_placement` - CPU, biggest block, no easy handle

45.4% of the cell and scaling 10.1x on 21 threads. There is no scheduling win here; it is the
work of actually building the world. Reducing it means doing less per block or fewer blocks,
which is an algorithmic and quality question rather than a performance one. Any change here
must be held to the golden-hash gate.

## What this means for 1000 region files/min

A cs4 cell is 16 region files, so 1000 regions/min = **62.5 cells/min**. Measured per-cell CPU
is 24.1-26.2 cpu-s depending on run, so the CPU-conservation ceiling on 24 cores is **55-60
cells/min = 880-955 regions/min**, and the benchmark currently achieves 33.4-35.0 cells/min
(535-560 regions/min), i.e. about 60% of that ceiling.

**1000 regions/min sits at or just above the ceiling** at today's per-cell cost. Reaching it
needs both: the ~15% of cpu-seconds that items 1 and 2 would delete, *and* closing most of
the remaining 40% gap to the ceiling - and the last experiment shows that gap is contention,
not a bad worker/thread split. Nothing measured so far explains where the contention goes;
that is the honest next question, and it is a measurement question (memory bandwidth, shared
cache, allocator, or disk), not a tuning one.

## Recommended order, and why

1. **Profile the contention itself.** The gap between 33.4 cells/min achieved and ~55
   theoretical is the single largest remaining quantity and nobody has measured what it is.
   Everything else is guesswork until this is named.
2. **OSM decode dedup** - largest measured deletable block, quality-neutral, CPU.
3. **blinear level sweep** - one afternoon, no code, may be free.
4. **GPU blur** - mechanism proven, size bounded at 9%, needs the approximate-mode decision
   made up front.
5. **element_placement** - only with a specific algorithmic idea and the hash gate in hand.
