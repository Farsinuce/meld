# Phase 2 - Research Annex

Companion to [perf-phase2-plan.md](perf-phase2-plan.md). 15-agent run (2026-08-26) over `perf/speed-to-worldgen-phase2`: 5 code maps, 4 competing designs, 3 adversarial judges, synthesis, completeness critic, final edit.


## 1. Completeness critic (every correction was applied to the plan)

Not sound. 21 corrections, ordered by blast radius.

---

## A. Confidence >85% that the evidence does not support (auto-implemented = loaded guns)

**1. A1 (88%, GO) — `from_slice` is NOT acceptance-identical to today's path.** `osm_parser.rs:149-150` uses `Deserializer::from_reader(...)` + `OsmData::deserialize(&mut de)` and **never calls `end()`**, so a tile with trailing bytes after the closing brace parses fine today. `serde_json::from_slice` *does* call `end()` and would return `Err` → the existing handler at `:151-155` prints `skip unreadable tile` and `continue`s, silently dropping 150k–1.2M elements and changing the world. Correction: mandate `serde_json::Deserializer::from_slice(&buf)` + `OsmData::deserialize(&mut de)` (same non-terminating semantics), and say so in the task text. As written, "byte-identical by construction" is false.

**2. A1 (88%) — the `(u8,u64)` dedup key must not collapse unknown types into one `other` bucket.** `seen.insert((el.r#type.clone(), el.id))` distinguishes every distinct string. A shared `other` discriminant makes `("foo",1)` and `("bar",1)` alias, dropping the second. Verified the four cached tiles carry only node/way/relation at element level, so this is latent, not live — but it is a silent-output-change footgun in an auto-implemented task. Correction: 4 variants where the 4th is `(hash(type), id)` or keep a side `HashSet<(String,u64)>` for the non-canonical tail. Drop A1 to 84 / HOLD unless the task text carries items 1 and 2 verbatim.

**3. A1 needs a kill switch, contrary to "none, by design."** Risk 6 concedes RSS grows (16 × 147.8 MB worst case; `osm_g1_z11_1172_741.json` is 147,837,444 bytes, confirmed on disk) and N4 fails the arm if `ram_peak` rises — with no off-switch and no fallback shipped. Correction: ship the size-threshold `from_reader` fallback (e.g. >64 MB) **in the same commit**, not as a contingency. Hard constraint 5 is currently being read as "output unchanged"; RSS is behaviour.

**4. C3a (90%, GO) — atomic `_write` converts a benign race into a hard Windows failure.** CPython's `open()` on Windows shares read/write but **not delete**; `os.replace` over a destination another thread holds open raises WinError 5/32. `master_world_path()` → `PROJECT.settings()`/`load()` → `_read` runs **without** `_LOCK` (the very hazard C2 targets), 2 reads/cell × 16 workers. Today that window yields a defaulted read; after C3a it yields an exception inside `subworld_number`. Correction: land C2 **before** C3a, and wrap `os.replace` in a bounded retry (5 × 20 ms) with the last attempt falling back to `write_text`. Same change for C4 (`meld-world.json` is read by the UI/export). Drop both to 80 / HOLD, or re-order 10→9 and add the retry to the task text.

**5. I3 (86%, GO) and I2 (88%, GO) — the telemetry channel they use is not plumbed.** `bench.mark`/`--benchmark` output only exists when arnis is run with `--benchmark`, and `grep -rn "\-\-benchmark" src/ server.py bench/` in meld-triagefix returns **nothing** — Meld never passes it (confirmed against the real A/B command line in `ab-perf-governor-cs4/logs/cell--1_-1_4.log`). So I3's go/no-go number for D1 and I2's `element_placement`/`tile_merge` labels are unreachable from a Meld-driven run. Correction: either add a Meld setting/env that appends `--benchmark`, or convert those labels to `meld_telemetry::phase` markers under the existing `ARNIS_PHASE_MARKERS` gate. Until that plumbing is a named task, I2→78, I3→78, both HOLD.

---

## B. Double-counted or single-pooled gains

**6. B1 and B2 are additive in the tables but draw on one pool, and the split is unmeasured.** The ~340 core-s of discarded region work leaves through `save_java` **or** `flush_region_via`, never both. The plan asserts B1 "moves little" on the streaming benchmark with no measurement behind it. Add a W0 task (call it **I5**, 1 agent-h): on one cell under the real benchmark config, count regions written via each path (`mod.rs:437` vs `java.rs:190`) and log the split. Until then the GO+HOLD target rows bank money that may sit entirely behind the 68%-confidence task. Restate: **if B2 is rejected in review, P1 ≤150.0 s is unreachable** — say it in the table, not only in the prose.

**7. E2 and E3 do not jointly recover 42.1 worker-s; E3 alone owns it, and E2's wall value is plausibly negative.** The 42.1 ws is the 16→14→16 oscillation (workers 14/15 idle 26.05 s + 16.06 s). E2's separate anchor is the t+122.4 late-grow — but refusing that grow does not delete the 37.9 s cell, it re-queues it onto the first freeing worker, finishing **no earlier and plausibly later**. And the oscillation happened inside a window the same research measured at ~100% CPU, where two idle slots cost ~0 wall. Correction: change "E2+E3 recover … roughly 3-5 s of wall" to **"0-2 s of wall; justified as RAM/contention stability, not throughput."** Remove E2/E3 from any arithmetic that reaches the P1 target.

**8. The GO-only target rows sit inside the plan's own stated noise floor.** GO-only cs4 warm is 154-158 s from 160.3 = a 2.3-6.3 s ask, against a declared ~5 s noise floor and 3 repeats (median SE ≈ 3.5 s). The only GO item that delivers seconds is A1: 80-150 cpu-s / 24 cores = 3.3-6.3 s at 100% conversion, less after the plan's own Risk-5 haircut. Correction: change GO-only warm to **156-159 s** and cold to **167-170 s**, and add one sentence: *the GO set alone is not expected to be separable from noise at 3 repeats; use 5 repeats or do not report a GO-only delta.*

---

## C. Claims not grounded in the maps / the artifacts

**9. The config divergence is worse than stated, and one of the extra mismatches is load-bearing for W2.** The real command line (verified) is `--no-buildings --interior false --bake-lighting --region-format blinear --blinear-level 6`. `bench/matrix.json` declares `buildings:true, overture:true, interior:true, bake_lighting:false, region_format:"anvil"`. That is **five** mismatches, not the three the plan lists — and `bake_lighting` decides W2's headline. The ~43% discarded-save share weights the 16,128 base chunks at 0.5, which is only defensible **because the measured arm baked lighting**. With `bake_lighting:false` the base chunks are near-free and the discarded share falls toward the content-chunk floor of **4,352/20,736 = 21%** (≈29% at a generous 0.15 weight). Correction: state the 43% as conditional on `--bake-lighting`, and add `bake_lighting`, `interior`, `region_format` to H2's assert list.

**10. W6's reason for cutting "gate building-suppression on `args.buildings`" is inverted.** It says "the real benchmark runs buildings on." The measured arms ran `--no-buildings` (verified in the cell log). Three full passes over every way and node of a 195 km² tile (`osm_parser.rs:890-905`) run and are discarded, inside the 2.68 s `parse` span. Either fix the stated reason or reopen the item — it cannot be both.

**11. The `effective_parallelism` row uses an inconsistent denominator.** `summary.workers_peak` is **20** in the cs4 governor report; the capacity schedule was 4/6/12/20/16/14/16. Dividing Σduration/elapsed by a flat 16 is not an occupancy. Correction: drop the row, or restate it as `Σduration_s / elapsed_s` with the capacity schedule named, and delete the "12.7-12.9/16 → 12.9-13.3/16" targets, which are inside the model's own noise.

**12. `340 core-s` rests on three unverified layers, stated as one derived number.** It is 9.46 core-s (a **two-point Amdahl fit**, which the source map explicitly labels "not a measurement") × 43% (a weighting assumption) × 81 (using the **cheapest cell in the grid** as the unit). Correction: quote the band as **200-500 core-s** in the "where the deletable cpu-seconds are" table and everywhere downstream, and make I4 re-derive it on a ring-3 cell.

---

## D. Things the stated benchmark structurally cannot measure

**13. G6 is blind on every arm the plan actually runs.** `bench_scheduler.py:864-880` `region_hashes()` does `for f in sorted(region.glob("*.mca"))`. The arms produce `r.X.Z.b_linear`. It returns `{}` and G6 **passes vacuously** — the only gate that can see a merge-order or `level.dat`-donor effect (C6, C7, B1/B2). Correction: extend the glob to `("*.mca","*.b_linear")`, hash `.b_linear` whole (no timestamp table), and fail loudly on an empty dict.

**14. G3 as written cannot pass, and its file extension is wrong.** The same function's own docstring says `.mca` bytes "depend on chunk write ORDER and zlib output, both of which a different worker/flush layout can legitimately change without changing a single block" — which is precisely what B2 changes. And the benchmark canonical files are `.b_linear`. Correction: G3/N3/P5/G8 must say `.b_linear` for the streaming arms and `.mca` for the anvil arm, must zero `[4096:8192]` for `.mca` (as `region_hashes` already does), and must compare **decoded chunk payloads**, not raw bytes, for the eviction arm. A raw `cmp` on B2 will produce false failures.

**15. P8 is not independent of P1 and cannot be measured with the instrument named.** `timeline[]` in the real report has **10 samples at 20 s**, each a mean of ~4 `cpu_percent` samples clamped at 100, and the mid-run buckets are pinned at 100. With cores pinned, the integral ≈ elapsed × 24, so "≤2960 cpu-s" is a restatement of "≤ ~140 s wall," not an independent check of the cpu-second claim. Correction: add task **I6** (2 agent-h) — extend arnis's `[meld] v=1 phase=done wall_s= gpu_ms=` line with `cpu_s=` from `GetProcessTimes`, sum it per run in the report, and make **that** P8. Otherwise delete P8.

**16. P4, N7 and the results table reference report fields that do not exist.** `meld-run-report/3` `summary` keys are exactly: `buildings, cell_avg_s, cell_fastest_s, cell_median_s, cell_size, cell_slowest_s, cores, cpu_avg, cpu_peak, elapsed_s, ended, failed, incomplete, merged, on_disk_mb, overture_failed_cells, ram_peak, regions, retries, scale, started, total, workers_peak, workers_setting`. There is no `cells_per_min` and no `effective_parallelism`. `config` has no `stream_to_disk`, and `config.region_format` is present but **null** on a run that used blinear. Correction: fold a `meld-run-report/4` bump into H2 (add `cells_per_min`, `stream_to_disk`, fix `region_format`), or restate P4/N7 against the harness-computed field and name where it lands.

**17. N6 (`merge Xs prune Ys ≤ 7 s`) cannot be harvested.** I1 is scoped "log-only, no schema change," and `harvest()` reads `meld-report.json`. Correction: either add log parsing to `harvest()` or put the four timers in the report — pick one and give it hours.

**18. P5/G8 cannot be observed on a benchmark run.** `server.py:2907-2912` `rmtree`s the cell world after merge, so "region files in a cell output dir before prune" is gone by the time anyone looks, and the `-M seam` count lives in the server log, not the report. Correction: specify the isolation arm runs with `prune_cell_after_merge` off, and state the expected `+N/-M` values per subdir (`region/`, `poi/`, `entities/`) since the merge counts across all three.

**19. G2 as specified exercises zero duplicate elements.** 56 of 72 cells read exactly **one** tile, so a single-cell G2 never executes a second `seen.insert` hit and cannot detect a dedup-key regression. Correction: G2 must run on the one 4-tile cell **and** on a synthetic fixture containing the same id under two different `type` strings across two tiles.

---

## E. Missing work

**20. Five items have no owner and no hours.**
- **Settings that the kill-switch table promises do not exist.** `grep -rn "governor_churn_guards\|governor_history_scope"` in meld-triagefix returns **nothing**. Needs `project.default_settings()`, `/api/settings` validation, `runreport.py` config capture, a settings-UI control, and docs — plus the unnamed project setting that gates `--canonical-regions` emission. ~3 agent-h + 2 test-h.
- **The arnis version gate for B1.** `arnis_cmd.arnis_version()` exists (`src/arnis_cmd.py:202`, parses the banner's trailing `arnis 3.x.y`) — B1 needs a `MIN_ARNIS_CANONICAL_REGIONS` constant, the fallback path, and a test with an older exe stub. Name it; it is not "part of B1's 4 h."
- **Meld-side test for the invariant the whole design rests on**: assert `--canonical-regions` == `coords.canonical_region_bounds(cell_key)` for a sample of keys at cs4 and cs8. B3 is arnis-side only.
- **Release/migration.** `build.py` pulls arnis `releases/latest`; B1 requires an arnis tag+release **before** the Meld build, version bumps, changelog, and — per this repo's known gotcha — `golden_hash.sh` never rebuilds, so **G1 must include an explicit `cargo build --release` step** or it validates a stale binary.
- **Docs.** `docs/generation-performance.md:622-626` documents "the governor measures generator wall time and does not see [the merge]"; I1 and I5/I6 change what is measured. Add a doc task.

---

## F. Contradiction with the phase-1 measurements

**21. H2's resolution direction is unspecified, and one of the two directions invalidates every baseline in the plan.** All four phase-1 numbers (174.9 / 171.0 / 160.3 / 239.4) were measured with `--no-buildings --interior false --bake-lighting --region-format blinear`. If H2 "fixes" the arms to match `matrix.json` (buildings on, anvil, lighting off, interior on), then: every "from" column in the pass-criteria table is void; Overture's serial parquet path (`overture.rs:503`, `:1008`, no rayon anywhere in the file) becomes a new dominant phase of unknown warm cost; and W2's 43% drops to ~21-29% per item 9. Correction: **H2 changes `matrix.json` to match the measured arms, not the reverse** — say it explicitly in the task row. And if the config is changed for any reason, budget the re-baseline: 4 arms × 3 repeats × ~3 min plus prep ≈ 45-60 min of wall that the plan currently does not account for.

## 2. Judge panel


### Lens 1: Throughput realism

| design | gain | conf realism | determinism | effort | total |
|---|---|---|---|---|---|
| Decode Once, Write Once | 9 | 8 | 9 | 9 | **44** |
| Merge Offload — bounded merge lane, run-end keyed on MERGED | 1 | 10 | 7 | 2 | **28** |
| Built-Up Gaussian GPU Offload | 4 | 6 | 6 | 3 | **27** |
| Tail-First Dispatch: learned ring ordering + a ramp that starts warm | 3 | 4 | 8 | 3 | **25** |

**Recommended order:** D2-T1 + D2-T2 — split the parse/place markers and switch from_reader->from_slice + kill the per-element String clone in the tile dedup. Byte-identical, ~5 agent-hours, and it PRICES the two expensive decisions downstream (whether the sidecar is worth 12 hours, and whether the 18.6 MB/s single-point rate holds). Re-profile one cell under the REAL matrix.json config (buildings:true, overture:true) at the same time — the 18.96 s / 27.7 cpu-s profile is from --no-buildings on the cheapest cell in the grid. -> D2-T5 — --canonical-regions. The single largest verified cpu-second deletion in the whole set (~330-342 core-s, ~9-10% of measured run demand), byte-identical on kept regions by construction, and I verified both of its stated open risks are already retired: the drift guard has 2 regions of margin and the eviction path is inactive at cs4. Gate it on a byte cmp of all 16 canonical .mca in both stream-to-disk states, not on golden_hash.sh (which uses --file fixtures and cannot see the write filter). -> D4-T6 — the three governor churn guards. Cheapest idle removal available: 11 hours for ~42 measured worker-seconds plus the elimination of a verified garbage-input bug (governor.py:707-715 has no span floor). Ships behind governor_churn_guards, fails toward today's behaviour. Do NOT bundle T1/T2/T5 with it — the learned-ring apparatus is calibrated on the wrong config and its headline result contradicts its own map. -> D1-T1 + D1-T2 + D1-T7(a)(b) — WITHOUT the offload. The merge timer line, the _merge_cell_unit extraction, the master_world_path-cached-per-run fix, and atomic tmp+os.replace on project.json and meld-world.json. ~8 hours, fixes a live race that can merge a cell into the wrong world folder, and gives design 4's T8 for free. Park T3-T6 (MergePool, terminal funnel, Stop rework, UI lane) indefinitely — 37 hours across 20 correctness-critical consumers for 0.27% of worker-seconds in a window with zero core headroom. -> D2-T3 + D2-T4 — the binary sidecar, ONLY if T2's marker shows from_slice bought >=1.3x. If it did not, allocation dominates, the sidecar's ceiling is low, and the hours belong elsewhere. When it does ship, price T4 honestly as a cold-run-only benefit — on the warm reference arm the sidecars already exist and the pre-warm returns nothing. -> D3-T1(a) + D3-T1(b) — two measurements, ~5 hours, no implementation. (a) one bench.mark pair around smooth_built_up_gaussian converts the derived 3.1-4.6 core-s into a number; (b) the cs4 A/B at 16 workers with --caves --gpu igpu vs off, recording package power and effective P-core clocks, settles whether iGPU load under 23.4/24-core saturation inverts the sign of every GPU plan on this machine. Both are prerequisites, neither is a commitment. -> D3-T2 — the bit-exact flat-layout blur, if T1(a) says the Gaussian is worth >=1.5 core-s. Zero determinism debt, no flag, golden_hash.sh 5/5 as the proof, plus the to_bits() unit test. Budget it at 0.2-0.4 core-s per cell, not 0.95-1.5. Note its share shrinks once D2-T5 lands and the whole cell is shorter. -> D3-T3..T7 (the GPU kernel) — park unless T1(b) comes back clean AND T1(a) comes back above 2 core-s. Even then the tile-seam test at T6 is a coin flip with an unencouraging precedent in this codebase. -> D4-T1/T2/T5 (learned ring ordering) — park until someone reconciles the +3.19 s vs -4.67 s contradiction between the design and the scheduler-idle map on the same 81 durations, and re-derives the ring profile under the config the benchmark actually uses. D4-T4 (machine-scoped history) is worth doing eventually for cold first runs, but it must not be counted against the warm 160.3 s reference. -> Fix bench/ab_bucharest.py:302-306 (do_run(reuse=True) skips prepare_project and never calls /api/projects/switch) BEFORE any A/B in this list. Three separate designs independently found it, it already destroyed the cs8 governor report, and none of the numbers above can be harvested cleanly while it stands.

**Over-confident claims flagged:**
- D2 gain_confidence 55% -> 50%. The 1.20x band rests on a single measured rate (18.6 MB/s from one 1.01 s fetch on one 18.79 MB tile) and on an OSM saving whose reasoning is wrong (see fatal flaws). Lever B alone is worth ~9-10% at high confidence; the composite is not 55%.
- D2 T3 confidence 70% -> 55%. The design's own risk section concedes the sidecar keeps every per-element HashMap+String allocation, which is plausibly half the cost, and nothing in the plan measures that split before committing 12+6 hours.
- D2 T4 confidence 78% -> mechanically fine, but the VALUE is misstated: on the warm reference arm the sidecars already exist and the pre-warm returns ~0. It is a cold-run and thundering-herd fix priced as a throughput lever.
- D2 T5 confidence 72% -> 80%. I am raising this one. The two hazards the design left open are both closed by the source: merge.py:178's drift guard leaves 2 regions of margin under canonical-only writes, and server.py:2761 shows the eviction path is not active at cs4.
- D3 gain_confidence 45% -> 30%. Three independent unmeasured gates (kernel size, iGPU-under-saturation, bandwidth-regime tiling) plus a tile-seam test with a bad precedent. Any one failing removes most of the 6-9%.
- D3 T2 confidence 85% -> 40% for the STATED NUMBER (the refactor will work; the 0.95-1.5 core-s will not). The gather already produces a contiguous column at postprocess.rs:1008, so the tap loop is already cache-local. Real gain 0.2-0.4 core-s + ~0.2 s serial.
- D3 T5 confidence 55% -> 35%. Shared-memory tiling quality decides whether a 0.5-flop/byte kernel beats a CPU at all, and the design's own stop-rule (abandon if under 5x standalone) is the honest admission that this is a coin flip.
- D3 T4 confidence 70% -> 50%. Sixteen simultaneous Vulkan device creations against one Intel driver is described as 'untested' and then priced as if the semaphore makes it safe. The 130 ms init is paid per CELL, not per process-lifetime, because one process handles exactly one cell.
- D4 gain_confidence 55% -> 30%. The headline blends an already-banked warm start with an ordering claim that contradicts the design's own supporting map.
- D4 T2 confidence 82% -> 45%. Its measured result has the opposite sign to the scheduler-idle map's simulation on identical input, and the map calls the design's hypothesis explicitly unsupported.
- D4 T5 confidence 74% -> 55%. Difficulty-normalising _rate_tp depends entirely on a ring profile whose sign the design itself says may flip with buildings on — which is the config the benchmark uses.
- D1 T4 confidence 66% -> 55%. An idempotent terminal funnel that must be the single mutator of _RUN counters, own _timing_finished and the run-end block, survive retry re-queues, and never fire early enough to let auto-export delete .mca under a pending copy2 — across 20 consumers — is harder than 66%.
- D1 T3 confidence 78% -> 70%. Non-daemon threads joined via a shutdown hook, plus a 1.5 s WinError backoff inside a 2-slot lane, is the kind of exit path that is only proven by killing the app mid-run a hundred times.

**Fatal flaws:**
- D2: 'only 14 cpu-s of it is irreducible one-decode-per-distinct-tile' is wrong, and it is the sentence the OSM half's sizing rests on. Meld spawns ONE arnis.exe per cell (hard constraint 2), so all 90 tile reads still happen — the sidecar makes each decode cheaper, it does NOT collapse the 31.3x duplication. The floor is 90 x sidecar-decode-cost, not 4 x decode-cost. If the sidecar is 3x faster, 441 cpu-s becomes ~147, not ~14. The claimed ~270 cpu-s saving happens to land near that, so the number survives — but the ceiling the design implies does not exist, and if the per-element HashMap+String allocation is half the cost (the design's own T3 risk) the saving is ~150.
- D2: T4's pre-warm pass returns approximately zero on the warm reference arm the whole plan is measured against. Once the sidecars exist, a warm run never misses. Nine hours are priced as a throughput lever for what is a cold-start and thundering-herd fix.
- D3: T2's '0.95-1.5 core-s removed per cell' is not supported by the code. postprocess.rs:1008 gathers each column into a contiguous local Vec<f64> BEFORE the tap loop, and the horizontal pass reads row-contiguous — so the tap loop is already cache-local, and bit-exactness forbids touching it. A flat layout deletes 2049 Vec allocations per pass and a ~0.1 s serial scatter, i.e. 0.2-0.4 core-s. The flag-OFF gain is ~1%, not 2-3%.
- D3: the wgpu device init (130 ms iGPU measured, 790 ms dGPU) is paid ONCE PER CELL because one arnis process renders exactly one cell — there is nothing to amortise it over. Against the 2.2-2.8 core-s the kernel displaces that is a 5-20% tax before any contention, and 16 simultaneous Vulkan device creations against one Intel driver is entirely unmeasured.
- D4: the headline ordering result contradicts its own supporting map on the same data. The scheduler-idle map simulates outer-ring-first at 168.94 s against a 165.75 s no-stagger spiral — +3.19 s WORSE — and writes 'this is the finding that contradicts the task's hypothesis'. The design reports outer-first at -3.94/-4.67 s BETTER on the same 81 baseline durations. Ring-descending LPT on --no-buildings data is outer-first, so this is one experiment with two opposite answers and no reconciliation. T2 is unproven.
- D4: DOUBLE-COUNT. The 10.7 s warm-start gain (171.0 -> 160.3) is already reported in the phase-1 ground truth as the warm governor result (161.2 s). Measuring T4 against the 171.0 s COLD arm re-sells a win the stated baseline already contains. Against the warm reference every other design uses, T4 contributes ~0 and the honest total drops from 6-11% to ~2-4%.
- D4: the learned-ring apparatus (T1/T2/T5) is calibrated on --no-buildings data while bench/matrix.json sets buildings:true and overture:true. I confirmed arnis_cmd.py:338 emits --no-buildings only when the setting is off, so the A/B cell logs prove the measured runs used a different world config than the benchmark. Under --no-buildings the dense core is CHEAP; the design concedes the sign may flip with buildings on, in which case outer-first becomes the WORST possible order (its own mirror simulation costs +17.5 s).
- D1: under the throughput lens it removes neither cpu-seconds nor exploitable idle, and the design knows it. The maps show six consecutive 20 s buckets at 100% CPU mid-run, so an 80 ms slot release converts to nothing there; the only place it converts is the ramp and the tail, both of which D4 is separately claiming. 45.5 hours restructuring auto-export, run-end, Stop, the render-queue PROJECT rebind and 16 other consumers, to bank a measured 0.27% of worker-seconds, is a phase-2 priority failure even though it is not an engineering failure.
- CROSS-DESIGN DOUBLE-COUNT — the tail. D2 shortens the ~38 s tail by shortening the one cell that sets it (-2.3 s claimed). D4 shortens the same tail by reordering (4-7 s claimed). D1 claims the slot freed at the tail. Those are the same seconds and only one design can have them. Since the tail is 14-16 cells draining and its length is one cell's wall, D2's mechanism is the only one that shrinks it unconditionally.
- HARNESS — shared blocker, found independently by three designs. bench/ab_bucharest.py:302-306 calls do_run(reuse=True), which skips prepare_project() and therefore never calls /api/projects/switch, so a warm re-run renders into whatever project is active. It already overwrote the cs8 governor report (which is why the 239.4 s cs8 figure can be cited but not recomputed) and made harvest() emit 'no fresh meld-report.json produced'. Until it is fixed, none of the four designs can harvest its own A/B.

### Lens 2: Determinism & risk

| design | gain | conf realism | determinism | effort | total |
|---|---|---|---|---|---|
| Merge Offload — bounded merge lane, run-end keyed on MERGED | 2 | 9 | 6 | 3 | **5.2** |
| Decode Once, Write Once | 7 | 6 | 6 | 7 | **6.6** |
| Built-Up Gaussian GPU Offload | 4 | 6 | 3 | 3 | **3.8** |
| Tail-First Dispatch: learned ring ordering + a ramp that starts warm | 5 | 5 | 9 | 6 | **6.8** |

**Recommended order:** D2-T2 — from_slice + (u8,u64) dedup key in osm_parser.rs:149-162 (and retrieve_data.rs:146/171). Byte-identical by construction, ~3h, attacks the largest measured waste in the run (441 cpu-s of JSON deserialisation, 97% of it re-decode). -> D3-T2 — flat-layout bit-exact blur in postprocess.rs:941-1043 + parallelised blend at :901-920. Bit-for-bit identical, no flag, golden_hash 5/5 is the proof, plus a to_bits() unit test so a future refactor cannot drift. Kills the strided gather, the serial scatter and two of three named contributors to the 0.23 s serial residue. -> D1-T1 + D1-T7 — the merge/prune/health/meta timers AND the three race fixes, shipped WITHOUT the offload. Atomic project.py:_write (tmp+os.replace), _read distinguishing missing from unreadable, master path resolved once per run in _submit_cells instead of per-merge at server.py:2863, locked+atomic meld-world.json, tailed _scan_cell_health read. These are correct today, independent of any flag, and they close the wrong-world-merge race I verified. -> D4-T7 — pin the level.dat donor and drop the subworld_number gzip round-trip when prune_cell_after_merge is on. Two hours; removes the only order-dependent artifact in the master world. -> D4-T4 — machine-scoped governor history keyed on bucket + hardware fingerprint. The only MEASURED wall-clock win in the whole set (171.0 -> 160.3 s), zero output risk, and it also fixes the fact that bench_scheduler has never once warm-started. -> D2-T1 + D4-T1/T8 — split the phase markers (parse vs precompute, place vs tile_merge, elev_builtup_gaussian) and the arnis/merge/prune timing, and re-profile ONE cell under the REAL benchmark config (buildings:true, overture:true, anvil, bake_lighting:false) on a non-centre cell. Every remaining gain claim in every design is unfalsifiable until this exists. -> D2-T5 — --canonical-regions. Biggest remaining CPU deletion (~43% of save, ~340 core-s/run). Gate: cmp of all 16 canonical .mca with and without the flag, in BOTH stream-to-disk states, committed as a fixture test. Filter only worker.send(); never touch the remove/insert ordering. -> D4-T6 then D4-T5 — churn guards (no-grow-near-drain, _rate_tp span floor, shrink cooldown) behind governor_churn_guards, then the difficulty-weighted step metric. Both degrade to today's behaviour with no profile. -> D4-T1/T2 — learned ring profile and dispatch order, ONLY after re-deriving the profile under the config the benchmark actually uses and reconciling the sign contradiction. Keep spiral as the unlearned fallback. -> D2-T3/T4 — OSM binary sidecar. Only if T2's measurement shows tokenising rather than allocation dominates. MELD_OSM_CACHE_VERIFY must be default-ON for the first N runs, not a CI-only 9-tile check, and the orphan sweeper must be incapable of touching a .json. -> D3-T1(b) — the iGPU-under-16-worker-saturation A/B (--caves --gpu igpu vs off, recording package power and effective P-core clocks). Cheap, uses only code that already exists, and it kills or unlocks everything below it. -> D1-T2..T8 — merge offload, default OFF, justified ONLY as the prerequisite for later per-cell CPU cuts (at 1:10, or after D2-T5, the merge's share triples). Never shipped as a performance change. T8's byte-comparison and the 20-shuffled-ordering stress arm are mandatory. -> D3-T3..T7 — GPU elevation kernel. Last, and probably never for master-origin renders. If attempted, the seam test must be run across a driver update, not just a 2x2 in one session.

**Over-confident claims flagged:**
- D1 T4 (_cell_terminal funnel) 66% -> 55%. This is the one change in all four designs that can corrupt the user's real world, its blast radius is 20 named consumers, and its worst case (double-count -> early run-end -> _maybe_run_export converting .mca while a pending copy2 lands a raw one) is exactly the failure safeguard D exists to prevent. 4.5 agent-hours + 3.5 test-hours is thin for an idempotency invariant that must hold across retry re-queue, Stop, no-world-dir, and merge-failure paths simultaneously.
- D1 T5 (Stop / _run_active / render-queue guard) 72% -> 60%. It re-implements in code a guarantee that is currently unbreakable by construction, and the failure mode is the sharpest in the merge-path map: _switch_project rebinds the global PROJECT while merges are in flight, delivering cells into the NEXT project's world. Also adds a new background finalize thread to /api/stop.
- D1 T3 (MergePool) 78% -> 70%. Non-daemon threads + register_shutdown_hook + a bounded 30 s join interacting with childproc's atexit and TerminateJobObject on Windows, where the explicit fallback is 'log and exit anyway' — i.e. a truncated .mca.
- D2 gain_confidence 55% -> 45%. ~270 of the claimed 441 cpu-s rests on the sidecar's unproven 2-3x ceiling, and the 18.6 MB/s rate that scales the entire 441 s figure comes from ONE data point on ONE tile. The design says both things itself; the aggregate number does not reflect them.
- D2 T3 (binary sidecar) 70% -> 55%. A hand-rolled ~200-line codec for a struct containing Option<HashMap<String,String>>, Option<Vec<u64>> and Vec<OsmMember>, writing a PERSISTENT cache, with zero coverage from the existing golden gate (I confirmed the fixtures use --file), defended by a 9-tile VERIFY run. 12+6 hours.
- D2 T5 (--canonical-regions) 72% -> 65%. The byte-identity of kept regions is solid; the eviction-path ordering requirement is not the kind of thing a single warm cs4 cmp catches. It must be proven at cs8 under real RAM pressure with ARNIS_STREAM_TO_DISK=1, because that is the only regime where flush_region_via is the dominant exit path.
- D2 T4 (--osm-bake-tiles) 78% -> 70%. New early-exit CLI mode needs the validate_args exemption AND a gui.rs Args field (the fork's own documented gotcha), and the plan bolts a sweeper that DELETES files from the user's shared OSM cache onto the same task.
- D3 gain_confidence 45% -> 30% for the flag-ON figure. The 3.1-4.6 core-s is derived from a tap count times an assumed 1.0-1.5 ns/tap, the iGPU's 250 GFLOP/s came from a compute-bound noise kernel with essentially infinite arithmetic intensity while a blur is 0.5 flop/byte naive, and T1(b) can invert the sign. The flag-OFF 2-3% from T2 alone is solid and deserves ~85%.
- D3 T5 (WGSL kernel) 55% -> 40%. Shared-memory tiling is stated as mandatory, the renormalisation semantics (sum/wsum over in-grid FINITE taps only, NaN otherwise) must be replicated exactly across four passes plus an on-device blend, and the whole thing must beat a rayon f64 CPU path by 5x on an adapter whose bandwidth-regime f32 throughput has never been measured. 10 agent-hours.
- D3 T6 (parity + seam test) 72% -> 55%. The design itself says the 2x2 seam test may kill T5 and cites the shoreline precedent, which went the wrong way. A task whose stated purpose is to arbitrate a coin flip should not carry 72%.
- D3 T4 (cross-process GPU admission) 70% -> 55%. A Global\ named semaphore across up to 16 GUI-subsystem processes, plus lazy device init with a 2 s timeout, is new cross-process synchronisation on a path that has never had any. 4 agent-hours.
- D3 T7 (Meld plumbing + governor RAM) 80% -> 70%. The padding of _peak_rss_mb_estimate for iGPU device memory that 'may not fully surface in peak_mb' is explicitly a guess (60-120 MB baseline is labelled an assumption).
- D4 T2 (dispatch by learned ring weight) 82% -> 65%. See fatal flaws: the design's own accompanying scheduler analysis measures outer-ring-first at +3.19 s WORSE and history-LPT at +4.33 s WORSE on the baseline set, the opposite sign to this task's -3.94/-4.67 s. The 'learned' framing protects against shipping the wrong direction; it does not restore confidence in the size of the effect.
- D4 expected_gain 6-11% / gain_confidence 55% -> 40%. The ordering component (4-7 s) is unproven and possibly zero or negative; the ramp component (~10.7 s) is real but is 'stop re-running CALIBRATE', and the design correctly notes the two are not additive because warm start flattens the concurrency that creates the ring spread in the first place.
- D4 T6 (churn guards) 68% -> 60%. Only task in the design that edits the live governor state machine, and its failure mode is a starved or hung run rather than a slow one.
- D4 T5 (difficulty-weighted _rate_tp) 74% -> 65%. Changing the metric the hill-climb compares across worker levels is a behaviour change to the one component whose output is already non-reproducible run-to-run, so a regression here is hard to distinguish from noise.
- D4 T4 (machine-scoped history) 76% -> 70%. A store shared across projects on one machine will warm-start a different city, a different seam buffer, or a 13,092-cell grid at a worker count learned somewhere else; the bucket key + RAM re-check help but do not close it.
- D1 T8 88% -> 82% is a minor trim: the 20-shuffled-ordering stress arm requires synthesising the full 6x6 generated ring including the two contested filenames per cell, which is more fixture engineering than 4+2 hours suggests. It is still the best test in the set.

**Fatal flaws:**
- D3 (GPU) — FATAL for T5, and unaddressed anywhere in the plan: cross-TIME reproducibility. Meld worlds are built incrementally, cell by cell, over days or weeks, into ONE master world. The GPU path's output is f32, driver-dependent (naga exposes no NoContraction, so FMA contraction in the 183-tap loop can change with an Intel driver update), and the blur is ALREADY not tile-invariant on the CPU (91-cell radius with edge-renormalised weights, postprocess.rs:977/1016). So a cell generated today with the flag on will not agree along its seam with a neighbour generated tomorrow after a driver update, or with a neighbour generated with the flag off, or on a different machine. The env-flag kill switch cannot repair blocks already written. T6's seam test renders a 2x2 in one session with one driver and therefore cannot see this failure at all. Precedent is directly on point: shoreline ring-fitting had to be forced OFF in master-origin cells for exactly this class of reason. T5/T6/T7 should be considered dead for master-origin renders, which is Meld's primary mode. T2 must be extracted and shipped on its own.
- D2 (Decode Once) — near-fatal for T3 as scoped: the sidecar is a PERSISTENT, hand-rolled binary cache written into the shared OSM cache directory, and NO existing gate can see a bug in it. I verified golden_hash.sh: the fixtures are committed .osm.gz converted to Overpass JSON and fed with --file --offline, so from_tile_dir never executes; and ARNIS_BLOCK_HASH covers placed blocks in memory, which is the wrong side of the encoder. A codec bug (None vs empty Vec, a non-UTF8 tag value, a NaN lat, a truncated varint) therefore produces silently different worlds on EVERY future run, on every site, until someone thinks to delete cache/osm/*.bin — and the content hash protects only against staleness, never against a wrong encoding of correct input. The four invalidation paths are all staleness paths. MELD_OSM_CACHE_VERIFY over 9 Bucharest tiles in CI is not sufficient coverage for a global input format. Fix: VERIFY default-ON for the first N runs (or permanently for the first sighting of each tile), and the read path must fall through to JSON on ANY decode anomaly rather than trusting its own header.
- D4 (Tail-First) — not fatal to the design, but its headline lever is unproven and its own evidence contradicts itself. The design states 'outer-ring-first beats spiral on every dataset under every model: baseline -3.94/-4.67 s'. The scheduler analysis of the same three reports measures, at m=16 on the baseline duration set: spiral 165.75 s, outer-ring-first 168.94 s (+3.19 s WORSE), history-seeded LPT 170.08 s (+4.33 s WORSE), oracle LPT 156.97 s. Those two claims cannot both be true. Compounding it, the ring profile that motivates outer-first was measured on a --no-buildings run (dense core CHEAP, ring 0-1 median 14.6-16.8 s) while bench/matrix.json specifies buildings:true + overture:true, under which the sign plausibly flips and outer-first becomes the WORST order. The design flags this itself and mitigates with 'learned, not hardcoded', which is the correct engineering response — but it means the ordering gain should be booked at 0 +/- 5 s (the frozen-duration model's own noise floor) until re-derived under the real config, and the design's real deliverables are T7 (level.dat donor), T4 (warm start) and T6 (churn guards).
- D1 (Merge Offload) — not a correctness flaw, a risk-allocation one, and it is the plan's own conclusion read honestly. It proposes ~51 agent+test hours that increase concurrent write activity on the user's real Minecraft save, delete a safety invariant that is currently TRUE BY CONSTRUCTION (no kill path can reach a merge because POOL.stop() only flags idle workers and terminate_all() only touches the arnis Popen), and replace it with code in T4/T5 that must be right — in exchange for 0.0-0.4% on a run whose noise floor is ~5 s. The correct extraction is T1 (timers) + T7 (the three races, all of which I confirmed exist TODAY: master_world_path() resolved inside the merge retry loop at server.py:2863 against a non-atomic project.json write from subworld_number, whose _read swallows every exception and returns master_world_dir='' => the cell merges into PROJECT.root; the unsynchronised _META_WRITE read-then-write with a non-atomic write_text; daemon worker threads killable mid-copy2 at interpreter exit). Ship those two. Do not ship the offload as a performance change, and if it is ever shipped as a structural prerequisite, T4's idempotency needs materially more test budget than 3.5 hours.
- CROSS-CUTTING — three of the four designs quote scripts/golden_hash.sh as their release gate, and for two of the four levers it proves nothing. D2 is the only design that states this plainly. Concretely: golden_hash.sh exercises the --file/--offline path with ARNIS_BLOCK_HASH=1, so (a) the OSM tile-dir path (D2-T3) is never executed by it, (b) the region write filter (D2-T5) is invisible to it because the hash covers placed blocks in memory not files on disk, and (c) the GPU flag is never set when it runs, so D3's 'golden hashes need no exemption' is true but is also the reason they are not evidence. Any plan that ships without the two dedicated gates D2 names — ARNIS_BLOCK_HASH parity with and without the tile cache over every cached tile, and a byte cmp of all canonical .mca with and without --canonical-regions in BOTH stream-to-disk states — is shipping unverified.

### Lens 3: Effort & sequencing

| design | gain | conf realism | determinism | effort | total |
|---|---|---|---|---|---|
| Decode Once, Write Once | 7.5 | 7.5 | 8 | 8 | **7.6** |
| Built-Up Gaussian GPU Offload | 4.5 | 6.5 | 5 | 4 | **5** |
| Tail-First Dispatch | 4 | 3 | 8 | 4.5 | **4.6** |
| Merge Offload | 1.5 | 9 | 9 | 2 | **4.4** |

**Recommended order:** STEP 1 (day 1, ~6h, all measurement, zero risk) — D2-T2 `from_reader`->`from_slice` + (u8,u64) dedup key in osm_parser.rs:149-162 and retrieve_data.rs:146/171. Verified in code, byte-identical by construction, and it is the gate that prices D2-T3: if the fetch marker moves less than 1.3x, allocation dominates and the 18-hour sidecar is dead. -> STEP 2 (day 1, ~2h) — D3-T1(a): one `bench.mark` pair around `smooth_built_up_gaussian` (postprocess.rs:208-214). Today `elev_landcover_repair` is ONE label covering level_water_surfaces + reclassify + both Gaussians + the coastal pull. This two-line change converts the derived 3.1-4.6 core-s into a number and is the go/no-go for the ENTIRE Design 3. Do not write a line of D3-T2 before it returns. -> STEP 3 (day 1, ~2h) — D1-T1 / D4-T8 (same task, do it once): four monotonic timers in server.py's _runner tail, extending the MERGE log line at :2880 to `merge Xs prune Ys health Zs meta Ws`. Settles the merge question permanently, is the tripwire for every later change to the post-arnis tail, and costs nothing. It will also confirm what the mtime-derived split already implies: merge is 0.27% and Design 1 should not be built. -> STEP 4 (day 1, ~1h, PREREQUISITE FOR ALL MEASUREMENT) — fix `ab_bucharest.py:302-306` `do_run(reuse=True)` skipping `prepare_project()`/`/api/projects/switch`. I confirmed the damage first-hand: ab-perf-governor-cs8/meld-report.json holds 106 cells, 81 with durations, cell_size 4, elapsed 160.3 — a cs4 warm run written over the cs8 report. Until this is fixed no A/B in any of the four designs can be harvested. Also reconcile bench/matrix.json (buildings:true, anvil) with what the A/B actually ran (buildings:false, blinear, stream-to-disk) — they are different runs and three designs quote numbers across the gap. -> STEP 5 (day 2, ~4h, not a perf item — ship it because it is cheap and it is a data-loss bug) — D1-T2's path caching + D1-T7(b): resolve the master world path ONCE per run into the job dict at _submit_cells, and make project.py `_write` atomic (tmp + os.replace) with `_read` distinguishing missing from unreadable. Today 16 workers do (1 write + 2 reads) of project.json per cell against a swallow-all _read; a losing read returns defaults, master_world_dir becomes '' and the cell merges into PROJECT.root. This is live now, before any offload. -> STEP 6 (day 2-3, ~10h, CONDITIONAL on step 2 returning >=1.5 core-s) — D3-T2: flat Vec<f64> + stride, one cache-blocked transpose between passes, parallelised scatter and blend. Keep the tap loop verbatim (same f64 accumulators, same left-to-right kernel order, same is_finite guard, same wsum>0 branch). Ships ON with golden_hash.sh 5/5 as the identity proof plus a to_bits() unit test. This is the only bit-exact CPU win in the set with no contract debt. -> STEP 7 (day 3-5, ~12h) — D2-T5 `--canonical-regions`. The biggest verified waste anywhere in this set (~43% of save-phase chunk writes discarded, ~340 core-s/run) and the smallest mechanism (intersect a CLI rectangle into the filter that already exists at java.rs:153-160, fed from the same coords.canonical_region_bounds merge.py uses). TWO non-negotiables: patch flush_region_via keeping `world.regions.remove()` and `flushed_regions.insert()` at the identical instant and skipping ONLY `worker.send()` (the A/B ran stream-to-disk at cs4, so this is where the work actually is, NOT save_java); and gate on a byte `cmp` of all 16 canonical .mca in BOTH stream-to-disk states, because golden_hash.sh provably cannot see this change. -> STEP 8 (day 5-6, ~8h, scoped honestly) — D4-T4 machine-scoped governor history, sold as 'first run in a NEW project on a known machine', NOT as the full 10.7 s (project-scoped history already warm-starts repeat renders today). Keep the hardware fingerprint, keep the world-meta exclusion, keep _warm_start's RAM re-check, and add a CONFIG fingerprint to the bucket key — scale+cell_size alone will warm-start a dense city from a sparse one's knee. -> STEP 9 (day 6, ~4h) — D4-T6(a) and (b) only: refuse a grow when `queue_size < workers` (the warm run grew 16->20 four seconds before drain and one of those workers took the 37.9 s cell that set the run's end), and floor the `_rate_tp` span so co-finishing cells stop producing the -75.0 / -126.2 / -266.5 cells-per-min readings the state machine acted on. Both are one-sided and fail toward today. SKIP the cooldown until the corrupted warm pair is re-run. -> STEP 10 (day 6, ~3h) — D1-T7(e): flip server.py:2867 `overwrite_collisions` to False with an explicit same-cell-rectangle allowance, plus a server-side job_size_regions freeze once any cell reaches 'merged'. The disjointness argument holds only for a uniform cell_size and the freeze is client-side only (web/index.html:2125-2140); plan_keys, /api/cell/regenerate and a hand-edited grid.json all bypass it. -> DEFERRED — decide, do not schedule: D2-T3/T4 (binary sidecar + bake pass) only after step 1's measured fetch delta; D3-T1(b) as a standalone 5h spike measuring package power and effective P-core clocks at 16 workers under --gpu igpu vs off, with no budget behind it; D4-T1/T2/T5 (learned ring ordering + weighted step metric) blocked until three clean warm repeats exist, because I could not reproduce the sign of the ordering gain. -> NEVER (this phase) — D1-T3/T4/T5/T6 (MergePool, terminal funnel, Stop rework, UI lane): 28 hours across 20 correctness-critical consumers for a self-measured 0.27% that the design itself says is unresolvable. D2-T7 (mmap zero-copy tag view, 34h, self-scored 35%). D3-T3/T4/T5/T6 (GpuContext extraction, cross-process admission, WGSL kernel, parity+seam tests): 24 agent-hours + 17 test-hours downstream of a spike that can zero it, delivering an approximate contract whose seam test has a documented precedent for failing.

**Over-confident claims flagged:**
- D4-T2 'Dispatch by learned ring weight' — claims 82%. I replayed all three real reports (my LPT-oracle reproduces the map's 156.97 s exactly, so the harnesses agree) and got outer-ring +3.04 s WORSE on gov-cold; the scheduler map got +3.19 s worse on baseline; Design 4 claims it wins on every dataset under every model. My baseline outer figure differs from the map's by 5.4 s purely from the within-ring angle tie-break — larger than the claimed effect. I would give 45%, and I would score the GAIN at 0%, not 4-7 s.
- D4 headline 'expected_gain 6-11% at 55% gain confidence' — strip the ordering lever (unreproducible) and net out what project-scoped history already delivers on repeat renders, and the new gain is ~3-6% on a first-run-in-a-new-project and ~0% on a repeat. I would give 35-40%.
- D4-T5 'Difficulty-normalise _rate_tp' — claims 74%. This edits the live hill-climb metric, its payoff is unmeasurable by construction, and its failure mode is a WORSE converged worker count than the 16 a human picked. I would give 60%.
- D4-T6 'Three churn guards' — claims 68%. Two of the three anchor numbers (42.1 ws oscillation, t+122.4 late grow) come from the report I confirmed is corrupted. Cannot start. I would give 55% until re-anchored, and only 75% for guards (a) and (b) in isolation.
- D2-T3 'Per-tile binary sidecar' — claims 70%. Hand-rolled little-endian format + content-hash header + tmp/rename under up-to-16 concurrent Windows writers + a VERIFY mode + CI coverage in 12 agent-hours is optimistic, and its VALUE is unknown until T2 measures. It is a spike wearing a task's costume. I would give 60% on delivery and ~45% that it is worth the hours.
- D2-T4 '--osm-bake-tiles warm pass' — claims 78%. It is entirely downstream of T3 and additionally needs a new arnis early-exit mode with a validate_args exemption and a gui.rs Args field (the fork's own documented gotcha). Cascading dependency on an unpriced task. I would give 68%.
- D2 headline 'expected_gain 1.20x at 55%' — T5's ~340 core-s is well grounded; the ~270 core-s OSM half rests on a single-sample 18.6 MB/s measurement and an unbuilt sidecar. Realistic combined 1.10-1.18x. I would give 45%.
- D3-T5 'The WGSL kernel' — claims 55%. It cannot start until T1(b) clears, it must be shared-memory tiled or it loses to the CPU outright (0.5 flop/byte naive), and the design's own instruction is 'if it is not at least 5x, stop'. A task with a documented abort condition and an unresolved prerequisite is not 55%. I would give 40%.
- D3-T4 'Cross-process GPU admission' — claims 70%. Sixteen simultaneous Vulkan device creations against one Intel driver is explicitly untested; a Global\ named semaphore, an init timeout and a CPU fallback are all being designed against behaviour nobody has observed. I would give 55%.
- D3-T6 'Guards, parity contract, tile-seam test' — claims 72%. The guards are easy; the seam test is the arbiter and the design itself cites the precedent where this exact class of failure forced shoreline ring-fitting OFF in master-origin cells. Confidence that this task SHIPS A USABLE GPU PATH: 55%.
- D3-T2 'Flat-layout bit-exact blur' — claims 85%. The mechanism is right (I read the code) but preserving bit-exactness through a transpose while also parallelising the scatter and converting total_influenced to a sum reduction is fiddlier than 6 hours suggests, and the public gaussian_blur_grid wrapper's Vec<Vec<f64>> signature has callers. I would give 80% — still the highest genuine >75% task in Design 3.
- D3 headline '6-9% flag on at 45%' — the flag-on number is contingent on a spike that can zero it, so the shippable-now expectation is the 2-3% flag-off figure, and even that halves if T1(a) returns under 1.5 core-s. I would give 30% to the 6-9% band and treat 2-3% as the plan's actual content.
- D1-T9 'Kill switch, settings, docs, A/B' — claims 90%. The task will be completed, but its central artifact (an A/B run) is stated by the same document to be incapable of resolving the effect. Scoring 90% on a deliverable that cannot deliver evidence is a category error; as an evidence-producing task it is 15%.
- D1-T3 'MergePool' — claims 78%. Non-daemon threads + register_shutdown_hook + bounded queue + drain semantics + a clamped setting, correct on Windows at shutdown, in 4 agent-hours. I would give 65% — and it should not be built at all this phase.

**Fatal flaws:**
- Design 4 (FATAL): the ordering lever's sign is not reproducible. I ran the list-scheduling replay on all three real meld-report.json files; my LPT-oracle reproduces the scheduler map's 156.97 s exactly, so the harnesses agree — yet ring-descending comes out +3.04 s WORSE than spiral on gov-cold in my replay and +3.19 s worse on baseline in the map's, against Design 4's claim of a win on every dataset under every model. Worse, my baseline outer-ring number differs from the map's by 5.4 s purely from the within-ring angle tie-break, i.e. an arbitrary implementation choice moves the answer further than the claimed effect. T2 carries 82% confidence and books 4-7 s of the 6-11% headline on this.
- Design 4 (serious): T4's real-world scope is smaller than sold. governor_history is already project-scoped and persists (project.py:183); a user re-rendering an existing project warm-starts TODAY. The 10.7 s cold->warm delta exists because bench_scheduler.py:16,701 gives every repeat a fresh project. Machine-scoping helps the first run in a NEW project and the benchmark — real, but not the full 10.7 s, and the design books the full amount.
- Design 4 (blocking): T6's two anchor measurements come from a report whose provenance is the harness reuse bug. I confirmed the file: ab-perf-governor-cs8/meld-report.json contains 106 cells of which 81 have durations, cell_size 4, elapsed 160.3 — a cs4 warm run written over the cs8 report. T6 cannot start until that pair is re-run, and the cs8 governor arm (239.4 s / 5 workers) can now only be cited, never recomputed.
- Design 3 (FATAL under the stated lens): the 6-9% headline is entirely contingent on T1(b), a spike whose own documented possible outcome is 'T3-T7 are dead on this machine'. A plan whose gain is unknowable until a research spike resolves is not >85% by definition — that is the user's rule and this design is its clearest instance. 24 agent-hours + 17 test-hours sit downstream of it.
- Design 3 (serious): the GPU path is approximate-by-contract with a VISIBLE failure mode (~0.02% of surface columns shift a block, 40x the cave kernel's drift, on flat urban ground), and its own T6 tile-seam test is likely to fail — the blur radius is 91 blocks, both passes renormalise over in-grid samples only, so adjacent Meld cells already disagree near a shared border, and f32 makes that disagreement noisy. The design names the precedent itself: shoreline ring-fitting had to be forced OFF in master-origin cells for exactly this. The honest expected outcome of the GPU branch is that it is written and then disabled.
- Design 3 (scoping): even the 'safe' half is gated on the same unmeasured number. T2's 0.95-1.5 core-s comes from the same derived 3.1-4.6 core-s Gaussian estimate (3.073 G taps x an ASSUMED 1.0-1.5 ns/tap). If T1(a) returns under 1.5 core-s, the design's default-config 2-3% becomes ~1% and it has no shippable content.
- Design 1 (FATAL as a perf item): 51 hours to move 0.27% of worker-time, self-scored at 15% gain confidence and self-described as 12x below the harness noise floor, while touching 20 correctness-critical consumers — run-end, auto-export, Stop, and the render-queue driver that REBINDS the global PROJECT. The load-bearing task (T4, the terminal funnel) is self-scored 66%. The design is correct in every particular and should still not be built this phase; extract T1, T2's master-path caching and T7's race fixes (~8h) and stop there.
- Design 1 (unaddressed): T5 has /api/stop hand finalize to a background thread that awaits MERGE.drain(timeout=120) — but the document never says what happens ON timeout. Proceeding to finalize with merges outstanding is precisely map row 7's half-mca/half-linear world, the exact hazard safeguard D exists to prevent. A 120 s bound on an invariant stated as absolute (constraint 4) is a contradiction the design does not resolve.
- Design 2 (attribution error): T5's gain is sized as '~43% of the save phase' off the NON-eviction shape, but the cs4 A/B ran blinear + stream-to-disk (server.py:2761 sets ARNIS_STREAM_TO_DISK when the setting is on OR cell_size>=8), where most regions leave via flush_region_via inside `place`, not save_java. The design does flag the eviction path as the primary RISK, but still quotes the save-phase percentage as the GAIN, and never states which stream-to-disk state the profile was taken in. The total waste is almost certainly still real (36 written / 16 kept holds for .b_linear too — merge.py treats both identically), but the code path that must be patched is not the one the number was measured on.
- Design 2 (sequencing): T3+T4 are 18 agent-hours + 9 test-hours whose value is explicitly unknown until T2 measures, and they carry 270 of the 630 cpu-s in the headline. The design says so in its own risk register, which is to its credit, but the headline 1.20x is quoted as if the conditional half were funded.
- ALL FOUR DESIGNS (shared, blocking): every measurement in every plan runs through bench/ab_bucharest.py, whose do_run(reuse=True) at :302-306 skips prepare_project() and therefore never calls /api/projects/switch. Designs 3 and 4 both spot it; nobody makes it step one. Compounding it, bench/matrix.json declares buildings:true / overture:true / anvil while the A/B report records buildings:false and the ground truth says blinear + stream-to-disk — the benchmark config and the measured config are different runs, and three of the four designs quote numbers across that gap.

## 3. Subsystem maps

--- merge-path ---
## src/mca.py does not exist

`find . -name "mca*.py"` in `C:\Users\LEGION\Documents\Meld\meld-triagefix` returns nothing. There is no NBT/Anvil reader/writer in Meld's merge path at all — **the merge never opens a region file**, it only globs and copies. That single fact drives most of the answers below.

---

## 1. Where arnis stops and the merge starts, and what the merge does

### The handoff, line by line (all `server.py` unless stated)

| Line | What happens on the worker thread |
|---|---|
| `workers.py:275` | `ok = bool(runner(job, state))` — the pool calls `_runner`; the slot is held for the whole call |
| `2584` | `def _runner(job, state)` |
| `2605` | `PROJECT.set_cell_status(cell_key, "running")` → locked read+rewrite of `grid.json` |
| `2609` | `_timing_started(cell_key, worker_id)` — **the report's clock starts here** |
| `2610` | `clean_output_dir(out)` (`arnis_cmd.py:613`) — `rmtree` of any stale cell world |
| `2806-2808` | `ok = run_arnis(...)` — **blocking; this is the only line arnis owns** |
| `2811-2813` | `_stopped_now = _run_stop_requested()`; `_governor_cell_done(...)` — the governor is fed here, **before the merge**, so the governor's clock stops at arnis exit |
| `2817` | `state["phase"] = ""` — explicit comment: *"everything after this line is Meld's own work (merge, prune, meta)"* |
| `2818-2823` | close the per-cell log file |
| `2824-2837` | failure exits (`set_cell_status`, `_record_fail`, `_surface_failure_tail`) |
| `2839` | `world_dir = find_world_dir(out)` (`arnis_cmd.py:593`) — `iterdir()` + `stat()` per candidate |
| `2846-2852` | `PROJECT.subworld_number(cell_key)` (locked read+rewrite of `project.json`) then `patch_level_name(cell/level.dat, "Meld Sub World N")` — **gzip decompress + gzip recompress of the cell's level.dat** (`level_dat.py:68,88`) |
| `2854` | `state.update(progress=96, message="Merging…")` ← **this is the exact moment the UI calls it a merge** |
| `2861-2879` | 3-attempt loop; `master = str(master_world_path())` (`2863`) then `merge_cell_into_master(...)` (`2864`) with `overwrite_collisions=True` (`2867`) |
| `2880-2881` | `log("MERGE {cell}: +N regions, -M seam, level.dat=...")` — **no duration** |
| `2882-2899` | drift / collision / generic handlers |
| `2901-2904` | `set_cell_status("merged")`, `_clear_fail`, `_scan_cell_health`, `_post_merge_export_hook` |
| `2907-2912` | **prune**: `shutil.rmtree(out, ignore_errors=True)` — deletes the entire cell world (canonical + seam regions) |
| `2913-2921` | else-branch: `strip_buffer_regions` (`merge.py:259`) — unlinks the seam files instead |
| `2924` | `write_world_meta(Path(master), throttle=True)` |
| `2925-2926` | `progress=100, message="Merged."`, `return True` |
| `workers.py:280-289` | `record_completion(monotonic-t0)` (EWMA), `state.update(running=False, ...)`, then `_on_complete(job, ok, err)` |

So: **the slot is held from `2605` to `workers.py:289`**, of which arnis is only `2806-2808`.

### What `merge_cell_into_master` actually does (`src/merge.py:84-236`)

Pre-flight, read-only:
- `world_container()` on cell and master (`127-128`, defined `65-81`) — a `glob` per suffix to decide `.mca` vs `.b_linear`; refuses mixed/mismatched (`129-140`).
- Builds `copy_plans` (`142-163`): for each of `region/`, `poi/`, `entities/` (`146`), globs region files (`151` → `_region_files`, `59-62`), parses `r.X.Z.{mca|b_linear}` (`_MCA_RE`, `35`), drops anything outside the canonical rectangle (`157-159`), records `dst.exists()` as a collision (`161-162`).
- Drift guard (`170-186`): pure integer comparison of the generated region extent against `canonical_region_bounds(cell_key)` (`coords.py:102-116`).
- Collision raise (`188-195`) — **bypassed in production**, `server.py:2867` passes `overwrite_collisions=True`.

Mutate:
- `199-205`: `for src, dst, sub in copy_plans: dst.parent.mkdir(...); shutil.copy2(src, dst)`. **Sequential, one file at a time, no lock.**
- `218-221`: datapacks under `_MASTER_LOCK` — `_copy_datapacks` (`239-256`) skips packs already present (`249-250`), so after cell #1 this is a directory listing under a global lock.
- `224-234`: `level.dat` under `_MASTER_LOCK`, guarded by `if not dst_dat.exists()` (`227`) — **copied exactly once per world**, then `patch_level_name` + `gold_name`.

**blinear vs mca:** identical code path. `merge.py:30-33` states it explicitly — `REGION_SUFFIXES = (".mca", ".b_linear")` and "*the merge treats both identically because it never looks inside a region file.*"

**Compression:** none. No zlib, no NBT, no chunk rewrite. The only (de)compression anywhere in the merge path is `gzip` on `level.dat` (`level_dat.py:68,88`) — once for the master, once per cell for the sub-world name at `server.py:2850`.

**fsync:** none. `grep fsync src/merge.py` → nothing. `shutil.copy2` = `copyfile` + `copystat`; both buffered. The bytes land in the Windows page cache and are written back later by the OS, off this thread.

---

## 2. How long a merge takes relative to arnis

### Nothing separates them. Confirmed three ways.

1. The `MERGE` log line (`2880`) prints counts only — `regions_copied`, `regions_skipped`, `level_dat`. No timer, no timestamp.
2. `_CELL_TIMING[ck]["duration"]` (`_timing_started` `2609` → `_timing_finished` `793-802`, called from `_on_complete` `3251`/`3255`) is **slot time**: setup + arnis + merge + prune + bookkeeping. It reaches the report as `duration_s` (`runreport.py:96`) and as `cell_median_s` / `cell_avg_s` (`runreport.py:138-140`). It does **not** include the admission gate (`workers.py:251-259` runs before `_runner`).
3. The governor's `wall_s` is arnis-only — `run_arnis` measures `_started` at `arnis_cmd.py:819` to `proc.wait()` at `842`, or takes arnis's own `phase=done wall_s=` (`850-851`), and `_governor_cell_done` consumes it at `server.py:2812` *before* the merge. `docs/generation-performance.md:622-626` says this in as many words: *"Merging is Meld-side and runs after the generator exits, so the governor measures generator wall time and does not see it… merge itself is untouched work."*

The two numbers exist in the same process and are **never written to the same artifact**: `build_report` (`server.py:864-869`) is passed `run`, `timing`, `timeline`, `grid`, `prefetch_timings`, `settings`, `machine` — no governor and no occupancy data. And the `[meld] v=1 phase=done wall_s=…` line never reaches the cell log, because `run_arnis` consumes marker lines at `arnis_cmd.py:828-839` and `cell_log_fp.write` only runs inside `on_line` (`server.py:2674-2678`).

### My estimate of the size (arithmetic shown; treat as an estimate, not a measurement)

Bytes copied per cs4 cell:
- canonical rectangle = `size × size` = **4 × 4 = 16** region files kept (`coords.py:110-116`); everything outside is skipped (`merge.py:157-159`).
- seam: `seam_buffer_chunks=8` → 8 × 16 = 128 blocks per side at scale 1.0 (`coords.py:235-236`); a cell is 4 × 512 = 2048 blocks, expanded to 2304 → arnis typically writes **6 × 6 = 36** region files, so ~20 of 36 are discarded per subdir.
- region size: Meld's own model is `_MB_PER_REGION = 3.5` (`server.py:323`). Measured on a real 1:1 Bucharest world on this machine (`.minecraft/saves/CaveBiomes3-Bucharest/region`): 52.7 MB over 6 files, dense-centre regions 11.0-11.5 MiB, edge regions 4.0 MiB, **mean 8.8 MB**.

→ 16 × 8.8 MB ≈ **141 MB** copied per cs4 cell in `region/`. `poi/` and `entities/` I did **not** measure — the sample world has no such directories. Unknown, but they are chunk-sparse and normally far smaller.

Copy throughput, **measured just now** on this machine (`shutil.copy2` of those real `.mca` files, source pre-warmed, two trials): **3045 and 3076 MB/s single-threaded, ~3 ms per 8.8 MB file.**

- 141 MB / 3060 MB/s ≈ **46 ms**
- pessimistic sustained-NVMe 500 MB/s: 141 / 500 ≈ **280 ms**

Against the measured arnis per-cell wall (5.16 s at T=21, 18.96 s at T=2), the copy is **0.9% to 5.4%** of the cell. **The copy is not the phase-2 win.** What I cannot rule out:

- The 3 GB/s number is page-cache-to-page-cache with **no fsync**. The write is deferred. At 16 concurrent workers × 141 MB, a run pushes ~2.3 GB of dirty pages per merge wave; if Windows write-back throttling kicks in, the cost shows up as a stall inside a *later* `copy2`, not in this measurement. **Unmeasured.**
- The **prune** (`2909`) deletes the whole cell world — 36+ region files ≈ 300 MB plus `datapacks/` (arnis_tall = many small files), per cell, per-file NTFS metadata work. **Unmeasured, and it is on the same worker thread as the merge.**
- `_scan_cell_health` (`2903` → `956`) does `read_text()` of the **entire** cell log with no tail limit. arnis prints hundreds of lines per cell; a cs8 cell log can be megabytes. `_record_fail` by contrast correctly tails to `[-6000:]` (`998`).

### Cheapest way to measure it — recommended

One Meld-side change, zero arnis change (so the 5 golden hashes are untouched by construction):

```python
_t_m0 = time.monotonic()          # immediately before line 2855's `try:`
...
_merge_s = time.monotonic() - _t_m0
_t_p0 = time.monotonic()          # around the prune block, 2907-2921
_prune_s = time.monotonic() - _t_p0
```
and extend the existing line at `2880` to `MERGE {cell}: +N regions, -M seam, level.dat=…, merge {x:.2f}s, prune {y:.2f}s`. Log-only, no schema change, no new setting, parseable straight out of the log for an A/B.

If you want it in the report too, add `merge_s` / `prune_s` / `arnis_s` keys in `_timing_finished` (`793`) → they flow through `runreport.build_report` `timing` (`server.py:865`, consumed `runreport.py:92-98`); that does touch the report schema (`SCHEMA`), so it is the more expensive option.

**Zero-code approximation available today** (usable but noisy): `meld-report.json` `summary.cell_median_s` (slot time) minus `workers × 60 / governor.cells_per_min` (median arnis wall — `governor.py:683-699`, exposed at `server.py:2537`). Caveats: `cells_per_min` is `None` in `governor_mode="off"`, it is a live snapshot that `_governor_end_run()` (`3273`) runs before `_write_run_report()` (`3276`), and the difference also absorbs setup + `subworld_number` + status writes. Good enough to say "merge is 5% or 40%", not good enough to plan against.

---

## 3. CPU-bound, disk-bound, or lock-bound?

**Neither CPU-heavy nor decompression-bound. It is a buffered byte copy, and per-cell it is short.**

Evidence:
- **No decompress/recompress of region data.** `merge.py` imports `math, re, shutil, threading, Path` (`20-24`) and nothing else. There is no zlib, no NBT. The comment at `merge.py:32-33` is explicit that the merge "never looks inside a region file."
- **memcpy, and specifically a Python-level one.** On Windows, CPython's `shutil.copyfile` has no fast path — `_HAS_FCOPYFILE` is macOS, `_USE_CP_SENDFILE`/`_USE_CP_COPY_FILE_RANGE` are Linux; Windows falls through to the `copyfileobj` read/write loop. Verified against the installed interpreter: **Python 3.14.3, `shutil.COPY_BUFSIZE = 1048576`**. So one 1 MiB `read()` + one `write()` per MiB, ~141 syscall pairs per cs4 cell, with the GIL released inside each syscall and held between them. No `CopyFileW`, no reflink, no `FILE_FLAG_SEQUENTIAL_SCAN`.
- Plus `copystat` per file (mode + mtime) — extra metadata syscalls, `copy2` semantics.
- **One region file at a time**, `merge.py:200-204`, single-threaded within a cell. But N cells merge **in parallel** across N worker threads today, and the region copies are outside every lock.
- **No fsync.** Wall cost on the worker thread is page-cache work; the physical write is the OS's problem, later, on another thread.

**Where locking actually bites — and it is not `_MASTER_LOCK`:**

- `merge.py:41 _MASTER_LOCK` is module-global but held only for datapacks (`218-221`) and `level.dat` (`226-234`). `level.dat` is copied once ever (`227`); datapacks degenerate to a `sorted(iterdir())` after cell #1 (`249-250`). Contention here is negligible.
- **`src/project.py:15 _LOCK` is the real serializer.** It is a process-global `threading.Lock`, and every mutation is a full `_read` (`403-409`: `json.loads(read_text())` of the whole file) + `_write` (`411-413`: `json.dumps(indent=2)` + `write_text`) **inside the lock**. `_runner` takes it at least **twice per cell** on the happy path — `set_cell_status("running")` (`2605`) and `set_cell_status("merged")` (`2901`) on `grid.json` — plus `subworld_number` (`2849` → `project.py:484-493`) rewriting `project.json`. Failure paths add more (`2825, 2841, 2883, 2889, 2895`).
  - Arithmetic: at 81 cells `grid.json` is ~2 KB → invisible. At the 13,092-cell run named in `docs/TRIAGE-2026-08.md:236-237`, `grid.json` is ~400 KB, re-serialized and rewritten **twice per cell under a global lock** — that is where this becomes a wall, not at 81 cells.
- `server.py:921 _CELL_HEALTH_LOCK` + `_save_cell_health()` (`943-947`) — another whole-file JSON write per merged cell.

**Verdict:** per-cell, the merge is a short buffered copy whose cost is dominated by page-cache memcpy and per-file syscalls; the surrounding Meld bookkeeping (three global-lock JSON rewrites, a gzip round-trip on level.dat, a full log read, and an unmeasured 300 MB `rmtree`) is plausibly comparable to or larger than the copy itself. **I have not measured that split — see §2.**

---

## 4. Shared state, and whether two cells can merge concurrently

### They already do

Merging runs on the pool's worker threads (`workers.py:227-296`), N at a time. There is no serialization of merges today beyond `_MASTER_LOCK`'s two narrow sections.

### What is shared

| Shared thing | Where | Held for |
|---|---|---|
| `merge._MASTER_LOCK` | `merge.py:41` | datapacks (`218-221`), level.dat (`226-234`) |
| `project._LOCK` (process-global) | `project.py:15` | every `grid.json` / `project.json` read-modify-write |
| `_META_WRITE = {"at": …}` | `server.py:2167` | **nothing — no lock** |
| `_CELL_HEALTH_LOCK` + `cell_health.json` | `server.py:921, 943` | per merged cell |
| `_STREAM_LOCK` / `_STREAM["session"]` | `server.py:687-691` | the export streaming session shared by all merges; `sess.submit(mca)` per region (`704`) |
| `_CELL_FAIL` under `_CELL_HEALTH_LOCK` | `server.py:979, 1011, 1044` | per cell |
| `_RUN_LOCK`, `_RUN_TIMING_LOCK` | `server.py:296, 765` | `_on_complete` / timing only |
| `WorkerPool._lock/_cv` | `workers.py:46-47` | slot state |
| the master world directory | `merge.py:201, 220, 229` | `mkdir(exist_ok=True)`, safe |
| **no region cache, no index, no manifest, no open world handle** | — | — |

There is deliberately **no master-world handle**: constraint 2 is structurally satisfied because nothing in Meld ever opens the master world's regions during a run.

### Which files two adjacent cells can both touch

**Region files: none. Ever.** Canonical rectangles tile exactly, by integer arithmetic:

`coords.py:76-89, 102-116` — for key `"rx,rz,size"`:
```
rx_min = rx*size,  rx_max = rx*size + size - 1
rz_min = -rz*size - size,  rz_max = -rz*size - 1
```
For a fixed `size`, distinct `(rx, rz)` give disjoint half-open blocks of length `size` on both axes. The seam-buffer regions two neighbours *both generate* are the ones that get discarded at `merge.py:157-159`, in the pre-flight, before any write. The same disjointness is what `_post_merge_export_hook` relies on when it enqueues regions into the shared stream session (`server.py:692-704`).

**Shared files two merges genuinely contend on:**
1. `<master>/level.dat` — only the first merge writes it (`merge.py:227`), under `_MASTER_LOCK`.
2. `<master>/datapacks/**` — `_copy_datapacks` under `_MASTER_LOCK` (`merge.py:218-221`).
3. `<master>/meld-world.json` — `write_world_meta(throttle=True)` (`server.py:2924`, impl `2171-2191`). **The throttle guard at `2181` is an unsynchronized read-then-write of `_META_WRITE["at"]`.** Two workers can both pass it in the same 20 s window and both `write_text` the same path. Non-atomic write → a torn sidecar is possible. Benign in practice (rewritten unthrottled at run end, `3270`) but it is a real race.
4. `<project>/grid.json`, `<project>/project.json`, `<project>/cell_health.json` — correctly locked on the write side.

### The two hazards I would flag before touching this

**(a) `overwrite_collisions=True` disables the only guard against a region being owned twice.** `server.py:2867`. The justification at `2856-2859` is that the v1 uniform grid makes rectangles disjoint — correct **only while every cell in the grid has the same `size`**. Disjointness fails across sizes (a `size=8` cell at `rx=0` owns regions 0..7; four `size=4` cells own 0..3 and 4..7 — overlap). I found **no server-side guard** freezing `job_size_regions` once a cell is merged: the only occurrences are clamps and defaults (`4062-4063, 4332, 4929, 5333, 5440`). The freeze is **client-side only** (`web/index.html:2125-2140`). Any code path that reaches `_submit_cells` without going through the browser — the render queue's `plan_keys` (`5429`), `/api/cell/regenerate`, a hand-edited `grid.json` — can produce mixed sizes, and the merge will then silently overwrite rather than raise.

**(b) `master_world_path()` is called inside the merge loop and reads project.json without the lock.** `server.py:2863` → `1082-1098`, which calls `PROJECT.settings()` (`project.py:503-510`) and `PROJECT.load()` (`433-434`). Both go through `_read` (`403-409`), which **swallows every exception and returns the default** (`407-409`). `_write` is a non-atomic `write_text` (`413`). Meanwhile `subworld_number` (`2849` → `project.py:484-493`) rewrites `project.json` **on every cell**. If a `master_world_path()` read lands inside that write window, `load()` returns `_default_project()` — `name = "Meld World"` (`project.py:420`) — and `settings()` returns `default_settings()`, so `master_world_dir` is `""` and `parent = PROJECT.root` (`server.py:1092`). The cell would then be merged (and a directory `mkdir`'d, `1094-1097`) into **the wrong world folder**. I have not observed this; the window is one small `write_text`. But 16 workers × (1 write + 2 reads) per cell makes it a real, un-guarded race, and moving merges to a different thread pool does not remove it.

---

## 5. Every consumer that breaks if merging leaves the worker thread

The load-bearing coupling is: **worker slot busy ⟺ cell not yet merged**, and **`_on_complete` fires once, after the merge, with the merge's outcome folded into `ok`.**

| # | Consumer | Where | What breaks |
|---|---|---|---|
| 1 | **Run-end condition** | `3257-3271`: `_RUN["done"]+_RUN["failed"] >= _RUN["total"]` inside `_RUN_LOCK` | Fires at arnis-exit instead of merge-completion. The run is declared over with merges still writing into the world. Everything in 2-6 below hangs off this. |
| 2 | **`_governor_end_run()`** | `3273` | Persists the learned bucket + `POOL.admit_cb = None` (`2553`) while merges are still consuming disk/RAM. |
| 3 | **`_record_size_calibration()`** | `3275` → `492-515` | Divides on-disk MB by merged regions. Runs before the last merges land → `mb_per_region_observed` written too low, and it is **persisted into settings** (`509-513`) so it poisons future estimates. |
| 4 | **Run report** | `3276` → `_write_run_report` `841-881` | `_CELL_TIMING.duration` (`800`) would no longer contain the merge; `cell_median_s`/`cell_avg_s` (`runreport.py:138-140`) silently change meaning. `_timing_finished` is called from `_on_complete` (`3251/3255`), so it must be re-anchored to whatever now signals merge-done. Report also gets written while merges continue. |
| 5 | **`_maybe_write_map_item()`** | `3277` → `3123-3157` | Spawns `arnis --map-item-only` over the **master world**. A partial world → a wrong map, baked in. |
| 6 | **`_scan_missing_regions()`** | `3278` → `3196-3220` | `finalcheck.find_missing_regions` over the master world → every not-yet-merged cell reported as an interior hole, `⚠️` on the map, "Retry missing" offered for cells that were fine. |
| 7 | **Auto-export trigger** | `3279` → `_maybe_run_export` `3160-3193` | The worst one. Guarded once per run by `_EXPORT_STARTED["run"] = _RUN["started"]` (`3178-3180`). It either finalizes the streaming session (`_finish_stream_session`, `741-760`) or starts a compress pass (`_start_export_job`, `2951`). Both convert/compress/**delete** `.mca` in the master world. A pending merge then `copy2`s a raw `.mca` over a world the exporter already converted → half-mca/half-linear, exactly what safeguard D at `671-706` exists to prevent. Also `sess.submit(mca)` from `704` racing `sess.finish()` from `749`. |
| 8 | **ETA** | `/api/mini` `3419-3422`: `eta = (elapsed / finished) * remaining` | `finished` counts arnis exits → ETA becomes optimistic by exactly the merge tail, and goes to 0 while merges continue. |
| 9 | **`/api/mini` shape** | `3498-3535` | `workers_busy` (`3438`), `workers[]` (`3433-3435`), `tasks[]` (`3440-3444`), `percent` (`3506`), `active` (`3504`), `task.title` (`3479-3494`). Slots go `idle` while merges pend → the bar shows an idle pool and "Finished" with work outstanding. |
| 10 | **`/api/status` shape** | `5668-5693` | `workers` (`5669`, from `POOL.get_states()`), `running: POOL.is_running()` (`5671`), `run.active` (`5627`), and the timeline sample `n_running` (`5631-5635`) which feeds the report's activity graph (`_timeline_sample` `805-826`). |
| 11 | **Tray / status-bar stages** | `WORKER_STAGES` `3315-3316`; `_worker_stage` `3331-3385`; colours `statusbar.py:46,50-54` | `"merge"` and `"finishing merges"` are derived **purely from the worker slot** — `state["message"]` containing `"merg"` (`3363-3367`) or `state["phase"]` mapping (`3322-3328, 3359-3362`). Move the merge off the slot and **both stages become unreachable**; `STAGE_ORDER` (`statusbar.py:54`) loses its last element and the bar shows idle blocks during merges. A new signal has to be published for them. |
| 12 | **Stop** | `/api/stop` `5587-5618`; `POOL.stop()` `workers.py:105-114`; `terminate_all()` `192-206` | The invariant *"a merge in flight is always allowed to finish; only arnis is ever killed"* (constraint 4) is currently **structural**: `stop()` only sets a flag that idle workers read at `workers.py:236`, and `terminate_all()` only touches `state["process"]` — which `on_proc` (`2684-2685`) sets to the arnis `Popen` and `workers.py:283` clears. A merge simply cannot be reached by either. Move merges to another executor and **that guarantee has to be re-implemented explicitly**: `POOL.stop()` no longer drains them, and `_worker_stage`'s `"finishing merges"` (`3362, 3367`) no longer has a slot to render on. Note also that `/api/stop` finalizes a partial report at `5605-5617` — that would now run with merges outstanding. |
| 13 | **Retry model** | `_on_complete` `3223-3246` | `ok` currently means *generated **and** merged*. Split them and a merge failure has no path back into `_RETRYABLE_FAIL`/`_MAX_CELL_RETRIES` (`2932-2943`) unless a second completion channel is built. |
| 14 | **`_run_active()` → render queue** | `4375-4378`; driver `5385-5479`, busy check `5466`, 3-idle-poll advance `5469-5472` | `POOL.is_running()` goes False at arnis-exit. The queue advances and calls `_switch_project` (`5421` → `5104-5115`) which **rebinds the global `PROJECT`** while pending merges are still calling `master_world_path()` (`2863`) — cells would merge into the *next* project's world. This is the sharpest failure in the list. |
| 15 | **Grid-edit guards** | `_run_active()` consumers: `5106`, `5369`, `5775`, `5816`, `5868` | Routes that refuse mid-run (project switch, project delete, statusbar toggles) would stop refusing while merges are in flight. |
| 16 | **`_post_merge_export_hook`** | `2904` → `671-706` | Lazily creates the stream session under `_STREAM_LOCK` (`687-691`). If merges run on a different pool, the first-merge lazy-init races the run-end `_finish_stream_session` (`3170-3174`, `3187`). |
| 17 | **Governor** | `_governor_cell_done` `2812`, `2342-2360`; `_rate_tp` `governor.py:701-715` | Already merge-blind, so its *numbers* survive. But its notion of `active` comes from `sum(s["running"])` (`workers.py:248`) — freeing the slot earlier changes what `active` means at the admission gate (`governor.py:628-645`), so the RAM gate would admit new cells while merge memory/dirty-page pressure is still outstanding. |
| 18 | **Prune** | `2907-2912` | `shutil.rmtree(out)` must not run before the merge that reads from `out` completes. Trivially true today; becomes an ordering constraint. |
| 19 | **`_dir_size_mb` at run end** | `3266` → `1122-1127` | `p.rglob("*")` + `stat()` over the **entire master world**, executed **while holding `_RUN_LOCK`**, on the last worker thread. `/api/status` (`5623`) and `/api/mini` (`3409`) both take `_RUN_LOCK` → the UI freezes for the duration on a large world. Pre-existing, not caused by offloading, but it lives in the same tail and gets worse if run-end fires earlier and more often. |
| 20 | **Interpreter-exit safety** | `workers.py:223` `daemon=True`; `childproc.kill_all` `251-269`, `atexit` at `248` | Worker threads are daemons, so a **quit** (not a Stop) can already terminate a thread mid-`copy2`, leaving a truncated `.mca` in the master world. `kill_all` waits on child *processes*, never on Python threads, and `TerminateJobObject` (`315`) does not reach them. Any new merge executor inherits this and must be non-daemon or explicitly joined in a `register_shutdown_hook` (`198-207`). |

---

## 6. Failure / retry model today, and what would change

### Today

**Inside `merge_cell_into_master`** — all validation is read-only pre-flight (`merge.py:95-97`, `123-196`), so a refused merge leaves the master genuinely unchanged:
- `MeldCoordinateDriftError` (`174-186`) — zero region files, or the generated extent fails to reach the canonical rectangle within `buffer_regions = ceil(seam/32) + 1` (`170`). Deliberately gross-displacement-only; a few missing edge regions is tolerated (`165-169`).
- `MeldCollisionError` — container mismatch/mixed (`130-140`), or canonical collisions (`190-195`) **which production never sees** because `overwrite_collisions=True` (`server.py:2867`).

**In `_runner`** (`2855-2899`):
- `OSError` with `winerror in (433, 21, 112, 1167)` → up to 2 retries, `sleep(0.5 * (attempt+1))` → **~1.5 s total** before failing (`2870-2879`). Flaky external save drive. A persistently-offline drive is caught earlier by the queue-time pre-flight `_output_drive_ok` (`1101-1110`).
- Drift → `set_cell_status("drift")` + `_record_fail("coordinate drift…")` (`2882-2887`).
- Collision → `set_cell_status("collision")` (`2888-2893`).
- Any other exception → `set_cell_status("failed")` + `_record_fail(f"merge error: {ex}", out=out)` (`2894-2899`).

**In `_on_complete`** (`3223-3246`): `deterministic` = status in `("drift","collision")` **or** reason contains `"disk full"|"panic"|"merge error"` **or** matches `_NEVER_RETRY_FAIL` (`3230-3232`). `transient` = matches `_RETRYABLE_FAIL` (`2932-2937`). Retry only if `transient and not deterministic and retries < 2 and not POOL.is_stopped and not _run_stop_requested()` (`3239-3240`), which re-queues via `POOL.submit` with `_retries+1` and returns **before** counting the cell (`3246`).

Net: **every merge-side failure is deterministic and is never retried**, except the WinError drive blip, which is retried inside the merge call and never surfaces to `_on_complete`. Retries exist only for arnis-side transients.

Note a deliberate asymmetry documented at `docs/generation-performance.md:623-625`: `_governor_cell_done` reports `ok=True` (`2812`) for a cell whose merge *later* fails — the governor's sample is about generation, not delivery.

### What would change if merging moved off the worker thread

1. **`ok` splits in two.** `runner`'s return value (`workers.py:275`) currently answers "did this cell land in the world?". It would answer only "did arnis succeed?". `_on_complete`'s whole classifier (`3223-3246`) and the `_RUN` counters (`3259-3263`) key off it. You need a second completion event, and `_timing_finished` (`3251/3255`) has to be re-anchored to it or `duration_s` silently changes meaning.

2. **Merge failures lose their retry path entirely** — today they at least reach `_record_fail` + `set_cell_status` on the same thread that owns the cell. Off-thread, `_record_fail(…, out=out)` (`990-1012`) reads `logs/cell-<tag>.log`, which is fine, but `_surface_failure_tail` (`2835`) and `_scan_cell_health` (`2903`) also read `out`, and `out` is **deleted by the prune** at `2909`. The prune and the merge must stay in the same ordered unit.

3. **The WinError-433 retry becomes wrong-shaped.** Today it blocks one worker for ≤1.5 s. In a shared merge executor with a smaller pool, a drive blip serializes behind it. And the constraint that a merge must never be killed (constraint 4) means that executor cannot be a daemon-thread pool you abandon at exit — see row 20 above.

4. **`overwrite_collisions=True` becomes more dangerous, not less.** With merges decoupled from generation order, two cells that *should* be disjoint but are not (mixed `size`, hazard (a) in §4) will overwrite each other with less deterministic timing — a heisenbug instead of a reproducible one. If any of this is touched, I would flip the production call back to `overwrite_collisions=False` with an explicit "same cell re-merging" allowance keyed on the cell's own canonical rectangle, rather than a blanket bypass.

5. **`master_world_path()` race (hazard (b) in §4) gets a wider window**, because merges would no longer be interleaved with the arnis wall that currently spaces them out. Worth fixing first regardless: cache the resolved master path once per run in `_submit_cells` (`4618-4625`) instead of resolving it per merge at `2863`.

---

## Bottom line for the phase-2 decision

- Meld's merge is a **buffered file copy with no NBT, no compression, no fsync, and no global lock on the region path**. Measured `copy2` throughput here is ~3 GB/s warm; the ~141 MB a cs4 cell copies is **~46 ms**, against a 5.16 s arnis wall. On this arithmetic the merge copy is **under 1%** of the cell and offloading it buys approximately nothing.
- **Nothing in the codebase separates merge time from arnis time**, and the 21% idle worker-time in the headroom arithmetic is therefore **not attributed** — it could be the merge, the prune, the three global-lock JSON rewrites, the full log read, the gzip round-trip, the admission gate, or queue drain at the tail. **Measure before planning:** the one-line timer in §2 costs nothing and settles it.
- The parts of the post-arnis tail I would suspect **ahead of** the region copy: the `rmtree` prune (`2909`), `project._LOCK` serialization (2-3 whole-file JSON rewrites per cell, `project.py:15/403/411`), and `_scan_cell_health`'s untailed log read (`956`).
- Offloading the merge has an unusually large blast radius — **20 distinct consumers**, of which run-end (`3263`), auto-export (`3279`), the render-queue advance (`5466-5472`), and the Stop guarantee (`workers.py:192-206`) are correctness-critical, not cosmetic.

--- arnis-save-place ---
## 0. Framing corrections you need before anything else

Two things about the profile you handed me are not what the phase names suggest. Both change the plan.

**(a) `place` is not "element placement". It is placement *plus every serial tile merge*.**
The marker fires at `data_processing.rs:579` and the next marker (`merge`) does not fire until `data_processing.rs:938` — *after* the batch loop has finished, merges included. Inside that span the code alternates parallel placement (`data_processing.rs:669-859`) with a strictly single-threaded merge loop (`data_processing.rs:861-936`). arnis already times the two halves separately and reports them under `--benchmark` as `element_placement` and `tile_merge` (`data_processing.rs:939-940`). **Re-run the profile cell with `--benchmark` before planning: that one number splits `place` for you and removes the biggest guess in this report.**

**(b) `parse` is not `parse_osm_data`.** The `parse` marker is at `main.rs:494`; the next marker is `place`. Between them sits all of `generate_world_with_options`'s precompute: highway connectivity (`data_processing.rs:504`), `FloodFillCache::precompute` (`data_processing.rs:511`, which *is* rayon-parallel at `floodfill_cache.rs:326`), building/residential footprints, `road_bearings::set_from_elements`, `lowland::set_from_ground`, `collect_road_surface_coords`, rail/tunnel footprints, the bridge maps, the tree-pack disk load (`data_processing.rs:441-464`), `compute_big_water_field`, `compute_waterway_field`. That is why `parse` scales 2.1x instead of 1.0x. "parse is single-threaded" is true of `osm_parser::parse_osm_data` only, and that is a *subset* of the 2.68 s.

---

## 1. What runs in `place`

**Path taken.** `use_parallel_tiles = tiles.len() >= 3 && JavaAnvil` (`data_processing.rs:574`). Tiles come from `tile::create_tiles(&xzbbox, 512)` (`data_processing.rs:568`; `DEFAULT_TILE_SIZE = 512` at `tile.rs:41`), region-grid-aligned at `tile.rs:62-65`. **Tile == Minecraft region, 1:1.**

**The parallel unit is the TILE, not the element.** There is no `par_iter` anywhere in `src/element_processing/` — I grepped the whole directory; the only rayon in the sibling passes is `caves/carver.rs:59`, `caves/mod.rs:207,547`, `water_depth.rs:1024`, `floodfill_cache.rs:326`. Inside a tile the element loop is a plain `for &elem_idx in &tile_assignments[tile_idx]` at `data_processing.rs:701`, dispatching through `process_element` (`data_processing.rs:42`), which is a tag-match chain into `buildings::` / `highways::` / `landuse::` / etc. `generate_ground_region` (`ground_generation.rs:149`) is likewise a serial `for chunk_x { for chunk_z { for x { for z {` nest (`ground_generation.rs:226-291`).

**Per tile task (`data_processing.rs:673-858`), in order:**
1. `WorldEditor::new` on a halo-expanded bbox (`data_processing.rs:686`; halo `TILE_EDITOR_HALO = 64` at `tile.rs:51`).
2. Element loop → `process_element` (`:701-737`).
3. `generate_ground_region` over **strict** bounds only (`:743`).
4. `land_scatter::scatter_untagged_chunks` if `--land-texture` (`:762`).
5. `apply_deepslate_region` if `--fillground` (`:777`).
6. `caves::carve_region` if `--caves` (`:786`).
7. `water_depth::carve_lc_water_region` (`:798`).
8. `water_depth::sweep_floating_veg_region` (`:813`).
9. Under eviction only: rail/highway tunnel carve + `seal_floating_fluid_region` (`:826-843`).
10. `tile_editor.into_world()` → the tile's whole `WorldToModify` is returned by value (`:852`).

**Barrier structure — there are two, and they nest:**

- **Outer, per batch:** `for batch in indexed_tiles.chunks(tile_batch_size)` (`data_processing.rs:669`) with `tile_batch_size = rayon::current_num_threads().max(1)` (`data_processing.rs:592`). `batch.par_iter().map(...).collect()` (`:672-858`) is a **full join**: every thread must finish before *any* merge starts.
- **Inner, the merge:** `for (...) in batch_results` (`:864-935`) runs on the calling thread alone. It calls `editor.merge_world` (`:872` → `mod.rs:464` → `common.rs:910`), `merge_road_surface_overrides` (`:876/883`), and under eviction `flush_region_via` (`:910`). All rayon threads idle through this.

So the shape per batch is: **join → single-threaded merge → next batch**. With 36 tiles (see §3) and `tile_batch_size = 21`, that is exactly 2 batches: 21 tiles wide, then 15 — six threads idle for the whole second batch even before the merge.

Merge cost is not uniform. `common.rs:929-968` is an O(1)-per-chunk wholesale move for the tile's **own** region (`fully_authoritative` is true because auth bounds == the tile == the region exactly). The tile's 64-block halo lands in up to 8 *neighbour* regions, which take `fully_outside` → `merge_region_write_if_air` (`common.rs:1095`) → `merge_section_write_if_air` (`common.rs:1127`), a **4096-iteration per-section scan** on the merge thread.

---

## 2. What runs in `save`

Marker at `data_processing.rs:1184`, immediately before `editor.save()`.

`WorldEditor::save` (`mod.rs:1483`):
1. **`self.world.compact_sections()` (`mod.rs:1495` → `common.rs:1273`)** — single-threaded walk of every region → chunk → section, calling `try_compact` (`common.rs:222`) which scans up to 4096 entries per section. **Strictly serial prologue.**
2. `save_java` (`java.rs:123`).

`save_java`:
- `save_metadata`, plus a B_Linear scaffold cleanup (`java.rs:127-146`) — serial, trivial.
- Region filter: `min_region_x = xzbbox.min_x().div_euclid(512)` … `max_region_z` (`java.rs:154-157`).
- **`self.world.regions.par_iter().for_each(...)` (`java.rs:190-192`) — the parallel unit is the REGION.**
- Each region → `save_single_region` (`java.rs:236`) → `write_region_to_disk` (`java.rs:383`).

`write_region_to_disk` — the three costs, separated:

| Work | Where | Parallel? |
|---|---|---|
| **NBT construction** (palette build, index packing, biome compound, heightmaps) | `common.rs:347` `to_section`, called via `chunk_to_modify.sections()` at `java.rs:419`; `java.rs:1044` `create_chunk_nbt`; `java.rs:1157` `build_section_value`; `java.rs:1197` `compute_heightmaps` | **No — strictly sequential `for` over the region's chunks at `java.rs:409`, and again over the 1024-slot fill at `java.rs:456-479`** |
| **Lighting bake** (only with `--bake-lighting`) | `java.rs:900` `compute_lighting`, called from `java.rs:1075` | No — inside the same sequential chunk loop |
| **NBT serialisation** | `fastnbt::to_writer` at `java.rs:443` and `java.rs:114` | No |
| **Compression** | Anvil: `fastanvil` `Region::write_chunk`, zlib per chunk, `java.rs:337`. B_Linear: `zstd::bulk::compress` per 64-chunk bucket, `blinear.rs:233` | B_Linear only: `buckets.into_par_iter()` at `blinear.rs:163` — **nested rayon inside `save_java`'s `par_iter`** |
| **I/O** | Anvil: publishes per chunk into the `.mca` as it goes. B_Linear: whole region built in RAM (`blinear.rs:172`), one `write_all` to a temp + rename (`blinear.rs:122-138`) | Per region |

**`ARNIS_FLUSH_THREADS` does *not* control this path.** It is read only by `flush_thread_count()` (`mod.rs:1669-1680`, default `(cores/4).clamp(2,6)`), consumed only by `FlushWorker::spawn` (`mod.rs:1682`), which is only constructed at `data_processing.rs:618` **when `eviction_active`**. `eviction_active` requires `should_stream_to_disk` (`data_processing.rs:603-604` → `:275`), i.e. `ARNIS_STREAM_TO_DISK=1` or a RAM-pressure heuristic.

What the flush pool parallelises over: **whole regions**, taken off the merge thread via a bounded `sync_channel` (`mod.rs:1729`) with one shared `Mutex<Receiver>` (`mod.rs:1732`, lock held only across `recv`, never across a write — `mod.rs:1706-1710`). Each worker does `section.compact()` for the region (`mod.rs:1713-1717`) then `ctx.write` (`mod.rs:1719` → `java.rs:539` → the same `write_region_to_disk`).

**Consequence for phase attribution:** when Meld sets `ARNIS_STREAM_TO_DISK=1` (`server.py:2761-2762`: forced for `cell_size >= 8` or the `stream_to_disk` setting), most serialisation+compression moves **out of `save` and into the `place` span** (flushes happen at `data_processing.rs:910`, inside the merge loop), and `FlushWorker::finish()` (`data_processing.rs:1155`) blocks inside **`post`**. Your profile shows `save = 26.7%` and `post = 2.0%`, which is the *non*-eviction shape. **Confirm whether the profiled run had `ARNIS_STREAM_TO_DISK` set; the answer moves ~9 core-seconds between two phases.**

---

## 3. Why they stop scaling

**Neither is a lock. Both are bounded by a fixed unit count of 36, plus a serial residue.**

### The geometry — this is the load-bearing number

Meld: `cell_size = 1 # regions per axis` (`server.py:2621`), so cs4 = 4 regions = **2048 blocks**, region-aligned (`coords.py:165`, `blocks_per_job = size * REGION_BLOCKS`). Then `expand_bbox_for_seam(base_bbox, seam, ...)` at `server.py:2617` with `seam_buffer_chunks: 8` (`project.py:43`) adds **128 blocks on every side**, and `snap_bbox_to_global_grid` (`coords.py:183`) snaps to the *block* grid, not the region grid.

So arnis receives a bbox of **2304 blocks spanning `[-128, 2176)`** in cell coordinates.

- `create_tiles`: `aligned_min = -512`, `aligned_max = ((2176+512)>>9)<<9 = 2560` (`tile.rs:62-65`) → tiles at −512, 0, 512, 1024, 1536, 2048 → **6 per axis = 36 tiles**.
- `save_java` filter: `(-128).div_euclid(512) = -1`, `2176.div_euclid(512) = 4` → regions −1…4 → **6 per axis = 36 region files written**.
- This is not specific to cell (0,0): `min_x = rx*2048 − 128` always sits 128 below a 512 boundary, so **every cs4 cell is 36 tiles and 36 regions.**

### Two-point Amdahl fit (T=2 and T=21; 36 > 21, so `min(T,36) = T`)

**place:** `P/2 + M = 5.95`, `P/21 + M = 1.32`
`P(0.5 − 0.047619) = 4.63` → `P = 4.63 / 0.452381 =` **10.23 core-seconds parallel**, `M = 5.95 − 5.117 =` **0.83 s strictly serial**.
Check: `10.23/21 + 0.83 = 0.487 + 0.833 = 1.32` ✓
At T=21, **63% of `place` is the serial merge.**

**save:** `S/2 + C = 5.06`, `S/21 + C = 0.78`
`S = 4.28 / 0.452381 =` **9.46 core-seconds parallel**, `C = 5.06 − 4.73 =` **0.33 s serial**.
Check: `9.46/21 + 0.33 = 0.450 + 0.330 = 0.78` ✓

Cross-check against your own numbers: `(10.23+0.83) + (9.46+0.33) = 20.9` of the measured 27.70 cpu-s at T=2 → 75%, leaving 6.8 cpu-s for fetch+elevation+parse+post. Consistent.

This is a **two-point fit, not a measurement.** It is self-consistent and it predicts `tile_merge ≈ 0.83 s`, which `--benchmark` will confirm or refute in one run.

### Name the constructs

**place stops scaling because of three things, in order of size:**

1. **The single-threaded merge loop, `data_processing.rs:861-936`.** ~0.83 s per cell that does not move with thread count at all. Its candidate cost centres, which I can see but cannot rank without a profiler:
   - `merge_section_write_if_air` (`common.rs:1127`) — 4096-iteration scans for every halo section from every tile into its 8 neighbour regions.
   - `merge_road_surface_overrides` (`mod.rs:564`) — `extend` of a per-tile `FnvHashMap<(i32,i32), i32>` holding every rendered road cell in the tile, 36 times, on one thread.
   - Cross-thread deallocation: the tile's `WorldToModify` is allocated on a rayon worker (`data_processing.rs:686-852`) and dropped on the merge thread (`common.rs:917`, consumed by value). Every `Vec<u8>` of 4096 in a halo section is a remote free through mimalloc's thread-free list.
2. **Batch quantisation, `data_processing.rs:592` + `:669`.** 36 tiles / 21 threads = 2 batches of 21 and 15. The second batch runs 15-wide on a 21-thread pool. There is no reason for the batch structure other than memory capping (the comment at `data_processing.rs:557-559 / :585-590` says so explicitly) — and it costs both idle threads *and* peak RAM (§4).
3. **Tile-count ceiling.** At 36 tiles the cap is 36. It has headroom at 21, so it is not binding today — but it *is* binding at cs8-per-worker arithmetic and it is why the LPT sort exists (`data_processing.rs:635-641`).

**save stops scaling because of two things:**

1. **Region count = 36 is the hard cap** (`java.rs:190-192` iterates `self.world.regions`). Within one region, the chunk loop at `java.rs:409` and the base-fill loop at `java.rs:456` are strictly sequential. The critical path is *one region's full 1024-chunk serialisation*, and the dense-centre region is the longest. Efficiency achieved: ideal ratio for T=2→21 would be 21/2 = 10.5x, measured 6.5x → **62% of the ideal**, the gap being region-size imbalance plus the serial residue.
2. **`compact_sections` (`mod.rs:1495` → `common.rs:1273`) is a serial prologue** over the entire resident world before any region write starts. It is trivially parallelisable per region — and the eviction path *already* does it per-region on the flush threads (`mod.rs:1713-1717`), proving it is safe there.

**There is no lock in either path.** `WORLD_BOUNDS` (`common.rs:26`) is a Relaxed load off a never-written cacheline. `road_bearings::bearing_at` (`road_bearings.rs:126`) has a thread-local memo in front of its `RwLock`. The one real shared-cacheline read in an inner loop is `lowland::is_lowland` (`lowland.rs:75`), a global `RwLock` read **per farm plot block** with no memo, called from `field_texture.rs:610`. On a farmland-heavy cell with `--land-texture` that is 21 cores contending on one reader-count line. Bucharest centre is urban so it probably did not fire in your profile; **it will fire on rural cells.**

---

## 4. Where the 23% extra CPU at 21 threads comes from

`27.70 → 34.08 cpu-s` for the same work is +6.38 cpu-s. Same instruction count, more cycles. Four contributors, ranked by what the code actually shows:

**(1) Memory-bandwidth saturation in `compute_lighting` — the largest, if `--bake-lighting` is on.** Per chunk, `java.rs` allocates and zeroes:
- `java.rs:914` `opacity = vec![0u8; height*256]`
- `java.rs:915` `emission = vec![0u8; height*256]`
- `java.rs:946` `sky = vec![0u8; height*256]`
- `java.rs:978` `block = vec![0u8; height*256]`

`height = 24 sections × 16 = 384`, so each is `384 × 256 = 98,304` bytes → **393,216 bytes zeroed per chunk**, plus 48 × 2048-byte Vecs at `java.rs:994-995` (98 KB more).
Per cell: `36,864 chunks × 393,216 B = 14.5 GB` of memset, plus `36,864 × 98,304 B = 3.6 GB` → **~18 GB of allocate-and-zero inside `save` alone**. That is a pure DRAM-bandwidth workload. At 2 threads it fits in the bandwidth budget; at 21 it does not, so cycles-per-instruction rises and `GetProcessTimes` bills the stall as CPU time. **This is the mechanism by which "same work costs 23% more CPU" happens with no redundant work at all.**

**(2) Peak-RAM-driven page-fault and TLB pressure from batch quantisation.** `tile_batch_size = rayon::current_num_threads()` (`data_processing.rs:592`) means the number of simultaneously resident tile `WorldToModify` structs *is* the thread count. Measured RSS 1074 MB → 2495 MB across T=2 → T=21 is +1421 MB over 19 extra resident tile editors = **~75 MB per tile editor**, exactly what the design comment at `data_processing.rs:585-590` predicts. Every one of those megabytes is first-touched (page-faulted) by the placing thread and freed by the merge thread.

**(3) Visible allocation churn — fixed per chunk, not thread-scaled, but large:**
- `java.rs:1092` `biome_value.clone()` — deep-clones a `Value::Compound` (HashMap + `Vec<Value>` + Strings + LongArray) **once per section**, i.e. 24× per chunk → `36,864 × 24 = 884,736` clones per cell.
- `java.rs:1085` `get_air_block_states().clone()` — one HashMap + Vec + Compound + String per *empty* section. With `--fillground` most chunks have ~18 air sections of 24 → ~660k per cell.
- `java.rs:1139` `get_structures_value().clone()` — 36,864 per cell.
- `java.rs:412` `chunk_to_modify.other.clone()` then `dedup_compound_list` (`java.rs:607`) — clones every block-entity/entity list per chunk before deduping it.
- `common.rs:411` and `common.rs:501` `format!("{}:{}", block.namespace(), block.name())` — a fresh `String` **per palette entry per section**. `namespace()` is the constant `"minecraft"` (`block_definitions.rs:118`). `884,736` sections × ~5-30 palette entries = **millions of `format!` calls per cell** producing one of at most 450 distinct strings.
- `common.rs:512-514` `block.properties()` — `block_definitions.rs:569` builds a fresh `HashMap` + two `String`s per palette entry, again for one of a handful of constant compounds.
- `common.rs:376` `let mut block_to_palette = [u16::MAX; 512]` — a 1 KB stack array initialised per section (`BLOCK_ID_CEILING = 512`, `block_definitions.rs:70`). 884,736 × 1 KB = 884 MB of stack writes per cell.

**(4) Rayon nesting.** `blinear.rs:163` `buckets.into_par_iter()` runs inside `java.rs:192`'s `par_iter`. Work-stealing across the nest means a stolen bucket task can hold a region's full raw NBT image alive on another thread's stack, inflating live memory during `save`. Task overhead itself is negligible at these counts; the memory effect is not.

**What I cannot tell you from static reading:** the *split* of the 6.38 cpu-s between (1), (2), (3), and (4). That needs a sampling profiler on one cell at T=2 and T=21. Anything I gave you as a ranking above is reasoning from allocation sites, not measurement.

---

## 5. Redundant or cacheable work that can simply be deleted

Ordered by measured/derivable cpu-seconds, biggest first.

### 5.1 **arnis writes 36 region files per cs4 cell and Meld deletes 20 of them.** ★ biggest single win

`merge.py:157`: `if not (rx_min <= frx <= rx_max and rz_min <= frz <= rz_max): result["regions_skipped"] += 1; continue  # seam-buffer region — discard`.

Canonical bounds are exactly `size × size` regions (`coords.py:112-115`) = **16**. arnis writes **36** (§3). Chunk arithmetic for the profiled geometry:

- content chunks (bbox `[-128, 2176)` → chunk indices −8…135) = 144² = **20,736**
- chunk slots written (regions −1…4 → chunk indices −32…159) = 192² = **36,864**
- base/empty chunks, filled by the second pass at `java.rs:456-479` = 36,864 − 20,736 = **16,128**
- kept by merge (regions 0…3 → chunks 0…127) = **16,384**, all of them content
- **discarded = 20,480 chunk writes = 55.6% of every chunk arnis serialises, compresses and writes**

Weighting a base chunk at roughly half a content chunk (it still pays the full 24-section NBT and the full lighting bake, just with a tiny palette): discarded share ≈ `(4,352 + 16,128×0.5) / (16,384 + 4,352 + 16,128×0.5) = 12,416 / 28,800 =` **~43% of `save`**, i.e. **~4.1 of the 9.46 core-seconds per cell, ~330 core-seconds across the 81-cell run — 15% of the whole 2,244 cpu-s budget.**

The seam expansion is *needed* for placement and ground continuity (arnis clips at the bbox). What is not needed is *writing region files that will be deleted*. The fix is a caller-supplied canonical rectangle narrowing the filter at `java.rs:154-157`. Region serialisation is independent per region (`write_region_to_disk` is free-standing and takes no cross-region state — `java.rs:383`), so the kept regions are byte-identical.

Two things to verify before committing: (i) merge.py's drift guard (`merge.py:170-186`) uses `min/max` of generated region coords against `rx_min ± buffer_regions` where `buffer_regions = ceil(8/32)+1 = 2` — writing exactly the canonical set gives `min(rxs) = rx_min`, which passes, but confirm on a real merge; (ii) Meld's missing-region scan must not read the absent ring as a hole to regenerate.

For cs8 the same arithmetic gives 10×10 = 100 regions written, 64 kept → 36% discarded.

### 5.2 **Half of the lighting bake is computed and thrown away.** ★ pure deletion

`java.rs:1001-1002`, inside `for s in 0..num_sections { for ly { for z { for x {`:
```
pack_light_nibble(&mut sl, local, sky[g]);
pack_light_nibble(&mut bl, local, block[g]);
```
Then at `java.rs:1104`: `if block_light.iter().any(|&b| b != 0)` — the BlockLight array is **dropped whenever it is all-zero**, which is most chunks (terrain, fields, water, roads, unlit shells; the comment at `java.rs:970-977` says exactly this).

And `any_emitter` is **already known** before the packing loop — it is computed at `java.rs:936` during section decode and used at `java.rs:979` to skip the flood fill. The packing loop just doesn't consult it.

Volume: `36,864 chunks × 24 sections × 4096 = 3.62e9` wasted `pack_light_nibble` read-modify-writes per cell, plus 884,736 wasted `vec![0i8; 2048]` allocations (`java.rs:995`). Total nibble packs across both arrays: `7.25e9` per cell. Gating `bl` on `any_emitter` deletes half of that, output-identical by construction (the array being dropped is the one the branch already proves is all zero).

Two further free wins in the same function: `sky` above `top` is uniformly 15 (`java.rs:947` `sky[top*256..].fill(15)`), so every section entirely above terrain can be a `vec![0x77i8; 2048]` memset instead of 4096 nibble packs; and `decode_section_light_props` (`java.rs:795`) calls `light_opacity(&p.name)` / `block_light_emission(&p.name, ...)` — **string comparison on the block name** (`java.rs:671`, `java.rs:637`) — per palette entry per section, when the caller has the numeric `Block` id available upstream.

**Load-bearing unknown:** all of §5.2 evaporates if the profiled run did not pass `--bake-lighting` (`args.rs:453`, `default_value_t = false`). Meld defaults it **on** (`arnis_cmd.py:405-406`, `project.py:95`). Confirm from the actual command line of the profiled run before sizing this.

### 5.3 **Bilinear elevation interpolation recomputed per block write.** ★ biggest `place` win

`WorldEditor::set_block` (`mod.rs:1169`) → `get_absolute_y` (`mod.rs:478`) → `get_ground_level` (`mod.rs:495`) → `Ground::level` (`ground.rs:614`) → `get_data_coordinates` (`ground.rs:686`, two f64 divisions and two clamps) + `interpolate_height` (`ground.rs:694`).

`interpolate_height` reads `data.heights[z0][x0]`, `[z0][x1]`, `[z1][x0]`, `[z1][x1]` — and `heights` is **`Vec<Vec<f32>>`** (`elevation/mod.rs:29`), a jagged nested Vec. That is **two dependent pointer chases per sample, four samples, two distinct rows** — plus floor/round and ~10 float ops, **on every relative-coordinate block write**.

`ground_generation.rs` already avoids this: it is 100% absolute-coordinate calls (53 `*_absolute` sites, 0 relative) and additionally builds a per-chunk `ChunkGroundCache` (`ground_generation.rs:242-252`). **The element passes do not.** `landuse.rs` has 110 relative `set_block`/`fill_blocks` call sites and `natural.rs` has 77 — the land-cover and vegetation passes, which cover the whole cell.

Two output-identical fixes, either or both:
- Flatten `heights` to a single `Vec<f32>` with a stride. Halves the pointer chases; touches no arithmetic.
- Memoise ground-Y per column for the tile. A tile is 640×640 with halo = 409,600 columns × `i32` = 1.6 MB per tile editor (`i16` would be 0.8 MB). Values are identical by construction, so golden hashes cannot move.

Compounding this: `get_ground_level` also does `road_surface_overrides.get(&(x,z))` (`mod.rs:496-499`) on every call **once the highway pass has populated the map**. The `is_empty()` guard the comment at `mod.rs:487-493` describes only helps *before* highways run; after that every subsequent relative write in the tile pays a hash probe. A column memo subsumes this too.

### 5.4 Cheaper, still real

- **Block name and property tables.** `common.rs:411` / `common.rs:501` `format!` and `common.rs:512` `block.properties()` reconstruct one of ≤450 constant strings and one of ~20 constant compounds, millions of times per cell. A `LazyLock<[&'static str; 512]>` for `"minecraft:"+name` and a `LazyLock<[Option<Arc<Value>>; 512]>` for properties deletes the formatting outright. Getting the *allocation* out too requires changing `PaletteItem::name: String` (`common.rs:129`) to a `Cow<'static, str>`, which touches the serde derive — check `fastnbt` accepts it before planning on it.
- **`compact_sections` should be per-region.** `mod.rs:1495` is a serial full-world walk; the eviction path already proves per-region compaction is correct (`mod.rs:1713-1717`). Folding it into `save_single_region` (`java.rs:236`) removes the serial prologue `C ≈ 0.33 s` from `save` and costs nothing.
- **`lowland::is_lowland` needs the memo `road_bearings` already has.** `lowland.rs:75` takes a global `RwLock` read per farm-plot block (`field_texture.rs:610`) with no cache; `road_bearings.rs:119-126` shows the exact pattern to copy. Only matters on `--land-texture` cells with real farmland.
- **`biome_value` per-section clone.** `java.rs:1092` clones the same compound 24× per chunk. `Value` has no `Arc` variant so this is not a one-liner, but the biome compound is identical for every section of a chunk by construction (`biome.rs:97-100` samples a 4×4 xz grid and `pack_biome_indices` repeats it across y — `biome.rs:190`), so 23 of 24 clones are provably redundant.

### 5.5 Things I looked at and found are **not** wins

- **Base-chunk lighting recomputation.** Every one of the 16,128 empty chunks recomputes an *identical* `compute_lighting` over the cached `get_base_chunk_sections()` (`java.rs:68`). Caching it would be a large win — except **all 16,128 of them live in the ring regions that §5.1 deletes entirely.** In the canonical 16 regions the bbox covers every chunk, so `region_to_modify.chunks.len() < 1024` at `java.rs:456` is false and the second pass never runs. Do §5.1 and this disappears; do this instead of §5.1 and you have fixed a symptom.
- **Structure schematics.** `src/structures/*.rs` are `include_bytes!` + `OnceLock` (e.g. `boat.rs:9,19`; `bushes.rs:12`), lazily parsed once per process on first use. Fine as-is.
- **The tree pack, however, is a real per-process disk cost.** `RegionLibrary::load` (`region.rs:185`) → `load_pack` (`region.rs:121`) does a serial `std::fs::read` + gzip-NBT `load_schem` for **every** schematic in the manifest, once per arnis process, i.e. **81 times per run**. It sits inside the `parse` span (`data_processing.rs:441-464`). I do not know its magnitude — measure it with `ARNIS_PHASE_MARKERS` plus a temporary marker, or by timing a `--tree-pack` vs no-pack run. It is parallelisable (`par_iter` over the file list) and cacheable across cells only via a shared preparsed on-disk cache, which is a bigger change.
- **The GPU.** `caves/gpu.rs` behind `--gpu`, cave density only, `GPU_BUSY_MS` read at `data_processing.rs:1172`. Nothing in `place` or `save` touches it. Confirmed, as your ground truth states.

### 5.6 Unquantified, flagged rather than guessed

`assign_elements_to_tiles` (`tile.rs:178`) assigns area, relation and linear elements to **every** tile whose halo-expanded bounds they overlap (`tile.rs:207-265`). A road crossing 6 tiles is rasterised 6 times; the halo merge then discards 5 of those results at `common.rs:1127`. This is genuine duplicated CPU in `place` and it is *constant* in thread count (tile count is fixed at 36), so it does not appear in the T=2→T=21 delta — but it is real cpu-seconds. **I cannot size it from static reading.** Counting `sum(len(tile_assignments)) / elements.len()` at `data_processing.rs:598` costs one line and gives you the duplication factor exactly.

---

## Files

- `C:\Users\LEGION\Documents\Meld\arnis-triagefix\src\data_processing.rs`
- `C:\Users\LEGION\Documents\Meld\arnis-triagefix\src\world_editor\mod.rs`
- `C:\Users\LEGION\Documents\Meld\arnis-triagefix\src\world_editor\java.rs`
- `C:\Users\LEGION\Documents\Meld\arnis-triagefix\src\world_editor\common.rs`
- `C:\Users\LEGION\Documents\Meld\arnis-triagefix\src\world_editor\blinear.rs`
- `C:\Users\LEGION\Documents\Meld\arnis-triagefix\src\tile.rs`
- `C:\Users\LEGION\Documents\Meld\arnis-triagefix\src\ground.rs`
- `C:\Users\LEGION\Documents\Meld\arnis-triagefix\src\elevation\mod.rs`
- `C:\Users\LEGION\Documents\Meld\arnis-triagefix\src\block_definitions.rs`
- `C:\Users\LEGION\Documents\Meld\arnis-triagefix\src\biome.rs`
- `C:\Users\LEGION\Documents\Meld\arnis-triagefix\src\lowland.rs`
- `C:\Users\LEGION\Documents\Meld\meld-triagefix\server.py`
- `C:\Users\LEGION\Documents\Meld\meld-triagefix\src\merge.py`
- `C:\Users\LEGION\Documents\Meld\meld-triagefix\src\coords.py`

No files were modified.

--- gpu-surface ---
# GPU offload assessment — arnis `perf/speed-to-worldgen-phase2`

Read from `C:\Users\LEGION\Documents\Meld\arnis-triagefix` (branch confirmed `perf/speed-to-worldgen-phase2`, HEAD `a1143f70`). Read-only; nothing modified.

---

## 1. The existing GPU cave path, end to end

### Entry and gating

- `--gpu` defaults to `"off"` — `src/args.rs:92-93`. `--caves` defaults to `false` — `src/args.rs:173-174`.
- The selector is read per-run at `src/caves/mod.rs:150`: `ARNIS_GPU` env overrides `args.gpu`.
- **Consequence for the current baseline: in the measured Bucharest runs (no `--caves`, no `--gpu`) the GPU path is not merely idle, it is never constructed.** `carver_for` returns `None` at `src/caves/gpu.rs:391-392` before touching wgpu at all. There is no existing GPU contribution to subtract from or build on in the 161 s number.

### Device / adapter selection — `src/caves/gpu.rs:112-149`

- `wgpu::Instance::new(default)` then **explicit** `instance.enumerate_adapters(Backends::all())` (`gpu.rs:113-115`). `PowerPreference` is deliberately not used; the module doc (`gpu.rs:23-27`) records why: on this laptop `HighPerformance` picked the iGPU over the RTX 5080.
- Filter by selector (`auto`/`dgpu`/`igpu`/name-substring) at `gpu.rs:119-129`, then rank `class*10 + backend` where Discrete=3, Integrated=2, Vulkan=2, Dx12=1 (`gpu.rs:130-143`).
- Backends actually compiled: `Cargo.toml:73` is `wgpu = { version = "23", default-features = false, features = ["wgsl", "wgc", "metal"] }`. I verified the claim in the comment: `wgpu-23.0.1/Cargo.toml:163-165` attaches `wgc` with `features = ["vulkan"]` unconditionally for `cfg(windows)`, and `dx12` is a separate opt-in feature (`wgpu-23.0.1/Cargo.toml:104`) that is **not** enabled. So on Windows this build is **Vulkan-only**. The comment at `Cargo.toml:68-73` is accurate.
- Device request is blocking via `pollster::block_on` (`gpu.rs:147-149`) with `DeviceDescriptor::default()` — i.e. **no optional features requested**, no limits raised.

### Static upload (once per process) — `gpu.rs:84-109, 156-168`

`flatten_noises` collapses 20 `NormalNoise` objects into four flat storage buffers:
- `perms` — one 256-entry permutation per octave, widened to `u32`
- `octs` — `vec4<f32>` of `(xo, yo, zo, input_factor)`
- `amps` — `value_amp` per octave
- `ranges` — `vec4<u32>` per noise: `(first_octave, count, value_factor_bits, pad)`

Octave count per noise comes from the amplitude tables at `src/caves/density.rs:48-67`. Total buffer size is a few hundred KB. `NOISE_COUNT = 20` (`gpu.rs:43`) must stay in lockstep with the `NOISE_*` constants in the shader (`gpu_carve.wgsl:42-61`).

### Per-dispatch buffers — `gpu.rs:277-311`

| buffer | binding | direction | size formula |
|---|---|---|---|
| `params` (uniform, `#[repr(C)]` bytemuck Pod, `gpu.rs:63-82`) | 0 | up | 64 B |
| `perms`/`octs`/`amps`/`ranges` | 1-4 | up, once | ~few hundred KB, process-lifetime |
| `surf` | 5 | up | `w*h*4` |
| `corner_vals` | 6 | **device-only, never read back** | `nx*nz*ny*4` (`gpu.rs:292`) |
| `mask` (atomic u32) | 7 | down | `ceil(w*h*nyb/32)*4` (`gpu.rs:299`) |

Bind group layout is hard-coded: 8 entries, binding 0 uniform, bindings 1-4 read-only storage, 5-7 read-write, via `(1..=4).contains(&i)` at `gpu.rs:170-187`.

### Dispatch — `gpu.rs:352-364`

One encoder, one compute pass, two pipelines back to back:
1. `corners` (`@workgroup_size(64)`, `gpu_carve.wgsl:282-295`): one thread per lattice corner, `(nx*nz*ny).div_ceil(64)` groups. Evaluates the full `combined_density` at `(cx*4, cy*8, cz*4)`.
2. `blocks` (`@workgroup_size(256)`, `gpu_carve.wgsl:302-353`): dispatch is 2-D — `((w*h).div_ceil(256), nyb, 1)`. Per thread: surface gate (`by > surf - top_gate` → return, `wgsl:313-314`), trilerp the 8 corners, short-circuit `noodle_density` only when `combined > 0` (`wgsl:345-348`), then `atomicOr` one bit into the mask.

No barriers between the two — they are ordered by pipeline switch inside one pass, which is legal because wgpu inserts the memory barrier at the dispatch boundary.

### Readback — `gpu.rs:363-378`

`copy_buffer_to_buffer(mask → read_buf)`, `queue.submit`, then the standard blocking idiom: `slice.map_async` with an `mpsc` channel, `device.poll(wgpu::Maintain::Wait)`, `rx.recv()`, `bytemuck::cast_slice(...).to_vec()`, `unmap()`. Wall time is accumulated into `GPU_BUSY_MS` (`gpu.rs:39, 377`), which Meld reads from the report line (`src/data_processing.rs:1178`, `src/main.rs:730`) to budget workers against a GPU target — the process reports its own usage because Meld cannot observe an adapter from outside.

### CPU fallback

Three independent fallbacks, all silent-and-correct:
1. Selector off/empty → `None` (`gpu.rs:391-392`).
2. Device creation failure → `eprintln` **once** (behind `OnceLock`, `gpu.rs:382, 399-402`).
3. Dispatch `Err` → `eprintln` and `None` (`src/caves/mod.rs:189-192`).

The fallback is the `else` arm at `src/caves/mod.rs:206-274` — a rayon `par_iter` over `(cx, cz)` cell columns with the vertical corner-carry optimisation (`mod.rs:216-232`) that halves density evaluations. Note this is *not* a naive reference: the CPU path is already the optimised one, and the corner carry has no GPU equivalent in the current shader (each corner thread recomputes independently).

There is a further subtlety in `carver_for` (`gpu.rs:405-410`): the singleton is keyed to one seed, and a second seed in one process falls back to CPU rather than silently using stale noise tables.

### Measured benefit

**The code comments contain no speedup number.** The only measured figures in the source are:
- Init: ~130 ms iGPU, ~790 ms waking the dGPU (`gpu.rs:387-388`).
- Parity contract: >99.9% block agreement, explicitly not bit-pinned (`gpu.rs:16-21`, test at `gpu.rs:417-421, 533-536`).

The speedup numbers live in `docs/PHASE2-GPU-MEASURED.md`:

| | measured |
|---|---|
| 1:1 cell wall | **1.18× (dGPU), 1.10× (iGPU)** |
| 1:1 fleet, core-s/cell | **1.37×** (852 → 623) |
| kernel parity vs CPU | **0.0005%** (11 of 2.26 M) |
| full-render diff vs CPU | **0.0005%** (905 of 176 M) |
| dGPU vs iGPU vendor drift | **1 block / 176 M** |
| GPU memory | corner+mask ≈ 10 MB/region in flight |

Same doc, "Finding 2": *"GPU speed is not the constraint — the offloadable share is."* iGPU measured at 5.0-5.3 × 10⁹ octave-evals/s vs 45.2 × 10⁶ on one CPU core and 1.09 × 10⁹ on all 24 — i.e. **the iGPU alone beats the entire CPU by 4.7-4.9× on this kernel shape**.

Same doc, "Finding 1", and this is load-bearing for everything below: **`enumerate_adapters(Backends::all())` returns only Intel adapters plus the software rasteriser — the RTX 5080 is invisible to graphics APIs on this machine.** Two documented opt-ins (`HKCU\...\UserGpuPreferences`, `NvOptimusEnablement` export) were tried and failed. It is a MUX/hybrid-graphics state, fixable from Windows settings, not from code. Every number that matters here is therefore an **iGPU** number.

### What a SECOND kernel reuses vs duplicates

**Reusable as-is, but currently private to `caves`:**
- Adapter enumeration + selector semantics + ranking — `gpu.rs:112-144`
- Blocking device request — `gpu.rs:147-149`
- Process-wide `OnceLock<Option<..>>` singleton with log-once failure — `gpu.rs:382-411`
- The readback idiom — `gpu.rs:363-378`
- `GPU_BUSY_MS` accounting and its Meld consumers — `gpu.rs:39, 377`; `data_processing.rs:1178`; `main.rs:730`
- The bytemuck `#[repr(C)] Pod` uniform-struct pattern — `gpu.rs:63-82`

**Must be duplicated or refactored — five concrete blockers:**
1. `device` and `queue` are private fields of `GpuCarver` (`gpu.rs:48-49`) with no accessor. A second kernel either creates a **second logical device on the same adapter** (double the ~130 ms init, separate memory pools, separate submission queue) or the struct must be split.
2. The bind group layout is hard-coded to 8 bindings with a fixed read-only mask (`gpu.rs:170-187`). Not general; a second kernel needs its own `BindGroupLayout` and `PipelineLayout`.
3. `carver_for` takes `&CaveGen` (`gpu.rs:389`) — the singleton constructor is coupled to cave state and cannot be called before a `CaveGen` exists.
4. The seed guard at `gpu.rs:408` (`c.seed == seed`) is cave-specific and would wrongly gate a kernel with no seed.
5. The shader is one `include_str!` module with two entry points (`gpu.rs:45, 207-208`).

**The minimum structural prerequisite** is extracting a `GpuContext { instance, adapter, device, queue, adapter_name }` built once per process from the `--gpu` selector, with `GPU_BUSY_MS` and a generic `dispatch_and_read(layout, entries, pipeline, groups, readback_buf)` helper; `GpuCarver` then borrows `&'static GpuContext`. Roughly a 150-line refactor of `gpu.rs` with no behaviour change, provable by the existing parity test.

---

## 2. What the `elevation` phase actually computes

`meld_telemetry::phase("elevation")` at `src/main.rs:489` wraps exactly one call: `ground::generate_ground_data(&args)` (`src/ground.rs:886`). That expands to `Ground::new_enabled` (`ground.rs:199-303`), which is **land cover fetch first, then elevation** (`ground.rs:218-247`), so the phase is bigger than its name.

Full ordered inventory, with GPU-friendliness verdict:

| # | step | file:line | shape | GPU? |
|---|---|---|---|---|
| 1 | ESA COG fetch + `sample_grid` | `land_cover.rs:210` | disk/net + tile decode | no (I/O) |
| 2 | `fill_gaps` | `land_cover.rs:242` | nearest-neighbour sweep | marginal |
| 3 | `smooth_class_boundaries` | `land_cover.rs:1322-1440` | 13×13 Gaussian-weighted class vote, **boundary cells only** (`:1367-1382`), 169 taps each | **hostile** — output is `argmax` over a sparse 256-slot vote table with per-row scratch reuse (`:1354-1356, 1387-1390`); highly divergent, and the "seen" list trick is inherently serial per cell |
| 4 | `compute_water_distance` | `land_cover.rs:1237-1290` | multi-source FIFO BFS | **hostile** (see traps) |
| 5 | `compute_water_blend_smooth` σ=3 | `land_cover.rs:108-131` → `postprocess.rs:931` | separable Gaussian, 19 taps | friendly but small |
| 6 | provider `fetch_raw` | `elevation/mod.rs:179` | see below | mixed |
| 7 | `filter_elevation_outliers` | `postprocess.rs:1122-1199` | global IQR + per-row NaN-out | **skipped entirely in Meld tiled mode** (`elevation/mod.rs:265-267`) |
| 8 | `repair_terrain_anomalies` | `postprocess.rs:22-125` | 5×5 median + MAD, up to 10 passes, early break | friendly-looking; small (see traps) |
| 9 | `fill_nan_values` | `postprocess.rs:1066-1113` | 3×3 mean, iterate to fixpoint | friendly, but data-dependent pass count needs a host readback per iteration |
| 10 | `level_water_surfaces` | `postprocess.rs:253-451` | 4-connected component labelling + per-component histogram mode + per-cell 25×25 local median | **hostile**, and **fully serial today** — no rayon anywhere in it |
| 11 | `reclassify_non_surface_water_cells` | `postprocess.rs:665` | nearest non-water search | hostile |
| 12 | **`smooth_built_up_gaussian`** | `postprocess.rs:837-926` → `gaussian_blur_grid_reported` `postprocess.rs:941-1043` | **two separable Gaussian blurs at σ = 30 m / m-per-cell** | **the one genuinely GPU-shaped kernel** |
| 13 | `pull_coastal_land_toward_water` | `postprocess.rs:733-836` | BFS carrying a value | **hostile** (see traps) |
| 14 | `scale_to_minecraft` | `postprocess.rs:1205-1313` | global min/max reduce + per-cell affine + clamp | friendly, but trivially cheap |
| 15 | f64 → f32 downcast | `elevation/mod.rs:353-356` | pure map | trivial |

### Step 6 broken out — tile decode, reprojection, interpolation

The default global provider is Mapterhorn (`elevation/mod.rs:187`). For the measured cell:

- `TILE_PX = 512` (`mapterhorn.rs:18`), terrarium WebP.
- Decode: `decode_terrarium(p) = p[0]*256 + p[1] + p[2]/256 - 32768` (`mapterhorn.rs:619-621`).
- **Tile decode is SERIAL**: `load_chunk_tiles` calls `image::open` in a plain nested `for` loop (`mapterhorn.rs:722-741`). No rayon. This is a genuine CPU parallelisation gap, not a GPU one.
- Reprojection: `norm_x = (lng + 180)/360`, `norm_y = (1 - asinh(tan(lat)))/2` (`mapterhorn.rs:628-630`). Web-Mercator forward only. Hoisted per row/column.
- Interpolation: **bilinear, pyramid top-down** — `sample_height` (`mapterhorn.rs:747-790`) walks zoom levels from `top_zoom` down to `floor`, does a hashmap `contains_key` per level, and returns the first level where the NaN-renormalised blend is finite. That per-cell hashmap-lookup-in-a-loop is the GPU-hostile part; the bilinear arithmetic itself is trivially GPU-friendly.
- The regional/fixed-tile path (`fixed_tile.rs:595-635`) is cleaner: a flat `into_par_iter` over rows with `tile_x`/`tile_y` hoisted per column/row (`fixed_tile.rs:582-592`) and a tile-reference carry so most cells skip the hashmap.

**There is no per-column height solve inside the elevation phase.** The only per-column solve is `Ground::interpolate_height` (`ground.rs:694-718`) called from `Ground::level` (`ground.rs:614-622`), and that runs during placement/ground generation, not here.

### Sizing the phase from the measured numbers

Bucharest, `cell_size = 4` → 4 Minecraft regions per side = **2048 blocks** per cell (Meld calls it `job_size_regions`, `meld-triagefix/src/runreport.py:145`). Cross-check: `ceil(16400 / 2048) = 9`, `9² = 81` cells ✓, and `ceil(16400 / 4096) = 5`, `5² = 25` cells at cs8 ✓.

Grid dims at scale 1.0 (`elevation/mod.rs:114-120`): `world_width = 2048 + 1 = 2049`, and `2049 < MAX_ELEVATION_GRID_DIM = 16384` (`elevation/mod.rs:78`), so **grid = 2049 × 2049 = 4,198,401 cells**.

The σ, from `elevation/mod.rs:298-309`:
```
BUILT_UP_SIGMA_M = 30.0
m_per_cell = (2048/2049 + 2048/2049) * 0.5 = 0.99951
built_up_sigma_cells = 30.0 / 0.99951 = 30.015
```
Kernel size (`postprocess.rs:946`): `ceil(30.015 * 3) * 2 + 1 = 91*2 + 1 = 183 taps`.

`smooth_built_up_gaussian` calls `gaussian_blur_grid_reported` **twice** — once on the binary built-up mask (`postprocess.rs:875`), once on the NaN-masked heights (`postprocess.rs:896`). Each blur is 2 separable passes.

```
taps = 4,198,401 cells × 183 taps × 2 passes × 2 blurs
     = 4,198,401 × 183 = 768.3 M per pass
     × 2 passes        = 1.537 G per blur
     × 2 blurs         = 3.073 G taps per cell
```
Each tap (`postprocess.rs:975-984`): bounds check, `is_finite` branch, `sum += v*k`, `wsum += k`. The branch defeats auto-vectorisation.

**Amdahl decomposition of the supplied measurement.** Model `T(n) = S + P/n`:
```
3.82 = S + P/2
0.57 = S + P/21
3.25 = P(1/2 - 1/21) = P × 0.45238   →   P = 7.185 core-s
S = 3.82 - 7.185/2 = 0.227 s
```
So the elevation phase is **~7.19 core-seconds of parallel work + ~0.23 s irreducibly serial** per cell. Its total CPU (7.4 core-s) is **26.7% of the cell's 27.7 cpu-s** — larger than its 20.1% wall share.

At 1.0-1.5 ns per tap per core (branchy scalar loop, one cache-friendly stream in the horizontal pass, strided in the vertical), 3.073 G taps ⇒ **3.1-4.6 core-seconds**, i.e. **43-64% of the elevation phase's entire parallel pool**. Every other item on the list is at least an order of magnitude smaller. I have not isolated this with a profiler — see "what to measure first" below — but the op-count arithmetic and the Amdahl fit agree, which is the strongest evidence available without running the binary.

**The ~0.23 s serial residue is identifiable** and worth naming, because it caps any parallel win:
- `level_water_surfaces` (`postprocess.rs:297-429`) — fully serial, no rayon.
- `load_chunk_tiles` WebP decode (`mapterhorn.rs:722-741`) — serial `for` loop.
- The vertical-pass **scatter** in the blur (`postprocess.rs:1034-1038`) — `out[y][x] = v` on the calling thread, outside the rayon region: 4.198 M f64 writes per blur, each into a different `Vec<f64>` row allocation. 8.4 M scattered writes per cell across the two blurs.

### Dense-uniform vs branchy-irregular, summarised

**Dense uniform grid math (GPU-friendly):** the two σ=30 separable Gaussians (`postprocess.rs:966-1041`); the σ=3 water-blend Gaussian (`land_cover.rs:127`); the bilinear resample arithmetic proper (`fixed_tile.rs:257-286`, `mapterhorn.rs:760-784`); the 5×5 median/MAD stencil (`postprocess.rs:66-104`); the affine scale + clamp (`postprocess.rs:1289-1305`); the terrarium decode (`mapterhorn.rs:619-621`); the f64→f32 downcast.

**Branchy / irregular (GPU-hostile):** connected-component labelling and per-component reductions (`postprocess.rs:297-429`); both multi-source BFS distance-and-value propagations (`land_cover.rs:1270-1287`, `postprocess.rs:756-768`, `postprocess.rs:801-818`); the class-vote argmax with sparse scratch (`land_cover.rs:1392-1425`); the pyramid-walk hashmap lookup inside `sample_height` (`mapterhorn.rs:754-758`); every data-dependent iteration-count loop (`postprocess.rs:44-115`, `postprocess.rs:1074-1112`).

---

## 3. Determinism: would an f32 GPU elevation path change block output?

**Short answer: yes, and unlike the cave field the elevation pipeline has amplifiers — but the specific kernel worth porting sits at the one point in the pipeline where the amplifiers are behind it.**

### f64 is not available in WGSL as used here

I checked the stack rather than asserting from memory:
- naga's WGSL frontend **does** accept `f64` — `naga-23.1.0/src/front/wgsl/parse/conv.rs:122-125` maps `"f64"` to `Scalar { Float, width: 8 }`.
- It is gated on `Capabilities::FLOAT64` — `naga-23.1.0/src/valid/mod.rs:86`.
- wgpu exposes it as `Features::SHADER_F64` — `wgpu-types-23.0.0/src/lib.rs:857`.
- **But** `GpuCarver::create` requests `DeviceDescriptor::default()` (`gpu.rs:148`), i.e. no optional features, so today's device has it off.

**I do not know whether this machine's Intel adapter exposes `SHADER_F64` on Vulkan, and I will not guess** — it requires enumerating `adapter.features()` at runtime. Intel's Windows Vulkan driver commonly reports `shaderFloat64 = false` on Xe-class integrated parts, and where it is reported the throughput is a small fraction of f32. Even if it were available, bit-identity is not guaranteed: reproducing `postprocess.rs:975-984`'s left-to-right accumulation requires suppressing FMA contraction, and naga offers no `NoContraction` control. **Treat f64-on-GPU as unproven, not as a fallback plan.**

So: any GPU elevation kernel is **approximate by contract**, exactly like the cave kernel (`gpu.rs:16-21`). Under hard constraint #1 it ships env-gated OFF, and delivers nothing until a flag is flipped.

### Where elevation becomes an integer

The single conversion is `src/ground.rs:717`:
```rust
let result = lerp_top + (lerp_bot - lerp_top) * dz;
result.round() as i32
```
The comment above it (`ground.rs:703-709`) already reasons about exactly this: f32 storage gives ~10⁻⁷ relative precision, "far smaller than the 0.5-block half-width used by `round()`", so "for any value that isn't pathologically close to a half-integer boundary" the f32 and f64 paths agree. **That is a genuine absorber — but it is a probabilistic one, and it is the only one.**

Quantify it for a GPU f32 Gaussian. Bucharest heights are ~60-90 m; f32 ulp at 80 is ~7.6 × 10⁻⁶. A 183-tap sequential f32 accumulation has error roughly `sqrt(183) × ulp ≈ 13.5 × 7.6e-6 ≈ 1.0 × 10⁻⁴ m`. With `blocks_per_meter ≈ 1` (flat city, range fits, `postprocess.rs:1270-1275`), that is 10⁻⁴ blocks. Probability a given column's value lies within 10⁻⁴ of a half-integer:
```
2 × 1.0e-4 = 2.0e-4
over 2048² = 4,194,304 block columns  →  ~840 columns flip by exactly 1 block
840 / 4,194,304 = 0.020%
```
That is **40× the cave kernel's measured 0.0005%** — and the visual consequence is different in kind: a flipped cave block is an interior wall nobody sees; a flipped surface column is a visible 1-block pimple on flat urban ground.

### The amplifiers — every hard threshold that elevation crosses

These are the reason "f32 is fine, `round()` absorbs it" is the wrong general conclusion for this pipeline:

| amplifier | file:line | gain |
|---|---|---|
| MAD anomaly test `deviation > 6.0 && deviation > 3.0*mad` | `postprocess.rs:100` | a flip replaces the cell with the 5×5 median — **a jump of >6 m, i.e. >6 blocks**, not 1 |
| water-surface tolerance `orig <= surface + 2.0` | `postprocess.rs:385, 415` | a flip moves the cell between "flattened to water surface" and "kept as DSM terrain" — arbitrary metres, **and** it changes `is_water_surface`, which feeds `reclassify_non_surface_water_cells` (`postprocess.rs:665`) → changes the land-cover **class**, i.e. block type, not just height |
| `histogram_mode` 1 m bin assignment | `postprocess.rs:548-577` | a value crossing a bin edge can move the **whole component's** surface by 1 m |
| flowing/still IQR classification `iqr > 5.0` | `postprocess.rs:357` | flips an entire water body between a single flat surface and a per-cell local-median gradient |
| coastal cliff test `orig - wl > 15.0` | `postprocess.rs:781` | flips one cell between pulled and unpulled |
| **global min/max reduce** | `postprocess.rs:1223-1240` | a perturbation of a single extremal cell changes `min_height`/`height_range` → changes `blocks_per_meter` (`postprocess.rs:1308-1312`) → **re-scales every column in the grid coherently**, which produces a whole iso-height contour of flipped columns rather than scattered noise |
| snow line → i32 | `ground.rs:77` | `min_height_m` shift moves the snow threshold Y by a whole block, changing surface **block type** along a contour |

### The one piece of good news, and it is decisive

**In Meld's default tiled configuration the global min/max amplifier is already disabled.** `meld-triagefix/src/arnis_cmd.py:414-418` passes `--elevation-min`/`--elevation-max` whenever `terrain` is on and `elevation_mode == "global"` (the default). That takes the `(Some(lo), Some(hi))` branch at `postprocess.rs:1218-1222` and **skips the per-tile reduce entirely**; `min_height`, `max_height`, `height_range`, `blocks_per_meter` and the snow threshold all become CLI-fixed constants shared by every cell.

And the candidate kernel's position in the pipeline is the best available:
```
level_water_surfaces  →  reclassify  →  [ GAUSSIAN ]  →  coastal pull  →  scale_to_minecraft  →  round()
       ^^^ every high-gain threshold is UPSTREAM ^^^        one 15 m test    affine (locked)     the absorber
```
So for a GPU port of `smooth_built_up_gaussian` **and only that function**, in Meld's tiled default, the error propagates with **gain exactly 1** to `round()`. Estimated impact: **~0.02% of surface columns shift by 1 block**, confined to built-up areas and their ~91-cell feather (the blend is skipped where `feathered_mask <= 1e-4`, `postprocess.rs:909-911`), plus a negligible number of coastal-pull cells within f32-epsilon of a 15 m drop. That is a defensible approximate contract of the same family as the cave kernel's — **but only for the Gaussian, only after `level_water_surfaces`, and only when the elevation lock is on**.

Porting anything **upstream** of `level_water_surfaces` to f32 — the tile resample, `repair_terrain_anomalies`, `fill_nan_values` — puts f32 noise in front of six hard thresholds with gains of 1 m to "whole component". That would not be "approximate", it would be non-reproducible terrain.

**A side observation the Gaussian analysis surfaced, unrelated to GPU but load-bearing for touching this code:** at cs4/1:1 the built-up blur has a radius of 91 grid cells = 91 blocks, and both passes renormalise weights over in-grid samples only (`postprocess.rs:977, 1016`). Two adjacent Meld cells therefore compute *different* smoothed heights within 91 blocks of their shared border — the kernel is already not tile-invariant. Any change here should be validated against a 2×2 cell seam test, not just the golden hashes.

---

## 4. Data volume per cell, and whether transfer eats the win

All figures from the dimensions established in §2.

### Candidate kernel: the built-up Gaussian, cs4 / scale 1.0 / grid 2049²

| buffer | size | direction |
|---|---|---|
| heights, f32 | 4,198,401 × 4 = **16.79 MB** | up |
| built-up mask, bit-packed | 4,198,401 / 8 = **0.52 MB** | up |
| `is_water_surface`, bit-packed | **0.52 MB** | up |
| `after_h` scratch, f32 | 16.79 MB | device-only |
| blurred mask, f32 | 16.79 MB | device-only |
| blurred heights, f32 | 16.79 MB | device-only |
| blended heights, f32 | **16.79 MB** | down |

```
uploaded   = 16.79 + 0.52 + 0.52 = 17.83 MB
downloaded =                       16.79 MB
round trip =                       34.62 MB
device-resident peak ≈ 17.8 + 50.4 = 68 MB
```

At cs8 (grid 4097² = 16,785,409): round trip = **138.4 MB**, device peak ≈ **272 MB**. With 5 concurrent workers at cs8 that is ~1.4 GB — on an iGPU this is *system* RAM, the same 31.4 GB the CPU workers use, so it must be declared to the governor the way RSS already is.

### Transfer cost

The adapter is integrated (dGPU invisible per `docs/PHASE2-GPU-MEASURED.md` Finding 1), so **there is no PCIe hop at all** — `create_buffer_init` is a CPU memcpy into a mapped staging buffer plus a device-side copy, both DRAM↔DRAM on the same DDR5-6400 controller. Peak is 102.4 GB/s dual-channel; under a 24-core saturated load, assume 10-30 GB/s available:
```
34.62 MB × 2 (staging + device copy) = 69 MB of DRAM traffic
69 MB / 15 GB/s ≈ 4.6 ms per cell
```
Against **3.1-4.6 core-seconds** of CPU work displaced. Transfer is **0.1-0.2% of the work moved**. It does not eat the win.

### The right comparison — bytes per core-second displaced

Cave kernel, per 512² region with surface ~Y 80 (`MIN_Y = -64` at `world_editor/common.rs:14`, `TOP_GATE = 6` at `caves/mod.rs:66`, so `y_lo = -63`, `y_hi = 74`, `nyb = 138`):
```
surf up   = 512 × 512 × 4                = 1.049 MB
mask down = 512 × 512 × 138 / 8          = 4.522 MB
corners   = 130 × 130 × 19 × 4 = 1.285 MB  (device-only, never crosses)
round trip                               = 5.57 MB
```
From `PHASE2-GPU-MEASURED.md`, the caves delta on a 224-region cell was 430.6 core-s ⇒ **1.92 core-s per region**:
```
caves     : 5.57 MB  /  1.92 core-s   =  2.9 MB per core-second
elevation : 34.62 MB /  3.1-4.6 core-s = 7.5-11.2 MB per core-second
```
The elevation kernel is **2.6-3.8× less transfer-efficient** than the cave kernel — because the cave field has essentially infinite arithmetic intensity (54 octaves per sample, zero input bytes) while a blur reads real data. In absolute terms it is still three orders of magnitude below any bandwidth limit.

**One implementation caveat that decides whether that stays true:** the blur's arithmetic intensity depends entirely on tiling. A naive kernel reads 183 × 4 = 732 bytes per output for ~366 flops → **0.5 flop/byte, hopelessly memory-bound**. Staging each input tile into workgroup shared memory so it is reused by 183 outputs gives ~45 flop/byte → compute-bound. Get this wrong and the "GPU" version is slower than the CPU one.

### Compute estimate

`docs/PHASE2-GPU-MEASURED.md` measured the iGPU at 5.0-5.3 × 10⁹ octave-evals/s where an octave-eval is ~8 gradient dots + quintic fade + trilerp (call it ~50 flops) ⇒ **~250 GFLOP/s effective f32** on real-shaped work. 3.073 G taps × ~3 flops ≈ **9.2 GFLOP** ⇒ **~37 ms**, plus ~5 ms transfer ⇒ **~42 ms of GPU time to displace 3.1-4.6 core-seconds of CPU**. Even at 5× worse than that estimate (210 ms), the trade is overwhelmingly favourable in the resource that is actually saturated.

### The fleet arithmetic

```
per-cell CPU today            : 27.70 cpu-s   (measured, T=2)
Gaussian removed              : -3.1 to -4.6  →  23.1 to 24.6 cpu-s  (11-17% less)
fleet CPU, 81 cells           : 2244 → 1871-1993 cpu-s
best actual run 161 s at 58% efficiency, held constant:
                              →  134-143 s   ⇒   1.13-1.19×
```
Note this lands in **the identical band the cave GPU kernel actually delivered** (1.18× dGPU / 1.10× iGPU). That is not a coincidence — both are ~15% of a cell's CPU.

---

## 5. Honest verdict

### The one kernel: `gaussian_blur_grid_reported` as driven by `smooth_built_up_gaussian`

`src/elevation/postprocess.rs:941-1043`, called twice from `src/elevation/postprocess.rs:875` and `:896`.

**Why it wins on (win × confidence) / effort:**
- It is the **only** computation in the elevation phase whose op count is in the billions (3.073 G taps/cell, arithmetic in §2) — everything else on the 15-step inventory is 10-100× smaller.
- It is **dense, uniform, separable, and has no neighbour dependency** — the exact shape a compute shader wants, and it is *already* structurally parallel on CPU (`postprocess.rs:968, 1006`), so no algorithm has to be reinvented, only re-expressed.
- It sits at **the one point in the pipeline downstream of every high-gain threshold** (§3), so its approximation contract is bounded at ~0.02% of columns shifting 1 block, in built-up areas only — and in Meld's tiled default the global-affine amplifier is already switched off by `--elevation-min/--elevation-max` (`meld-triagefix/src/arnis_cmd.py:414-418` → `postprocess.rs:1218-1222`).
- The kernel is ~40 lines of WGSL. It reuses every piece of §1's scaffolding.
- The win scales with `1/m_per_cell`, so it is **largest exactly where the problem is** (1:1) and vanishes at 1:10 — matching `PHASE2-GPU-MEASURED.md`'s "~95% it delivers nothing at 1:20".

**Estimated: 1.13-1.19× fleet, ~55% confidence.** Lower than it looks, for the reason below.

### But the honest ranking puts a CPU fix first, and I would be misleading you to omit it

The same function has three defects that cost more than the GPU would save, and **two of the three fixes are bit-identical**:

1. **The serial scatter.** `postprocess.rs:1034-1038` writes 4.198 M f64 per blur, on the calling thread, one at a time into 2049 separate `Vec<f64>` allocations. 8.4 M scattered writes per cell, outside rayon. This is part of the ~0.23 s serial residue derived in §2, and it caps the phase's scaling at 6.7× on 10.5× threads.
2. **`Vec<Vec<f64>>` everywhere.** A pointer chase per row, no contiguity, no vectorisation. The vertical pass materialises each column via `after_h.iter().map(|row| row[x])` (`postprocess.rs:1008`) — a 2049-deep strided gather across 2049 separate allocations, per column, per blur.
3. **The `is_finite` branch per tap** (`postprocess.rs:979, 1018`) defeats auto-vectorisation of a loop that is otherwise a textbook FMA chain.

Fixing (1) and (2) — flat `Vec<f64>` with an explicit transpose between passes, keeping **the identical left-to-right tap accumulation order** — is **bit-for-bit identical output**, passes the golden gate unchanged, needs no flag, and plausibly returns 2-3× on 3.1-4.6 core-s ⇒ **1.07-1.11× fleet at a fraction of the effort and zero determinism risk**. Fixing (3) (branchless `select` + AVX2) is *not* bit-identical if it reassociates, but a masked-load formulation that preserves order is.

So the correct sequence is: **bit-exact CPU layout fix first, then the GPU kernel on top of what remains** — not GPU instead of it. The two are additive, and the CPU one carries no contract debt.

### What to measure before writing either

`--benchmark` already emits per-step timings (`src/bench.rs:19-24`) and the labels exist: `elev_landcover_fetch` (`ground.rs:247`), `elev_raw_fetch` (`elevation/mod.rs:254`), `elev_filter_outliers` (:270), `elev_repair_anomalies` (:272), `elev_fill_nan` (:276), `elev_landcover_repair` (:322), `elev_scale_to_mc` (:335), `elev_downcast` (:357).

**The gap: `elev_landcover_repair` is one label covering `level_water_surfaces` + `reclassify` + both Gaussians + the coastal pull.** One extra `bench.mark` on either side of `smooth_built_up_gaussian` (`postprocess.rs:869-875`) converts my derived 3.1-4.6 core-s into a measurement. That is a two-line, zero-risk change and it should precede any implementation work. My entire ranking rests on that number.

### Traps — things that look GPU-attractive and are not

**T1. `repair_terrain_anomalies` (`postprocess.rs:22-125`) — the most seductive one.** A dense 5×5 stencil with a 24-element median and MAD, already rayon-parallel. Looks perfect. It is a trap on size and on control flow: (a) the op count is 4,198,401 × 24 = **101 M taps per pass, 3.3% of the Gaussian's 3.073 G**; (b) it early-breaks when a pass repairs nothing (`postprocess.rs:110-112`), which on a clean urban DEM is pass 1 — so the common case is one pass; (c) each pass needs the `repaired` count **back on the host** to decide whether to continue, i.e. a full round-trip per pass, up to 10. Sub-millisecond dispatches (0.19-0.66 ms per `PHASE2-GPU-MEASURED.md`) make that survivable but it turns a 3% win into a wash. And it sits **upstream of every threshold in §3** — the worst possible place to introduce f32.

**T2. Both BFS distance transforms — a silent determinism break, not just a hard port.** `compute_water_distance` (`land_cover.rs:1270-1287`) and `pull_coastal_land_toward_water` (`postprocess.rs:801-818`). A distance transform is a well-known GPU problem (jump flooding), which is exactly why this is a trap: the coastal one propagates a **value** alongside the distance (`water_level[nyu][nxu] = wl`, `postprocess.rs:815`) and the winner among equidistant sources is decided by **FIFO queue order** (`if d + 1 < dist[..]`, strict `<`). Any wavefront or jump-flood variant picks a different source for equidistant cells, giving a different `water_level`, giving different heights — with *no* numerical error anywhere to point at. Bit-identical distances, wrong output.

**T3. `scanline_fill_area` (`floodfill.rs:332-400`) — a dead kernel.** Perfectly parallel per row, no visited set, pure arithmetic. It only runs when the polygon's bounding box exceeds `MAX_FLOOD_FILL_AREA = 25,000,000` blocks (`floodfill.rs:13, 282`). **A cs4 cell is 2048² = 4,194,304 blocks total**, so no polygon inside one can reach the threshold. At the measured configuration this path is unreachable. It would be a correct, tested, zero-impact kernel.

**T4. The rest of `floodfill.rs`.** `optimized_flood_fill_area` (`:403-488`) and `original_flood_fill_area` (`:491-`) are BFS over a shared bitmap with a `geo::Polygon::contains` ray cast per candidate cell (`:447, 478`) against rings of 4 to a few thousand edges (`floodfill.rs:84-86`). Maximal divergence, pointer chasing, and a work budget that must remain a pure function of input — the entire point of the new `FillLimit` (`floodfill.rs:30-99`). Also: it lives in the parse/place phases, not elevation.

**T5. `level_water_surfaces` (`postprocess.rs:253-451`).** Connected-component labelling on GPU is a research problem, and the per-component chain (histogram mode → IQR classify → `clamp_by_adjacent_land` p25 over a deduped 4-connected boundary) is variable-size reduction with a HashSet in it. **But note the real finding: it is completely serial today** — no rayon anywhere in `postprocess.rs:297-429` — and it is one of the three contributors to the 0.23 s serial residue. That is a CPU parallelisation target (components are independent once labelled), not a GPU one.

**T6. `ground_generation` / the surface pass.** 2048² block columns with per-column value noise (`ground_generation.rs:1985-2007`), climate, land-cover class, footprint bitmaps, road masks and RNG streams, writing through a mutable region/chunk hashmap. Not a kernel. Also note a measurement gap: `ground_on_merged` is **false** on Meld's parallel-tile path (`data_processing.rs:1049`, `use_parallel_tiles` at `:574`), so ground generation runs *inside* the per-tile closure at `data_processing.rs:743` — meaning the `phase("ground")` marker at `data_processing.rs:1051` fires around an empty branch and **the ground layer's cost is hidden inside the supplied profile's 31.4% "place" bucket**, along with `merge`. If you intend to attack `place`, that bucket needs splitting before anything else.

**T7 — the largest, and structural. "The GPU is idle" was measured on an unsaturated machine.** `docs/PHASE2-GPU-MEASURED.md` reports "the two runs differ by only ~5 s of wall between the 5080 and the iGPU — the GPU is idle most of the time either way" and "7 workers ≈ 20-25% GPU utilisation". Those numbers come from the Phase-1 binary at **7 workers**. Your ground truth says the governor now settles at **16 workers using 23.4 of 24 cores**. On a Core Ultra 9 275HX the Intel iGPU shares the DDR5-6400 memory controller *and* the package power budget with those cores. Loading the iGPU while the P-cores are at full tilt can (a) steal the DRAM bandwidth the CPU workers need and (b) force a package-power redistribution that drops core clocks. **Nobody has measured GPU throughput or CPU throughput under 16-worker saturation.** This is the single largest unknown in the whole assessment, and it can invert the sign of the result.

It is also cheap to settle, and it should be settled before any code: run the existing cs4 A/B at 16 workers with `--caves --gpu igpu` versus `--caves --gpu off`, and record **package power and effective core clocks**, not just wall time. If iGPU load costs more in CPU clocks than it saves in CPU work, no second kernel is worth writing on this machine and the answer becomes "make the 5080 visible first" (Windows → Display → Graphics → add `arnis.exe` → High performance, or the vendor MUX switch — `PHASE2-GPU-MEASURED.md` states this is fixable from settings, not from code).

### Files worth citing back

- `C:\Users\LEGION\Documents\Meld\arnis-triagefix\src\caves\gpu.rs`
- `C:\Users\LEGION\Documents\Meld\arnis-triagefix\src\caves\gpu_carve.wgsl`
- `C:\Users\LEGION\Documents\Meld\arnis-triagefix\src\caves\mod.rs`
- `C:\Users\LEGION\Documents\Meld\arnis-triagefix\src\elevation\postprocess.rs`
- `C:\Users\LEGION\Documents\Meld\arnis-triagefix\src\elevation\mod.rs`
- `C:\Users\LEGION\Documents\Meld\arnis-triagefix\src\elevation\providers\mapterhorn.rs`
- `C:\Users\LEGION\Documents\Meld\arnis-triagefix\src\elevation\providers\fixed_tile.rs`
- `C:\Users\LEGION\Documents\Meld\arnis-triagefix\src\ground.rs`
- `C:\Users\LEGION\Documents\Meld\arnis-triagefix\src\land_cover.rs`
- `C:\Users\LEGION\Documents\Meld\arnis-triagefix\src\floodfill.rs`
- `C:\Users\LEGION\Documents\Meld\arnis-triagefix\docs\PHASE2-GPU-MEASURED.md`
- `C:\Users\LEGION\Documents\Meld\meld-triagefix\src\arnis_cmd.py`

### Stated plainly: what I do not know

1. The per-function split of the 3.82 s elevation phase. My 3.1-4.6 core-s for the Gaussian is derived from a 3.073 G tap count and an assumed 1.0-1.5 ns/tap, cross-checked against the Amdahl fit (7.19 core-s parallel pool). **It is not measured.** One `bench.mark` pair settles it.
2. Whether this machine's Intel adapter exposes `Features::SHADER_F64`, and at what rate. Requires enumerating `adapter.features()`.
3. Whether iGPU load degrades CPU throughput at 16-worker saturation (T7). Nothing in the repo or the docs measures this.
4. Actual iGPU throughput on a *bandwidth-bound* kernel. The 5.0-5.3 G evals/s figure was measured on a compute-bound noise kernel; a blur is a different regime and the tiling quality decides which side it lands on.
5. The real content of the `place` bucket (T6) — `merge` and `ground` markers exist but are absent from the supplied profile.

--- serial-fronts ---
## Summary

The two "non-scaling" phases are one problem wearing two hats: **every cell decodes whole z11 OSM tiles (~14 km × 14 km, 19–148 MB of JSON) to render a 2.3 km cell, single-threaded, and the same four tiles get re-decoded 90 times across a 72-cell run.** Measured: **8.20 GB of JSON decoded per run against 262 MB of distinct source on disk — 31.3× duplicated work.**

Also, two corrections to the ground truth before anything else:

1. **The `parse` marker does not measure `parse_osm_data`.** `meld_telemetry::phase("parse")` fires at `arnis-triagefix/src/main.rs:494`, and the next marker on a `--no-buildings` run is `phase("place")` at `arnis-triagefix/src/data_processing.rs:579`. Everything in between is inside the 2.68 s: `parse_osm_data`, the priority sort, three land-cover overrides, `transform_map`, height profile, `WorldEditor` construction, and the entire `precompute` block. Details in §2.
2. **Cell 0,0 is not the Bucharest centre.** With `bench/matrix.json`'s origin `44.5072, 25.96` (the NW corner of the site bbox), cell key `0,0,4` spans `44.5072..44.5256 N, 25.9600..25.9858 E` and covers exactly one z11 tile: `(1171,740)`, **18.8 MB — the smallest of the four**. The true centre tile `(1172,741)` is **147.8 MB, 7.9× larger**. The 18.96 s / 27.7 cpu-s per-cell profile is from the cheapest quadrant and understates the run.

---

## 1. What `fetch` actually does with `--osm-tile-dir` on a warm cache

**Call path:** `main.rs:467` `phase("fetch")` → `main.rs:473` `osm_parser::OsmData::from_tile_dir(dir, args.bbox, args.osm_tile_z)` → `main.rs:486` `bench.mark("osm_fetch")`, then `main.rs:489` `phase("elevation")`.

**The whole body is `arnis-triagefix/src/osm_parser.rs:122-174`:**

- `osm_parser.rs:130-133` — compute the covering slippy-tile rectangle from the bbox corners (`slippy_tile`, `osm_parser.rs:180-188`, the same formula as Meld's `survey._lat_lng_to_tile`).
- `osm_parser.rs:139-140` — **a plain nested `for x { for y { … } }`.** No `rayon`, no threads. `osm_parser.rs:1-11` imports no rayon at all. This is the direct, mechanical reason fetch is 1.01 s at T=2 and 1.03 s at T=21: **there is no code path by which a thread count could change it.**
- `osm_parser.rs:141-148` — `File::open` on `osm_g1_z{z}_{x}_{y}.json`.
- `osm_parser.rs:149-150` — **`serde_json::Deserializer::from_reader(BufReader::new(file))` + `OsmData::deserialize`.** This is the cost centre.
- `osm_parser.rs:158-162` — dedup: `seen.insert((el.r#type.clone(), el.id))` into a `HashSet<(String, u64)>`, then `elements.push(el)` — **first-occurrence-wins**, and one `String` allocation per element.

### Disk read, decompress, or deserialise?

**Deserialise, overwhelmingly.** Evidence:

- The tiles are **plain uncompressed `.json`** — Meld writes them raw (`meld-triagefix/src/prefetch.py:597` → `_download_one`; `osm_grid.merge_tiles` at `src/osm_grid.py:84-121` writes plain UTF-8 JSON). There is **no decompress step anywhere on this path.**
- 262 MB of distinct tiles on a 31.4 GB machine is fully page-cached after the first touch, so the `read()` side is a memcpy.
- The target type is expensive: `OsmElement` (`osm_parser.rs:83-93`) carries `Option<HashMap<String, String>> tags`, `Option<Vec<u64>> nodes`, and `Vec<OsmMember>` — every element allocates a `HashMap` and its `String` keys/values.

**Measured rate:** cell 0,0 reads exactly one tile, `(1171,740)` = 18,786,817 bytes / 172,843 `"type"` tokens, in 1.01 s.
→ **18.6 MB/s, ~171 k elements/s, single-threaded.** That is a credible serde_json-into-`HashMap`-structs rate and it independently corroborates the 1.01 s figure.

Note `serde_json::from_reader` uses the `IoRead` path, which is the *slowest* of serde_json's three readers — it cannot use the slice fast paths. `retrieve_data.rs:171` (`fetch_data_from_file`) and `retrieve_data.rs:146` (`parse_overpass_response`) have the same shape. Reading the file into a `Vec<u8>` and calling `from_slice` is the classic 2–3× win here, and is **byte-identical by construction** (same parser, same struct, same element order).

### Overlap / cache-across-cells / mmap?

- **Cached across cells inside one process: impossible today, and worth nothing if it were.** One `arnis.exe` per cell (hard constraint 2; Meld spawns it at `server.py:2630` via `build_arnis_cmd`). A process handles exactly one cell and exits, so a process-local tile cache has a 0 % hit rate. The 31× duplication is *inter-process*.
- **Overlapped with the previous cell: no useful room.** `fetch` is the first thing a cell process does (`main.rs:467`, right after arg parsing); there is no previous cell in that process. At the *run* level the overlap already exists — 16 workers each doing an independent single-threaded decode is 16 cores busy, which is fine utilisation. **Fetch is not a serialisation bottleneck; it is a pure CPU-work bottleneck.** That distinction matters for the plan: parallelising it buys nothing, *deleting* it buys everything.
- **Memory-mapping: a rounding error.** mmap removes the `read()` copy, not the JSON parse. Since the files are already page-cached and the work is deserialisation, expect ~0.

**What actually would work** (ranked in §5): a pre-digested binary tile sidecar Meld bakes once per z11 tile (byte-identical if it round-trips the same `OsmElement` sequence in the same order), or finer grid tiles (**not** byte-identical — see §5).

---

## 2. `parse_osm_data`: sequential structure and what byte-identity requires

### 2a. The span is mis-attributed

With `--no-buildings`, `args.buildings && args.overture` at `main.rs:522` is false, so `phase("overture")` (`main.rs:523`) never fires and the `parse` span runs from `main.rs:494` all the way to `data_processing.rs:579`. Inside it:

| Step | Location | Threading |
|---|---|---|
| `parse_osm_data` | `main.rs:495-503` | **serial** |
| `sort_by_key(get_priority)` | `main.rs:559-560` | serial (stable sort) |
| `apply_osm_water_override` / `..._land_override` / `..._bridge_land_cover_repair` | `main.rs:565-570` | **serial** (no rayon in `land_cover_osm_*.rs`, `land_cover_bridge_repair.rs`) |
| `transform_map` + optional rotate | `main.rs:~594` | serial |
| `height_profile::for_run` | `data_processing.rs:327` | serial |
| `WorldEditor` construction, datapack install, void platform | `data_processing.rs:~340-410` | serial |
| `water_depth::compute_big_water_field` | `data_processing.rs:460` | **rayon** (`water_depth.rs:1024` `into_par_iter`) |
| `waterways::compute_waterway_field` | `data_processing.rs:466` | serial |
| `build_highway_connectivity_map` | `data_processing.rs:475` | serial |
| `FloodFillCache::precompute` | `data_processing.rs:493` | **rayon** (`floodfill_cache.rs:326` `par_iter`) |
| `collect_road_surface_coords`, `collect_tunnel_footprint`, bridge index/structure/surface maps | `data_processing.rs:521-539` | **serial** |

**Two rayon islands in an otherwise serial span is exactly the shape that yields 2.1×.** But you cannot plan against 2.68 s until you split it. **Add markers around `parse_osm_data` (`main.rs:494/504`), the override block, and `precompute` (`data_processing.rs:555` already has `bench.mark("precompute")`).** Anything else is guesswork — I will not guess a split here.

### 2b. Structure of `parse_osm_data` itself (`osm_parser.rs:855-1166`)

Strictly sequential, five stages:

1. **`osm_parser.rs:874`** — `SplitOsmData::from_raw_osm_data` (`osm_parser.rs:197-226`): one pass bucketing the flat element vec into `nodes` / `ways` / `relations` / `others`, **preserving input order within each bucket**.
2. **`osm_parser.rs:890-905`** — three suppression passes plus `pack_part_style_hints`:
   - `compute_outline_suppression` (`osm_parser.rs:1192-1315`) — two passes over relations, one filtered pass over ways, one over nodes, plus shoelace areas.
   - `compute_spatial_part_suppression` (`osm_parser.rs:1353-1489`) — a full pass over *every* way, a filtered pass over nodes, a 0.0005°-cell spatial grid, ring areas, point-in-ring.
   - `compute_part_way_outline_suppression` (`osm_parser.rs:1504-1553`) — builds `HashMap<u64, (&tags, &nodes)>` over **all** ways.
   - **None of these is gated on `args.buildings`** — `parse_osm_data`'s signature (`osm_parser.rs:855-868`) does not take a buildings flag. On the profiled `--no-buildings` run this building-geometry analysis ran in full over the whole 14 km tile and every result was then discarded. That is free money if you gate it.
3. **`osm_parser.rs:913-937` — the node pass.** For **every node in every covering tile** (~173 k for cell 0,0, ~1.19 M for a centre cell): `LLPoint::new`, `coord_transformer.transform_point`, `filter_tags` (`osm_parser.rs:66-71`, a `retain` over 22 exact keys + 15 prefixes), construct `ProcessedNode`, **`nodes_map.insert(id, processed.clone())`**. Only then is `xzbbox.contains` applied (`osm_parser.rs:933`) to decide whether it becomes an output element. **This is O(tile), not O(cell)** — it is the same 31× duplication as `fetch`, paid a second time.
4. **`osm_parser.rs:951-1032` — the way pass.** Per way: resolve every node ref through `nodes_map` with a `ProcessedNode` clone each, `filter_tags`, optional `compute_node_bounds` / `compute_polygon_area`, `clip_way_to_bbox`, `continue` if empty. Also O(tile).
5. **`osm_parser.rs:1046-1155` — the relation pass.** Per member: role classification, `ways_map` lookup, optional per-member clip and re-`Arc`.

### 2c. Every place order affects output

This is the killer, and it is a chain, not a single site:

1. **`from_tile_dir` (`osm_parser.rs:139-162`)** fixes the raw order: **x-major, then y**, then within a tile the JSON array order, with **first-occurrence-wins dedup**. A boundary way present in two tiles keeps the copy from the lower-x (then lower-y) tile.
2. **`SplitOsmData::from_raw_osm_data` (`osm_parser.rs:203-211`)** preserves that order per bucket.
3. **`processed_elements` push order (`osm_parser.rs:934`, `1030`, `1150`)** — all tagged in-bbox nodes first, then all ways, then all relations, each in bucket order.
4. **`main.rs:559-560` `parsed_elements.sort_by_key(get_priority)`** — `Vec::sort_by_key` is a **stable** sort. Ties (which is most of the vector; `get_priority` at `osm_parser.rs:1587` returns a small integer class) **retain the parse order verbatim.**
5. Downstream, `process_element` (`data_processing.rs:43`) walks that vector in order and later writes overwrite earlier ones at the same `(x, y, z)`. **Element order is world bytes.**

So a parallel parse must reproduce that exact `Vec` order. Concretely:

- **Safe:** decode tiles with `par_iter().map(...).collect::<Vec<Vec<OsmElement>>>()` (rayon's `collect` preserves index order — the same guarantee `aws_terrain.rs:128` already relies on), then run the existing serial dedup loop over the collected vecs in tile order. Bit-identical.
- **Safe:** parallelise the node pass into a per-index `Vec<Option<ProcessedNode>>` and rebuild `nodes_map` + `processed_elements` serially in index order. The transform is pure.
- **Not safe without proof:** parallelising the way pass, because it reads `nodes_map` (needs the node pass complete) and pushes into the shared output vector.

**Things that are *not* order hazards** (I checked each):
- `nodes_map` / `ways_map` are only ever `.get()` (`osm_parser.rs:958`, `1105`) — never iterated. Their `HashMap` iteration order never reaches output.
- `for ns in way_nodes.values()` (`osm_parser.rs:1229`) extends a `HashSet` — order-free.
- `for (gi, cov) in covered` (`osm_parser.rs:1484`) inserts into a `HashSet` — order-free.
- `pack_part_style_hints` `for seed in part_groups.values_mut()` (`osm_parser.rs:748`) is a per-key pure transform — order-free.
- `best` in `compute_spatial_part_suppression` (`osm_parser.rs:1467-1471`) tie-breaks on `(area, id)`, and ids are unique — order-free.

**One real order hazard beyond the element vector:** `PartGroups` is `HashMap<u64, u64>` (`osm_parser.rs:458`). `compute_outline_suppression` inserts at `osm_parser.rs:1283`, `compute_spatial_part_suppression` at `osm_parser.rs:1477`. A way hit by both gets the *spatial* value because that pass runs second (`osm_parser.rs:891` then `893`). **That relative order is load-bearing** and must be preserved if the passes are ever run concurrently.

**A trap worth naming explicitly:** the obvious optimisation — "pre-filter nodes to near the bbox before transforming them" — **breaks output**. `clip_way_to_bbox` needs the out-of-bbox endpoints to compute the boundary intersection, and a way whose refs go unresolved is silently shortened (`osm_parser.rs:958-961`, and the warning at `osm_parser.rs:1035-1044` exists precisely because of this). Dropping distant nodes turns filled polygons into unclosed rings that flood-fill to nothing.

---

## 3. Overture: did it run?

**On the profiled command (`--no-buildings`): no. Not one line of `overture.rs` executed.**

The gate is `main.rs:522`: `if args.buildings && args.overture && !skip_objects`. `--no-buildings` sets `args.buildings = false` (`args.rs:239-240`, `ArgAction::SetFalse` on a `default_value_t = true` field), so the whole block `main.rs:522-557` is skipped: no STAC index read, no partition reads, no `hints.apply`, no `deduplicate_against_osm`. Consistent with the profile table having no `overture` row — `phase("overture")` at `main.rs:523` is inside the gate.

**So yes: the 14 % `parse` cost is 100 % OSM-side** (plus all the non-parse work listed in §2a). And per §2b, it includes the full building-suppression analysis that `--no-buildings` then throws away.

**With buildings ON the picture changes twice over, and one of the changes is an accounting artefact:**

1. **The `parse` number stops meaning the same thing.** With Overture on, `phase("overture")` fires at `main.rs:523`, so `parse` now spans only `main.rs:494 → 523` — i.e. `parse_osm_data` alone. Everything from the sort through `precompute` moves into the `overture` bucket. **The two configs' `parse` figures are not comparable. Do not put them in the same table.**
2. **Real new work appears:**
   - `fetch_overture_buildings_inner` (`overture.rs:462-577`): STAC index (disk-cached weekly, `overture.rs:737-742`), then a **serial** `for` over partitions (`overture.rs:503`), each doing `process_partition_file` (`overture.rs:902-1021`) — footer decode, row-group bbox filter, then a **serial** `for &rg_idx in &matching_groups` parquet decode loop (`overture.rs:1008`). Warm, the bytes come from the per-range disk cache (`overture.rs:1940-1958`, keyed `{url_hash}_{start}_{len}.bin`), so it is **parquet decode, single-threaded — no rayon anywhere in `overture.rs`.**
   - `hints.apply` (`overture.rs:164`, called at `main.rs:537`) — O(elements), serial.
   - `deduplicate_against_osm` (`overture.rs:380-455`, called at `main.rs:547`) — builds a 64-block spatial grid then filters; not quadratic, but serial.
   - `bench/matrix.json` sets `"buildings": true, "overture": true`, and Meld only passes `--overture=false` when the setting is off (`arnis_cmd.py:343-344`). **The A/B run you measured 1.02×/1.11×/1.08× on had Overture ON. The 18.96 s profile did not.**

The stale in-tree comment at `main.rs:521` ("Overture is ~93% of a cell's wall time") predates the per-range disk cache. **I do not know Overture's current warm cost and will not invent one — re-run `ARNIS_PHASE_MARKERS=1` with `buildings=true, overture=true` before ranking it.**

---

## 4. Cross-cell tile duplication — the number

Computed by driving Meld's own code (`grid.cells_for_bbox`, `coords.cell_bbox`, `coords.expand_bbox_for_seam`, `osm_grid.grid_tiles_for_bbox`) on `bench/matrix.json`'s `1to1-cs4` group, which is the same math `server.py:2626-2627` uses to build `--bbox` and the same `prefetch.py:542-543` uses to plan tiles. Arnis then recomputes the identical set from that bbox (`osm_parser.rs:130-133`; the formula is documented as matching at `osm_parser.rs:177-179`).

**Site: `44.36..44.5072 N, 25.96..26.1662 E` = 16.36 km × 16.39 km. Origin `44.5072, 25.96`. scale 1.0, `cell_size` 4 (= 4 × 512 = 2048 m/cell), `seam_buffer_chunks` 8 (+128 m/side → 2.304 km expanded).**

```
cells                       = 72          (not 81 — see caveat below)
distinct z11 tiles          = 4           (1171-1172) x (740-741)
tile decodes across the run = 90
tiles per cell              = 1 x 56,  2 x 15,  4 x 1      (56+30+4 = 90 ✓)
```

Per-tile decode counts, against the real files in `C:\tmp\meld-ab-data\cache\osm\` (the A/B run's own cache, written 00:06–00:09):

| tile | bytes | `"type"` tokens | decoded |
|---|---:|---:|---:|
| `1171_740` | 18,786,817 | 172,843 | **9×** |
| `1171_741` | 33,742,154 | 303,930 | **21×** |
| `1172_740` | 61,682,803 | 542,907 | **18×** |
| `1172_741` | 147,837,444 | 1,186,769 | **42×** |

```
bytes decoded per run = 9(18.79) + 21(33.74) + 18(61.68) + 42(147.84) MB
                      = 169.1 + 708.6 + 1110.3 + 6209.1  =  8,197 MB  = 8.20 GB
distinct on disk      = 18.79 + 33.74 + 61.68 + 147.84   =    262 MB
DUPLICATION           = 8197 / 262                       =    31.3x

element-decodes       = 67.55 M      (distinct 2.21 M)   =    30.6x   (independent check ✓)
```

**Converted to CPU at the measured 18.6 MB/s:**

```
run-wide JSON decode CPU   = 8197 MB / 18.6 MB/s   =  441 s single-threaded
irreducible (each tile 1x) =  262 MB / 18.6 MB/s   =   14 s
pure waste                 =                          427 s
share of the machine       = 441 / (161 s x 24 cores = 3864 core-s)  =  11.4%
```

**≈ 11 % of the entire machine's capacity for the whole run is JSON deserialisation, and 97 % of that is re-decoding four files you already decoded.**

**Geometric over-read, exactly** (spherical-rectangle areas, same code):

```
summed expanded-cell area  =   382.7 km²  (72 cells x 5.32 km²)
z11 decoded tile area      = 17,567 km²   ->  over-read 45.9x
```

Every cell decodes a **195 km²** tile to render **5.3 km²**.

Zoom sweep, same method:

| grid z | tile | decodes | distinct | decoded area | over-read | re-decode |
|---|---:|---:|---:|---:|---:|---:|
| **z11 (today)** | 195.2 km² | 90 | 4 | 17,567 km² | **45.9×** | 22.5× |
| z12 | 48.8 km² | 110 | 9 | 5,370 km² | 14.0× | 12.2× |
| z13 | 12.2 km² | 195 | 36 | 2,380 km² | 6.2× | 5.4× |
| z14 | 3.05 km² | 360 | 110 | 1,099 km² | **2.9×** | 3.3× |

**Aggravating factor from the dispatch order:** `server.py:4603-4614` sorts cells centre-out by Chebyshev ring. The centre tile `(1172,741)` is the one decoded 42×, at 147.8 MB = **7.9 s each**. `42 × 7.9 = 332 s` of the 441 s — **75 % of the total decode cost lands in the opening of the run**, exactly where the governor is also ramping workers.

### Caveats on this number, stated plainly

- **72 cells, not the 81 in the ground truth.** `count_cells_for_bbox` (`grid.py:36-46`) on `matrix.json`'s current site bbox gives 8 × 9 = 72. The on-disk cache holds **9** tiles (x 1171–1173 × y 740–742), which no 72-cell or 81-cell selection from that origin produces — those extra 5 are a partial `1to10-cs4` prefetch (that group's full covering set is 81 tiles, and its first clump starts in exactly this corner). **The actual A/B selection was therefore not byte-for-byte the one in `matrix.json` today.** Scaled to 81 cells at the same 1.25 tiles/cell the ratios move by ~12 %; the 31× duplication and the 11 % machine share are unchanged in character.
- **18.6 MB/s comes from one data point** (cell 0,0's 1.01 s over one verified single tile). It is corroborated by the element rate (171 k/s) and it is the *only* rate measurement available. Everything downstream of it scales linearly with it — **re-measure it before committing to 441 s.**
- The `"type"` token count slightly over-counts elements (relation members also carry `"type"`), so element counts are upper bounds. The **byte** ratio does not depend on that and is exact.

---

## 5. Ranking

### Worth attacking

**#1 — The 31× tile re-decode. ~441 cpu-s/run, ~11 % of the machine, ~97 % of it waste.**
This is the whole ballgame, and it hits `fetch` *and* the O(tile) node/way passes in `parse` (§2b), so the true prize is larger than 11 %. Three options, in order of risk:

- **(a) Pre-digested binary tile sidecar — byte-identical, highest value.** Meld bakes, once per z11 tile, a `bincode`/`rkyv` dump of the same `Vec<OsmElement>` in the same array order; `from_tile_dir` reads it and falls back to JSON when absent. Same elements, same order, same dedup → the golden hashes cannot move. Expect **5–20×** off the decode. Pairs naturally with mmap (a zero-copy format finally makes mmap worth something, which it is not today).
- **(b) `from_reader` → `from_slice`** at `osm_parser.rs:149`, plus killing the per-element `String` clone at `osm_parser.rs:159` (a `u8` discriminant works: `"node"|"way"|"relation"|other`). Byte-identical, a few dozen lines, **~2×** on 441 cpu-s. **Do this first — it is the cheapest real win in the whole investigation.**
- **(c) Finer grid (z13/z14)** — 45.9× → 6.2×/2.9× over-read. **NOT byte-identical, do not ship ungated.** `from_tile_dir` does no clipping; the tile set *is* the element universe, and `ways_map` (`osm_parser.rs:908`) only holds ways from tiles that were read. Shrinking tiles drops distant member ways of large water/building multipolygons, changing ring assembly (`osm_parser.rs:1071-1078`, `keep_unclipped`). It also forces a re-bake (`osm_grid.py:29-30`) and multiplies file count 8–27×. Env-gated, hash-verified, or not at all.

**#2 — Gate the building-suppression passes on `args.buildings`.** `osm_parser.rs:890-905` runs `compute_outline_suppression` + `compute_spatial_part_suppression` + `compute_part_way_outline_suppression` + `pack_part_style_hints` over every way and node of the full tile, then `--no-buildings` discards the lot. Threading `args.buildings` into `parse_osm_data` and skipping to empty is trivially byte-identical *for that flag* (the outputs are only consumed by building rendering — verify that claim before shipping). Only helps `--no-buildings` runs, which the real bench is not, so: **cheap, correct, narrow.**

**#3 — Split the `parse` marker before planning anything else in it.** 2.68 s is currently unattributable between a serial `parse_osm_data` and a mostly-serial `precompute` block containing two rayon islands. Markers at `main.rs:504`, `main.rs:571`, and `data_processing.rs:460` would settle it in one run. **This is a prerequisite, not an optimisation** — without it any parse plan is a guess.

**#4 — Re-profile with `buildings=true, overture=true`.** The 18.96 s profile is from a configuration the benchmark does not use, and Overture's serial parquet path (`overture.rs:503`, `overture.rs:1008`) has no rayon at all. Its warm cost is currently **unknown** — the `main.rs:521` "93 %" comment predates the per-range cache and should not be trusted.

### Rounding errors — do not spend on these

- **mmap-ing the JSON tiles.** Files are page-cached; the cost is deserialisation. ~0 %. (Becomes worthwhile only *after* #1a, as part of it.)
- **Parallelising `from_tile_dir` across tiles.** 56 of 72 cells read exactly **one** tile; the maximum any cell reads is 4. Even perfect scaling on the 16 multi-tile cells removes ~1 % of run CPU. It is also the *wrong* fix: fetch is CPU-work-bound, not serialisation-bound — 16 workers already keep 16 cores busy decoding.
- **`post`.** 0.38 s = 2.0 % of a cell, already 2.3×. Nothing there.
- **`elevation`.** Already 6.7× and genuinely parallel: `aws_terrain.rs:81` builds a dedicated rayon pool, `aws_terrain.rs:117` parallel tile fetch/decode, `aws_terrain.rs:172` parallel bilinear resample; Mapterhorn likewise (`mapterhorn.rs:275`, `mapterhorn.rs:344`). It has the *same* cross-cell re-decode duplication (`fetch_or_load_tile`, `aws_terrain.rs:467+`, re-decodes cached PNGs per cell) but it hides behind cores. Leave it.
- **Overpass / `retrieve_data.rs`.** Entirely off this code path — `main.rs:469-484` takes the `(Some(dir), _)` arm whenever `--osm-tile-dir` is set. The only thing worth borrowing there is the same `from_reader` → `from_slice` fix at `retrieve_data.rs:146` and `retrieve_data.rs:171`.

### Bottom line

`fetch` and `parse` are not two scaling problems. They are one **volume** problem: the unit of caching (a 195 km² z11 tile) is 37× larger than the unit of work (a 5.3 km² cell), and the unit of *process* (one cell) is smaller than the unit of caching — so the same four files get deserialised 90 times per run at 18.6 MB/s. Fix the encoding first (free, byte-identical, ~2×), then the format (bake a binary sidecar, byte-identical, another 5–20×). Only consider re-tiling the grid after those, and only behind a flag with the golden hashes as the gate.

--- scheduler-idle ---
## Where the 21% goes — measured, not estimated

Everything below is computed from `meld-report.json` (schema `meld-run-report/3`) plus one extra signal I recovered: the **mtime of each `logs/cell-*.log`**. `_runner` opens that file before spawning arnis (`server.py:2637-2645`), writes every child line into it, then writes `=== arnis exit ok=… ===` and closes it immediately after `run_arnis` returns (`server.py:2806`, `server.py:2820-2825`). Its mtime is therefore the **arnis exit instant**, which splits each cell's `duration_s` into arnis-wall and Meld-side (merge + prune + meta). Merge lives inside `_runner` (`server.py:2864`), and `_timing_finished` runs in `_on_complete` after `_runner` returns (`server.py:3247-3255`), so `duration_s` = arnis + merge + prune. `_timing_started` fires *after* admission (`server.py:2609`, called from `_runner`, which the pool invokes only after `admit_cb` returns — `src/workers.py:251-259, 275`), so gate time is **not** inside `duration_s` and shows up as a worker gap instead.

Data sets used (all cs4, 81 cells, Bucharest, 1:1, blinear, warm cache):

| run | file | E (elapsed_s) | Σduration | eff_par |
|---|---|---|---|---|
| baseline 1.9.7, hand-tuned 16w | `c:/tmp/meld-ab-data/data/projects/ab-baseline-1.9.7-cs4/ab-baseline-1.9.7-cs4/meld-report.json` | 174.94 | 2401.7 | 13.73 |
| governor, cold (CALIBRATE) | `.../ab-perf-governor-cs4/ab-perf-governor-cs4/meld-report.json` | 171.00 | 2174.6 | 12.72 |
| governor, **warm start** | `.../ab-perf-governor-cs8/ab-perf-governor-cs8/meld-report.json` | 160.30 | 2185.5 | 13.63 |

**The third row is a recovered dataset, and it matters.** `ab_bucharest.py:302-306` — `do_run(reuse=True)` skips `prepare_project()` and therefore never calls `/api/projects/switch`, so the warm cs4 re-run rendered into whatever project was *active*, which was `ab-perf-governor-cs8` (`c:/tmp/meld-ab-data/data/projects/.active` still reads `ab-perf-governor-cs8`). That report holds 81 cs4 cells, `cell_size: 4`, `elapsed_s: 160.3` — it is the 161.2 s warm run. Two consequences: (1) the **cs8 governor report is destroyed**, so the 239.4 s cs8 figure cannot be recomputed, only cited; (2) `harvest()` (`ab_bucharest.py:247-260`) looked in the cs4 folder, found nothing fresh, and wrote `B-cs4-warm.json` with `"error": "no fresh meld-report.json produced"`.

---

## 1. Concurrency over time, and the worker-second ledger

Concurrency = cells with `started ≤ t < ended`, 5 s ticks, t relative to `summary.started`.

```
baseline   0: 0   5: 3  10: 5  15: 8  20:16  25:16 ... 145:16 150:16 155:13 160: 9 165: 7 170: 1
governor   0: 0   5: 4  10: 4  15: 6  20: 6  25: 6  30:12 ... 50:20 ... 75:20  80:19  85:18  90:17
           95:16 100:16 ... 120:16 125:15 130:15 135:15 140:14 145:10 150: 7 155: 5 160: 4 165: 2 170: 1
warm       0: 0   5:14  10:14 ... 25:16 ... 65:16  70:15  75:15  80:14  85:14  90:14  95:14 100:16
         105:16 ... 125:18 130:15 135:12 140: 8 145: 6 150: 2 155: 2 160: 1
```

### Ledger against a **16-worker reference** (this is the frame the "21% idle" number lives in: 12.72/16 = 79.5%)

**baseline** — cap16 = 16 × 174.94 = **2799.0 ws**, busy = 2401.7 ws, **idle = 397.3 ws = 14.2%**

| segment | window | cap16 | busy | idle | share of idle |
|---|---|---|---|---|---|
| ramp | 0 → 16.08 s | 257.3 | 72.6 | **184.7** | 46.5% |
| middle | 16.08 → 144.91 s | 2061.2 | 2057.3 | **3.9** | 1.0% |
| tail | 144.91 → 174.94 s | 480.5 | 271.8 | **208.7** | 52.5% |

**governor cold** — cap16 = 16 × 171.00 = **2736.0 ws**, busy = 2174.6, **idle = 561.3 ws = 20.5%**

| segment | window | cap16 | busy | idle | share |
|---|---|---|---|---|---|
| ramp (CALIBRATE climb) | 0 → 48.83 s | 781.3 | 409.3 | **372.0** | 66.3% |
| middle | 48.83 → 140.24 s | 1462.5 | 1585.9 | **−123.4** | — (it ran at 20, *above* 16) |
| tail | 140.24 → 171.00 s | 492.2 | 179.4 | **312.8** | 55.7% |

Sum checks: 184.7 + 3.9 + 208.7 = 397.3 ✓ · 372.0 − 123.4 + 312.8 = 561.4 ✓

### The ramp is not mysterious — it is the two pacing schemes, in closed form

* **baseline ramp = the fixed stagger, exactly.** `src/workers.py:261-268` applies `_first_job_delay` (`src/workers.py:68-80`) when `admit_cb` is unset; `step = cpu_stagger_seconds = 2` (`server.py:4278`, config in the report reads `cpu_stagger_seconds: 2`), `stagger_cap_workers = 8` (`src/workers.py:37`). Measured first-job offsets per worker: `0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.1, 16.0 ×8`. Closed form: `2·(0+1+…+7) + 8·16 = 56 + 128 = 184 ws`. **Measured: 184.7 ws.** The adaptive widening (`src/workers.py:75-79`) never engaged because `new_run_epoch()` zeroes the EWMA (`src/workers.py:138`) and all 16 workers pop their first job at t≈0.
* **governor ramp = the CALIBRATE hill-climb.** `CALIBRATE_START = 4` (`src/governor.py:145`), `STEP_SAMPLES = 3` (`:79`), `CLIMB_FAST_FACTOR = 3.0` (`:96`); each resize goes through `POOL.set_max_workers` at `server.py:2381`. Reconstructed capacity step-function (anchored on the arnis-exit mtime of the cell named in the `MERGE` line that follows each `[Governor] N → M` line in `ab-results/server-B.log`):

  `t+0.0 → 4w · t+11.8 → 6w · t+25.5 → 12w · t+48.8 → 20w · t+77.4 → 16w · t+114.2 → 14w · t+153.7 → 16w`

  Measured first-job offsets confirm it exactly: `0×4, 11.8×2, 25.5×6, 48.8×6, 48.9×2`. The governor spends **48.8 s (29% of the run) below 16 workers**, and that is 66% of its idle.

* **warm start removes almost all of it.** Warm run offsets: `0.0×14`, i.e. 14 workers live at t=0 (`_warm_start`, `src/governor.py:471-504`; log: `[Governor] warm start 14 workers from history 1:1/4`). Cold 171.00 s → warm 160.30 s = **−10.70 s, measured, not simulated**.

---

## 2. The tail

| run | last dispatch | tail length | share of E | cells in flight | last cell to finish |
|---|---|---|---|---|---|
| baseline | t+144.91 | **30.03 s** | 17.2% | 16 | `-4,1,4`, 36.16 s, dispatched t+138.8 (index 78/81) |
| governor cold | t+140.24 | **30.76 s** | 18.0% | 14 | `-4,2,4`, 35.85 s, dispatched t+135.1 (index 77/81) |
| governor warm | t+122.37 | **37.93 s** | 23.7% | 20 | dispatched t+122.4 by a worker that had just been spawned |

Remaining seconds of each in-flight cell at last dispatch:
* baseline: `5.1 7.3 8.0 13.1 13.9 13.9 14.6 16.1 18.9 20.3 20.5 21.4 22.2 22.9 23.4 30.0`
* governor cold: `3.7 3.9 4.1 4.7 5.1 7.3 7.5 13.5 13.7 16.2 20.3 20.5 28.3 30.8`

So the tail is **not one monster cell running alone**. It is 14–16 cells all mid-flight when the queue empties, draining over half a minute. The structural cause is *cell granularity*: 81 / 16 = 5.06 cells per worker. Per-worker cell counts in the baseline are `6,6,6,6,5,6,5,5,5,4,5,4,4,5,5,4`, and per-worker busy time ranges **136.1 s → 164.6 s** — a 28.5 s spread produced entirely by whether a worker's last draw was a 23 s cell or a 49 s cell.

### Do the slow cells land last? Yes — but far less than the raw numbers suggest

Raw first-20 vs last-20 by dispatch order:

| run | first 20 (median / mean) | last 20 (median / mean) |
|---|---|---|
| baseline | 24.9 / 23.7 | 33.3 / 33.8 |
| governor cold | 14.1 / 16.7 | 28.4 / 28.8 |
| governor warm | 24.5 / 25.1 | 28.1 / 29.7 |

**That comparison is confounded and I do not trust it.** Duration is strongly a function of how many peers were running:

* Pearson(duration, mean concurrency during that cell): baseline **+0.500**, governor cold **+0.706**, governor warm −0.117.
* Governor cold, by concurrency quartile: conc 3.9–11.9 → median 14.1 s; conc 17.2–20.0 → median 31.5 s. The "fast first 20" is mostly the CALIBRATE ramp handing those cells 4–6-way contention instead of 16–20-way.

**Unconfounded measurement.** The baseline held concurrency at exactly 16 from t+16.1 to t+144.9. 48 cells lie entirely inside that window:

* first half (dispatch idx 17–40) median **29.13 s**, second half (idx 41–65) median **31.21 s** → a **+7%** drift, not +40%.
* spread collapses: CV falls from **0.244** (all 81) to **0.160** (constant-16 window). min 23.0 / p25 27.1 / median 30.1 / p75 33.6 / max 49.1, stdev 4.9.
* by spiral ring (`server.py:4610-4613` uses Chebyshev ring then angle): ring 2 → 29.80, ring 3 → 29.40, ring 4 → 31.30. Essentially flat.

The one real effect is at the very centre: ring 0–1 (**9 cells of 81**) median 14.62 / 16.80 s (baseline) and 11.52 / 12.10 s (governor cold), versus ~29–32 s for rings 2–4. **Spiral order front-loads the nine cheapest cells in the grid and nothing else.** With `--no-buildings` in the command line (visible in every cell log), the dense core has the least to draw.

I could not find any pre-run feature that predicts duration inside the constant-16 window: Pearson(duration, `core built-up cells` from the land-cover repair line) = **−0.129**, vs OSM `unique element(s)` = **+0.256**, vs elevation range = **+0.106**. Built-up quartile medians are 29.51 / 29.84 / 31.58 / 29.50 — flat. Cross-run per-cell agreement is only moderate: Pearson **0.720**, Spearman **0.599** between the two runs' durations for the same cell.

---

## 3. Per-cell gap (finish one cell → start the next): spawn + merge + admission

Measured per worker as `started[i] − ended[i−1]`:

| run | n | median | mean | p95 | max | **total across the run** |
|---|---|---|---|---|---|---|
| baseline | 65 | **0.031 s** | 0.060 | **0.148 s** | 0.612 | **3.9 ws** |
| governor cold | 61 | **0.009 s** | 0.011 | **0.017 s** | 0.111 | **0.7 ws** |
| governor warm | 61 | 0.009 | — | 0.020 | **26.05** | 42.68 ws |

The baseline's 3.9 ws total **is exactly its whole mid-run idle** (3.9 ws in the ledger above). The accounting closes to the last decimal.

**Spawn + merge + admission overhead is 0.1–1.0% of the idle. It is not where the 21% goes.**

* Admission never blocked: `grep -c "admitted on timeout" server-B.log` = **0**, `ram_peak` 52%, and `_gate` is RAM-only by design (`src/governor.py:602-616`). `ADMIT_TIMEOUT_S = 3.0` (`:139`) never came near firing.
* **Merge is effectively free.** From the log-mtime split: baseline merge+prune total **6.54 s** across 81 cells (median 0.073, p95 0.147, max 0.216); governor cold **4.60 s** (median 0.055, p95 0.074, max 0.154); warm **4.24 s**. Independent confirmation from the cs8 baseline (64 canonical regions per cell instead of 16): median **0.179 s**, max 1.324 — 2.45× the cs4 cost for 4× the files, i.e. it scales with file count and is page-cache bound. `merge.py:199-204` is a `shutil.copy2` loop over ~16 files that arnis wrote seconds earlier.
* The warm run's `max 26.05 s` gap is **not** overhead — it is the governor shrinking 16→14 and re-growing. Workers 14 and 15 sat alive-but-jobless `t+69.2→95.2` (26.05 s) and `t+79.1→95.2` (16.06 s) = **42.1 ws burned by a mid-run shrink/re-grow oscillation**. (`set_max_workers`/`_ensure_workers_locked`, `src/workers.py:176-225`.)

---

## 4. Structurally unavoidable vs recoverable

Model validation first, so the counterfactuals mean something. Frozen-duration list-scheduling replay:

* baseline, capacity = the measured 2 s stagger schedule → **174.62 s** vs measured **174.94 s** (0.2% error).
* governor cold, capacity = the reconstructed 4/6/12/20/16/14/16 schedule → **174.95 s** vs measured **171.00 s** (2.3% error).

The model is good. Now the decomposition of the governor cold run's **561.3 ws (20.5%)**:

| bucket | ws | % of idle | recoverable? |
|---|---|---|---|
| CALIBRATE ramp (48.8 s below 16 workers) | 372.0 | 66% | **Yes, and already demonstrated** — warm start removes it: 171.00 → 160.30 s measured |
| middle | −123.4 | — | Nothing to recover; CPU was pinned at 100% in every bucket t+65.6→145.6 |
| tail (drain of 14 in-flight cells) | 312.8 | 56% | **Partly.** Ordering can shorten it; it can never be zero |
| gaps (spawn + merge + admission) | 0.7 | 0.1% | Nothing to recover |

And the baseline's **397.3 ws (14.2%)**: 184.7 stagger (recoverable outright), 208.7 tail (partly), 3.9 gaps (no).

**The middle is CPU-bound, not slot-bound**, and this is the load-bearing constraint. Per 20 s bucket, unused core-seconds on 24 cores (from `timeline[].cpu`, which is `psutil.cpu_percent(interval=1.0)` machine-wide — `server.py:5962-5974`):

| run | core-s available | unused | of which ramp | of which tail | of which middle |
|---|---|---|---|---|---|
| baseline | 4198.6 | **413** (9.8%) | 224 | 189 | **0** |
| governor cold | 4104.0 | **627** (15.3%) | 265 | 329 | **~34** |
| governor warm | 3847.0 | **452** (11.7%) | ~0 | ~452 | ~0 |

Six consecutive 20 s buckets at 100% in the baseline, four at 100% in the governor. There is **no core headroom in the middle of either run** — adding workers there cannot help, which is also why the `m=18` / `m=20` simulations below are not credible.

**Structural floor on the tail.** With a work-conserving queue, `E ≥ (time the last cell starts) + (its duration)`. All 16 slots were busy until the queue emptied, so `E ≥ Σd/m` and the excess is bounded by roughly one cell-time. 81 cells on 16 slots is 5.06 waves; the granularity penalty is ~1/5 of a cell ≈ 6 s at best. LPT leaves `16 × 156.97 − 2401.7 = 109.8 ws = 4.4%` idle on the baseline set and `16 × 141.40 − 2174.6 = 87.8 ws = 3.9%` on the governor set. **≈4% of worker-time is irreducible at cs4/16w; the other ~16 points are ramp + ordering.**

---

## 5. The honest ceiling

Ordering simulations, m = 16, durations frozen from each run. Caveat stated up front: **durations are not schedule-invariant** (Pearson 0.50–0.71 against concurrency), so any counterfactual that raises early-run concurrency will lengthen the cells it moves there. The **baseline** set is the only internally consistent one for a 16-worker-throughout counterfactual, because it is the only run that actually held 16 workers throughout.

**Baseline arm — the defensible arithmetic:**

```
measured                                              174.94 s
 − remove the 2 s stagger (16 slots from t=0)          -9.19  ->  165.75 s
 − oracle longest-processing-time-first                -8.78  ->  156.97 s
 − move merge off the worker                           -0.48  ->  156.49 s
partition lower bound  Σd/16 = 2401.7/16                          150.10 s
CPU-conservation floor 3785 cpu-s / 24 cores                      157.66 s   <-- binds
```

Two independent methods — frozen-duration LPT and conservation of measured CPU work — land within **0.7 s** of each other at **≈157 s**. That is the ceiling: **174.94 → ~157 s = 1.11×.**

**Governor arm:**

```
cold measured                                          171.00 s
 − warm start (measured, real run)                    -10.70  ->  160.30 s
 − LPT oracle on the warm duration set, m=16           -7.42  ->  152.88 s
 − LPT from a PRIOR run's durations (realistic)       -11.41  ->  148.89 s
CPU-conservation floor, warm arm 3395/24                          141.5 s
CPU-conservation floor, cold arm 3477/24                          144.9 s
```

**≈145–153 s, i.e. 171 → ~148 s = 1.15×**, of which 10.7 s is already banked by warm start today.

*(On the governor-cold duration set LPT looks worth 11.29 s and outer-ring-first 9.68 s — ignore those. Those durations were measured at 4–6-way concurrency for the first 49 s and are not transferable. On the warm set, history-LPT beats oracle-LPT by 4 s, which is the frozen-duration model's noise floor showing; treat anything under ~5 s in these sims as noise.)*

**Realistic ordering heuristics, m = 16, on the baseline set** (vs the 165.75 s no-stagger spiral baseline):

| order | makespan | vs spiral |
|---|---|---|
| spiral (today, `server.py:4610-4613`) | 165.75 s | — |
| outer-ring-first (no history needed) | 168.94 s | **+3.19 s worse** |
| LPT seeded from a prior run's durations | 170.08 s | **+4.33 s worse** |
| LPT oracle (unattainable) | 156.97 s | −8.78 s |

**This is the finding that contradicts the task's hypothesis.** "The slowest cells land last, so longest-first is a free win" is not supported. Under constant contention the cells are nearly homogeneous (CV 0.160, medians flat across rings and across built-up quartiles), so there is very little to sort; and the only predictor available in advance — the previous run's own durations, Spearman 0.599 — is *worse than spiral* on the baseline set. LPT is worth ~5% **only with an oracle**, which does not exist.

**Merge off the worker is worth 0.3–0.5 s on a 160–175 s run (0.2–0.3%).** Total merge cost is 4.24–6.54 worker-seconds per 81-cell run. Do not build for it.

---

## Ranked, by measured worker-seconds

1. **Kill the ramp** — 372 ws (governor) / 184.7 ws (baseline). The governor's warm start already does this and is worth a measured **−10.7 s (1.07×)**. Making it the default for a repeat bucket, and shortening CALIBRATE for grids well above `SMALL_GRID_CELLS = 32` (`src/governor.py:86`), is the single largest lever. On the baseline path, `cpu_stagger_seconds` should scale with worker count rather than being a flat 2 s × min(id, 8) (`src/workers.py:36-37`, `server.py:4278`).
2. **Stop the mid-run oscillation** — 42.1 ws measured in the warm run alone (workers 14/15 idle for 26.05 s and 16.06 s across a 16→14→16 bounce). Root cause is visible in the log: `_rate_tp` (`src/governor.py:701-716`) computes `(n−1)·60/(stamps[-1]−stamps[0])` over only `STEP_SAMPLES = 3` completions; cells that started together finish together, so the span collapses and the estimate explodes. `server-B.log` records gains of **−75.0, −126.2, −266.5 cells/min** against a true rate of ~30 — those are the numbers the state machine acted on. Also: the warm run grew 16→20 at **t+122.4**, four seconds before the queue emptied; workers 16–19 each took exactly one cell and one of them (37.9 s) set the run's end time. Never grow when `queue_size < workers`.
3. **Tail** — ~209–313 ws, ~30 s in every run, ~18% of wall. Reachable only through ordering, and ordering is worth ~5% with an oracle and *negative* with the predictors that actually exist. Realistic lever: make the last wave finer-grained (split the final ~m cells, or plan a mixed cell size) rather than reorder a homogeneous set.
4. **Merge / spawn / admission** — 0.7–6.5 ws. Nothing here.

## Corrections to the premises I was handed

* **"Theoretical ceiling ~52 cells/min, floor 93.5 s"** — this multiplies 27.7 cpu-s from the **Bucharest centre cell `0,0`**, which is one of the nine cheapest cells in the grid: 12.38 s in the governor run against a 27.16 s run median, and ring 0–1 medians are 11.5–16.8 s against 29–32 s for rings 2–4. The machine-level integral says these runs consumed **≥3395–3785 cpu-s**, i.e. 51–69% more than the 2244 assumed. The correct CPU floor is **141.5–157.7 s**, not 93.5 s, and the ceiling is ~26–30 cells/min, not 52.
* **"~1.7× is available before touching per-cell CPU work"** — no. Only ~1.11–1.18× is available from scheduling. Going past that requires reducing per-cell CPU work (the serial `parse_osm_data` front, the 23% parallel-overhead tax at high thread counts), which is a different lever.
* **"Killing a merge corrupts .mca, so the merge blocks a worker slot"** — true as written, but the block is 55–73 **milliseconds**.

## What I could not determine from this data

* **No per-phase attribution.** `ARNIS_PHASE_MARKERS` was not set for these runs (`server.py:2758-2760`); no phase markers appear in any of the 243 cell logs. I cannot split the 2170–2401 worker-seconds into fetch/elevation/parse/place/post/save from these reports.
* **arnis spawn cost is not separable.** Meld writes `RUN …` before spawning and the child's first output carries no timestamp, so process-creation time is folded into my "arnis" figure. It is bounded above by the 9–31 ms inter-cell gap only in the sense that the gap excludes it — I have no direct measurement.
* **cs8 governor**: its report was overwritten by the harness reuse bug. I can only cite the ground-truth 239.4 s / 5 workers.
* **CPU integrals are subsampled.** `timeline[].cpu` averages the ~4 one-second `cpu_percent` samples that land in each 20 s bucket (harness polls `/api/status` every 5 s, `ab_bucharest.py:198-234`), and `cpu_percent` clamps at 100%, so 3395/3477/3785 cpu-s are **floors** on demand, not exact totals.
* One reporting caveat that did not bite here but would on a RAM-tight machine: `src/workers.py:240` sets `running=True` *before* `admit_cb` is called at `:251`, so a worker parked at the gate is counted in `get_states()` and therefore in `timeline[].active` and `workers_peak`, and in the `active` peer count passed to the governor at `:248`. In these runs the gate never fired, so no distortion.

Analysis scripts (read-only, scratchpad, no project files touched): `C:/Users/LEGION/AppData/Local/Temp/claude/c--Users-LEGION-Documents-Meld/a793c437-9c66-47a5-aa18-f44730dbae88/scratchpad/idle/an.py` … `an6.py`.