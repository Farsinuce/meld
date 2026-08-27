# Phase 5 - measured results

Branch `perf/speed-to-worldgen-phase5`, both repos. Same 81-cell Bucharest cs4 benchmark.

## The ladder, updated

| configuration | wall | cells/min | region files/min | vs stock |
|---|---|---|---|---|
| stock 1.9.7, 4 workers | 230.3 s | 21.10 | 338 | 1.00x |
| 1.9.7 hand-tuned, 16 workers | 174.9 s | 27.79 | 445 | 1.32x |
| phase 1 (governor), warm | 160.3 s | 30.32 | 485 | 1.44x |
| phase 2 (canonical regions), best warm | 137.6 s | 35.32 | 565 | 1.67x |
| phase 3/4 (convergence + blur), cold median | 144.3 s | 33.7 | 539 | 1.60x |
| **phase 5, cold** | **135.9 / 135.8 s** | **35.8** | **573** | **1.70x** |

Two repeats, 0.1 s apart, 81/81 cells, zero failures, CPU 81-82%, RAM 55-56%. Against the
phase-3/4 cold median: **-5.9%**, past the ~2.3% noise floor. Against bare arnis on identical
ground: **~1.8x**.

## What shipped

| task | what | measured |
|---|---|---|
| A1 | `from_slice` tile decode + (u8,u8) dedup key, no String clone per element | osm_fetch 946 -> 571 ms |
| A2/A3 | per-tile bincode sidecars: bake-on-miss, verify-at-bake (element-for-element, f64 by to_bits), content-hash freshness on every read, atomic first-writer-wins rename | osm_fetch 429 ms sidecar-warm; block_hash unchanged in every arm; 16/16 concurrent bakes persisted verified |
| A7 | sidecar lifecycle: reap with the paired .json at every publish site, orphan + crash-tmp sweep each prefetch walk | 9 tests |
| D1a | halo regions dropped at merge time (RAM freed early), auto-suppressed under the historical whole-world hash currency | merge_dropped=112 on a boundary-heavy cell, all 16 canonical hashes identical |
| D2 | `ARNIS_BLOCK_HASH_CANONICAL` - the per-region comparison currency every future halo A/B needs | distinct label, unset = today byte-identical |
| B2 | merge_section fast paths (props-empty hoist + wholesale replace in provable cases) | golden 5/5, hash unchanged; tile_merge priced at 2.1% so booked as free CPU, not contention relief |
| B0 | bench/accept_protocol.md - the N=16-gain >= N=1-gain rule for any contention claim | tile_merge share measured, B2 demoted accordingly |
| H1 | P-core vs E-core pinning experiment | cpu_s differs 1.1% - memory-bound confirmed, topic closed |

## The 441 reconciliation - the A bracket is settled

The phase-2 figure of 441 cpu-s/run of decode waste implied 5.4 cpu-s/cell, but the per-cell
marks only allow ~3.2-3.5 contended. The A6 measurement settled it: the high bracket is
refuted, the floor booking (~1.6 contended cpu-s, of which A1 already took part) is what the
sidecars deliver. The plan's own probability-weighted expectation (~43 steady-state) was
honest; the all-heads 50 needed the 441 figure to survive, and it did not.

## A harness lesson that mattered

The first "phase 5" benchmark measured an Aug-26 binary: custom drivers called `do_run`
directly, which never pinned the arm's binary, so 140.1 s of plausible-looking numbers
contained no phase-5 code at all. `do_run` now pins unconditionally. The tell was sidecars
not appearing after a fleet run - trust artifacts on disk over wall-clock numbers.

## Where this leaves the goals

* **Efficiency vs bare arnis (the stated goal): ~1.8x on identical ground**, with per-cell
  RAM an order of magnitude lower and constant in area.
* 50 cells/min steady-state: the conjunction it needed is now partly refuted (441) and
  partly unbought (placement diet unmeasured). ~43 steady-state remains the honest target;
  the remaining HOLD work (B1 flatten, C1 save churn, D3-D7 halo write-drop) is the path.
* 573 region files/min against the ~685 honest ceiling = **84% of what this hardware allows**.
