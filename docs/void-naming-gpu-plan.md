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

### Correction: what `osm_fetch` actually is, and why the share depends on it

`osm_fetch_ms` wraps a three-way branch (`main.rs:457-477`): `--osm-tile-dir` reads
the cell's slippy tiles from a local cache, `--file` reads a pre-merged JSON, and
with neither flag arnis makes a **live HTTP request to the Overpass API**.

The runs above used no flag, so they went to the network. **Meld does not work that
way** - it pre-caches tiles and passes `--osm-tile-dir`. Same bbox
(`44.425,26.095,44.445,26.125`), both paths measured:

| | cached tiles (Meld's path) | live Overpass |
|---|---|---|
| `osm_fetch_ms` | **1157** | **13192** |
| `parse_osm_ms` | 720 | 44 |
| `generation_time_ms` | 2658 | 2645 |
| cell total | **~4.5 s** | ~15.9 s |
| **CPU compute share** | **~75%** | ~17% |

So the "compute is only ~10% of a cell" figure is a property of the *uncached*
path. In the regime Meld actually runs, a cell is ~4.5 s of which only ~1.2 s is
reading tiles off disk; **the rest really is CPU work.**

This raises the GPU ceiling considerably - deleting all of `generation_time` in the
cached regime would be ~2.4x on a cell, not 1.16x - and it is the strongest
argument *for* the idea. It does not change the recommendation, because the
decisive objections below were never about Amdahl: they are about one GPU shared
by many processes on a platform with no MPS, per-cell context cost, and a
reproducibility gate that GPU floating point cannot satisfy. It does, however,
make the CPU fixes in this section considerably more valuable than the uncached
numbers suggested, since they attack the ~75% that is genuinely compute.

### Why it gets worse, not better, in a real Meld run

**Correction to an intuition worth stating, because it is the crux.** It is tempting
to argue "the CPU is saturated, so move work to the idle GPU". Meld does **not**
saturate the CPU at default settings. Per-worker demand is ~0.81 cores, so:

| workers | cores demanded | CPU utilisation |
|---|---|---|
| 4 (the stored default, `src/project.py:145`) | 3.2 | **13%** |
| 11 (what Recommend produces on this box) | 8.9 | **37%** |
| ~30 | 24 | 100% |

Default Meld leaves **63-87% of the CPU idle, waiting on Overpass.** You cannot
relieve an idle resource. Offloading there is not neutral, it is negative.

The rest of the case against:

- **CUDA MPS - the feature that lets processes share a GPU concurrently - is
  Linux only.** On Windows/WDDM, concurrent processes are *time-sliced*, never
  co-resident, so N processes give the aggregate throughput of one. A 24-way CPU
  queue becomes a 1-way GPU queue.
- **Meld spawns one `arnis.exe` per cell**, so GPU context init is paid **per
  cell** (100-300 ms realistic on a 33.8 GB + 16 GB box). Against a ~400 ms
  serialized per-cell GPU budget, that *straddles the budget* - at the pessimistic
  end the GPU becomes the new bottleneck and throughput ends up worse than
  CPU-only.
- **VRAM:** 8 contexts x 300-800 MB = 2.4-6.4 GB of 16 GB burned on empty
  contexts, on a laptop GPU that is also driving the display.
- **WDDM's 2-second TDR** becomes a scheduling hazard with 8 queued contenders; a
  driver reset kills a multi-hour render.
- **Meld has no cross-worker resource serialiser** - no semaphore anywhere in
  `server.py`. Gating a GPU across workers means building that machinery first.
- **It breaks the golden-hash gate.** `tests/golden_hashes.txt` pins five worlds
  to exact 64-bit hashes. GPU floating point is not bit-reproducible across
  vendors, generations or drivers. The one prior-art project with a published
  win, C2ME-OCL, ships exactly that failure: "biome borders may get shifted by one
  or two blocks."
- **The world model is GPU-hostile.** `FnvHashMap<region> -> FnvHashMap<chunk> ->
  FnvHashMap<section>` with per-block `Arc<NBT>` side tables and first-writer-wins
  semantics. Porting element placement means rewriting ~20k lines (buildings.rs
  alone is 7,951) into a language with no strings and no hash maps. No prior art
  exists for GPU OSM-to-voxel.

### The arithmetic

| GPU eats... | share of wall | max speedup |
|---|---|---|
| cave density only | 1.4% | **1.014x** |
| all of `element_placement` | 6.5% | 1.070x |
| **every CPU phase in the program** | 13.8% | **1.161x** |

With OSM pre-cached (`--osm-tile-dir`), the cell drops to ~2883 ms and the numbers
improve but stay modest: cave density **1.108x**, every GPU-mappable FP kernel
1.275x, a full `element_placement` port 1.83x. At fleet saturation the hard
ceiling for the one defensible kernel, with a *free and instant* GPU, is
**1.70x** (16.3 -> 9.6 core-seconds).

Worth conceding: the cave density field is **41% of the cell's CPU core-seconds**
while being **1.4% of its wall clock**. That gap is the entire argument.

### Confidence

| Claim | Confidence |
|---|---|
| GPU gives **> 20%** end-to-end improvement in a real Meld run | **12%** |
| GPU gives **> 2x** | **2%** |
| Cave density is a genuinely GPU-shaped kernel | ~95% (conceded) |
| A 30-line CPU cache captures ~half the same win | ~85% |

### What to do instead - four fixes that beat the GPU ceiling

Ranked. The first deletes work (helps latency *and* throughput); the rest add
parallelism or remove waiting.

1. **Corner-plane cache in `carve_region`** (`caves/mod.rs:150-159`). Every 4x8x4
   cell recomputes all 8 corners; vertically adjacent cells share exactly 4, so
   carrying the top plane into the next iteration **halves** `combined_density`
   calls - verified by reading the loop. (Sharing across columns could approach
   4-8x but fights the `par_iter` decomposition at `caves/mod.rs:143`.)
   Byte-identical, ~30 lines, golden gate intact. **Do this before benchmarking
   any shader - it shrinks the GPU's remaining prize by about half.**
2. **Parallelise the post passes.** `sweep_floating_veg` (`water_depth.rs`) and
   `seal_floating_fluid_region` (`caves/mod.rs`) contain **zero rayon** - 382-446 ms
   on **1 of 24 cores**. rayon over Z-strips: ~17% off `generation_time`, in an
   afternoon.
3. **Raise `max_workers`.** Stored default is **4**; saturation is ~30. Going
   4 -> 11 is **~2.75x fleet throughput for free** - more than the theoretical
   ceiling of GPU-ing the only defensible kernel. It is a number, not code.
4. **Kill `osm_fetch`.** 16.7-21.2 s of a 20-24 s cell. `--osm-tile-dir` and
   `--offline` already exist and Meld already has a .pbf bake pipeline. Fully
   overlapping or eliminating the fetch is worth **~7x end-to-end** - roughly 6x
   better than an infinite GPU applied to every compute phase combined.

Also worth noting from the profile: `tile::DEFAULT_TILE_SIZE = 512` gives a cell
of this size only **4 tiles**, so element placement runs 4-wide on a 24-core box.
Halving the tile size would widen it, but the eviction bookkeeping assumes one
tile per region, so it needs care.

### If a GPU toggle is still wanted

Ship it only after the four fixes above, and only if `--caves` stays on by
default, OSM goes fully offline, and the profile *still* shows noise on top. There
is nothing to drive today: arnis 3.1.2 has no GPU flag and no GPU crate.

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
