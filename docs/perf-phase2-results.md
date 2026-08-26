# Phase 2 - measured results

Branch `perf/speed-to-worldgen-phase2`, both repos. Same Bucharest-centre bbox as phase 1
(16.4 km, Piata Unirii, 1:1 scale, blinear + stream-to-disk, warm shared cache), driven by
`bench/ab_bucharest.py`. The baseline is released Meld 1.9.7 + arnis 3.1.7 hand-tuned to 16
workers - a real opponent, not the stock default of 4.

## The full ladder

Every row is the same 81-cell Bucharest cs4 render, same warm cache, GPU off. "Stock" is
what a fresh install actually does - the shipped default of 4 workers - and it is the honest
denominator for "how much faster did this get", because the hand-tuned 16-worker baseline is
already a third of the way up the ladder.

| configuration | wall | cells/min | vs stock | vs hand-tuned |
|---|---|---|---|---|
| stock 1.9.7, 4 workers (shipped default) | 230.3 s | 21.10 | 1.00x | 0.76x |
| 1.9.7 hand-tuned to 16 workers | 174.9 s | 27.79 | 1.32x | 1.00x |
| phase 1 governor, cold | 171.0 s | 28.42 | 1.35x | 1.02x |
| phase 1 governor, warm | 160.3 s | 30.32 | 1.44x | 1.09x |
| phase 2, cold (median of 3) | 173.9 s | 27.95 | 1.32x | 1.01x |
| phase 2, warm (median of 3) | 141.7 s | 34.30 | **1.63x** | **1.23x** |
| phase 2, warm (best) | 137.6 s | 35.32 | **1.67x** | **1.27x** |

Phase 2 over phase 1, warm: **1.13x** median, 1.16x best. The write filter isolated against
its own branch with the filter off: 163.5 s -> 141.7 s, **1.15x**.

## GPU: nothing, and nothing was expected

No GPU work was implemented in either phase. `gpu_accel` was `off` in every benchmark run,
neither `--gpu` nor `--caves` appears in any command line, and the phase markers report
`gpu_ms=0` for every cell profiled. arnis has had a wgpu compute path since before this work
(`src/caves/gpu.rs`) but it evaluates cave density only, and these renders have no caves.
GPU offload was designed and scored during phase-2 research and did not clear the confidence
gate; it remains unbuilt. Any speedup above is CPU-side work only.

## Headline

| configuration | cs4 cold | cs4 warm (runs 2+) |
|---|---|---|
| baseline 1.9.7, 16 workers | 174.9 s | - |
| phase 1 (governor) | 171.0 s | 160.3 s |
| phase 2, write filter OFF (control) | 172.8 s | 163.5 s |
| **phase 2, write filter ON** | 173.7 / 173.9 / 175.7 s | **137.6 / 141.7 / 149.2 s** |

Best measured phase-2 run: **137.6 s against the 174.9 s baseline, 1.27x**; median of the
three warm runs 141.7 s, **1.23x**. Against phase 1's warm run, **1.13x**. Every run merged
every cell with zero failures.

## What actually produced the gain

`--canonical-regions` stops arnis writing the seam-halo region ring that Meld deletes on
merge. Measured on one real cell (Bucharest ring-3, 2 threads, same bbox and seed):

| | filter off | filter on |
|---|---|---|
| block_hash | `54e7c9becb2f8f80` | `54e7c9becb2f8f80` |
| region files written | 36 | 16 (20 skipped) |
| wall | 17.752 s | 15.707 s (-11.5%) |
| CPU | 29.859 cpu-s | 26.188 cpu-s (-12.3%) |

The identical block_hash is the gate that matters: it is taken from the in-memory content
before any file is written, so it proves the flag changes what is *written*, never what is
*placed*. The halo is still generated - blocks spilling across a seam are still authored,
and the neighbouring cell renders that ground itself.

**Do not byte-compare `.b_linear` files to check this.** Two identical unfiltered runs
produce 0 of 36 byte-identical region files: the container is not reproducible at the byte
level, so such a comparison proves nothing in either direction. This was checked with a
control run rather than assumed.

## Two defects the benchmark caught

**A negative rectangle was read as a flag.** A cell west or north of the origin owns a
rectangle starting with a minus, and clap rejected `-4,-1,0,3` outright. 36 of 81 cells
failed - and because failures shorten a run, the first phase-2 attempt *presented as a
52-second speedup*. Fixed with `allow_hyphen_values` plus the `--flag=VALUE` spelling, and
the harness now fails any row whose report carries failed cells. A run that did less work
can never look like a faster one again.

**The idle-machine guard could not keep the climb alive.** Three cs4 runs converged at 8
workers with CPU at 53-60% - sixteen cores idle - costing 8%. Reproducible at 186.4 / 186.3
/ 187.1 s. The cause was in the governor, not the write filter: `_budget_spent()` appeared
in the stop condition only as `(gain < 0 and spent)`, so it could make a stop happen sooner
but never prevent one, the opposite of what its own comment promised. Cheaper cells make
each +2 step a smaller *fraction* of throughput, so the relative threshold is reached while
headroom remains. Requiring 4 consecutive non-paying steps while cores are idle (2 when the
CPU budget is spent) recovered 186 s -> 174 s.

## The remaining gap, stated plainly

Cold runs still under-converge: 6-12 workers at ~60% CPU, against 18 workers and 80% CPU on
the same branch with the filter off. The warm runs show what the machine can actually do
with the same code - 14-18 workers, 79% CPU, 137-149 s - so the ceiling is not the write
filter and not the hardware, it is the governor's climb on a cheaper cost curve. That is the
obvious next target, and it is a scheduling problem, not a cpu-second problem.

Note the interaction, because it is counter-intuitive: with the filter on, 6 workers of
cheap cells delivers roughly what 18 workers of expensive cells did. The wall time barely
moves while CPU drops by 20 points - the saving is real but it lands as idle capacity
instead of as speed until the governor spends it.

## Gates

Meld 524 tests. arnis 499 tests, `clippy --all-targets --all-features -- -D warnings`
clean, golden hashes 5/5 identical (against a binary the script now rebuilds itself).
