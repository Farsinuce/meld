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
