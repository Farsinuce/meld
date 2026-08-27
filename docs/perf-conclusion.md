# Performance work - conclusion across phases 1-4

Everything below is measured on this machine (24 cores, 31.4 GB DDR5-6400, RTX 5080 Laptop,
NVMe) on the 81-cell Bucharest cs4 benchmark unless stated. Baseline is stock Meld 1.9.7 +
arnis 3.1.7, i.e. what a fresh install does.

## What was done, and what it bought

| phase | what shipped | measured effect |
|---|---|---|
| 1 | Adaptive governor: closed-loop worker count, per-cell thread/flush grants, RAM-gated admission, exact arnis telemetry, deterministic fill budget, Stop actually stops | 174.9 -> 171.0 s cold, 160.3 s warm; RAM 82% -> 52% |
| 2 | `--canonical-regions`: never write the seam-halo ring the merge deletes (36 -> 16 region files/cell). Plus the >85% GO set: measurement, gate repair, settings, and a live wrong-world-merge race | 137.6 s best warm; identical block_hash |
| 3 | Rate estimator needs a real window; climb opens near the machine; RAM envelope reads `stream_to_disk` | cold 173.9 -> **144.8 s**, CPU 60% -> 79%; every run now 139-145 s instead of 138-186 s |
| 4 | Measurement only: named the contention, corrected the ceiling, settled cell size | no code change; corrected three wrong numbers |

**Result: 230.3 s -> 144.8 s cold, 139.1 s warm. 1.59-1.66x against a stock install, 1.21-1.26x
against a hand-tuned one. 338 -> 559 region files/min.** Output byte-identical throughout;
golden hashes 5/5; Meld 533 tests, arnis 499, clippy clean.

## The single most important finding

**The system already runs at 93% of what this hardware allows, and the "scheduling gap" I
spent two phases chasing was an arithmetic error.**

The CPU-conservation ceiling was computed from the *uncontended* per-cell cost of 24.5 cpu-s.
That only happens when one cell runs alone. At the operating point a cell genuinely costs
33.7 cpu-s, because concurrency inflates it:

| N concurrent | cpu-s/cell | honest ceiling | achieved | efficiency |
|---|---|---|---|---|
| 4 | 25.1 | 57.3 cells/min | 15.8 | 28% |
| 8 | 28.5 | 50.5 | 27.8 | 55% |
| 12 | 31.9 | 45.2 | 36.8 | 81% |
| **16** | 33.7 | **42.8** | **39.7** | **93%** |
| 20 | 35.7 | 40.3 | 38.7 | 96% |

Throughput peaks at 16 workers and *declines* at 20. Any earlier document here quoting "60%
of ceiling", "1.7x available from scheduling" or "a 40% gap" is comparing against a number
that cannot exist at 16 concurrent cells.

**Scheduling work is finished.** Phase 3 already showed the governor beating both
hand-configured arms; phase 4 explains why there was nothing left for it to find.

## Is the GPU worth it? Yes - and for a better reason than the one I first gave

I originally rejected GPU work on a contention argument: 16-20 processes fighting over one
device. **That argument is false and the test is ten minutes**, because arnis already ships a
wgpu cave-density kernel:

| | wall | CPU | GPU busy |
|---|---|---|---|
| caves, GPU off, 1 process | 32.27 s | 54.22 cpu-s | - |
| caves, GPU on, 1 process | 23.22 s | 39.11 cpu-s | 70 ms |
| caves, GPU off, 8 concurrent | 37.46 s | 65.69 cpu-s | - |
| caves, GPU on, 8 concurrent | 26.99 s | 45.24 cpu-s | 441 ms total |

-28.0% at one process, -27.9% at eight. The device is ~98% idle; 70 ms of GPU time replaced
15.1 cpu-seconds.

Now note something phase 4 makes visible: the CPU saving **grew** with concurrency, from
-27.9% at one process to **-31.0% at eight**. That is what you expect if the GPU is relieving
memory pressure and not just compute - the offloaded work stops competing for DDR5.

**So the GPU is worth it, and worth more than its share of CPU time suggests**, because at the
operating point the binding resource is memory bandwidth and a discrete GPU brings its own.

The catch is unchanged and confirmed, not assumed: the same cell hashes `7d3ac20b32e5788a` on
CPU and `79cce91095795787` on GPU. f32 against f64, ~0.0005% of blocks. Any GPU kernel feeding
block choices ships as an **opt-in approximate mode**, exactly as caves already does, with its
own golden baseline. That is a product decision, and it should be made before the kernel is
written.

## Where a cell's time goes, and what is left

Ring-3 cell, 2 threads, 15.275 s:

| stage | ms | share | scales? |
|---|---|---|---|
| element_placement | 6939 | 45.4% | 10.1x on 21 threads - fine |
| elevation | 2860 | 18.7% | 6.7x |
| save | 2678 | 17.5% | 6.5x |
| osm_fetch + parse_osm | 1592 | 10.4% | ~1x - serial |
| everything else | 1206 | 8.0% | - |

Threads are genuinely free up to 8 per cell (CPU flat at 24.1-24.8 cpu-s at T=1/2/4/8) and
cost 17-23% above that. Cell size is already optimal: 399 regions/min at cs2, **537 at cs4**,
393 at cs8.

## What to do next, in order

### 1. Flat-layout Gaussian blur (bit-exact) - the best remaining item

`gaussian_blur_grid_reported` operates on `Vec<Vec<f64>>`, and its vertical pass does:

```rust
let column: Vec<f64> = after_h.iter().map(|row| row[x]).collect();
```

That is a strided gather across 2049 independent heap allocations - one pointer chase per row,
per column, every column. The arithmetic for one cell: 2049^2 grid x 183 taps x 2 passes x 2
blurs = 3.07 G tap-reads = **24.6 GB of reads per cell**. At 16 concurrent cells that is
~393 GB of demand against roughly 100 GB/s of DDR5.

This is the clearest identified contributor to the contention phase 4 measured, it is inside
the single largest elevation stage (`elev_landcover_repair`, 1501 ms, 9.8% of a cell), and
**the bit-exact gate for it already exists** - task D2 shipped in phase 2 specifically so this
change would have something to fail against.

Flattening to a single `Vec<f64>` with row-major indexing, and transposing once for the
vertical pass instead of gathering per column, is a contained change with a ready test and no
quality risk. It attacks bandwidth directly, which is the binding resource.

### 2. OSM decode dedup - largest deletable CPU block

`osm_fetch` 946 ms + `parse_osm` 646 ms = 10.4% of a cell, and the underlying waste is
measured: 8.20 GB of JSON decoded per 81-cell run against 262 MB distinct on disk, a 31x
duplication, because every cell is a separate process decoding the same z11 tiles. A
pre-decoded artifact produced once per run and memory-mapped by every cell removes most of it.
Estimated 5-8%, quality-neutral by construction provided the artifact is keyed so a stale
entry cannot change output.

### 3. GPU blur - after 1, and only with the approximate-mode decision made

If item 1 lands, measure again before building this: a flat-layout CPU blur may take enough of
the cost that the GPU kernel is no longer the best use of the effort. If it is still worth it,
the mechanism is proven and the pattern to copy is `caves/gpu.rs`.

### 4. Nothing else has evidence behind it

`element_placement` is 45.4% of a cell but scales 10.1x and is the actual work of building the
world; reducing it is an algorithmic and quality question, not a performance one. Merge
offload is dead (0.27% of worker time). Sparse seam patches save the same cpu-seconds the
write filter already took. Cell size is optimal. Worker and thread counts are solved.

## On 1000 region files/min

1000 region files/min is 62.5 cells/min. The honest steady-state ceiling on this machine is
**42.8 cells/min = 685 region files/min**; the benchmark achieves 537.

**It is not reachable on this hardware**, and no amount of scheduling gets there - that was
settled by phase 4. Items 1-3 above might plausibly reach 600-650 regions/min, which would be
~88-95% of the physical ceiling. Beyond that needs either less work per region or a machine
with more memory bandwidth.

## Two traps recorded, because both caught me

* **Never extrapolate single-cell timings to machine throughput.** Phase 3 predicted 24
  workers x 1 thread would deliver 58.3 cells/min from exactly that arithmetic. Measured, it
  was the worst configuration tested at 27.25. The ceiling error above is the same mistake
  wearing different clothes.
* **Never byte-compare `.b_linear` files.** Two identical runs share zero byte-identical
  region files; the container is not reproducible. Use `ARNIS_BLOCK_HASH=1`, which is taken
  from in-memory content before any write.
