# Phase 2 Plan - branch perf/speed-to-worldgen-phase2

Repo roots: `C:\Users\LEGION\Documents\Meld\arnis-triagefix\` and `C:\Users\LEGION\Documents\Meld\meld-triagefix\`. All paths in tables are relative to those roots.

---

## Where the time actually goes (the measured case for phase 2)

**Correction first, because the phase-1 headroom figure is wrong and it changes the whole plan.**

The ground truth states "2244 cpu-seconds, a 93.5 s floor, 58% efficiency, ~1.7x available before touching per-cell CPU work." That arithmetic multiplied cell `(0,0)`'s measured 27.7 cpu-s by 81 cells. Cell `(0,0)` is not the Bucharest centre - with the site origin at `44.5072, 25.96` it is the NW corner cell, it covers exactly one z11 tile (`1171_740`, 18.8 MB, the smallest of the four), and it ran **12.4 s against a 27.2 s run median**. Ring 0-1 cells (9 of 81) run 11.5-16.8 s; rings 2-4 run 29-32 s.

The machine-level integral from `timeline[].cpu` in the real reports says the runs consumed **≥3395-3785 cpu-seconds**, 51-69% more than assumed. Corrected:

| | claimed in ground truth | measured |
|---|---|---|
| CPU demand, 81-cell cs4 run | 2244 cpu-s | **3395-3785 cpu-s** (floor, `cpu_percent` clamps at 100) |
| CPU-conservation floor on 24 cores | 93.5 s | **141.5-157.7 s** |
| Warm run efficiency against that floor | 58% | **~88%** (160.3 s vs 141.5 s) |
| Headroom from scheduling alone | ~1.7x | **~1.11-1.15x** |

**Read that integral for what it is.** `timeline[]` holds **10 samples at 20 s**, each a mean of ~4 `cpu_percent` readings clamped at 100, and the mid-run buckets are pinned at 100. With cores pinned, the integral collapses to ≈ elapsed x 24 - it is a *floor on demand* and it is **not an instrument independent of wall time**. Measuring the cpu-second claim itself needs per-process CPU time, which is task I6 below.

**The machine is already ~88% efficient. Phase 2 cannot raise efficiency; it can only lower the floor by deleting cpu-seconds.** Three independent measurements agree the middle of the run has zero core headroom - six consecutive 20 s buckets at 100% CPU in the baseline, four in the governor run - so any change that frees a worker slot or shaves a hand-off mid-run converts to exactly nothing.

**Where the deletable cpu-seconds are, ranked by verified size:**

| block | measured / derived | evidence | verdict |
|---|---|---|---|
| arnis writes 36 region files per cs4 cell, Meld deletes 20 | **200-500 core-s/run** (band, not a point) | `java.rs:154-157` filters on the **seam-expanded** `xzbbox` (regions -1..4 = 6x6 = 36); `merge.py:157-159` keeps only `coords.canonical_region_bounds` (4x4 = 16). 20,480 of 36,864 chunk slots serialised, zlib'd, written, then `rmtree`'d. The old point figure (~340 core-s) stacked three unverified layers: a **two-point Amdahl fit** the source map itself labels "not a measurement", a 43% weighting assumption that holds **only with `--bake-lighting`**, and the **cheapest cell in the grid** as the per-cell unit. I4 re-derives it on a ring-3 cell. | **Take it** |
| OSM JSON re-deserialisation | **441 cpu-s/run**, 427 of it waste; honest recoverable band **80-150 cpu-s** | 8.20 GB decoded per 81-cell run against 262 MB distinct on disk = **31.3x**. `osm_parser.rs:149` uses `Deserializer::from_reader` (serde_json's `IoRead` slow path); `:159` clones `el.r#type` (a `String`) per element, 67.6 M times. | **Take it, but not blind - now HOLD** |
| Built-up elevation Gaussian | **derived** 3.1-4.6 core-s/cell, unmeasured | 2049² grid x 183 taps x 2 passes x 2 blurs = 3.073 G taps/cell, content-independent. `Vec<Vec<f64>>` with a per-column strided gather across 2049 separate allocations and a 4.2 M-write serial scatter outside rayon. The instrument that would measure it does not reach a Meld-driven run today (see W0 blind spot 6). | **Measure, then take the bit-exact part** |
| Merge + prune + health + meta | **4.24-6.54 worker-s/run = 0.27%** | Recovered from cell-log mtimes across three real reports: median 0.055-0.073 s, p95 0.147, max 0.216. `merge.py` imports only `math/re/shutil/threading/Path` - no NBT, no zlib, no fsync. | **Not a target. Do not build for it.** |
| Scheduler idle | 20.5% governor / 14.2% baseline | 66% ramp, 56% tail (they over-sum; the middle ran *above* 16). Spawn+admission gap total: **0.7-3.9 worker-seconds per run.** | **Worth 0-2 s of wall. Take the two cheap guards for stability, not throughput.** |

**Bottom line:** phase 2 is a cpu-second-deletion phase. Roughly **300-685 cpu-s (9-20% of measured demand)** is available from work that is provably discarded, and it is available *before* touching anything approximate. The band is wide because the largest term is a derived estimate resting on a two-point fit, a lighting-dependent weighting, and the cheapest cell in the grid.

---

## What phase 1 already took (so phase 2 does not double-count it)

| phase-1 deliverable | measured effect | what phase 2 must NOT re-claim |
|---|---|---|
| Adaptive governor (worker count) | cs4 174.9 -> 171.0 s (1.02x); cs8 257.5 -> 239.4 s (1.08x, 5 workers); RAM 82% -> 52% / 93% -> 42% | Worker-count tuning. The governor settles at 16 on cs4 - the same point a human picked - and 16 x 1.46 cores/cell = 23.4 of 24 cores. There is no worker count left to find. |
| **Warm start (`_warm_start`, 14 workers at t=0)** | **171.0 -> 160.3 s = -10.7 s** | **This is the single largest double-count risk in the whole set.** The warm number is already the phase-1 baseline. Machine-scoping `governor_history` (W5/E1) helps only the *first run in a new project*; against the warm reference it is worth ~0. It is measured against the **cold** arm only. |
| Admission gate replaces the fixed stagger | The legacy 2 s x min(id,8) stagger (184.7 worker-s) applies only when `admit_cb` is unset (`workers.py:261-268`), so the governor path never pays it | The ramp on the governor arm. What remains there is CALIBRATE climb time, and warm start already removes most of it. |
| arnis stdout protocol v1 (`[meld] v=1 phase=done wall_s= gpu_ms=`), phase markers, deterministic fill budget | Governor gets generator wall time; `gpu_ms` already feeds `occupancy.py`'s GPU budget | The plumbing. It exists; phase 2 extends the marker set and adds `cpu_s=` (I6), it does not build the channel. |
| Merge investigation (this phase's own research) | Merge measured at 0.27% of worker-time | Any wall-clock claim from moving, parallelising, or accelerating the merge. |
| The stated "58% of CPU floor" | Was computed against a wrong 2244 cpu-s denominator | The missing 42%. It does not exist - the real figure is ~88%. |

---

## Workstreams

### W0 - Measurement and harness (prerequisite; delivers 0 seconds, gates everything else)

**Mechanism.** Six blind spots make every remaining claim unfalsifiable, and one harness bug makes them unharvestable.

1. `ab_bucharest.py:302-306` calls `do_run(reuse=True)`, which skips `prepare_project()` and therefore never calls `/api/projects/switch`. The warm cs4 re-run rendered into whatever project was active. Confirmed first-hand: `ab-perf-governor-cs8/meld-report.json` holds 106 cells, 81 with durations, `cell_size: 4`, `elapsed_s: 160.3` - a cs4 warm run written over the cs8 governor report, which is why that arm can be cited but not recomputed, and why `harvest()` wrote `B-cs4-warm.json` with `"error": "no fresh meld-report.json produced"`.
2. `bench/matrix.json` declares `buildings:true, overture:true, interior:true, bake_lighting:false, region_format:"anvil"`; the real command line (verified in `ab-perf-governor-cs4/logs/cell--1_-1_4.log`) is `--no-buildings --interior false --bake-lighting --region-format blinear --blinear-level 6`. That is **five mismatches, not three**, and one of them - `bake_lighting` - decides W2's headline weighting.
3. The `parse` marker spans `main.rs:494 -> data_processing.rs:579` - it covers `parse_osm_data`, the priority sort, three land-cover overrides, `transform_map`, height profile, `WorldEditor` construction and the whole `precompute` block including two rayon islands. `place` likewise covers placement **plus** the strictly single-threaded tile-merge loop at `data_processing.rs:861-936`.
4. Nothing separates arnis wall from merge time. The `MERGE` log line prints counts only; `duration_s` is whole-slot time; the governor's `wall_s` stops at arnis exit.
5. `bench.mark("elev_landcover_repair")` is one label covering `level_water_surfaces` + `reclassify` + **both** Gaussians + the coastal pull.
6. **The `bench.mark` channel is not reachable from Meld.** Those labels only exist when arnis runs with `--benchmark`, and `grep -rn "\-\-benchmark" src/ server.py bench/` in meld-triagefix returns **nothing**. Every `bench.mark`-based number in this plan is unobtainable until I0 lands.

Two gates are also vacuous on the arms actually run - `region_hashes()` globs `*.mca` while the arms produce `r.X.Z.b_linear`, and `golden_hash.sh` never rebuilds - so H3 and M4a are part of this workstream, not afterthoughts.

**Files touched.** `meld-triagefix/bench/ab_bucharest.py`, `bench/matrix.json`, `bench/bench_scheduler.py` (`region_hashes` ~864-880), `meld-triagefix/server.py` (`_runner` tail ~2839-2926), `meld-triagefix/src/runreport.py`, `arnis-triagefix/src/main.rs`, `src/data_processing.rs`, `src/world_editor/mod.rs`, `src/world_editor/java.rs`, `src/elevation/postprocess.rs`, `src/elevation/mod.rs`, `arnis-triagefix/scripts/golden_hash.sh`, `meld-triagefix/src/arnis_cmd.py`, `meld-triagefix/docs/generation-performance.md`.

**Why it matters.** It removes no cpu-seconds and no idle. It is here because W2's headline number was sized on a code path the benchmark does not exercise, W4's entire budget is a derived estimate behind a telemetry channel Meld never opens, two of the eight required gates currently pass without looking at anything, and no A/B in this plan can be harvested until H1 lands.

| id | task | files | confidence % | agent-h | test-h |
|---|---|---|---|---|---|
| H1 | Fix `do_run(reuse=True)` skipping `prepare_project()`/`/api/projects/switch`; make `harvest()` fail loudly on a report whose `cell_size` or cell count disagrees with the group | `bench/ab_bucharest.py` | 92 | 1 | 1 |
| H2 | **Edit `matrix.json` to match the measured arms - never the reverse.** Declare `buildings:false, interior:false, bake_lighting:true, region_format:"blinear", blinear_level:6`, and have the harness **assert** the live `/api/settings` match on all five keys plus `overture` and abort on mismatch. Also bump the report to `meld-run-report/4`: add `summary.cells_per_min`, `config.stream_to_disk`, a non-null `config.region_format`, and the four I1 timers | `bench/matrix.json`, `bench/ab_bucharest.py`, `bench/bench_scheduler.py`, `src/runreport.py` | 87 | 4 | 3 |
| M4a | `scripts/golden_hash.sh` runs `cargo build --release` before hashing - today it never rebuilds, so G1 can validate a stale binary | `scripts/golden_hash.sh` | 92 | 1 | 1 |
| I1 | Four monotonic timers in `_runner`; extend the `MERGE` line to `..., merge Xs prune Ys health Zs meta Ws` **and** emit the four sums into the run report under schema/4, so N6 is harvestable rather than log-only | `server.py` (~2839-2926), `src/runreport.py` | 92 | 3 | 2 |
| I5 | On one cell under the real benchmark config, count and log how many regions leave via `flush_region_via` (`mod.rs:437`) versus `save_java` (`java.rs:190`). B1 and B2 draw on **one pool**; this is the only thing that says which of them holds the money | `src/world_editor/mod.rs`, `src/world_editor/java.rs` | 88 | 1 | 1 |
| I4 | Re-profile **one non-centre cell and one ring-3 cell** with `ARNIS_PHASE_MARKERS=1` under the *real* benchmark config, record the stream-to-disk state explicitly, and re-derive the 200-500 core-s region band off the ring-3 cell rather than the cheapest cell | measurement only | 90 | 4 | 2 |
| M5 | Update `docs/generation-performance.md:622-626` ("the governor measures generator wall time and does not see [the merge]") for what I1, I5 and I6 change about what is measured | `docs/generation-performance.md` | 90 | 2 | 1 |
| I0 | **Plumb the telemetry channel that I2/I3 assume exists**: convert `element_placement`, `tile_merge` and the new `elev_builtup_gaussian` to `meld_telemetry::phase` markers under the existing `ARNIS_PHASE_MARKERS` gate (preferred), or add a Meld setting/env that appends `--benchmark` | `src/main.rs`, `src/data_processing.rs`, `src/elevation/mod.rs`, `src/arnis_cmd.py` | 82 | 3 | 2 |
| H3 | Gate-instrument repair: extend `region_hashes()`'s glob to `("*.mca","*.b_linear")`, hash `.b_linear` whole (no timestamp table) and `.mca` with `[4096:8192]` zeroed, **fail loudly on an empty dict**, and add a decoded-chunk-payload comparator for the eviction arm | `bench/bench_scheduler.py` | 80 | 4 | 3 |
| I2 | Split the `parse` marker (`main.rs:504`, after the override block, around `precompute`); surface `element_placement` / `tile_merge` through the marker reader | `src/main.rs`, `src/data_processing.rs`, `src/arnis_cmd.py` | 78 | 4 | 2 |
| I3 | `bench.mark` pair around `smooth_built_up_gaussian` (`postprocess.rs:208-214`) - the go/no-go number for W4 | `src/elevation/postprocess.rs`, `src/elevation/mod.rs` | 78 | 2 | 1 |
| I6 | Extend the done line to `[meld] v=1 phase=done wall_s= gpu_ms= cpu_s=` using `GetProcessTimes`, and sum it per run into the report - the only instrument that can test the cpu-second claim independently of wall time | `src/main.rs`, `src/arnis_cmd.py`, `src/runreport.py` | 84 | 2 | 2 |

I2 and I3 are lowered from 88/86 to 78 because the `bench.mark` channel they read is not wired into any Meld-driven run and their new dependency I0 has not landed.

---

### W1 - Delete the cheap half of the OSM decode cost

**Mechanism.** `from_tile_dir` (`osm_parser.rs:122-174`) is a plain nested `for x { for y {` with no rayon, doing `serde_json::Deserializer::from_reader(BufReader::new(file))` - the `IoRead` path, which forfeits every slice fast path - into `OsmElement { Option<HashMap<String,String>> tags, Option<Vec<u64>> nodes, Vec<OsmMember> }`. Then the dedup at `:159` does `seen.insert((el.r#type.clone(), el.id))`, one `String` allocation per element.

Read the file into a `Vec<u8>` and deserialise from the slice; key the `HashSet` on a compact discriminant instead of a cloned `String`. Apply the same fix to `retrieve_data.rs:146` and `:171` so the `--file` and Overpass paths benefit too. **Two hazards ride with this task and must be in the diff, not in the discussion:**

1. **Use `serde_json::Deserializer::from_slice(&buf)` + `OsmData::deserialize(&mut de)`, never `serde_json::from_slice`.** Today's code is `Deserializer::from_reader(...)` + `OsmData::deserialize(&mut de)` and **never calls `end()`**, so a tile with trailing bytes after the closing brace parses fine. `serde_json::from_slice` *does* call `end()`; it would return `Err`, the handler at `:151-155` would print `skip unreadable tile` and `continue`, and 150k-1.2M elements would silently vanish from the world. The replacement must keep the same non-terminating semantics.
2. **The dedup key must not collapse unknown types into one `other` bucket.** `seen.insert((el.r#type.clone(), el.id))` distinguishes every distinct string; a shared `other` discriminant makes `("foo",1)` and `("bar",1)` alias and drops the second. The four cached tiles carry only node/way/relation at element level, so this is latent rather than live - which is exactly what makes it a footgun. Use four variants where the fourth is `(hash(type), id)`, or keep a side `HashSet<(String,u64)>` for the non-canonical tail.
3. **RSS is behaviour.** `from_slice` requires the whole file resident: 16 workers x 147,837,444 bytes (`osm_g1_z11_1172_741.json`, confirmed on disk) = 2.4 GB worst case, and N4 fails the arm on any `ram_peak` rise. The size-threshold `from_reader` fallback (>64 MB) ships **in the same commit**, not as a contingency.

**Files touched.** `arnis-triagefix/src/osm_parser.rs:149-162`, `src/retrieve_data.rs:146,171`.

**Why it raises the ceiling.** Measured 18.6 MB/s single-threaded on 8.20 GB per run = 441 cpu-s, ~11% of the machine's whole capacity, 97% of it re-decoding four files. This task does not touch the 31.3x duplication - **one arnis.exe per cell means all 90 tile reads still happen** - it only makes each of the 90 decodes cheaper. Honest band: 80-150 cpu-s. It is also the measurement that prices the deferred sidecar.

| id | task | files | confidence % | agent-h | test-h |
|---|---|---|---|---|---|
| A1 | `Deserializer::from_slice` (**not** `serde_json::from_slice` - no `end()`, per hazard 1); non-aliasing compact dedup key (hazard 2); >64 MB `from_reader` fallback in the same commit (hazard 3); same in `retrieve_data.rs` | `src/osm_parser.rs`, `src/retrieve_data.rs` | 84 | 4 | 3 |

A1 is lowered from 88 to 84 (and therefore HOLD) because `from_slice` is **not** acceptance-identical to today's non-terminating deserializer, the compressed dedup key can silently alias unknown element types, and the RSS fallback adds a third change to what was sold as a one-line swap.

---

### W2 - Stop writing the region files Meld deletes (the headline lever)

**Mechanism.** For a cs4 cell at 1:1, `coords.py:165` gives 4 x 512 = 2048 blocks, then `expand_bbox_for_seam` with `seam_buffer_chunks: 8` adds 128 blocks per side -> arnis receives `[-128, 2176)`. `java.rs:154-157` computes `(-128).div_euclid(512) = -1` and `2176.div_euclid(512) = 4`, so **36 region files are written**. `merge.py:157-159` keeps only `canonical_region_bounds` = 4x4 = **16**, and `server.py:2907-2912` then `rmtree`s the whole cell world.

Add `--canonical-regions minX,minZ,maxX,maxZ` and intersect it into the filter that already exists. Meld emits it from `coords.canonical_region_bounds(cell_key)` - **the same function `merge.py` uses to decide what to keep**, so the written set *is* the kept set by construction. Absent, behaviour is bit-identical to today, which is what keeps `golden_hash.sh`, the `--file` path and standalone arnis untouched.

**The split, one pool, and why it matters.** The ground-truth A/B ran **blinear + stream-to-disk**, so most regions leave through `flush_region_via` (`mod.rs:437`, called from `data_processing.rs:910` inside the `place` span), **not** `save_java`. The discarded work leaves through one path **or** the other, never both - B1 and B2 are additive on the page but draw on a single pool, and the split is **unmeasured until I5 reports**. Until then, any target row that banks the region saving may be banking money that sits entirely behind the 68%-confidence task:

- **B1** patches `save_java`'s filter (`java.rs:154-192`) and the `total_regions` progress count. Easy, byte-identical, but on the streaming benchmark it plausibly moves little - an assertion I5 either confirms or kills.
- **B2** patches `flush_region_via`. It must still call `world.regions.remove()` and `flushed_regions.insert()` **at the identical instant** and skip only `worker.send()`. Getting that ordering wrong changes peak RSS, which feeds `should_stream_to_disk`, which changes which regions get evicted, which changes output - silently.

**The 43% weighting is conditional on `--bake-lighting`.** It values the 16,128 discarded base chunks at 0.5, defensible only because the measured arm baked lighting. At `bake_lighting:false` those base chunks are near-free and the discarded share falls toward the content-chunk floor of **4,352/20,736 = 21%** (≈29% at a generous 0.15 weight). H2 keeps the arms on `--bake-lighting`, so 43% stands for the benchmark - but it is a config-dependent number and must always be quoted as one.

Two open items verified in favour: `merge.py:170-186`'s drift guard uses `buffer_regions = ceil(8/32)+1 = 2`, so canonical-only writes pass with two regions of margin; and `_scan_missing_regions` runs `finalcheck.find_missing_regions` over the **master** world, not the cell world, so an absent seam ring is invisible to it (confirm on a real run anyway).

**Files touched.** `arnis-triagefix/src/args.rs`, `src/world_editor/java.rs:154-192`, `src/world_editor/mod.rs:437`, `meld-triagefix/src/arnis_cmd.py`, `meld-triagefix/src/coords.py` (reuse only), `meld-triagefix/build.py`, `arnis-triagefix/tests/`, `meld-triagefix/tests/`.

**Why it raises the ceiling.** 20,480 of 36,864 chunk slots per cs4 cell are serialised, NBT-built, zlib/zstd-compressed, written and deleted: 4,352 content chunks plus 16,128 base chunks. Weighted at `--bake-lighting`, ~43% of the save-phase work = **200-500 core-s/run, 6-15% of measured demand** (band, not a point - see the three unverified layers noted above). At cs8 the ratio is 100 written / 64 kept = 36% discarded. Bonus, unmeasured: the per-cell prune drops from ~36 to ~16 region files.

| id | task | files | confidence % | agent-h | test-h |
|---|---|---|---|---|---|
| B1 | `--canonical-regions` flag; intersect into `save_java`'s region filter and `total_regions`; Meld emits it gated on the project setting (M1) and the bundled arnis version (M2) | `src/args.rs`, `src/world_editor/java.rs`, `src/arnis_cmd.py` | 84 | 4 | 3 |
| M2 | **The arnis version gate, named and costed rather than assumed inside B1's hours.** `arnis_cmd.arnis_version()` already exists (`src/arnis_cmd.py:202`, parses the banner's trailing `arnis 3.x.y`); add `MIN_ARNIS_CANONICAL_REGIONS`, the no-flag fallback path, and a test with an older-exe stub - an older `arnis.exe` **rejects** an unknown clap arg | `src/arnis_cmd.py` | 85 | 2 | 2 |
| M3 | Meld-side test of the invariant the whole design rests on: the emitted `--canonical-regions` equals `coords.canonical_region_bounds(cell_key)` for a sample of keys at cs4 **and** cs8. B3 is arnis-side only and cannot see this | `meld-triagefix/tests/` | 84 | 1 | 2 |
| B2 | Same filter in `flush_region_via`, skipping **only** `worker.send()` while `regions.remove()` + `flushed_regions.insert()` stay at the identical instant | `src/world_editor/mod.rs:437` | 68 | 6 | 6 |
| B3 | Fixture gate: `.b_linear` byte-compare for the streaming arms and `.mca` (with `[4096:8192]` zeroed) for the anvil arm, **decoded chunk payloads** for the eviction arm, with and without the flag, in **both** stream-to-disk states, at cs4 **and** cs8 under real RAM pressure. Depends on H3's comparator | `arnis-triagefix/tests/` | 82 | 3 | 6 |
| M4b | Release/migration for B1: `build.py` pulls arnis `releases/latest`, so an arnis tag + release must precede the Meld build; version bumps, changelog, bundle verify | `build.py`, release process | 84 | 3 | 2 |

---

### W3 - Correctness debt extracted from the merge investigation (delivers ~0 seconds; ships because it is cheap and one item is a live data-loss bug)

**Mechanism.** The merge investigation concluded that offloading the merge is worth 0.27% of worker-time in a window with zero core headroom - so **the merge stays on the worker thread and the offload is cut** (see W6). What the investigation *did* find is four live defects, all confirmable in source:

1. `server.py:2863` calls `master_world_path()` **inside the merge retry loop**. That goes through `PROJECT.settings()`/`load()` -> `project.py:403-409` `_read`, which **swallows every exception and returns the default**. Meanwhile `subworld_number` (`project.py:484-493`) rewrites `project.json` with a non-atomic `write_text` (`:413`) **on every cell**. A read landing in that window returns `master_world_dir = ""` -> `parent = PROJECT.root` -> **the cell is merged into the wrong folder**. 16 workers x (1 write + 2 reads) per cell, today, with no offload involved.
2. `write_world_meta`'s throttle guard (`server.py:2181`) is an unsynchronised read-then-write of `_META_WRITE["at"]` followed by a non-atomic `write_text`. Two workers in the same 20 s window both pass and both write - torn sidecar.
3. `_scan_cell_health` (`server.py:956`) does `read_text()` of the **entire** cell log with no tail limit, while `_record_fail` correctly tails to `[-6000:]` (`:998`). A cs8 cell log is megabytes.
4. The master's `level.dat` is copied from whichever cell merges **first** (`merge.py:227`, `if not dst_dat.exists()`), after that file has been patched to `"Meld Sub World N"` where N is assigned in merge order. If `gold_name` renaming ever fails, the master inherits an order-dependent world name.

**Atomic writes are not free on Windows, and the ordering matters.** CPython's `open()` on Windows shares read/write but **not delete**, so `os.replace` over a destination another thread holds open raises WinError 5/32. `master_world_path()` -> `PROJECT.settings()`/`load()` -> `_read` runs **without** `_LOCK` - the very hazard C2 targets - at 2 reads/cell x 16 workers. Today that window yields a defaulted read; after a naive C3a it yields an *exception* inside `subworld_number`. Therefore: **C2 lands before C3a**, and both C3a and C4 wrap `os.replace` in a bounded retry (5 x 20 ms) whose last attempt falls back to `write_text`. The same applies to `meld-world.json`, which the UI and the export path read.

**Files touched.** `meld-triagefix/server.py` (`_submit_cells` ~4618, `_runner` ~2846-2926, `write_world_meta` ~2171-2191, `_scan_cell_health` ~956), `meld-triagefix/src/project.py` (`_read` 403-409, `_write` 411-413), `meld-triagefix/src/merge.py` (224-234).

**Why it is here.** It removes neither cpu-seconds nor idle. It ships because C2+C3a together close a wrong-world-merge race for about five hours, and C6 removes the only order-dependent artifact in the master world - which W5's deferred reordering work would otherwise make load-bearing.

| id | task | files | confidence % | agent-h | test-h |
|---|---|---|---|---|---|
| C2 | Resolve the master world path **once per run** in `_submit_cells`, store it in the job dict, and have the merge read `job['master']`. **Must merge before C3a** | `server.py` | 88 | 2 | 2 |
| C5 | Tail `_scan_cell_health`'s log read to match `_record_fail`'s `[-6000:]` | `server.py` | 92 | 1 | 1 |
| C3a | `project.py:_write` becomes atomic (tmp + `os.replace`) **with a bounded 5 x 20 ms retry and a `write_text` last resort**, because `os.replace` over a file an unlocked `_read` holds open raises WinError 5/32 | `src/project.py` | 80 | 2 | 2 |
| C4 | `_META_WRITE` under a lock; `meld-world.json` written tmp + `os.replace` with the same retry/fallback - the UI and export both hold it open | `server.py` | 80 | 2 | 2 |
| C6 | Pin the master `level.dat` donor to the lexicographically lowest successfully-merged cell key; treat a failed `gold_name` rename as a merge failure; skip `subworld_number` + its gzip round-trip when `prune_cell_after_merge` is on | `src/merge.py`, `server.py`, `src/project.py` | 80 | 3 | 2 |
| C3b | `project.py:_read` distinguishes "missing" from "unreadable" instead of swallowing both | `src/project.py` | 76 | 2 | 2 |
| C7 | `overwrite_collisions` -> `False` with an explicit same-cell-rectangle allowance (so `/api/cell/regenerate` still works); add the **server-side** `job_size_regions` freeze once any cell reaches `merged` (today it is client-side only, `web/index.html:2125-2140`) | `server.py:2867`, `src/merge.py` | 74 | 3 | 3 |

C3a is lowered from 90 and C4 from 88 because an atomic replace converts today's benign defaulted read into a hard Windows sharing violation unless the retry and the C2-first ordering are both in place.

---

### W4 - Bit-exact elevation blur (CPU only; the GPU kernel is cut)

**Mechanism.** `gaussian_blur_grid_reported` (`postprocess.rs:941-1043`) operates on `Vec<Vec<f64>>`. The vertical pass materialises every column with `after_h.iter().map(|row| row[x]).collect()` at `:1008` - a 2049-deep gather across 2049 *separate heap allocations*, per column, per pass - then scatters `out[y][x] = v` at `:1034-1038` on the **calling thread**, 4.198 M f64 writes per blur, outside rayon. The blend loop in `smooth_built_up_gaussian` (`:901-920`) is a third 4.198 M-iteration serial `for`.

Flatten to a single `Vec<f64>` with a stride; transpose once with a cache-blocked tile transpose; run the vertical pass as a second horizontal pass; transpose back; parallelise the scatter and the blend (with `total_influenced` as a sum reduction). **Keep the tap loop verbatim** - same f64 accumulators, same left-to-right `for (j,&k) in kernel.iter().enumerate()` order, same `if v.is_finite()` guard, same `if wsum > 0.0 { sum/wsum } else { NAN }`. No branchless rewrite, no reassociation, no f32.

**Files touched.** `arnis-triagefix/src/elevation/postprocess.rs:837-1043`.

**Why it raises the ceiling - and an honest haircut.** The tap loop is *already* cache-local once the column is gathered, so this does **not** remove the 3.073 G taps. What it removes is 2049 `Vec` allocations per pass, 4.2 M pointer chases per gather pass, the ~0.1 s serial scatter and the serial blend. Realistic: **0.2-0.4 core-s/cell plus ~0.2 s of serial residue**, not the 0.95-1.5 core-s originally claimed. Fleet: 20-35 cpu-s. It ships **on, with no flag**, because it is bit-for-bit identical and `golden_hash.sh` 5/5 - rebuilt first, per M4a - is the proof that exemption grants.

**Conditional, and the chain is longer than it was.** Do not start until I3 reports, and I3 cannot report until I0 plumbs the channel. If the Gaussian comes back under 1.5 core-s, this task is a ~0.5% win and should be dropped.

| id | task | files | confidence % | agent-h | test-h |
|---|---|---|---|---|---|
| D2 | `f64::to_bits()` equality unit test, old vs new blur, on a fixed pseudo-random grid seeded with NaN holes | `arnis-triagefix/tests/` | 88 | 1 | 2 |
| D1 | Flat `Vec<f64>` + stride, one cache-blocked transpose between passes, parallelised scatter and blend; tap loop untouched | `src/elevation/postprocess.rs` | 80 | 6 | 4 |

---

### W5 - Scheduler idle (two guards only; the ordering lever is deferred)

**Mechanism.** The measured idle decomposes as: ramp 372 worker-s (66%), tail 313 worker-s (56%), gaps 0.7 worker-s (0.1%). Warm start already banked the ramp on the governor arm. What remains and is cheap:

- **`_rate_tp` has no span floor.** `governor.py:701-716` computes `(n-1) * 60 / (stamps[-1] - stamps[0])` over `STEP_SAMPLES = 3` completions. Cells that started together finish together, the span collapses, and `server-B.log` records step gains of **-75.0, -126.2 and -266.5 cells/min** against a true rate near 30. The state machine acted on those.
- **No guard against growing into an empty queue.** The warm run grew 16 -> 20 at t+122.4 with the queue emptying at t+122.37; workers 16-19 each took exactly one cell, and one of them ran 37.9 s and **set the run's end time**. `_total_cells` is used only for the `SMALL_GRID_CELLS` test.
- `governor_history` is project-scoped (`project.py:183`) and `bench_scheduler.py:16,701` gives every repeat a fresh project, so **no harness run has ever warm-started**. Machine-scoping it fixes the benchmark and the first-run-in-a-new-project case. It is worth ~0 against the warm reference; a repeat render of an existing project already warm-starts today.

**Files touched.** `meld-triagefix/src/governor.py` (`_rate_tp` ~701-716, `on_cell_complete`, `_warm_start` ~471-504), `meld-triagefix/server.py` (`_governor_cell_done` ~2811), `meld-triagefix/src/workers.py` (expose `queue_size` to the callback path), `meld-triagefix/src/project.py` (`default_settings`), `meld-triagefix/web/index.html`, new `meld-triagefix/src/machine_history.py`.

**Why it removes idle - corrected downward.** **E3 alone owns the 42.1 worker-seconds** (the 16->14->16 oscillation: workers 14 and 15 idle for 26.05 s + 16.06 s), and that oscillation sat inside a window this same research measured at ~100% CPU, where two idle worker slots cost approximately **zero wall**. E2's separate anchor - the t+122.4 late-grow - is not a saving at all: refusing the grow does not delete the 37.9 s cell, it re-queues it onto the first freeing worker, finishing **no earlier and plausibly later**. Joint honest value: **0-2 s of wall, and they ship as RAM/contention stability, not throughput.** Neither appears in any arithmetic that reaches P1. E1 recovers 10.7 s **on a cold first run only**. All three degrade to exactly today's behaviour when there is no history and no profile.

| id | task | files | confidence % | agent-h | test-h |
|---|---|---|---|---|---|
| M1 | **The settings the kill-switch table promises but which do not exist** (`grep -rn "governor_churn_guards\|governor_history_scope"` returns nothing): add `governor_churn_guards`, `governor_history_scope` and the `--canonical-regions` emission gate to `project.default_settings()`, `/api/settings` validation, `runreport.py` config capture, a settings-UI control, and docs. All default to today's behaviour. **Must land before B1, E2 or E3** | `src/project.py`, `server.py`, `src/runreport.py`, `web/index.html`, docs | 86 | 3 | 2 |
| E3 | Floor the `_rate_tp` span: return `None` (hold, take another sample - the state machine already handles it) when `span < max(1.0 s, 0.25 * median wall)` | `src/governor.py` | 82 | 2 | 2 |
| E2 | Refuse a target increase when `POOL.queue_size() < (new_target - current)` **or** `queue_size() < current_workers` (draining) | `src/governor.py`, `server.py`, `src/workers.py` | 80 | 3 | 3 |
| E1 | Machine-scoped `governor_history` keyed on bucket **+ hardware fingerprint + render-config fingerprint**; project entry wins, then machine, then CALIBRATE; keep the world-meta exclusion and `_warm_start`'s RAM re-check | new `src/machine_history.py`, `src/governor.py`, `server.py` | 70 | 5 | 3 |

---

### W6 - Explicitly cut, with the gate that would reopen each

Nothing below is scheduled. Each is listed so the hours are not quietly re-spent.

| cut | why | what would reopen it |
|---|---|---|
| **Merge offload / `MergePool` / terminal-completion funnel / Stop rework / merge UI lane** (~37 h) | Measured at 0.27% of worker-time (4.24-6.54 worker-s of 2401.7), 12x below the harness noise floor, in a mid-run window with zero core headroom. It would touch 20 correctness-critical consumers - run-end, auto-export deleting `.mca` under a pending `copy2`, the render-queue driver that **rebinds the global `PROJECT`**, and the Stop guarantee that is currently true *by construction* because `terminate_all()` only touches the arnis `Popen`. | Per-cell CPU work falling far enough that 80 ms is a real share (1:10 scale, or after W2 lands and the cell is seconds not tens of seconds). Re-measure with I1's timers first. |
| **OSM binary sidecar + `--osm-bake-tiles`** (~27 h) | The sidecar keeps every per-element `HashMap` + `String` allocation, which is plausibly half the 18.6 MB/s cost, so its 2-3x ceiling is unproven. It is a persistent hand-rolled cache in the user's shared OSM directory that **no existing gate can see** (`golden_hash.sh` fixtures use `--file`). The bake pass returns ~0 on the warm arm. | A1's measured `fetch` marker showing **≥1.3x**. If it shows less, allocation dominates and these hours belong nowhere. |
| **GPU elevation kernel** (GpuContext extraction, cross-process admission, WGSL, parity + seam tests, ~41 h) | Approximate by contract: ~0.02% of surface columns shift 1 block, 40x the cave kernel's drift and **visible** on flat urban ground. The blur is already not tile-invariant (91-cell radius, edge-renormalised weights), so f32 noise will not agree across a cell seam - and Meld worlds are built incrementally over days, so a seam test in one session with one driver cannot see the real failure. naga exposes no `NoContraction`, so the output can change with an Intel driver update. Precedent: shoreline ring-fitting had to be forced OFF in master-origin cells for exactly this class of reason. | Nothing this phase. If ever revisited, the standalone 5 h spike (`--caves --gpu igpu` vs `off` at 16 workers, recording **package power and effective P-core clocks**) comes first, and its own documented possible outcome is "dead on this machine". |
| **Learned ring ordering / difficulty-weighted step metric** (~13 h) | The claimed direction is contradicted by an independent replay on the same 81 durations: outer-ring-first measured **+3.19 s worse** in one simulation and **-4.67 s better** in another, and the within-ring angle tie-break moves the answer by 5.4 s - further than the claimed effect. The ring profile was also derived from a `--no-buildings` run where the dense core is *cheap*; with buildings on the sign plausibly flips and outer-first becomes the worst possible order. | Reconciling the two simulations, then re-deriving the ring profile under the config H2 settles on, with 3 clean warm repeats. |
| **Shrink cooldown (`E4`)** | Its anchor measurement (42.1 worker-s, the t+122.4 late-grow) comes from the report H1's bug corrupted. | A clean warm cs4 pair re-run after H1. |
| **mmap zero-copy tag view** | 34 h, self-scored 35%, blast radius across `filter_tags`, every `tags.get` site in `parse_osm_data`, and the three suppression passes that read *raw* tags. | Nothing this phase. |
| **Finer OSM tile grid (z13/z14)** | `from_tile_dir` does no clipping - the tile set **is** the element universe, and `ways_map` only holds ways from tiles that were read. Shrinking tiles drops distant member ways of large multipolygons and changes ring assembly, with no numerical error to point at. | Nothing this phase. |
| **Gating the building-suppression passes on `args.buildings`** | **Reason corrected - the previous one was inverted.** The measured arms run `--no-buildings` (verified in the cell log), so `osm_parser.rs:890-905` really does run three full passes over every way and node of a 195 km² tile and discard the result, inside the 2.68 s `parse` span. It stays cut this phase only because the size of that waste is **unmeasured** - I2's `parse` split is the instrument and I2 is HOLD behind I0 - and because a production render with buildings **on** gains nothing from it, so it is a benchmark-shaped win. | I2 or I4 showing the suppression passes at **≥0.3 s** of the `parse` span under the H2-settled config. Then it is a small, cheap, `--no-buildings`-only win with its own gate. |

---

## Implementation order and the >85% gate

Anything at **86% or above is GO** and safe to auto-implement. Everything else is **HOLD** - build it, but a human reviews the diff and the gate output before merge. Note that the headline lever (W2) is deliberately HOLD: it changes which files arnis writes, and the eviction-path half can change output silently. Note also that **A1 has moved to HOLD**, which leaves the GO set with no task that delivers seconds.

| # | id | task | conf % | gate | reason |
|---|---|---|---|---|---|
| 1 | H1 | Harness reuse-bug fix | 92 | **GO** | Benchmark script only; the bug is confirmed by a corrupted report on disk. Nothing else can be measured until this lands. |
| 2 | M4a | `golden_hash.sh` rebuilds before hashing | 92 | **GO** | One-line fix to a script that today can green-light a stale binary; every arnis gate depends on it. |
| 3 | H2 | matrix.json follows the **measured arms** + assert-on-mismatch + report schema/4 | 87 | **GO** | Mechanical once framed as "assert, do not silently diverge" - and the direction is fixed: the matrix moves, the arms do not. |
| 4 | I1 | merge/prune/health/meta timers, log **and** report | 92 | **GO** | Additive; the report half rides H2's schema bump so N6 is harvestable. |
| 5 | I5 | `flush_region_via` vs `save_java` region-count split | 88 | **GO** | One counter and one log line; it decides whether B1 or B2 holds the money. |
| 6 | I4 | Re-profile a non-centre and a ring-3 cell under the real config | 90 | **GO** | Measurement only, no code. |
| 7 | C5 | Tail `_scan_cell_health`'s log read | 92 | **GO** | One-line change matching an existing pattern in the same file. |
| 8 | C2 | Master path resolved once per run | 88 | **GO** | Closes a live wrong-world-merge race; small and contained. **Must precede C3a.** |
| 9 | M1 | Settings + kill switches that do not yet exist | 86 | **GO** | All three default to today's behaviour; nothing downstream can be flag-gated until they exist. |
| 10 | D2 | `to_bits()` blur equality test | 88 | **GO** | Test-only; write it before D1 so D1 has a gate to pass. |
| 11 | M5 | Docs update for what I1/I5/I6 change about measurement | 90 | **GO** | Documentation only; `docs/generation-performance.md:622-626` is now wrong. |
| 12 | I6 | `cpu_s=` on the done line, summed per run | 84 | HOLD | Extends the stdout protocol and the report schema; if it is rejected, **P8 is deleted, not weakened**. |
| 13 | I0 | Telemetry channel for the `bench.mark` labels | 82 | HOLD | New plumbing in both repos; I2, I3, D1 and P7 all sit behind it. |
| 14 | H3 | Gate-instrument repair (`region_hashes` glob, empty-dict failure, payload comparator) | 80 | HOLD | Rewrites the thing that decides whether other changes are safe; needs a `.b_linear` decoder. |
| 15 | I2 | Split the `parse` / `place` markers | 78 | HOLD | Lowered from 88: the channel it writes to is not plumbed until I0. |
| 16 | I3 | `elev_builtup_gaussian` mark | 78 | HOLD | Lowered from 86 for the same reason; it is D1's go/no-go and cannot currently fire. |
| 17 | C3a | Atomic `project.py:_write` + retry/fallback | 80 | HOLD | Lowered from 90: `os.replace` over a file an unlocked `_read` holds open raises WinError 5/32. Lands **after** C2. |
| 18 | C4 | Locked + atomic `write_world_meta` + retry/fallback | 80 | HOLD | Lowered from 88 for the same Windows sharing reason; `meld-world.json` is read by the UI and the export path. |
| 19 | A1 | `Deserializer::from_slice` + non-aliasing dedup key + >64 MB fallback | 84 | HOLD | Lowered from 88: not acceptance-identical (today's path never calls `end()`), the compact key can alias unknown types, and the RSS fallback is a third change in the same commit. Gated by G2. |
| 20 | M2 | arnis version gate for `--canonical-regions` | 85 | HOLD | Ships with B1; an older `arnis.exe` rejects an unknown clap arg outright. |
| 21 | B1 | `--canonical-regions` flag + `save_java` filter + Meld emission | 84 | HOLD | Changes which files arnis writes. Two verification items still open (drift guard on a real merge; `_scan_missing_regions` not reading the absent ring as a hole). |
| 22 | M3 | Meld-side canonical-rectangle invariant test | 84 | HOLD | Ships with B1; asserts the equality the whole design rests on, which B3 cannot see. |
| 23 | B3 | Region byte/payload fixture, both stream states, cs4 + cs8 | 82 | HOLD | Test authoring; must be reviewed because it is the *only* thing that can catch a B1/B2 regression. Depends on H3. |
| 24 | B2 | Eviction-path filter in `flush_region_via` | 68 | HOLD | The delicate half and, per I5, plausibly the half that holds the entire gain on the streaming benchmark. Wrong ordering changes peak RSS -> `should_stream_to_disk` -> which regions are evicted -> output, silently. |
| 25 | M4b | Release/migration choreography for B1 | 84 | HOLD | Sequencing an arnis tag ahead of the Meld build; process risk, not code risk. |
| 26 | D1 | Flat-layout bit-exact blur | 80 | HOLD | Bit-exactness through a transpose while parallelising the scatter and converting a counter to a reduction. **Conditional on I3 ≥ 1.5 core-s, which is conditional on I0.** |
| 27 | E3 | `_rate_tp` span floor | 82 | HOLD | Edits the live governor state machine; failure mode is a starved run, not a slow one. One-sided (returns `None` = hold). |
| 28 | E2 | No-grow-near-drain guard | 80 | HOLD | Same; needs `queue_size` plumbed to the governor callback. Justified as stability, not throughput. |
| 29 | C6 | `level.dat` donor pin + skip subworld patch when pruning | 80 | HOLD | "Failed rename becomes a merge failure" is a behaviour change; the numbering-allocation change needs a check that nothing depends on the sequence. |
| 30 | C3b | `_read` distinguishes missing from unreadable | 76 | HOLD | Call sites currently rely on the swallow-and-default; surfacing exceptions can change behaviour in paths nobody has enumerated. |
| 31 | C7 | `overwrite_collisions=False` + server-side size freeze | 74 | HOLD | Must not break the legitimate `/api/cell/regenerate` re-merge. Getting the same-cell allowance right is the whole risk. |
| 32 | E1 | Machine-scoped governor history | 70 | HOLD | A store shared across projects will warm-start a different city or a 13k-cell grid at a knee learned elsewhere. Worth ~0 on the warm reference. |

**Auto-implementable set (GO): 11 tasks, ~23 agent-h + ~18 test-h.** It delivers full attribution, a working harness, two repaired gates, the settings that every kill switch in this plan assumes, the docs, three correctness fixes including a live data-loss race - and, with A1 moved to HOLD, **no measurable seconds at all**. **HOLD set: 21 tasks, ~66 agent-h + ~57 test-h**, containing every second of the headline gain.

**Total: 32 tasks, 89 agent-h + 75 test-h**, up from 22 tasks / 58 agent-h / 45 test-h before this revision. The growth is entirely work that existed but had no owner: the settings plumbing, the arnis version gate, the release choreography, the docs, the two vacuous gates, the telemetry channel, and four measurement tasks that make the plan's own numbers checkable.

---

## Expected results vs the phase-1 baseline

Two rows of targets: what the GO set alone delivers (auto-implementable), and what lands if the HOLD set is approved. All wall figures are the **median of 3 repeats**; the frozen-duration model's noise floor is ~5 s, so anything smaller is not a result.

**Rows marked †** are unreachable if B2 is rejected in review. B1 and B2 draw on one pool; until I5 measures the split, the region saving on the streaming arms may sit **entirely** behind the 68%-confidence task.

**The GO set alone is not expected to be separable from noise at 3 repeats** (median SE ≈ 3.5 s against a ~5 s floor). Use 5 repeats, or do not report a GO-only delta at all.

| metric | baseline 1.9.7 | phase 1 measured | phase 2 target (GO only) | phase 2 target (GO + HOLD) | conf % |
|---|---|---|---|---|---|
| cs4 cold, `elapsed_s` | 174.9 s | **171.0 s** | 170-171 s (167-170 s **only if A1 is approved out of HOLD**) | **150-162 s †** | 50 |
| cs4 **warm**, `elapsed_s` | n/a | **161.2 s harness / 160.3 s report** | 160-161 s (156-159 s if A1 approved) | **140-152 s (point 147) †** | 50 |
| cs8, `elapsed_s` | 257.5 s | **239.4 s** | 238-239 s (233-238 s if A1 approved) | **218-232 s †** | 40 |
| cs4 cold, `cells_per_min` (new field, `meld-run-report/4` via H2) | 27.79 | **28.42** | 28.4-28.6 (28.6-29.1 if A1 approved) | **30.0-32.4 †** | 50 |
| cs4 warm, `cells_per_min` (same new field) | n/a | **30.32** | 30.2-30.4 (30.6-31.2 if A1 approved) | **32.0-34.7 †** | 50 |
| Σ`duration_s` / `elapsed_s` (**not** an occupancy - `summary.workers_peak` is 20 and the capacity schedule was 4/6/12/20/16/14/16, so a flat /16 denominator is meaningless; `effective_parallelism` is not a report field) | 13.73 | **12.72** | not a target | not a target | n/a |
| `ram_peak` cs4 / cs8 | 82% / 93% | **52% / 42%** | ≤52% / ≤42% | ≤52% / ≤42% | 85 |
| Run CPU demand (per-process `cpu_s=` sum, I6; the `timeline` integral is *not* an independent instrument) | n/a | **≥3395 cpu-s** | 3395 cpu-s (3245-3315 if A1 approved) | **2710-3095 cpu-s †** | 55 |
| Efficiency vs CPU-conservation floor | n/a | **~88%** (160.3 vs 141.5 s floor; *not* the 58% originally stated) | ~88% | **~85-90%** | 40 |
| Region files written per cs4 cell | 36 | 36 | 36 | **16** | 88 |
| `merge Xs prune Ys` sum per 81-cell run (now in the report, I1 + schema/4) | n/a | 4.24-6.54 worker-s (derived) | measured, ≤7 s | ≤7 s | 90 |

**Read the efficiency row carefully.** Phase 2 does not raise efficiency and does not claim to. It lowers the floor by deleting **300-685 cpu-s** of provably discarded work; efficiency stays roughly where it is because the ramp and tail become a slightly larger share of a shorter run. Anyone reporting "efficiency improved" has measured something else.

---

## Worked example - one cell, before and after

The measured cell: `ARNIS_PHASE_MARKERS=1`, cell key `0,0,4`, cs4, scale 1.0, `T=2` rayon threads (the point the governor actually grants at 16 workers), uncontended, single arnis process.

**Three caveats that must ride with every number below.** (1) This cell is the NW-corner cell, not the centre; it covers only the 18.8 MB `1171_740` tile while the centre tile is 147.8 MB, and it ran 12.4 s in the real run against a 27.2 s median - I4 re-derives the fleet region figure on a ring-3 cell. (2) It was profiled with `--no-buildings`, and its `save = 26.7% / post = 2.0%` shape is the **non-eviction** shape - the benchmark arm ran stream-to-disk, and the `save_java` / `flush_region_via` split is unmeasured until I5. (3) The 43% discarded-save weighting holds **only with `--bake-lighting`**; at `bake_lighting:false` it falls toward 21-29%, and both are shown below. So this walk shows the *shape* of the change, and it **understates** the fleet OSM saving (which is dominated by the 42 decodes of the 147.8 MB tile) while showing the region saving in the phase where an unstreamed run pays it.

| phase | before (T=2) | what phase 2 does to it | after | delta |
|---|---|---|---|---|
| `fetch` | **1.01 s** (18.79 MB / 172,843 elements = 18.6 MB/s, `from_reader`, one `String` clone per element) | **A1**: `Deserializer::from_slice` + non-aliasing dedup key. Pure serial, so wall ≈ cpu here. | 0.60-0.75 s | **-0.30 s** |
| `elevation` | **3.82 s** (7.19 core-s parallel pool + 0.23 s serial residue) | **D1**: kill 2049 allocations/pass, 4.2 M pointer chases/gather-pass, the serial scatter and the serial blend. Tap loop untouched, so the 3.073 G taps stay. | 3.50-3.60 s | **-0.27 s** |
| `parse` | **2.68 s** (`parse_osm_data` + priority sort + 3 land-cover overrides + `transform_map` + `WorldEditor` construction + `precompute`) | Nothing in scope. I2 splits the marker so the next phase knows what is in here - including the three building-suppression passes that run and are discarded under `--no-buildings`. | 2.68 s | 0 |
| `place` | **5.95 s** (Amdahl fit: 10.23 core-s parallel + **0.83 s strictly serial** in the tile-merge loop at `data_processing.rs:861-936`) | Nothing on this non-streaming profile. **On a stream-to-disk cell this is where B2's saving lands**, because `flush_region_via` fires from inside the merge loop at `:910`. | 5.95 s | 0 |
| `post` | **0.38 s** | Nothing. | 0.38 s | 0 |
| `save` | **5.06 s** (Amdahl fit - a two-point fit, not a measurement: 9.46 core-s parallel + 0.33 s serial `compact_sections` prologue). 36,864 chunk slots written across 36 region files. | **B1**: write 16 regions, not 36. 20,480 of 36,864 chunk slots deleted (4,352 content + 16,128 base) ≈ 43% of the phase **with `--bake-lighting`**; 21-29% at `bake_lighting:false`. | 2.85-2.95 s (lighting on) / 3.6-4.0 s (lighting off) | **-2.15 s / -1.06..-1.47 s** |
| **wall** | **18.96 s** | | **16.1-17.3 s** | **-8.8% to -15.0%** |
| **cpu** | **27.70 cpu-s** (1.46 cores busy, peak RSS 1074 MB) | fetch -0.33, elevation -0.30, save -2.06 to -4.21 | **22.9-25.0 cpu-s** | **-9.7% to -17.3%** |

**Fleet arithmetic, and why it is *lower* than the per-cell headline.** The region saving scales in *count* exactly - `min_x = rx*2048 - 128` always sits 128 below a 512 boundary, so **every** cs4 cell writes 36 and keeps 16, and denser cells discard *more* content chunks - but its *value* rests on a two-point Amdahl fit, a lighting-dependent weighting, and the cheapest cell as the unit, so the honest fleet band is **200-500 core-s**, not a point. The elevation saving is content-independent (fixed 2049² grid, fixed sigma): **20-35 core-s**. The OSM saving does *not* scale from this cell - the fleet pays 441 cpu-s across 90 tile-decodes, and this cell is the cheapest of them, so the honest fleet band is **80-150 cpu-s**, not 27.

Total: **300-685 cpu-s off ≥3395** = 9-20%. At constant occupancy that is 160.3 -> **128-146 s**; haircut for the ramp and tail becoming a larger share of a shorter run, and for the timeline integral being a *floor* on demand: **140-152 s, point estimate 147 s = 1.09x on the warm arm.**

---

## Determinism and safety gates

**The most important sentence in this plan: `scripts/golden_hash.sh` green is NOT evidence for W1 or W2.** Its 5 fixtures are committed `.osm.gz` files converted to Overpass JSON and fed with `--file --offline`, so `from_tile_dir` never executes; and `ARNIS_BLOCK_HASH` covers placed blocks **in memory**, so a change to which region files reach disk is invisible to it. It proves only that the *default* path was not disturbed - which is necessary, and is why it stays. It also never rebuilt the binary until M4a, so before M4a it did not reliably prove even that.

### Hard constraints, restated and honoured

1. **Per-cell arnis output stays byte-identical by default.** A1 is byte-identical **provided** it keeps the non-terminating deserializer (no `end()`) and a dedup key that cannot alias two type strings - it is *not* identical "by construction", and G2 is the gate that says so. D1 is bit-identical by construction (same f64 accumulators, same left-to-right tap order; only memory layout and which thread performs the writes change). B1/B2 change *which files are written*, never the bytes inside a kept file - `write_region_to_disk` is free-standing and takes no cross-region state - and are absent-by-default.
2. **Crash isolation: process-per-cell stays.** The master world is never opened inside arnis. Nothing in this plan touches that.
3. **Windows: `arnis.exe` is GUI-subsystem.** Run it from bash with redirects; `PowerShell` will not wait on it. Job Object `KILL_ON_JOB_CLOSE` cleanup stays intact.
4. **A merge in flight always finishes; only arnis is ever killed.** This is currently true *by construction* - `POOL.stop()` only sets a flag idle workers read (`workers.py:236`) and `terminate_all()` only touches `state["process"]`, which is the arnis `Popen`. **The merge offload is cut precisely so this guarantee is never re-implemented in code.**
5. **Default configuration changes nothing until a flag flips - and RSS is behaviour, not an implementation detail.** `--canonical-regions` absent = today. Governor guards behind `governor_churn_guards` (default off). History scoping behind `governor_history_scope` (default `project`). All three settings are created by M1; until then they do not exist. A1 needs no output flag but does ship its own memory fallback, because a peak-RSS rise is a behaviour change that N4 fails.
6. **Cell size stays uniform within a grid.** No mixed-size grids, no splitting the final wave - that is what makes canonical rectangles disjoint and `overwrite_collisions` survivable.

### Required gates

| id | gate | covers | blocking for |
|---|---|---|---|
| G1 | `scripts/golden_hash.sh` 5/5 identical in all 4 env configs, **with an explicit `cargo build --release` first** (M4a) - the script never rebuilt, so a green run could be validating a stale binary | the default path is undisturbed | every arnis change |
| G2 | `ARNIS_BLOCK_HASH` byte-identical with and without the A1 change, on **the one 4-tile cell** (56 of 72 cells read exactly one tile, so a single-tile cell never executes a second `seen.insert` hit and cannot detect a dedup regression) **plus a synthetic fixture carrying the same id under two different `type` strings across two tiles**, over all 9 cached Bucharest tiles | W1 - `golden_hash.sh` structurally cannot see this | A1 |
| G3 | Canonical-region comparison with and without `--canonical-regions`, in **both** `ARNIS_STREAM_TO_DISK` states, at **cs4 and cs8** under real RAM pressure: `.b_linear` compared whole for the streaming arms, `.mca` compared with `[4096:8192]` zeroed for the anvil arm, and **decoded chunk payloads rather than raw bytes for the eviction arm** - `.mca` bytes depend on chunk write order and zlib output, both of which a different flush layout legitimately changes without changing a single block, so a raw `cmp` on B2 produces false failures. Committed as a fixture | W2 - `golden_hash.sh` structurally cannot see this either | B1, B2 |
| G4 | `f64::to_bits()` equality unit test, old vs new blur, fixed pseudo-random grid with NaN holes | W4 - and it defends bit-exactness against future refactors, not just this commit | D1 |
| G5 | `bench_scheduler.determinism_gate()` verdict `identical`, strength `strong`, **zero key-set mismatches** across the phase-1 and phase-2 arms | whole-run block hashes + "which cells exist" | every change |
| G6 | One `--hash-mode region` run hashing the **master world's** region files. **Today this gate is vacuous on every arm the plan runs**: `bench_scheduler.py:864-880` globs `*.mca` while the arms produce `r.X.Z.b_linear`, so it returns `{}` and passes. H3 extends the glob to `("*.mca","*.b_linear")`, hashes `.b_linear` whole, zeroes `[4096:8192]` for `.mca`, and **fails loudly on an empty dict** | the only check that can see a merge-order or `level.dat`-donor effect; `block_hash` is per-cell and structurally cannot | C6, C7, B1/B2 - and H3 blocks all four |
| G7 | `cargo clippy -D warnings`; 489 arnis tests; 481 Meld tests | regressions | every change |
| G8 | On an isolation arm run with **`prune_cell_after_merge` off** (the server `rmtree`s the cell world at `server.py:2907-2912`, so the evidence is gone by default): the cell output holds 16 region files, not 36, in `region/`, `poi/` and `entities/` alike, and `merge.py`'s log line shows `-M seam` with **M = 0** across all three subdirectories | direct confirmation that W2 did what it claims | B1, B2 |

### Kill switches

| change | switch | effect when off |
|---|---|---|
| `--canonical-regions` | omit the flag; Meld gates emission on the project setting created by M1 **and** on the bundled arnis version (M2) | arnis writes 36 regions exactly as today; an older `arnis.exe` never sees an unknown clap arg |
| Governor guards | `governor_churn_guards: false` (default; **created by M1 - the setting does not exist today**) | today's state machine, byte-for-byte |
| Machine history | `governor_history_scope: "project"` (default; also created by M1) | today's warm-start behaviour |
| Whole governor path | `governor_mode: "off"` (pre-existing) | the legacy thread formula, preserved verbatim |
| A1 | no output flag, but a **>64 MB `from_reader` fallback ships in the same commit** | files above the threshold decode exactly as today, with today's peak RSS |
| D1 | none, by design | it is provably bit-identical; a flag would imply doubt the gates do not support |

---

## Benchmark protocol

**Harness:** `python bench/bench_scheduler.py --only 1to1-cs4 --repeats 3` and the cs8 group, plus `bench/ab_bucharest.py` for the warm-start repeat. Results land in `bench/results/<label>/*.json` and each run's `meld-report.json` (schema `meld-run-report/4` after H2).

**Preconditions - none of the numbers below are valid until these hold:**

1. **H1 merged.** Until `do_run(reuse=True)` calls `prepare_project()`/`/api/projects/switch`, a warm repeat renders into whatever project is active, exactly as it did when it overwrote `ab-perf-governor-cs8/meld-report.json` with a cs4 run.
2. **H2 merged, in the correct direction.** `matrix.json` is edited to declare what the arms actually ran - `buildings:false, interior:false, bake_lighting:true, region_format:"blinear", blinear_level:6` - and the harness asserts the live `/api/settings` match and aborts on mismatch. **The arms are not moved to the matrix.** If they ever were, every "from" column in the pass-criteria table would be void (all four phase-1 numbers were measured on the current config), Overture's serial parquet path (`overture.rs:503`, `:1008`, no rayon anywhere in the file) would become a new dominant phase of unknown warm cost, and W2's 43% would drop to ~21-29%. Any such change costs a full re-baseline: **4 arms x 3 repeats x ~3 min plus prep ≈ 45-60 min of wall** that must be budgeted, not absorbed.
3. **M4a merged.** `golden_hash.sh` must rebuild, or G1 is hashing a stale `arnis.exe`.
4. **Same site, same cache.** bbox `44.36..44.5072 N, 25.96..26.1662 E` (16.36 x 16.39 km), origin `44.5072, 25.96`, scale 1.0, `seam_buffer_chunks: 8`, warm shared OSM cache at `C:\tmp\meld-ab-data\cache\osm` (the four z11 tiles: `1171_740` 18.8 MB, `1171_741` 33.7 MB, `1172_740` 61.7 MB, `1172_741` 147.8 MB).
5. **3 repeats, compare medians - and 5 repeats for any GO-only claim.** The frozen-duration list-scheduling noise floor is ~5 s and run-to-run variance on this bench is comparable; the median standard error at 3 repeats is ~3.5 s, which is larger than the entire GO-only ask. A single-run delta under 5 s is noise.
6. **Record the `ARNIS_STREAM_TO_DISK` state of every arm in the result JSON.** H2 adds `config.stream_to_disk` for exactly this - today the field does not exist and `config.region_format` is present but **null** on a run that used blinear. The state moves ~9 core-seconds between the `place` and `save` buckets and decides which half of W2 is doing the work.

**Arms:** cs4 legacy (control), cs4 governor cold, cs4 governor **warm repeat**, cs8 governor. Plus two single-variable isolation runs: `--canonical-regions` on/off at fixed `workers=16` **with `prune_cell_after_merge` off**, and `governor_churn_guards` on/off.

### Pass criteria - the numbers that must move

| # | number | from | must reach | why this threshold |
|---|---|---|---|---|
| P1 | cs4 **warm** `summary.elapsed_s`, median of 3 | 160.3 s | **≤ 150.0 s** | 6.4% - comfortably above the ~5 s noise floor. This is the primary criterion, **and it is unreachable if B2 is rejected**: B1 and B2 share one pool and, on the streaming arms, the money is plausibly all on B2's side until I5 says otherwise. |
| P2 | cs4 **cold** `summary.elapsed_s` | 171.0 s | ≤ 162.0 s | Relaxed from ≤160.0 s to match the widened 200-500 core-s region band. |
| P3 | cs8 `summary.elapsed_s` | 239.4 s | ≤ 232.0 s | Relaxed for the same reason. Smaller relative win regardless: 100 written / 64 kept = 36% discarded, vs 55.6% at cs4. |
| P4 | cs4 warm `summary.cells_per_min` (**new field in `meld-run-report/4`; it does not exist in schema/3**) | 30.32 | ≥ 32.4 | Mirrors P1 exactly, and carries P1's B2 dependency. |
| P5 | Region files in a cell output dir, run with `prune_cell_after_merge` **off**; `merge.py`'s `-M seam` count | 36; M ≈ 20 | **16 in each of `region/`, `poi/`, `entities/`; M = 0 in all three** | Direct, countable confirmation of W2 - but only observable on an unpruned isolation arm, since the default run `rmtree`s the evidence. |
| P6 | `fetch` marker on a **centre-tile** cell (147.8 MB) | ~7.9 s (derived) | ≤ 5.5 s | Confirms A1. Also re-measures the 18.6 MB/s rate that scales the whole 441 cpu-s figure. Depends on A1 clearing HOLD. |
| P7 | `elev_builtup_gaussian` (new label from I3, which needs I0's channel) | unknown | measured, then ≥15% lower after D1 | If the first measurement is under 1.5 core-s, **drop D1** rather than ship a 0.5% win. If I0 or I3 is rejected, P7 is unmeasurable and D1 must not ship on an estimate. |
| P8 | Per-run sum of arnis's `cpu_s=` (I6, from `GetProcessTimes`) | ≥3395 cpu-s (timeline floor) | ≤ 2960 cpu-s | The cpu-second claim, measured independently of wall time. **It cannot be measured with `timeline[]`**: 10 samples at 20 s, clamped at 100, pinned mid-run, so that integral ≈ elapsed x 24 and "≤2960 cpu-s" would merely restate "≤ ~140 s wall". **If I6 is rejected, delete P8 - do not fall back to the timeline.** |

### Numbers that must NOT move

| # | number | requirement |
|---|---|---|
| N1 | `golden_hash.sh` | 5/5 identical, all 4 env configs, **after an explicit `cargo build --release`** (G1 + M4a) |
| N2 | `determinism_gate()` | `identical` / `strong`, zero key-set mismatches (G5) |
| N3 | Canonical region comparison | identical with and without `--canonical-regions`, both stream states, cs4 + cs8 - `.b_linear` whole for streaming arms, `.mca` with `[4096:8192]` zeroed for the anvil arm, decoded chunk payloads for the eviction arm (G3) |
| N4 | `summary.ram_peak` | ≤ 52% (cs4), ≤ 42% (cs8). A rise means B2 changed the eviction trajectory - or that A1's `from_slice` buffer escaped its size threshold. |
| N5 | cs4 legacy arm `elapsed_s` | 174.9 ± 2 s. **Do not also fix the legacy arm's 2 s stagger** - it is the control, and improving it would shrink the reported governor delta for an unrelated reason. |
| N6 | `merge Xs prune Ys` sum per run (I1, emitted **into the report** under schema/4 so `harvest()` can read it - a log-only version of I1 would leave this unharvestable) | ≤ 7 s. The tripwire for W3 and for any future change to the post-arnis tail. |
| N7 | `summary.cells_per_min` on any arm (new field, H2) | never lower than phase 1 |

### Explicit fail conditions

Any hash mismatch in G1-G6. `ram_peak` up on any arm. `cells_per_min` down on any arm. P1 not reached with all HOLD tasks approved and merged. A G6 run that returns an empty region dict (that is a vacuous pass, not a pass). And: **a green `golden_hash.sh` presented as evidence for A1 or B1/B2** - that is a process failure, not a result.

---

## Risks register

| # | risk | mitigation |
|---|---|---|
| 1 | **B2 silently changes kept-region contents.** Under stream-to-disk most regions leave via `flush_region_via`, and that path also governs peak RSS, which feeds `should_stream_to_disk`, which decides which regions are evicted, which changes output. There is no numerical error to point at - just different blocks. | Filter **only** `worker.send()`. Keep `world.regions.remove()` and `flushed_regions.insert()` at the identical instant so the RSS trajectory is bit-for-bit unchanged. Gate on G3 - and gate on the *right comparison*: decoded chunk payloads, not raw bytes, because chunk write order and zlib output legitimately differ under a changed flush layout. Both stream states, **cs8 under real RAM pressure**. B2 is HOLD at 68% precisely because a single warm cs4 raw `cmp` would both miss real regressions and invent false ones. |
| 2 | **`golden_hash.sh` green is mistaken for proof.** Three of the four candidate designs quoted it as their release gate; for W1 and W2 it proves nothing (fixtures use `--file`; the hash is in-memory blocks); and until M4a it did not even rebuild the binary. | G2 and G3 are separate, mandatory, committed gates. The plan states in three places that G1 is necessary and not sufficient. Any PR touching `osm_parser.rs` or `java.rs`/`mod.rs` without G2/G3 output attached is rejected on process grounds. |
| 3 | **The 441 cpu-s OSM figure rests on one data point on the cheapest cell, and the 200-500 core-s region figure rests on three unverified layers.** 18.6 MB/s comes from a single 1.01 s `fetch` on a single 18.79 MB tile; the region number stacks a two-point Amdahl fit, a lighting-dependent 43% weighting, and the cheapest cell as the unit. | P6 re-measures the OSM rate on the 147.8 MB centre tile - the one decoded 42 times per run - before any claim is made. I4 re-derives the region figure on a ring-3 cell. Both are already quoted as bands (80-150 cpu-s; 200-500 core-s) rather than points, and A1 is worth doing anywhere in its band because it is small and gated. |
| 4 | **The benchmark config and the measured config are five settings apart, and one of them prices W2.** `matrix.json` declares `buildings:true, overture:true, interior:true, bake_lighting:false, region_format:"anvil"`; the A/B ran `--no-buildings --interior false --bake-lighting --region-format blinear --blinear-level 6`. `bake_lighting` alone moves W2's discarded share between 43% and 21-29%. Overture's parquet decode path has **no rayon anywhere**, and its warm cost is currently unknown - the stale `main.rs:521` comment ("~93% of a cell's wall time") predates the per-range disk cache. | H2 makes the harness assert and abort, and resolves the divergence **by editing the matrix to match the measured arms**, so every phase-1 baseline stays valid. I4 re-profiles under that config. If anyone ever argues for buildings-on instead, the cost is a full 45-60 min re-baseline plus a new dominant phase this plan does not address - that is phase 3's problem, and it must not be silently absorbed into phase 2's numbers. |
| 5 | **Wall gain lands below the cpu-second arithmetic** because a shorter run gives the ramp and the ~30-38 s tail a larger share, so occupancy falls below the ~88% the projection holds constant. Also, `timeline[].cpu` clamps at 100%, so 3395 cpu-s is a **floor** on demand - if real demand is higher, the percentage is lower. | Priced into the band (128-146 s at constant occupancy, 140-152 s quoted) and into the 50% confidence, not mitigated away. P8 - measured with I6's per-process `cpu_s=`, not with the timeline integral, which is not independent of wall - separates a wall miss from a cpu-second hit rather than leaving it to argument. |
| 6 | **A1's transient RAM at 16 concurrent workers.** `from_slice` requires the whole file resident: 16 x 147,837,444 bytes = 2.4 GB worst case if every worker hits the centre tile simultaneously. | The >64 MB `from_reader` fallback ships **in the same commit**, not as a contingency, so the worst case never arises. The buffer is also dropped before the parsed `OsmData` accumulates, and the parsed form (173k-1.19M elements with per-element `HashMap`s) is already far larger than the source bytes. N4 remains the gate: any rise in `ram_peak` fails the arm. |
| 7 | **C7 breaks the legitimate re-merge path.** Flipping `overwrite_collisions` to `False` turns a silent overwrite into a raised `MeldCollisionError` - and `/api/cell/regenerate` legitimately re-merges a cell into its own canonical rectangle. | C7 is HOLD at 74%, and its allowance is keyed on **the cell's own canonical rectangle** rather than a blanket bypass. Pair it with the server-side `job_size_regions` freeze in the same commit, because the freeze is the reason the collision guard can be turned back on at all (the current client-side-only freeze at `web/index.html:2125-2140` is bypassed by `plan_keys`, `/api/cell/regenerate`, and a hand-edited `grid.json`). Test both the regenerate path and a deliberately mixed-size grid. |
| 8 | **Scope creep back into the cut workstreams.** MergePool, the GPU kernel and learned ring ordering all look like "the remaining headroom" and all have persuasive write-ups. Between them they are ~91 agent-hours for effects that are, respectively, 0.27% of worker-time in a zero-headroom window, contingent on an unmeasured hardware question whose own stated outcome may be "dead on this machine", and contradicted by two simulations of the same 81 durations. | W6 lists each with the **specific measurement that would reopen it** and nothing else. Reopening requires that measurement, not an argument. In particular: the GPU path's real failure mode is cross-*time* - Meld worlds are built cell by cell over days into one master world, and an f32, FMA-contraction-dependent kernel cannot agree with a neighbour rendered after a driver update. No seam test run in one session can see that, and an env flag cannot repair blocks already written. |
| 9 | **The atomic-write fixes convert a benign race into a hard Windows failure.** CPython's `open()` shares read/write but not delete; `os.replace` over a `project.json` or `meld-world.json` that an unlocked `_read` (or the UI, or the export path) holds open raises WinError 5/32. Today that window yields a defaulted read; after a naive C3a it yields an exception inside `subworld_number`. | C2 lands **before** C3a so the hot read disappears first; C3a and C4 both wrap `os.replace` in a bounded 5 x 20 ms retry whose last attempt falls back to `write_text`. Both are HOLD at 80 rather than GO at 88-90 for exactly this reason. |
| 10 | **The gates that would catch B1/B2/C6/C7 currently see nothing.** G6 globs `*.mca` on arms that write `.b_linear` and passes on an empty dict; G3 as originally written compared raw bytes that a changed flush layout legitimately alters; G8's evidence is `rmtree`'d before anyone can count it; G2 on a single-tile cell never exercises a duplicate element. | H3 repairs the instruments (glob, empty-dict failure, payload comparator) and blocks B1/B2/C6/C7. G8 moves to an unpruned isolation arm with per-subdirectory expectations. G2 gains the 4-tile cell plus a synthetic same-id-two-types fixture. None of these deliver a second, and all of them are prerequisites for believing the ones that do. |
| 11 | **The plan got bigger, and the GO set got emptier.** 32 tasks / 89 agent-h / 75 test-h against the original 22 / 58 / 45, and with A1 on HOLD the auto-implementable set now delivers no measurable seconds at all. | That is the honest state, not a regression: the added hours are work that already existed unowned (settings, version gate, release, docs, two broken gates, the telemetry channel). The consequence is stated in the results table rather than hidden - **do not report a GO-only delta at 3 repeats**, and expect the wall win only when the HOLD set, and specifically B2, is approved. |
---

## Phase 3 candidate - open polygons at the cell seam (user proposal, 2026-08-26)

**Not scheduled. Recorded here so the reasoning is not lost.**

### The observation

Generating a cell builds an artificial one-block wall along the cell boundary where a building or
water body crosses it. The proposal: give Meld an open-polygon mode so arnis does not close the cut
edge, do not write the region files that ring the cell, and merge neighbouring cells on the shared
building - the same seed should make the two halves line up.

### Mechanism, confirmed in code

`src/clipping.rs:11` clips ways with Sutherland-Hodgman, and around line 60 it deliberately
re-closes the result:

> `// Re-close the polygon: SH output is implicitly closed, and dedup may have removed the explicit`
> `// closing point. Re-adding it preserves the closure signal so downstream code (flood fill) can`
> `// distinguish closed ...`

So a building straddling the boundary becomes a closed polygon whose new edge lies exactly on the
bbox line, and the building renderer draws walls along polygon edges. The wall is a clipping
artifact, not a region-file artifact. Note the re-close is load-bearing for flood fill, so it cannot
simply be deleted - the cut edges need to be MARKED as cut rather than dropped, so wall rendering
skips them while fill still sees a closed ring.

### Two separable ideas

1. **Open polygons at the cut (QUALITY).** Mark bbox-clipped edges so the wall renderer skips them
   while flood fill keeps its closed ring. Local to `clipping.rs` plus the building renderer. This is
   the part worth doing, and it fixes a visible defect.
2. **Sparse overflow patches instead of halo regions (PERFORMANCE).** Write only the spilled blocks
   rather than whole halo region files.

### Why idea 2 is not the speed win it looks like

The saving is the same 200-500 core-s per run that tasks B1/B2 already target - both approaches avoid
writing the same 20 discarded region files per cs4 cell. The patch design does not save more; it
moves work into Meld's merge (today 0.27% of worker time) and adds a second merge path, so it costs
materially more complexity and determinism risk for roughly the same seconds. Prefer the plain
write-side filter for speed.

Also note the halo itself must keep being GENERATED: `data_processing.rs:581-586` uses a 64-block
tile halo so elements whose centroid falls inside a tile can render blocks past the strict boundary
("halo writes only if the target position is still AIR"). Skipping generation, rather than skipping
the write, deletes real overhang blocks at every seam.

### Open question that decides the fix

Do BOTH neighbouring cells currently render a straddling building - each clipping its own half, i.e.
two cut walls facing each other inside the building - or does only one cell own it? That determines
whether the fix is "skip cut edges when rendering walls" or "assign ownership and merge the overflow".
Answer this before designing further.

### Test, when it is built

Same Bucharest bbox, WITH buildings enabled (the phase-1 and phase-2 A/B runs all used
`--no-buildings`, so none of those numbers speak to this). Compare, in order:
baseline 1.9.7 -> phase 1 -> phase 2 GO -> phase 2 + canonical regions -> open polygons.
Quality gate is visual plus a block-diff along a cell seam; speed gate is the usual harness table.
