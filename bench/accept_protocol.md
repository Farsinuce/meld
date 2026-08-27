# Phase-5 accept protocol — contention-relief claims (task B0)

Locked per `docs/perf-phase5-plan.md` (WS-B row B0 + "Benchmark protocol" + "Booking rules").
Every phase-5 perf claim is booked through this document. The plan text is authoritative;
this file is the operational checklist.

## The rule

A fix is accepted as **contention relief** ONLY if **both** hold:

1. **Blur-transpose rule:** the **N=16 gain >= the N=1 gain**. Contention relief must *grow*
   (or at minimum hold) with concurrency — precedent: GPU caves grew -28% (N=1) -> -31% (N=16)
   and was the seat; blur transpose was flat +2.5% at both N and was **not** the seat.
2. **Noise floor:** the delta exceeds the measured 2-repeat spread of **~2.3%**. Below 2x the
   spread the result is labeled *provisional*; small-effect arms (halo flag, B3, C1 — anything
   whose predicted delta sits at the spread) run **4-5 repeats per arm**, not 2.

Anything that clears the noise floor but is **flat across N ships as free CPU**: booked against
contended cpu-s/cell only, **never** booked as contention relief. Second-order bandwidth relief
is booked at **zero** until the gain-grows-with-N signature is observed, and may be claimed by
at most **one** lever (plan, Booking rules).

**Primary metric:** contended cpu-s/cell, read from arnis's own end-of-run marker
`[meld] v=1 phase=done wall_s=... cpu_s=... peak_mb=...` (emitted under `ARNIS_PHASE_MARKERS=1`).
cpu_s is the discriminator: a core stalled on memory still burns cpu time, so memory/cache
contention inflates cpu_s, while waiting on disk or a lock does not. Wall alone is never the
accept metric; fleet runs report both denominators (81-cell wall cpm AND N=16 steady-state cpm)
plus utilization = sum(cpu_s)/(wall x 24), recomputed per arm, never assumed.

## Checklist, per candidate fix

Reference baseline (measured): N=1 cpu_s = 24.5, N=16 mean cpu_s ~= 32.6-33.7,
81-cell Bucharest cs4 wall 138.4-144.8 s at 76-79% CPU, N=16 steady-state 40.7 cells/min.

- [ ] **0. Build both arms explicitly.** `cargo build --release` in
      `c:/Users/LEGION/Documents/Meld/arnis-triagefix`; one fixed binary per arm.
- [ ] **1. Determinism gate BEFORE any timing.**
      `scripts/golden_hash.sh` 5/5 (the harness builds first itself — do NOT set `ARNIS_BIN`,
      which skips the rebuild and hashes whatever is there), plus `ARNIS_BLOCK_HASH=1` A/B on
      the ring-3 cell, fix on vs off. NEVER byte-compare `.b_linear` files (container not
      reproducible). f64 compares in verification code use `to_bits()`, never `PartialEq`.
- [ ] **2. N=1 arm.** From the scratchpad (the script's `phase/cont` paths are cwd-relative):
      ```bash
      cd "C:/Users/LEGION/AppData/Local/Temp/claude/c--Users-LEGION-Documents-Meld/a793c437-9c66-47a5-aa18-f44730dbae88/scratchpad"
      bash contention.sh 1
      ```
      (contention.sh = the exact working command: ring-3 bbox, `--osm-tile-dir
      C:/tmp/meld-ab-data/cache/osm`, `RAYON_NUM_THREADS=2 ARNIS_FLUSH_THREADS=2`,
      `--benchmark --canonical-regions=12,15,-4,-1`, GUI-subsystem exe redirected + waited.)
      2 repeats minimum; 4-5 for small-effect fixes.
- [ ] **3. N=16 arm.** Same script: `bash contention.sh 16` (repeat per the same rule).
- [ ] **4. Extract cpu_s per arm.**
      ```bash
      grep -h "phase=done" phase/cont/n1/c*.txt
      grep -h "phase=done" phase/cont/n16/c*.txt   # take the mean cpu_s across the 16
      ```
- [ ] **5. Compute gains.** `gain_N = (cpu_s_off - cpu_s_on) / cpu_s_off` at N=1 and N=16.
- [ ] **6. Book per the decision table:**

      | outcome | booking |
      |---|---|
      | gain_16 >= gain_1, both > 2.3% | **contention relief** — booked against the N=16 steady-state metric |
      | gain > 2.3% but flat across N (gain_16 < gain_1) | **free CPU** — booked against cpu-s/cell only |
      | delta <= 2.3% | not booked; label provisional if 2.3% < delta < 4.6% and re-run at 4-5 repeats |
      | delta <= 2.3% at both N | flat — reject as contention relief, keep only if free and byte-identical |

- [ ] **7. Fleet confirmation before final booking.** The 81-cell Bucharest cs4 A/B via
      `bench/ab_bucharest.py`:
      ```bash
      python bench/ab_bucharest.py warm      # once, shared caches
      python bench/ab_bucharest.py run A     # baseline arm (its own worktree, main + released arnis)
      python bench/ab_bucharest.py run B     # perf arm (this worktree + arnis-triagefix build)
      python bench/ab_bucharest.py report
      ```
      Record wall, both cpm metrics, utilization. Diet fixes (B1/B2/B3) are additionally
      measured **stacked** vs baseline, since the combined bracket clears the noise floor when
      individuals may not.

## tile_merge pricing (B0 deliverable)

From every existing `--benchmark` marks file in
`C:/Users/LEGION/AppData/Local/Temp/claude/c--Users-LEGION-Documents-Meld/a793c437-9c66-47a5-aa18-f44730dbae88/scratchpad/phase/out-*.txt`
that emits the marks (out-t2, out-t21, out-ring3, out-b1-*, out-ctl, out-h-*, out-gpu-* carry no
`tile_merge_ms`/`generation_time_ms` lines and are excluded):

| file | config | tile_merge_ms | generation_time_ms | share |
|---|---|--:|--:|--:|
| out-a1-1.txt | ring-3 cell, default halo | 202 | 9788 | 2.06% |
| out-a1-2.txt | ring-3 cell, default halo | 199 | 9767 | 2.04% |
| out-bench1.txt | ring-3 cell, default halo | 217 | 10235 | 2.12% |
| out-blur1.txt | ring-3 cell, default halo | 214 | 9895 | 2.16% |
| out-blur2.txt | ring-3 cell, default halo | 214 | 9782 | 2.19% |
| out-nohalo.txt | ring-3 cell, halo off (non-default arm) | 127 | 7908 | 1.61% |
| out-bare.txt | bare-arnis area render, 272 regions | 2801 | 21065 | 13.30% |
| out-barefull.txt | bare-arnis area render, 1089 regions | 11545 | 99599 | 11.59% |

**Share of a cell: ~2.1% (2.04-2.19% across the five default-config ring-3 runs).**

**Verdict (per the plan's B0 row): tile_merge is < 5% of a cell, therefore B2 drops to LAST
priority inside WS-B** (behind B1 and B3). Its GO status stands but it is scheduled last, and
its expected contribution is re-priced off a ~0.2 cpu-s/cell phase, not the 0.5+ bracket.

Caveat kept honest: in large single-process bare-arnis renders tile_merge grows to ~12-13% of
the run (out-bare / out-barefull, 272 / 1089 regions). That is not the phase-5 unit — Meld
drives one 16-region cell per process — so the per-cell 2.1% figure is the one the priority
call uses. If the process model ever changes, re-price before trusting this verdict.

## Reference baseline (re-baselined 2026-08-27, close-out mandate)

Current committed-HEAD reference (arnis ee839534): **N=1 23.394 +/- 0.268 cpu-s, N=16 32.050
+/- 0.180 cpu-s** (5 repeats each). The older 24.5 / 32.6-33.7 row predates the sidecar +
fast-path commit 8a9dd6fe and must not be used as a before-arm again. Likewise the B0 pricing
table's ~200 ms tile_merge: it is ~100-109 ms at HEAD.

Ledger after B1/C1: both booked at ZERO (B1 flat and shrinking with N; C1 +2.30% at N=16 -
real, all five paired rounds positive, but at the 2.3% floor and under its own save_ms bar).
Fleet 133.7 s vs 135.8 s = under the run spread. The 42-43 cells/min steady-state target now
rests on WS-A (banked) and the VTune N=1-vs-N=16 contention profile, which is the live lead
for the seat of the 37-46% inflation now that grid indirection is eliminated as a candidate.
