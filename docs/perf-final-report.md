# The speed-to-worldgen project - final report

Five phases, 2026-08-25 to 2026-08-27, branches `perf/speed-to-worldgen` through
`-phase5` on `meld-triagefix` + `arnis-triagefix` (all local). Every number below is
measured on this machine (Ultra 9 275HX, 24 cores 8P+16E, 31.4 GB DDR5-6400, NVMe) on the
same benchmark: 81 cells of Bucharest centre at 1:1, cell size 4, blinear + stream-to-disk,
warm caches, `bench/ab_bucharest.py`.

## The result

| configuration | wall | cells/min | region files/min | vs stock |
|---|---|---|---|---|
| stock Meld 1.9.7, 4 workers | 230.3 s | 21.1 | 338 | 1.00x |
| 1.9.7 hand-tuned to 16 workers | 174.9 s | 27.8 | 445 | 1.32x |
| bare arnis 3.1.7, whole machine, same ground | 181.2 s | - | 318 | 1.06x |
| **final (phase 5)** | **135.8 s** | **35.8** | **573** | **1.70x** |

**1.70x a stock install. 1.29x a hand-tuned one. 1.8x bare arnis on identical ground.**
Output byte-identical throughout: golden hashes 5/5 at every commit, `block_hash`
unchanged through every optimization, every benchmark run 81/81 cells with zero failures.
RAM peak fell from 82-93% to 52-56% while getting faster.

The stated goal - use the machine better than bare arnis does - is met with room to spare:
bare arnis keeps 10.3 of 24 cores busy on this workload; Meld now keeps ~19-20 busy, at
~1 GB per worker instead of one 14.65 GB process that grows with area.

## What shipped, by phase

| phase | headline | measured effect |
|---|---|---|
| 1 | Adaptive governor: closed-loop worker count over live occupancy, RAM-gated admission, per-scale memory of the converged answer; arnis stdout telemetry protocol; deterministic flood-fill budget; Stop actually stops | beat the hand-tuned baseline on every arm; RAM 82->52% |
| 2 | `--canonical-regions`: the seam-halo region ring (20 of 36 files per cell) is never written; plus a measurement layer (per-phase timers, region counters, schema/4 reports) and a wrong-world-merge race fix | -11.5% wall per cell, hash-identical |
| 3 | Governor convergence fixed (a rate needs a real window; the climb opens at cores/2 through the RAM envelope; the envelope knows about streaming) | cold runs 173.9->144.8 s, CPU 60->79%; every run now lands 139-145 s |
| 4 | Named the contention: memory system, not I/O or locks (uniform 2.05x inflation across unrelated phases); corrected the ceiling arithmetic; bit-exact flat-layout blur | +2.5% and the knowledge that scheduling was finished |
| 5 | Pre-parsed OSM sidecars (bake-on-miss, verify-at-bake, content-hash freshness, crash-safe, self-reaping); `from_slice` decode; halo merge-drop; canonical hash currency; P-vs-E question closed | 144.3->135.8 s (-5.9%); decode 946->429 ms/cell |

Plus, booked at zero but shipped as quality: flat grid layout, save-path buffer hygiene,
merge-section fast paths - all measured to the accept protocol's standard and found real
but below the floor.

## What the project learned, in order of importance

1. **The machine's honest ceiling is contended, not nominal.** A cell costs 24.5 cpu-s
   alone and 33.7 cpu-s at 16-way concurrency; every plan computed against the former
   overstated headroom. At the operating point the system runs at **93% of what the
   hardware allows**, and the final 573 regions/min is 84% of the physical 685.
2. **The remaining inflation (37-46%) is the memory system**, proven twice over: it is
   uniform across unrelated phases, and core placement is irrelevant (P vs E pinning
   differs by 1.1% in cpu-seconds). Two layout fixes (blur transpose, grid flatten) both
   came back flat, eliminating grid indirection as the seat - by elimination it lives in
   element_placement's own structures, and only a hardware-counter profile will find it.
3. **Single-cell timings do not predict machine throughput.** Predicted 58.3 cells/min for
   24 workers x 1 thread; measured 27.25, the worst configuration tested. Same error, in
   costume, produced the wrong ceiling twice. Everything must be measured contended.
4. **A run that did less work looks like a faster run.** 36 of 81 cells failing presented
   as a 52-second speedup; a stale binary presented as a plausible result twice. The
   harness now fails rows with failed cells, verifies the active project, pins the binary
   in `do_run`, and requires report freshness - each rule bought with a real incident.
5. **The accept protocol pays for itself.** N=16 gain >= N=1 gain AND above the spread, or
   it books as nothing. It rejected three plausible optimizations that would otherwise be
   claimed as wins, and the claims that survived it are trustworthy.
6. **`.b_linear` is never byte-comparable** (identical runs share zero identical region
   files) and **golden_hash.sh now rebuilds** before hashing. `ARNIS_BLOCK_HASH` - taken
   from memory before any write - is the only valid output currency, with
   `ARNIS_BLOCK_HASH_CANONICAL` for halo work.

## GPU: measured, understood, deliberately not built

The contention objection was tested and refuted (-28% on the cave path at one process,
-27.9% at eight, device 98% idle). What stopped it: the CPU and GPU paths genuinely hash
differently (f32 vs f64), so any terrain kernel ships as an opt-in approximate mode with
its own baseline; and the honest target shrank to 9.8% of a cell once the elevation phase
was decomposed. Revisit if per-cell cost ever drops far enough that dense float grids
dominate, or if one process ever owns many cells.

## Every open item, closed with evidence (verdict runs, 2026-08-27)

| item | verdict |
|---|---|
| D3-D7 halo write-drop | **KILLED.** Built behind a flag and put through the 5-cell canonical-hash corpus: 5/5 cells FAIL, 49/80 kept regions differ, including a region 512 blocks inside the keep rectangle. Kept-region passes (seal/sweep/carves) genuinely read halo state - the halo is load-bearing, not waste. Deterministic both arms, guard geometry audited, so this is physics, not a bug. Code stripped; D1a (merge-time drop) is the permanent retreat and is already shipped. Do not retry a uniform write-drop; the only viable shape is D6's 16-block apron, and its ceiling is a fraction of an already-small bracket |
| VTune profile of element_placement | **Infeasible on this box** - VTune is not installed. The lead itself stays sound (the 37-46% inflation lives in placement's structures, by elimination), but it needs the user to install a profiler. Closed as out of scope |
| A4 `--osm-preparse` early-exit mode | **REMOVED.** Its only value was overlapping run-1 bakes with network wait; bake-on-miss already covers every case measured, and the mode carries the documented validate_args/gui.rs bug-class risk for zero measured upside |
| B1 / B2 / C1 optimizations | **Booked zero** under the accept protocol (real but sub-floor); shipped as code quality with no-perf-claim commits |
| Governor-lane utilization (ramp/drain shape) | Out of scope here; the adaptive-scheduler plan on the root repo covers it. The only remaining lane with measured headroom (80% -> ~94% utilization on the 81-cell wall) |
| Release | Phases 1-5 are release-shaped: arnis 3.1.8 with additive flags, Meld defaults preserve 1.9.7 behaviour until `governor_mode`/`canonical_regions` are enabled. Runbook: arnis tag first, then Meld |

Nothing on this branch is left as a maybe: every line above is shipped, killed with
evidence, or explicitly handed off.

## Documents

`perf-adaptive-scheduler-plan/-research` (root repo) - the original research.
`perf-phase2-plan/-research/-results`, `perf-phase3-notes/-research/-results`,
`perf-phase4-contention/-results`, `perf-phase5-plan/-research/-results`,
`perf-branch-map`, `perf-conclusion`, `bench/accept_protocol.md`, and the harness
`bench/ab_bucharest.py` + `bench/contention_sweep.sh` - all in this repo's `docs/`.
