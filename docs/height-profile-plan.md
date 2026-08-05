# PLAN — configurable, version-aware build height (`HeightProfile`)

Status: **plan only, nothing implemented.** Written 2026-08-05 against arnis fork 3.0.4 /
Meld 1.8.0. Awaiting approval before any code changes.

Goal: a user picks a region and a target Minecraft version and gets a world whose vertical
range fits the real terrain — *including terrain below vanilla's floor* — with one object
defining that geometry for the datapack, the chunk writer and the UI alike.

---

## 1. What exists today (audited, with evidence)

### 1.1 The vertical geometry is defined in six places

| Where | What it decides | File |
| --- | --- | --- |
| `MIN_Y = -64`, `MAX_Y = 2031` consts | every block write is clamped to this | `src/world_editor/common.rs:9,16` |
| `MIN_SECTION_Y = MIN_Y/16` | lowest section index | `src/world_editor/common.rs:11` |
| `extended_max_y_for(args)` | 2031 Java / 512 Bedrock | `src/ground.rs:809` |
| `scale_to_minecraft()` | datum (`ground_level`), `v_scale`, compression, upper clamp | `src/elevation/postprocess.rs:1200` |
| `datapack_tall/…/overworld.json` | `min_y: -2032`, `height: 4064` — **fixed literal** | `assets/minecraft/datapack_tall/data/minecraft/dimension_type/overworld.json` |
| Meld `ground_level: -56`, `disable_height_limit: false` | the datum and the on/off switch | `light-meld/src/project.py:25,51` |

Nothing reconciles them. The spec's "exactly one object" rule is violated six ways, and the
disagreement is not theoretical — see 1.2.

### 1.2 Confirmed defects

**D1 — The floor is a lie (severity: high).**
The bundled datapack declares `min_y: -2032`, but every write goes through
`y.clamp(MIN_Y, MAX_Y)` with `MIN_Y = -64` as a *compile-time constant*
(`common.rs:455,464,479,648,693`). Extended height therefore only ever extends **upward**.
Terrain below vanilla's floor — the headline capability in the brief — cannot be produced
today at any setting. The world advertises 2032 blocks of basement and generates none of it.

**D2 — Meld's merge drops the datapack (severity: high, currently latent).**
`merge_cell_into_master()` copies region files and `level.dat` only
(`light-meld/src/merge.py:150-170`); there is no `datapacks/` copy. Arnis installs the pack
into each *cell* world (`src/main.rs:419`) and registers `file/arnis_tall` inside that cell's
`level.dat` (`src/world_utils.rs:308`). Meld then copies one cell's `level.dat` into the
master world. Result: **the master world asks for a datapack whose files were never
copied.** The world opens at vanilla height with the tall terrain invalid.
Latent only because every project on this machine has `disable_height_limit: false`
(verified across all `projects/*/project.json`) — the first user to tick the box hits it.

**D3 — Fixed preset, not derived (severity: medium).**
The pack is a constant 4064-tall dimension regardless of terrain. A Dutch polder and the
Carpathians get identical geometry. Cost is not free: heightmap entries widen to 12 bits
(`pack_heightmap_values`, `java.rs:1143`) and the client allocates the full column range.

**D4 — No version awareness at all (severity: medium).**
`DATA_VERSION = 4440` is a single hardcoded const (`java.rs:24`). `pack.mcmeta` carries a
static `61..101` format range with two overlays. There is **no target-version input**, hence
no `<1.17` gate, no pre-1.18 chunk-layout branch, and no way to be correct for more than one
version at a time. Section 3's capability matrix does not exist in any form.

**D5 — Datum has 8 blocks of headroom below it (severity: low, design smell).**
Meld ships `ground_level: -56` against a `-64` floor. Deep water then pushes the datum up
(`water_floor` in `ground.rs:207`), silently changing the sea-level of the world rather than
digging. With a real profile this becomes an explicit `underroom` knob.

### 1.3 What is already correct — do not "fix" these

- **Heightmap bit width is derived**, exactly as the brief requires:
  `bits = ceil(log2(total_height + 1)).max(9)` (`java.rs:1143`). A 4064 world gets 12 bits.
- **`yPos` / `min_section_y` are derived** from the sections actually present, starting at
  vanilla `-4` and expanding (`java.rs:896-954`).
- **Compression is reported**, though only to stderr per cell:
  `"Elevation compressed: {}m range -> {} blocks ({}:1)"` (`postprocess.rs:1275`).
- **Ordering is right on the CLI path**: the pack is installed immediately after
  `create_world_at` and before any chunk is written (`main.rs:408-431`).

---

## 2. Target design

### 2.1 `HeightProfile` — the single object

```rust
pub struct HeightProfile {
    pub min_y: i32,        // multiple of 16, -2032..=2031
    pub height: i32,       // multiple of 16, 16..=4064, min_y + height <= 2032
    pub datum_y: i32,      // Y that elevation 0 m maps to
    pub v_scale: f64,      // blocks per metre
    pub mc_version: String,
}
impl HeightProfile {
    pub fn elevation_to_y(&self, metres: f64) -> i32 { (metres * self.v_scale).round() as i32 + self.datum_y }
    pub fn max_y(&self) -> i32 { self.min_y + self.height - 1 }
    pub fn validate(&self) -> Result<(), HeightError>;   // enforces §1 of the brief
}
```

Constructed once per world, in one place, from: DEM min/max (or Meld's elevation lock),
`headroom`, `underroom`, target version capabilities, and the requested `v_scale`.
Consumed by exactly three call sites: the datapack writer, the chunk writer, and the UI.

### 2.2 Fitting

1. Span = `(peak + headroom) - (floor - underroom)`, in blocks after `v_scale`.
2. `height = round_up_16(span)`, `min_y = round_down_16(floor_y - underroom)`.
3. Clamp to the legal box; **round outward only at the end**.
4. If it still does not fit, reduce `v_scale`, recompute, and surface the ratio as a
   first-class field (`compression: 2.4` → shown in the UI, written to the sidecar, printed
   once per world rather than once per cell).
5. Never return 4064 unless the terrain genuinely needs it.

### 2.3 Version capability matrix — `versions.toml`, checked in

```toml
[["1.21.9"]]
data_version = 4440          # verified against a real world before committing
pack_format = 61
supports_extended_height = true
chunk_layout = "flat"        # 1.18+
```

Rule from the brief, adopted verbatim: **no constant enters this file from memory.** Rows
ship only when verified against a generated world or the wiki. The table starts with the one
value this repo already relies on (`4440`) plus whatever the user confirms; unknown targets
are refused, not guessed.

`< 1.17` → refuse at profile creation with the version floor, the span the region actually
needs, and the two real options (retarget ≥1.18, or accept compression into 0–255). Meld
disables the extended-height controls with that same reason inline — disabled and explained,
not hidden.

### 2.4 Sidecar

`<world>/meld.height.json`, written at world creation, immutable thereafter.
`detect()` on an existing world reads it; a disagreement with the requested profile is a
hard refusal with a "regenerate into a fresh directory" message. No migration, no best
effort.

---

## 3. Staged implementation, with confidence

Each stage is independently committable and leaves the tree green.

| # | Stage | Confidence | Note |
| --- | --- | --- | --- |
| **S0** | Fix **D2**: copy `datapacks/` in `merge_cell_into_master`, or install the pack into the master world once. Add a merge test. | **93%** | Small, isolated, fixes a world-breaking bug. Worth doing even if the rest is deferred. |
| **S1** | Introduce `HeightProfile` + `validate()` + unit tests. Nothing consumes it yet. | **95%** | Pure addition, no behaviour change. |
| **S2** | Route the *existing* geometry through it (`extended_max_y_for`, `scale_to_minecraft`, datapack writer). Defaults must stay byte-identical. | **90%** | Verified by re-running the seam harness: output must be unchanged. |
| **S3** | Emit the datapack **from** the profile instead of copying the fixed asset. | **88%** | JSON shape is already known-good; only the numbers become dynamic. |
| **S4** | Make the floor real (**D1**): `MIN_Y` becomes profile-driven instead of a const. | **75%** | The invasive one — ~20 call sites, several in caves/deepslate/highways, which §7 says I do not own. Mitigation below. |
| **S5** | Sidecar write + `detect()` + immutability refusal. | **90%** | Meld already writes `meld-world.json`; this is a sibling with stricter rules. |
| **S6** | `versions.toml` + capability resolution + the `<1.17` gate (Rust side and Meld UI). | **88%** mechanism / **60%** data | The mechanism is easy; *populating the table correctly is the risk*. Ships with verified rows only. |
| **S7** | Meld UI: version selector, extended-height gating with inline reason, compression ratio surfaced before the user commits. | **85%** | Touches the settings panel and `arnis_cmd.py`. |
| **S8** | Boundary + matrix test harness (see §4). | **90%** structural / **0%** in-game | See the honesty note in §5. |

**S4 mitigation.** `MIN_Y` becomes a process-global set once at startup
(`OnceLock<i32>`, default `-64`), so every existing call site keeps its current meaning
("the world floor") and an unset profile reproduces today's output byte-for-byte. I would
*not* rewrite cave/deepslate logic — only the constant they read. If any of them turns out to
assume `-64` arithmetically rather than semantically, I stop and report the coupling rather
than reaching into that subsystem.

**Recommended order:** S0 alone first (it is a real bug with a small diff), then S1→S3 as one
approval unit, then decide on S4 separately since it carries the real risk.

---

## 4. Test plan

Boundaries, never the middle:

- A column at exactly `min_y`, at `max_y`, and crossing `y = 0`.
- The 9-bit → 12-bit heightmap transition (a world just under and just over 511 tall).
- Section index at `-128` and `127` — the signed-byte edge.
- A profile that requires compression; assert the ratio is reported and the sidecar records it.
- A `< 1.17` target: assert it *refuses* and that no datapack is written.
- Pre-1.18 chunk layout (`Level` compound, int-array biomes) if that row is ever added —
  a genuinely separate code path that will rot untested.

Structural verification (automatable here): read the produced `.mca` back, assert the section
Y range, the heightmap bit width, the `yPos`, the `DataVersion`, the `level.dat` pack
registration, and the emitted `dimension_type` JSON against the profile.

---

## 5. Honesty notes / what I cannot verify

- **I cannot load Minecraft in this environment.** Everything above is verifiable
  structurally (NBT read-back, JSON assertions), but the brief's "generate a test world, load
  it, confirm the block range" step is yours. I will not report S3/S4 as done on structural
  evidence alone — I will report it as *structurally verified, awaiting in-game confirmation*.
- **Version constants are the known trap.** I will not write a `DataVersion` or `pack_format`
  I have not seen in a real file. Expect me to ask for a generated world, or to ship the
  table with a single verified row.
- **Bedrock is a second geometry.** `bp_tall` declares `-512..512` while
  `extended_max_y_for` returns `512` and the writer floor is `-64`. It needs its own profile
  branch; I have scoped it out of the stages above rather than pretend one object covers both.
- **Out of scope per §7:** OSM placement, biome selection, cave *generation* logic, the Meld
  scheduler. S4 touches constants those modules read; if it needs more than that, I stop.
