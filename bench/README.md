# bench/ — legacy scheduler vs governor

`bench_scheduler.py` renders the **same area, twice, with only the scheduling knobs moved**, and
answers two questions in one pass:

1. **Is the governor faster?** wall time, cells/min, effective parallelism, CPU/RAM, median and
   p95 cell time, peak workers — read out of Meld's own `meld-report.json`.
2. **Did it change the world?** the per-cell `block_hash` vector must be **identical** across
   every config. A mismatch aborts the sweep and voids every timing above it.

Question 2 is the one that matters. A scheduler that is 30% faster and moves one block is not a
faster scheduler, it is a bug. The gate is therefore not advisory: a strong mismatch exits `3`.

---

## Quick start

```bash
# 1. validate the matrix and print the plan. Renders nothing. Always start here.
python bench/bench_scheduler.py --dry-run

# 2. check the harness's own logic (config split, metrics, determinism gate).
#    No server, no arnis binary, no network — safe in CI.
python bench/bench_scheduler.py --selftest

# 3. the fast development config: 4x4 = 16 one-region cells at 1:1, both arms.
python bench/bench_scheduler.py --only smoke

# 4. the real sweep: legacy w4 / w12 / w16 vs governor auto<=20, at 1:1 and at 1:10.
python bench/bench_scheduler.py

# 5. one group only
python bench/bench_scheduler.py --only 1to1-cs4
```

`--only` takes a **run name or a group name**. Prefer a group: a single run has nothing to be
compared against, and the determinism gate will skip it.

### Running beside your own Meld

The harness starts its own server (port 5630) so nothing about your session leaks into the
numbers. If your Meld is already up, either close it or give the bench its own data directory —
that gives it its own projects and its own single-instance lock, while the caches stay shared:

```bash
python bench/bench_scheduler.py --port 5799 --data-dir C:/tmp/benchdata --only smoke
```

Attaching to a running Meld works too, but read the determinism caveat below first:

```bash
python bench/bench_scheduler.py --attach http://127.0.0.1:5630 --only smoke
```

---

## What it needs before it can measure anything

- **An arnis binary Meld can resolve.** Meld searches the repo folder, then its parent, then
  `arnis-source/target/release`, then `bin/`. The bench measures whichever one it finds — so if
  you are benching a new generator build, drop it where Meld looks *first*, or the sweep will
  faithfully measure the old one. The spawned server prints `arnis binary: <path>` into
  `bench/results/<label>/server.log`; check that line before trusting a run.
- **`requests` and `psutil` are optional.** Without `requests` the client falls back to
  `urllib`; without `psutil` the swap-growth watchdog and the attached-server kill go quiet.
  Everything else is stdlib.
- **A warm cache**, which the harness builds itself — see below.

### Prep: measuring compute, not the network

Before the first run the harness creates a throwaway `bench-prep` project and, for each distinct
area in the matrix:

- **surveys the elevation range** once, and then feeds *that same range* to every run as a manual
  lock. Elevation is a world-shaping input; letting each run survey for itself would let it drift
  between arms and quietly void the gate.
- **bakes the elevation tiles** (`/api/datapack/bake-mapterhorn`) at the generation scale.
- **pre-warms Overture** (`/api/overture/prewarm`) when `world_settings.buildings` is on.

Turn any of it off in `matrix.json` under `prep`. The harness **never sets `ARNIS_OFFLINE`** —
forcing offline mode would change what the generator produces, and this harness is not allowed to
change what the generator produces.

---

## Reading the table

Every finished sweep prints (and writes) one section per **group**. A group is one bbox at one
scale at one cell size; runs inside it differ only in scheduling. Groups are never compared to
each other — a 1:1 world and a 1:10 world are different worlds.

```
### 1to1-cs4  (baseline: `legacy-w4`)

| config | arm | w set | wall s | Δ wall | cells/min | Δ thru | eff.par | cpu avg | ram peak | median s | p95 s | w peak | outcome |
```

| column | means | read it as |
|---|---|---|
| `w set` | what was asked for: a fixed worker count, or `auto<=N` | the *input*, not what happened |
| `wall s` | `summary.elapsed_s` from `meld-report.json` | the headline |
| `Δ wall` | change vs this group's baseline, **sign-corrected: `+` is always better** | `+12%` = twelve percent faster |
| `cells/min` | merged cells ÷ wall | the only throughput number that matters |
| `Δ thru` | same sign convention | `+` = more cells per minute |
| `eff.par` | Σ(cell durations) ÷ wall — *effective parallelism* | "how many cells were genuinely in flight at once". Below `w set` means workers were queued, blocked, or starved |
| `cpu avg` / `ram peak` | from the run timeline | `ram peak` above ~90 means the next run is one cell away from swapping |
| `median s` / `p95 s` | per-cell wall time | **the tell.** Adding workers inflates these long before throughput moves |
| `w peak` | the highest concurrency actually reached | for a governor arm this is its *answer*: where it stopped climbing |
| `outcome` | `ok`, or how many cells failed | any failure makes the row's timings suspect |

**The trap this bench exists to expose.** Measured on the reference machine, at 1:1 / cell size 4,
throughput is *flat* at ~21-23 cells/min across every worker count from 8 to 24, while median cell
time climbs 21.1s → 33.9s → 41.6s → 56.5s → 63.5s. Every worker past ~8-12 buys nothing and costs
per-cell latency and RAM. So a governor arm that ends at `w peak` 8-12 with the same `cells/min`
as `legacy-w16` and a **lower median** is the win — even though `Δ wall` reads ~0%. Judge the
governor on `median s`, `p95 s`, `ram peak` and `w peak`, not on wall time alone.

### Measured ground truth

The numbers `THROUGHPUT_MODEL` and the paragraph above are built from, all from real
`meld-report.json` files on the reference machine (24 logical cores, 31.4 GB, NVMe,
Intel Core Ultra 9 275HX, Windows 11):

| area | scale / cell size | workers | cells | wall | throughput | median cell | ram peak |
|---|---|--:|--:|--:|--:|--:|--:|
| Berlin | 1:1 / 4 | 8 → 24, raised live | — | — | **flat 21-23 cells/min at every step** | 21.1s @8w, 33.9s @12w, 41.6s @16w, 56.5s @20w, 63.5s @24w | 81% |
| Bucuresti | 1:1 / 4 | 16 | 400 | 1221 s | 19.7 cells/min | — | 88% |
| — | 1:1 / 8 | 15 | 100 | 948 s | 6.3 cells/min | 123.9 s | 93% (too close to swap) |
| — | 1:20 (documented, not re-measured) | — | — | — | ~1.02 cores/cell, ~1.2 GB/cell | — | — |

Read together: at 1:1 the knee is **~8-12 workers** (~7.75 cores per cell), and at 1:10+ a cell
is cheap in cores but slow in wall time, so the knee moves out to ~16-20. A governor that climbs
past those numbers is not tuned, it is just louder.

---

## The determinism gate

`ARNIS_BLOCK_HASH=1` goes into the spawned server's environment, every arnis child inherits it,
and each one prints

```
[BENCHMARK] block_hash=<16 hex digits>
```

at the end of generation — arnis's own content hash of every region the cell wrote. Meld tees
every generator line into `<project>/logs/cell-<rx>_<rz>_<size>.log`, and the harness reads the
hashes back out of those logs. Inside each group, every run's `{cell: hash}` vector must equal
the group's first run — same keys, same values.

The report names the failing cells:

```
- **1to1-cs4**: **MISMATCH** vs baseline `legacy-w4` (81 hashes, block_hash — strong ...)
    - `governor-auto20`: 3/81 cells differ
        - `2,-1,4`: baseline `a41f...` vs run `9c02...`
```

A strong mismatch exits `3` with *"the sweep is void"*. That is the intended behaviour: fix the
scheduler, then re-read the timings.

### The fallback, and why it is weaker — READ THIS

If no `block_hash` lines are found — which is what happens when you `--attach` to a Meld that was
**not** started with `ARNIS_BLOCK_HASH=1`, or when the resolved arnis binary predates the flag —
the harness falls back to **hashing the produced region files**: sha256 of each `.mca` with the
4 KiB chunk-timestamp table zeroed.

**Say it plainly: a region-file mismatch is not proof of a bug.** `.mca` bytes depend on the order
chunks were written and on zlib's output, and a different worker or flush layout can legitimately
change both without moving a single block. So:

- a region-file **match** is still good evidence of sameness;
- a region-file **mismatch** is *"look closer"*, not *"the scheduler is broken"*. It is reported
  as `weak`, and by default it does **not** fail the sweep (exit `0`/`1`). Pass `--strict-fallback`
  to make it fatal anyway.

Before filing a bug off a weak mismatch, re-run without `--attach` so the harness starts a server
with the env var set and you get the strong gate.

`--hash-mode` controls this: `auto` (default, block_hash if present else region files), `block`
(strong or die), `region` (force the fallback), `off` (timings only, no gate).

---

## Aborting

The sweep stops itself when the numbers would be worthless anyway:

| trigger | default | why |
|---|---|---|
| RAM above `ram_abort_pct` for 3 consecutive samples | 95% | the machine is one allocation from swapping |
| swap grew by more than `swap_growth_mb` | 4096 MB | already swapping; every timing after this is fiction |
| run exceeded `run_timeout_s` | 7200 s | watchdog |
| no cell finished for `stall_timeout_s` | 1800 s | wedged |
| the run never started within 5 min | — | queue refused, or the elevation gate is unhappy |
| the server stopped answering | — | it died |

An abort **kills the server process directly** (`taskkill /F /T` on Windows, the process group
elsewhere), never `/api/stop` — an abort must not depend on the thing that may be wedged. Whatever
completed before the abort is still written to `bench/results/`, and the sweep exits `1`.

Tune the thresholds in `matrix.json` → `watchdog`.

---

## Wall-time budget

`--dry-run` estimates each run from `THROUGHPUT_MODEL` in the script — **measured on the reference
machine (24 logical cores, 31.4 GB, NVMe), not a promise.** Rough guide, warm cache:

| what | cells | budget |
|---|---|---|
| `--only smoke` (16 one-region cells × 2 arms) | 32 | **~2-5 min** |
| `--only 1to1-cs4` (81 cells × 4 runs) | 324 | **~20-30 min** |
| `--only 1to10-cs4` (25 cells × 2 runs) | 50 | **~5-10 min** |
| the whole default matrix | ~400 | **~30-60 min** |

Add **5-25 min of prep on a cold cache** (elevation survey + tile bake + Overture pre-warm), and
seconds on a warm one. Add ~20 s per run for the server restart between runs
(`--no-restart-between` skips it, at the cost of carrying governor state across arms).

Budget generously: the estimator assumes throughput is flat at and above the knee, so a
deliberately under-provisioned arm like `legacy-w4` is the one most likely to overrun.

---

## Output

```
bench/results/<label>.json        every run, every metric, the spec, the governor snapshot,
                                  the full hash vectors and the gate verdict
bench/results/<label>.md          the same markdown table that was printed
bench/results/<label>/server.log  the spawned server's stdout (find "arnis binary:" here)
bench/results/<label>/reports/    a copy of each run's meld-report.json
```

Timings are **never re-derived** by the harness. `meld-report.json` (schema `meld-run-report/3`)
is the source of truth; the harness only reads `summary{}` and `cells[]` out of it. The one number
it measures itself is `harness_wall_s`, kept alongside for sanity, and used only if a report has
no `elapsed_s`.

---

## Editing `matrix.json`

```jsonc
{
  "label": "bucharest-governor",       // names the results files
  "seed": 1,                           // one seed for the whole sweep
  "site":  { "origin": {...}, "bbox": {...} },
  "world_settings": { ... },           // applied IDENTICALLY to every run
  "prep":  { ... },
  "watchdog": { ... },
  "runs": [ { "name": "...", "group": "...", "arm": "legacy|governor",
              "baseline": true, "workers": 4, "governor_mode": "off",
              "governor_max_workers": 0, "cpu_target_pct": 90,
              "flush_threads_cap": 12, "ram_headroom_mb": 2048,
              "cell_size": 4, "scale": 1.0, "repeats": 1, "bbox": {...} } ]
}
```

Two rules the harness enforces, because breaking either makes the gate meaningless:

1. **World-shaping settings may not differ between runs.** `scale`, `cell_size`, `seed`,
   `buildings`, `terrain`, `caves`, `land_cover`, height flags, `region_format` and the rest live
   in `world_settings` and are applied to everything. A run whose `settings` block touches one is
   refused. `--allow-world-override` opens that door, and you own what comes through it.
2. **Every run in a group shares bbox + scale + cell size.** Otherwise the group's Δ column is
   comparing two different amounts of work. Give the odd run its own `group`.

Only these may differ per run: `max_workers`, `governor_mode`, `governor_max_workers`,
`cpu_target_pct`, `flush_threads_cap`, `ram_headroom_mb`, `min_threads_per_worker`,
`worker_autoscale`, the CPU-stagger knobs, `stream_to_disk`, `arnis_log_verbose`.

To bench your own city: change `site.bbox` **and** the per-run `bbox` overrides that shadow it,
keep the origin inside the area, and re-run `--dry-run` until the planned cell counts look sane.

---

## Exit codes

| code | meaning |
|---|---|
| `0` | sweep finished; the determinism gate passed (or was skipped / weak-and-not-strict) |
| `1` | sweep aborted or errored — partial results were still written |
| `2` | the matrix is invalid; nothing ran |
| `3` | **determinism gate failed on strong evidence — the sweep is void** |

---

## Full flag list

| flag | default | what |
|---|---|---|
| `--matrix PATH` | `bench/matrix.json` | the config |
| `--only A,B` | all | run names **or** group names |
| `--label NAME` | matrix `label` | names the results files |
| `--repeats N` | per-run | override every run's `repeats` |
| `--dry-run` | off | validate + print the plan, render nothing |
| `--selftest` | off | check the harness's own logic; no server |
| `--attach [URL]` | off | use a running Meld instead of starting one (weakens the gate) |
| `--port N` | 5630 | port for the spawned server |
| `--python PATH` | this interpreter | interpreter used to start `server.py` |
| `--data-dir PATH` | repo root | `MELD_DATA_DIR` for the spawned server |
| `--cache-dir PATH` | `<repo>/cache` | `MELD_CACHE_DIR` — keep it shared so caches stay warm |
| `--hash-mode M` | `auto` | `auto` / `block` / `region` / `off` |
| `--strict-fallback` | off | make a weak (region-file) mismatch fatal too |
| `--no-restart-between` | restarts | keep one server across runs |
| `--allow-world-override` | off | permit per-run world-setting overrides |
| `--cleanup` | off | delete the bench project workspaces at the end (worlds are kept) |
