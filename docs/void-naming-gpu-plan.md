# Void worlds, GUI world naming, and the GPU question

**Branch:** `feat/gpu-void-naming` in both repos (arnis fork and light-meld). Local only.
**Date:** 2026-08-21
**Targets:** arnis fork 3.1.2 → 3.1.3, Meld 1.9.3 → 1.9.4

Three requests came in together. Two are worth building and one is not, and the
measurements say which is which. The river-bank bug is deliberately out of scope.

---

## 1. GPU acceleration — recommended AGAINST

### What the profile actually says

One cell, warm caches, RTX 5080 Laptop / 24-core Ultra 9 275HX, bbox
`45.60,25.50,45.75,25.75` at scale 0.05 with terrain, caves and baked lighting:

| phase | run 1 | run 2 |
|---|---|---|
| `osm_fetch_ms` | 16693 | 21158 |
| `element_placement_ms` | 1307 | 738 |
| `post_passes_ms` | 395 | 382 |
| `save_ms` | 330 | 326 |
| **`generation_time_ms` (all CPU compute)** | **2281** | **1668** |

**All of the CPU compute is ~2 s inside a ~20 s cell.** Even a GPU that finished
instantly cannot touch the other 18 s, which is network.

Cave generation is the only genuinely GPU-shaped workload here, so it was measured
directly by toggling it on the same bbox and seed:

| | caves on | caves off | delta |
|---|---|---|---|
| `element_placement_ms` | 918 | 473 | +445 |
| `post_passes_ms` | 446 | 280 | +166 |
| **`generation_time_ms`** | **2010** | **1363** | **+647** |

So caves are 32% of compute, and compute is ~10% of the cell. **A perfect,
zero-cost GPU cave kernel buys about 3% of end-to-end wall clock.**

### Why it gets worse, not better, in a real Meld run

- **Meld runs many arnis processes at once** (up to ~11 on this box). They would
  all contend for one GPU.
- **CUDA MPS — the feature that lets processes share a GPU concurrently — is
  Linux only.** On Windows/WDDM, concurrent processes are *time-sliced*, never
  co-resident. Eight processes wanting the GPU queue behind each other.
- **Meld has no cross-worker resource serialiser.** There is no semaphore
  anywhere in `server.py`; every lock is a state mutex. Gating a GPU across
  workers means building that machinery first.
- **Each process pays a fresh GPU context init** (tens to hundreds of ms), on a
  phase that only lasts ~1 s.
- **The CPU is already saturated.** arnis parallelises with rayon inside each
  process and Meld runs several processes; offloading to one shared GPU moves the
  queue rather than shortening it.
- **No CUDA toolkit on this machine** (`nvcc` absent), so the CUDA path also adds
  a user-facing install dependency. `wgpu` avoids that but gives up peak throughput.
- **The world model is GPU-hostile.** It is
  `FnvHashMap<region> → FnvHashMap<chunk> → FnvHashMap<section>` with per-block
  `Arc<NBT>` side tables, not a dense array. Element placement, the post passes
  and flood fill are hash probes, allocations and early exits. Porting them means
  rewriting the world model.

### Confidence

| Claim | Confidence |
|---|---|
| GPU gives **< 20%** end-to-end improvement in a real Meld run | **~90%** |
| GPU gives **< 2×** | **~97%** |
| Cave density field alone could be 10–50× faster *as a kernel* | ~70% |
| That kernel changes end-to-end wall clock by more than 5% | **~10%** |

### What to do instead — same goal, far cheaper

Two CPU fixes beat the GPU port outright, and both were confirmed by reading the code:

1. **Parallelise the post passes.** `sweep_floating_veg` (`water_depth.rs`) and
   `seal_floating_fluid_region` (`caves/mod.rs`) contain **zero rayon** — 382–446 ms
   runs on ONE core while 23 sit idle. They are independent per-column scans.
2. **Halve the cave density work.** `caves/mod.rs:143` parallelises across cell
   columns, but inside a column every cell recomputes all 8 corners, and
   vertically adjacent cells share exactly 4 of them (`caves/mod.rs:150-159`).
   Carrying the top corner plane into the next iteration is ~30 lines and
   byte-identical output. (The full 8× the corner layout suggests would need
   cross-column sharing, which fights the parallel decomposition — claim 2×.)

Both are ordinary Rust, keep the golden-hash gate intact, and need no new
dependency, no driver, and no toggle.

### If a GPU toggle is still wanted

Ship it only after a 30-minute spike measuring `wgpu` device-init cost on this
machine. There is nothing to drive today: arnis 3.1.2 has no GPU flag among its
CLI flags and no GPU crate in `Cargo.toml`.

---

## 2. Void world — recommended, and already proven

### It is much closer than expected

The bundled `assets/minecraft/level.dat` **already declares `minecraft:flat`**:

```
generator: minecraft:flat
layers: dirt ×2, grass_block ×1
structure_overrides: strongholds, villages    lakes: 0    features: 0
```

So unvisited chunks never got vanilla terrain — they got a superflat plane. A void
world is that preset with no layers.

### Proven, not assumed

A world was generated, its `level.dat` patched to empty `layers` and empty
`structure_overrides`, then booted on **Leaf 1.21.11** and force-loaded well
outside the generated area:

| region | written by | chunks | non-air blocks |
|---|---|---|---|
| `r.6.6.mca` | the server | 484 | **0** |
| `r.5.5.mca` | the server | 324 | **0** |
| `r.0.0.mca` | arnis | 1024 | 309,759 |

**808 server-generated chunks, entirely empty.** The void half works.

The same test shows the other half of the job: arnis's own region still contains a
grass plane and bedrock (260,300 grass_block, 13,104 bedrock), because the base
chunk pass and ground generation still run.

### What to build

1. `--void` flag in `args.rs`.
2. Patch `WorldGenSettings` in `scaffold_world` (`world_utils.rs:185-200`, where the
   NBT is already being edited) to empty the layers. **Write it as an explicit
   air layer** rather than an empty list unless the Rust `fastnbt` round trip is
   verified — an empty NBT list can serialise with a `TAG_End` element type. The
   semantics are proven; the Rust serialisation is not yet.
3. Gate ground generation and the deepslate / land-scatter / water-depth siblings.
4. Make the base-chunk second pass emit all-air chunks instead of grass at Y=−62.
5. Stop seeding `region.template` (1024 pre-baked cobblestone chunks that survive
   any chunk left untouched).
6. Refuse `--void` with `--caves` (caves force `fillground`; there is no rock to carve).
7. Fix spawn — the player currently lands at y=−61 over nothing.

### The trap that matters most

**`merge.py`'s drift guard, not `finalcheck`.** A cell whose content does not reach
within one region of both far edges raises `MeldCoordinateDriftError`, and
`server.py` classifies `drift` as deterministic, so it is **never retried — the cell
is silently lost**. A void cell over sea, forest or desert has little or no content
and fails outright. Both `merge.py` and `finalcheck.py` need void-awareness before
this ships.

Also: keep writing all 1024 chunks as air rather than omitting them. Omitting
produces exactly the 8192-byte `.mca` / 142-byte `.b_linear` files that
`finalcheck._scan_present` treats as empty holes to retry. All-air chunks cost a
full 4.2 MB/region in Anvil but only ~8 KB in B_Linear, so **void pairs naturally
with `--region-format blinear`**.

**Confidence: 90%** that this ships cleanly, with the drift guard as the main risk.

---

## 3. World naming in the GUI — recommended, small

arnis has **no naming input anywhere**: no GUI field, and no CLI flag for any
format. Java is the gap twice over — both Java branches hard-code `None`
(`main.rs:433`, `gui.rs:1031`), and even a name would be dropped, because
`WorldEditor` stores it in a field named `bedrock_level_name` that only the
Bedrock writer reads. Java's `LevelName` comes solely from the folder name at
scaffold time (`world_utils.rs:193-196`).

Shape:
- `Args.level_name: Option<String>`, threaded into `create_world_at` and
  `apply_java_world_settings` (the cheapest correct Java consumer).
- A text input in the GUI's World settings, forwarded via `gui_create_world` /
  `gui_start_generation`.
- **Sanitise for the folder, keep the raw string for the NBT** — exactly what Meld
  already does. Collisions become `name (2)`, `name (3)`.
- **`--level-name` must touch only the NBT string, never the output directory** —
  Meld reads back from the exact path it passed.

**Meld needs no change here**: it already names worlds end to end (`#worldName` →
`/api/name` → `patch_level_name`/`gold_name`, re-patched at merge).

Bonus bug found: `gui.rs:356` computes `30 - base_name.len() - 2`, a usize
underflow unreachable today but reachable the moment a user can type a long name.

**Confidence: 95%.**

---

## Meld-side toggles

The settings pipeline is a clean ~8-step recipe, following `native_region_format`:
`web/index.html` control → `/api/settings` → validation in `server.py` →
defaults in `src/project.py` → `src/arnis_cmd.py` → arnis flag.

Two non-obvious rules:
- A new drawer **must be explicitly lifted** in the Generate-step relocation code
  (`web/index.html:4831-4836`) or it renders below the divider — the same bug the
  region-format picker hit.
- Host-specific settings belong in `_META_SKIP_SETTINGS` (`server.py:1876`) so world
  metadata never hijacks another machine. A GPU toggle would qualify; a void
  toggle must not.

Only **void** gets a Meld toggle in this release. Naming needs none, and GPU has
nothing to drive.
