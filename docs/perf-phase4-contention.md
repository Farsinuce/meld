# Phase 4 (experimental) - naming the contention

Branch `perf/speed-to-worldgen-phase4`. This phase is a measurement, not a feature. It exists
because every remaining performance estimate rested on a gap nobody had explained: the
benchmark achieved ~33-35 cells/min against a computed ceiling of 55-60, and the missing 40%
was being called "contention" without anyone measuring what it was.

It turns out the gap was largely an artifact of how the ceiling was computed.

## The experiment

The same Bucharest ring-3 cell, rendered N ways concurrently at 2 threads each, measuring
per-cell wall and per-cell CPU. CPU-seconds are the discriminator: **a core stalled on memory
still burns CPU time; a core waiting on disk or a mutex does not.** So if per-cell CPU
inflates with concurrency, the contention is in the memory system. If wall inflates while CPU
stays flat, it is I/O or locking.

| N | wall | cpu_s | cpu vs N=1 | place_ms | save_ms | elev_ms | cells/min |
|---|---|---|---|---|---|---|---|
| 1 | 14.65 s | 24.50 | 1.00x | 6524 | 2594 | 2820 | 4.1 |
| 2 | 14.78 s | 24.72 | 1.01x | 6486 | 2600 | 2846 | 8.1 |
| 4 | 15.21 s | 25.13 | 1.03x | 6572 | 2704 | 2988 | 15.8 |
| 8 | 17.26 s | 28.52 | 1.16x | 7534 | 3024 | 3253 | 27.8 |
| 12 | 19.58 s | 31.85 | 1.30x | 8208 | 3498 | 3838 | 36.8 |
| **16** | 24.19 s | 33.66 | 1.37x | 10317 | 4300 | 4386 | **39.7** |
| 20 | 30.98 s | 35.73 | **1.46x** | 13315 | 5346 | 5826 | 38.7 |

## What it says

**The contention is the memory system.** Per-cell CPU inflates 46% by N=20. That is stall
time, not extra work - the cell does exactly the same job either way.

**It is a shared resource, not a lock in one phase.** `place`, `save` and `elevation` inflate
by 2.04x, 2.06x and 2.07x respectively between N=1 and N=20 - the same factor, to within a
percent. A mutex in the save path would have inflated `save` alone. Uniform inflation across
three unrelated phases is the signature of memory bandwidth and shared cache.

**Scaling is free to N=4 and gone by N=20.** 1.03x CPU at four concurrent cells; 1.46x at
twenty. Throughput peaks at **N=16 (39.7 cells/min)** and *declines* at N=20 (38.7) - past
the peak, added workers cost more in stall than they deliver.

## The ceiling was computed wrong, and the scheduling gap is not real

The CPU-conservation ceiling was computed as `24 cores x 60 / cpu_per_cell` using the
**uncontended** per-cell cost of ~24.5 cpu-s. But 24.5 cpu-s only happens when one cell runs
alone. At the operating point a cell genuinely costs 33.7 cpu-s.

Recomputed against what a cell actually costs at each concurrency:

| N | real cpu-s/cell | honest ceiling | achieved | efficiency |
|---|---|---|---|---|
| 8 | 28.5 | 50.5 cells/min | 27.8 | 55% |
| 12 | 31.9 | 45.2 | 36.8 | 81% |
| **16** | 33.7 | **42.8** | **39.7** | **93%** |
| 20 | 35.7 | 40.3 | 38.7 | 96% |

**At the operating point the system already runs at 93% of what the hardware allows.** There
is no 40% scheduling gap to recover. Earlier documents in this repo that quote "60% of
ceiling" or "1.7x available from scheduling" are comparing against a number that cannot
exist at 16 concurrent cells, and should be read with this correction.

The practical consequence: **scheduling work is finished.** The governor picks 12-20 workers,
the measured peak is 16, and N=20 is only 2.5% off the peak. Nothing meaningful is left in
worker counts, thread splits or admission - phase 3 already demonstrated the governor beating
both hand-configured arms.

## Cell size: the default is already optimal

Same ground area, same everything else, varying `job_size_regions`:

| cell size | regions written | wall | **region files/min** |
|---|---|---|---|
| 2 | 1156 (289 cells) | 173.7 s | 399 |
| **4** | 1296 (81 cells) | 144.8 s | **537** |
| 8 | 1600 (25 cells) | 244.3 s | 393 |

There is a real optimum and Meld already ships on it. Smaller cells lose to halo overhead - a
2x2 cell touches 4x4 regions to keep 2x2, a 4x overhead against 2.25x for a 4x4 cell - and
the halo is still *generated* even though phase 2 stopped it being *written*. Larger cells
lose because a bigger per-process working set makes the memory contention worse and there are
fewer cells to overlap each other's serial sections.

This was worth testing precisely because it costs nothing to change and could have been free
speed. It is not.

## What this means for 1000 region files/min

1000 region files/min is 62.5 cells/min. The honest steady-state ceiling on this machine is
**42.8 cells/min = 685 region files/min**, and the benchmark achieves 537.

**1000 regions/min is not reachable on this hardware by scheduling, tuning, or cell size.** It
would need per-cell cost to fall by roughly a third *and* the memory contention not to grow
back into the space that frees. Anything that claims otherwise is extrapolating uncontended
per-cell timings, which is exactly the error corrected above.

What could still move the number, in the order the evidence supports:

| lever | resource | measured basis | est |
|---|---|---|---|
| OSM decode dedup | CPU | osm_fetch 946 ms + parse 646 ms = 10.4% of a cell; 8.20 GB decoded per run vs 262 MB distinct | 5-8% |
| GPU blur | **GPU** | bounded at `elev_landcover_repair` = 1501 ms = 9.8% of a cell; mechanism proven at -28% with 8 concurrent processes, device 98% idle | up to 9% |
| Reducing memory traffic | CPU/RAM | the 46% inflation is the largest single quantity in this document, and nothing has been tried against it | unknown |

The third row is now the interesting one. Every prior phase attacked *how work is scheduled*;
the measurement says the remaining loss is *how much memory the work touches*. Smaller working
sets, better data layout, or fewer passes over the grid would attack it directly - and a GPU
kernel helps twice, because it both removes CPU work and moves that work's memory traffic onto
a device with its own bandwidth.

## Method note, for whoever repeats this

`bench/ab_bucharest.py` drives the full benchmark; the concurrency sweep here was a standalone
script running the same arnis command N ways with `--benchmark` and the phase markers on. Two
traps worth avoiding:

* **Do not extrapolate single-cell timings.** Phase 3 predicted that 24 workers x 1 thread
  would deliver 58.3 cells/min from exactly that kind of arithmetic. Measured, it was the
  worst configuration tested (27.25 cells/min). This document's own ceiling error is the same
  mistake in a different costume.
* **Do not byte-compare `.b_linear` files.** Two identical runs share no byte-identical region
  files; the container is not reproducible. Use `ARNIS_BLOCK_HASH=1`.
