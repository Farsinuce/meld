# Branch status — `feat/gpu-void-naming` (Meld side)

**Local only. Nothing pushed. Nothing on `main`.**

The performance work in this programme lives in the **arnis fork**, and its full
write-up is the authoritative index:

> `arnis-283-src/docs/BRANCH-STATUS.md`
> plus `PHASE1-CPU-PERF.md` (measurements + traps) and `PHASE2-GPU-PLAN.md`

## What is on this branch, Meld side

Shipped in **Meld 1.9.3** (commit `5bf5d48`):
- native B_Linear region generation, the `native_region_format` setting, and the
  Region format picker — see `native-blinear-generation.md` / `-results.md`
- the animated MELD wordmark, and the drawer-ordering fix

Research and plans, not yet implemented:
- `void-naming-gpu-plan.md` — void worlds (mechanism **proven** on Leaf 1.21.11),
  GUI world naming, and the GPU analysis

## Open Meld work, in value order

1. **Occupancy-driven worker governor.** `cpu_target_pct` divides the CPU budget by
   *assumed* threads per worker, but a 1:20 cell actually uses **1.02 cores** while
   being allocated ~5 — roughly **17% real utilisation while the UI believes 90%**.
   Measure `cores_per_cell` at runtime and derive
   `workers = cores * pct/100 / cores_per_cell`: ~21 at 1:20 (default is 4), ~3 at
   1:1. Worth about **5x on the Romania 1:20 render**, more than any GPU work.
2. **Set `ARNIS_FLUSH_THREADS` per worker.** The fork now writes regions on a pool
   (default `cores/4`, 2..6). Every Meld worker spawns its own, so Meld should
   allocate it from one budget rather than letting each process guess.
3. **Void-world support before that feature ships.** `merge.py`'s drift guard
   raises `MeldCoordinateDriftError` for a cell whose content does not reach both
   far edges, and `server.py` treats drift as deterministic — **never retried, cell
   silently lost**. A void cell over sea or forest fails outright. `finalcheck`
   also needs to keep treating a chunkless region correctly.
4. **Root `meld/` package tools** (`mca.py`, `chunk_protection.py`, `subworld.py`)
   still glob `*.mca` only and would silently no-op on a b_linear world. They are
   outside the light-meld run path; `metadata.json` carries `regionContainer` as
   the detection hook.

## Benchmarking note

Meld always passes `--osm-tile-dir`, and that matters: with no source flag arnis
calls **Overpass over the network** (13192 ms on one bbox) instead of reading the
local cache (1157 ms). Benchmark the cached path, and pin `ARNIS_STREAM_TO_DISK`,
or runs are not comparable.

---

# Branch status — `perf/speed-to-worldgen` (Meld side)

Added 2026-08-25. **Local only, uncommitted.** Everything below is working-tree
state on `perf/speed-to-worldgen`; nothing is pushed and nothing is on `main`.

**Full write-up: [`docs/generation-performance.md`](./generation-performance.md)** —
user guide and developer reference in one document. This section is the index.

This branch closes item 1 and item 5 of the "NOT DONE" list above (the worker
governor, and Meld setting `ARNIS_FLUSH_THREADS` from one budget). The old
`worker_autoscale` opt-in is retired: projects that had it on migrate to
`governor_mode="auto"` at load.

## What landed

| Area | Files | What it does |
|---|---|---|
| **Governor** | `src/governor.py` (new), `tests/test_governor.py` (52 tests) | Closed-loop worker sizing driven by measured **delivered cells/min**, not by a CPU-budget division. CALIBRATE from 4, climb by +2 while a step still pays, settle, re-open on sustained drift. |
| **Measurement** | `src/occupancy.py`, `tests/test_occupancy.py` (56 tests) | `reset()`, RSS p95 (nearest-rank), `cells_per_min`, `cores_per_cell_recent`, injectable clock. `suggest_workers`/`damped_step` byte-identical, verified against `HEAD`. |
| **Pool** | `src/workers.py`, `tests/test_workers_pool.py` (16) | The `_stopped` flag is now actually written and honoured; per-run stagger epoch (runs 2..N no longer start in lockstep, EWMA no longer leaks across runs); `admit_cb` hook. |
| **Generator I/O** | `src/arnis_cmd.py`, `tests/test_phase_markers.py` (27) | Parses arnis stdout protocol v1, consumes marker lines before `on_line()`, prefers generator-reported `cpu_s`/`peak_mb` over the psutil sampler. No capability probe - Meld sets the env var whenever the governor is on. |
| **Settings** | `src/project.py`, `src/presets.py`, `tests/test_governor_settings.py` | Five new keys plus `migrate_governor_settings()`. All six governor keys in `_MACHINE_KEYS` **and** `_META_SKIP_SETTINGS`; `gpu_accel` leak fixed in `_MACHINE_KEYS`. |
| **Prefetch cancel** | `src/prefetch.py`, `tests/test_prefetch_cancel.py` | Uniform `should_stop=` on all four entry points, 1 s backoff slices, clean partial return. A stop-skipped tile is deliberately not recorded as broken. |
| **Wiring** | `server.py` (+580/-66) | Ten integration points: Stop actually stops, prefetch cancellation, unified `cpu_target_pct` 90, flush cap from settings, governor lifecycle, phase-marker env, clamps + 3 routes + `/api/status` + `/api/mini`, worker stages. |
| **UI** | `web/index.html` (+401/-20) | The "Generation performance" panel: mode picker, five knobs, live readout, meter, Recalibrate/Freeze, one-time ceiling prompt. Degrades to today's controls against an old server. |
| **Tray bar** | `src/statusbar.py`, `tests/test_statusbar.py` (34) | `gov 8→12` segment, two new stage colours, right-to-left segment placement with a hard left floor. |
| **Bench** | `bench/` (harness + `matrix.json` + README) | Legacy vs governor, same area, with a `block_hash` determinism gate. |

## The measurement that drove all of it

Real `meld-report.json`, Berlin, 1:1, cell size 4, 24 logical cores, workers raised
live mid-run:

| workers | 8 | 12 | 16 | 20 | 24 |
|---|---|---|---|---|---|
| median cell | 21.1 s | 33.9 s | 41.6 s | 56.5 s | 63.5 s |
| cells/min | ~22.7 | ~21.2 | ~23.1 | ~21.2 | ~22.7 |

**Throughput is flat across the whole climb** at 79% CPU and 81% RAM peak. Every
worker past ~8-12 inflated per-cell latency and RAM and delivered nothing. The
governor must reproduce this: fed these numbers it converges in 8..12 and never
visits 24 (asserted in `tests/test_governor.py` and `tests/test_occupancy.py`).

## Verification

- `pytest tests/ -q` → **433 passed, 0 failed**.
- **Off mode is bit-identical to the deleted formulas**: 13,824 combinations of
  cores × cpu_pct × min_threads × workers, 0 mismatches. Off never gates, never
  resizes, never persists history.
- API contract check in a sandboxed `MELD_DATA_DIR`: all 12 snapshot fields on
  `/api/status.governor`, `{state,w,target}` on `/api/mini.gov`, every pre-existing
  key still present, three routes live, 14 clamp cases, `MELD_GOVERNOR` override.
- Run-lifecycle check: auto opens at 4, settles at 6, `24 not in visited`; a stop
  mid-run blocks resizing; `end_run` writes `1:1/4` history and a warm start reads
  it back.

## Not done, Meld side

- **No live full render** with the governor driving a real multi-cell build. Every
  result above is unit/API level. The bench harness exists for exactly this and has
  not been run against a live sweep.
- **`ARNIS_PHASE_MARKERS` end to end** is unproven through Meld's runner (proven by
  signature and by arnis' own live runs).
- **`bench` picks the wrong binary by default** on this machine:
  `resolve_arnis_exe()` finds `Documents/Meld/arnis.exe`, not
  `arnis-triagefix/target/release/arnis.exe`. Drop the build you want benched where
  Meld looks first.
- `docs/index.md` has no entry for `generation-performance.md` yet.
- The open question is on the arnis side: **what the 1:1 shared-resource wall
  actually is.** See the arnis `docs/BRANCH-STATUS.md` section for the candidates.

---

# Branch status — `perf/speed-to-worldgen-phase2` (Meld side)

Added 2026-08-26. **Local only.** Branched from `perf/speed-to-worldgen` (Meld
`9ffec00`, arnis `a1143f70`), which shipped the adaptive governor and arnis stdout
protocol v1.

**Full plan, with the evidence and file:line citations behind every task:
[`docs/perf-phase2-plan.md`](./perf-phase2-plan.md).** The measurement half is
written up in [`docs/generation-performance.md`](./generation-performance.md) under
"What a run measures, and where it lands".

## Read this first

**This change set is not expected to move wall time, and no row below claims it
does.** It is measurement, gate repair, settings plumbing and two correctness
fixes. Every task that could actually remove seconds — the region-write filter, the
OSM parse change, the elevation blur, the governor guards — is HOLD in the plan and
is **not** on this branch. Judging this branch on a stopwatch will produce a "no
gain" verdict that is correct and beside the point: it exists so the next branch can
be judged on something better than a stopwatch.

Per-cell arnis output is byte-identical, and default configuration behaves exactly as
it did. The golden hashes over the 5 fixtures are the gate.

## What the GO set lands

The rows below are the eleven tasks that cleared the plan's >85% confidence gate.
Rows naming `arnis ...` files live in the fork repo on the matching branch; each row's
own verification belongs to whoever implemented it.

| id | What | Files |
|---|---|---|
| **H1** | `do_run(reuse=True)` no longer skips `prepare_project()` / `/api/projects/switch`, so a warm repeat renders into the project it says it does; `harvest()` fails loudly on a report whose `cell_size` or cell count disagrees with its group | `bench/ab_bucharest.py` |
| **H2** | `matrix.json` edited to declare what the measured arms actually ran; the harness asserts the live `/api/settings` against seven keys (the five divergent ones plus `overture` and `stream_to_disk`) and **aborts** on mismatch. Report bumped to `meld-run-report/4`; both schemas still readable | `bench/matrix.json`, `bench/ab_bucharest.py`, `bench/bench_scheduler.py`, `src/runreport.py` |
| **I1** | Four monotonic timers around merge / prune / health / meta in `_runner`; a `[Timers] <cell>: merge Xs prune Ys health Zs meta Ws` line after each `MERGE`, and the four values in the run report per cell and summed per run. The report half is unconditional (N6 must be harvestable from any run); `phase2_timers` gates the log line only | `server.py`, `src/runreport.py` |
| **I5** | arnis `region_stats` counts regions leaving via `flush_region_via` versus `save_java` and prints one `[regions] flushed= saved= canonical= discarded= flushed_discarded= saved_discarded=` line per cell — the split that decides which of the two HOLD region-write tasks holds the money | arnis `src/world_editor/mod.rs`, `src/world_editor/java.rs` |
| **I4** | Re-profile of a non-centre and a ring-3 cell under the real benchmark config, with the stream-to-disk state recorded | measurement only |
| **C5** | `_scan_cell_health` tails the cell log instead of reading it whole | `server.py` |
| **C2** | The master world path is resolved **once per run** in `_submit_cells` and carried on the job dict, closing a wrong-world-merge race that was live for about five hours per long run | `server.py` |
| **M1** | Three settings, all defaulting to today's behaviour: `phase2_timers` (`True`), `canonical_regions` (`False`), `parse_fast_json` (`False`). All three in `default_settings()`, `presets._MACHINE_KEYS`, `server._META_SKIP_SETTINGS`, and the `/api/settings` clamp | `src/project.py`, `src/presets.py`, `server.py` |
| **M4a** | `scripts/golden_hash.sh` rebuilds before hashing, instead of green-lighting a stale binary | arnis `scripts/golden_hash.sh` |
| **D2** | `to_bits()` blur-equality test, written ahead of the blur work it will gate | arnis tests |
| **M5** | This section, and the measurement rewrite in `generation-performance.md` | `docs/` |

`canonical_regions` and `parse_fast_json` are **declared and unread**. Nothing emits
`--canonical-regions` and no parser is swapped. They ship now so the HOLD tasks
behind them can be flag-gated later without a settings migration.

## The two corrections

Both contradict figures phase 1 left in the docs, and both change what the next
branch should attempt.

1. **The phase-2 per-cell profile was taken on cell `(0,0)`, the cheapest cell in
   the grid** — one z11 tile, 12.4 s against a 27.2 s run median. Scaling it by 81
   gave 2244 cpu-s, a 93.5 s CPU floor, 58% efficiency and ~1.7x scheduling headroom.
   Corrected off the machine-level `timeline[].cpu` integral in the real reports:
   **>= 3395 cpu-s, a ~141 s floor, ~88% efficiency, ~1.11-1.15x headroom.** That
   integral is a **floor on demand**, not an independent CPU-second measurement — it
   is used here only because it is a lower bound and already exceeds the old estimate
   by 51-69%.
   The warm run is already near the CPU floor; further wall gains must come from
   removing CPU work, not from scheduling it better.
2. **Merge offload is dead as a performance idea.** merge + prune + health + meta
   together **derive** to 4.24-6.54 worker-s per 81-cell run, about **0.27% of worker
   time** — some twelve times below the harness noise floor. I1's timers now measure
   it directly, with a ceiling of 7 s per run as the tripwire. The ~37 h of
   `MergePool` / terminal-completion / Stop-rework design is priced and rejected,
   not deferred.

## Still open

- No live full render on this branch. The bench harness can now be trusted to run
  the arm it claims to (H1 + H2); it has not been swept.
- Per-run CPU seconds are still not in the report (`I6`, HOLD), so the only run-level
  CPU figure is the `timeline[].cpu` integral — which is **not independent of wall
  time** and must not be used to claim a cpu-second win.
- The `bench.mark` telemetry channel is still unreachable from a Meld-driven run
  (`I0`, HOLD), so the `parse` / `place` internals stay unsplit.
- The atomic-write fixes (`C3a`, `C4`) are HOLD behind a Windows sharing hazard, and
  land after `C2` if they land at all.
- `docs/index.md` still has no entry for `generation-performance.md`.
