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
