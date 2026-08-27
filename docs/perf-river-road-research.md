# River beds and road grades - research annex

Raw maps and designs behind `perf-river-road-plan.md` (workflow, 7 agents, 2026-08-27).


## MAP: water

# Water/river bed pipeline map — arnis-triagefix (all paths under C:\Users\LEGION\Documents\Meld\arnis-triagefix)

## Pipeline overview (execution order)
1. Elevation post-process levels water surfaces per LC_WATER component: `src/elevation/postprocess.rs:253` (`level_water_surfaces`) — still bodies get one flat Y (histogram mode, `postprocess.rs:404-414`); "flowing" components (IQR > 5 m, `postprocess.rs:272`) keep their gradient via a per-cell 12-cell local median (`postprocess.rs:362-397`). This per-column surface is what `ground.water_level` later returns.
2. `compute_big_water_field` (BWF) bakes per-block carve depth from a chamfer-3-4 distance transform over the LC_WATER mask: `src/water_depth.rs:189-301`, called once per render at `src/data_processing.rs:464`.
3. Ground gen pre-paints water columns (WATER + SAND + SANDSTONE at `water_y`, `water_y-1`, `water_y-2`): `src/ground_generation.rs:434-442`.
4. OSM water polygons scanline-fill + carve: `src/element_processing/water_areas.rs:397-483` (`scanline_fill_water` → `carve_water_column_with_flags` at :471). Line waterways draw a flat surface ribbon only: `src/element_processing/waterways.rs:134-197`.
5. Post-ground carve of every LC_WATER cell: `src/water_depth.rs:838-943` (`carve_lc_water_pass`/`carve_lc_water_region`), called per-tile at `src/data_processing.rs:812` and post-merge at `src/data_processing.rs:1118`.

Note: the comment at `src/data_processing.rs:467` referencing `carve_waterway_region` is STALE — no such function exists; line-waterway depth carving is deliberately reverted (`waterways.rs:1-8`).

## (1) How bed depth is computed today, and its shape
- Depth per column = `ocean_depth_for_cell` (`water_depth.rs:399-414`): chamfer DT units + a ±2-unit deterministic bank wobble (`value_noise_01(x,z,12)`, `water_depth.rs:412`, wavelength const at :94), fed into `depth_from_dt` (`water_depth.rs:374-396`): `depth = local_max * sqrt((dt-shoal)/span)`, **rounded** (`:391-395` — this rounding IS the 3.1.7 "bed-profile rounding"), clamped to a width-tiered `local_max` of 2/3/4/6 (`polygon_local_max`, `water_depth.rs:353-364`). Flat shoal ring for DT < 9 units (~3 blocks) (`SHOAL_DT_UNITS`, `water_depth.rs:50`, test at :375).
- Shape produced: an integer-stepped sqrt bowl — i.e. **concentric terraces following the shoreline** (each integer crossing of the sqrt curve is a shelf), max 6 deep (`MAX_WATER_DEPTH`, `water_depth.rs:59`). Small-scale (<0.5) maps use a linear unit-step bowl instead (`bowl_depth_small_scale`, `water_depth.rs:420-446`), capped at 5 (`:99`) — not active in a 1:1 render.
- The carve anchors at `water_y = ground.water_level(coord)` **per column** (`water_depth.rs:895`, `src/ground.rs:628-660`); `bed_y = water_y - depth - 1` (`water_depth.rs:512`). For a *flowing* river the surface Y follows the DEM's local-median gradient quantized to integer blocks — so **the terrain contour steps translate the whole carve stack vertically mid-river**. The screenshot terraces are the sum of: (a) integer sqrt-tier shelves, (b) flowing-surface contour steps, (c) dune noise (below). Also: an interior DEM ridge > 3 blocks above water is left un-carved entirely (`water_depth.rs:896-914`).

## (2) Distance-from-shore — computed twice already
- Grid-resolution BFS `water_distance` (cap 15 cells): `src/land_cover.rs:1243-1296`, exposed at `src/ground.rs:524-535`. Used for shoreline blending and the ridge-flood gate (`water_depth.rs:907`), NOT for depth.
- Block-resolution chamfer-3-4 DT over LC_WATER: `water_depth.rs:220-230` (seeding) + `chamfer_3_4_dt` at `:304-350`. **The bed already runs on this** — a parabolic profile can reuse it directly; no new transform is needed for distance. What's missing is a *river-only* mask to run it on.

## (3) Where river WIDTH could come from
- Today's proxies: per-connected-component max DT `component_max_units` (`water_depth.rs:250-255`, used at :289) — a *global per-component* half-width, so a river connected to a lake/sea inherits the LAKE's max (relevant to your confluence-blend rule, and a real width signal problem); and `body_max`, a 7×7 max of baked depth (`water_depth.rs:922-931`, `water_areas.rs:461-470`), driving dune amplitude only.
- OSM `width=*` tag / type default for line waterways: `waterways.rs:25-62` (`waterway_width`, clamp 128 at :42).
- **Local half-width along a river is NOT computed anywhere today.** The classic derivation (local width ≈ 2 × max DT over the DT-ridge/medial axis near the column, or per-column max of DT within a window) does not exist — that defines work. `unclipped_bounds`/`unclipped_polygon_area` on `ProcessedWay` (`waterways.rs:270-271`) exist as tile-safe area hints but not width.

## (4) Noise passes touching underwater columns (and where the exemption goes)
Height-affecting (these make the bed "textured"):
- **Dunes**: `place_underwater_dunes` (`water_depth.rs:782-835`), 1-4 block bumps, gated at `water_depth.rs:639-641` (`depth >= 1 && !near_bridge`) — **this gate is where a `!is_river` test goes**. Mirrored in `dune_bump_at` (`:653-681`) used by vegetation.
- **Bank wobble** on the depth field: `water_depth.rs:412` (and small-scale `:442`) — shifts depth contours ±~0.7 blocks.
Palette-only (no height): bed-block noise + depth-tier jitter `water_depth.rs:534-620` (jitter at :555-563); ground_generation's under-OSM-water floor palette `src/ground_generation.rs:961-988` (coord_hash).
Vegetation: `place_underwater_vegetation` gate `water_depth.rs:644-646`.
NOT applied under water: land surface noise passes (shrubland/bare `ground_generation.rs:524, 602`), snow (`:900-904` skips water), shore band (land columns only). So "terrain noise continuing under water" is NOT ground_generation noise — it is (a) DEM/water-surface contour steps via per-column `water_y` and (b) the dune pass. Ground gen's water branch (`ground_generation.rs:417-442`) itself places no noise.

## (5) Shore/beach transition today; what a soft grade must change
- Water side: flat depth-0 shoal ring 3 blocks wide (`SHOAL_DT_UNITS`, `water_depth.rs:50`), SAND top for depth 0-1 (`water_depth.rs:534-535`).
- Land side: 6-block shore band re-palettes GRASS_BLOCK columns (SAND/COARSE_DIRT taper) — `ground_generation.rs:682-810` (ring search :688-725, palette :736-812). **Palette only — no bank height shaping exists anywhere.** Land bank height is raw DEM.
- The organic shoreline itself is the 0.5 isoline of the Gaussian-smoothed `water_blend` grid (`ground.rs:544-586`, `is_esa_water` at `ground_generation.rs:417-418`, blur grid at `land_cover.rs:264`).
- A rounded-parabola bed "anchored at a smoothed shoreline" therefore changes: `depth_from_dt`'s curve (replace sqrt+round for river columns), the shoal constant (the flat ring is the anti-grade), and optionally anchors DT=0 at the `water_blend` 0.5 isoline instead of the binary grid shore. Nothing on the land side needs touching for the stated scope.

## (6) Tile-invariance status of every input
- Block coords / noise: `value_noise_01` is a pure function of absolute (master-anchored) x,z + process-wide `--seed` (`ground_generation.rs:1985-2007`, seed at :1770-1793); `coord_hash` likewise (`land_cover.rs:1452`). SAFE.
- LC grid values: master-origin anchored via `GridMapping::new_master` (`land_cover.rs:524-556`) — same block, same class in every cell. SAFE. Shoreline ring-fitting is gated OFF in master-origin cells (`land_cover.rs:218-228`) precisely because it isn't tile-invariant.
- **HAZARD — chamfer DT + `component_max_units`**: `compute_big_water_field` runs over ONE CELL's bbox only (`data_processing.rs:464`; sub-rect at `water_depth.rs:196-208`). For a body crossing a cell border, DT near the seam measures distance to shore *visible in this cell*, and `comp_max` is the max over *this cell's fragment* — both differ in the neighbor cell → depth tier and profile can step at every cell seam. The "same in every tile" comments (`water_depth.rs:862-864`, :905) are about arnis's INTERNAL parallel tiles (which share the one per-render BWF/ground), not Meld cells. Any width-scaled parabola amplifies this: **local width must be derived from data that both cells see identically** (e.g. OSM way width tags / unclipped polygon geometry — the established tile-safe pattern per the Overture-hints work) or accepted with a documented seam bound.
- **HAZARD — `water_distance` BFS**: cell-local grid, `edge_is_shore=false` in master mode (`land_cover.rs:257`, :268-270; `edge_is_shore` field :80-82) — values near seams depend on in-cell land only.
- **HAZARD — `level_water_surfaces`**: per-cell components, per-cell histogram mode / IQR classification (`postprocess.rs:302-427`) — a river can be "flowing" in one cell and "still" in the next; surface Y itself can differ (this is the still-unexplained Navodari seam family). Any bed anchored to `water_y` inherits this.
- Carve writes are vertical-only per column (`water_depth.rs:862-864`), so within a render, internal tiling is safe. `--water-carve-clearance max` only fixes the shared datum (`args.rs:400-411`, used `ground.rs:907`), not DT seams.
- `road_surface_overrides` is an FnvHashMap but only key-looked-up (`world_editor/mod.rs:748-756`), never iterated for output ordering; last-writer-wins insert order follows deterministic element order (`:803-810`). OK.

## (7) Where water KIND is known — rivers vs lakes vs ocean
- **The BWF/carve pipeline is 100% tag-blind**: it sees only ESA `LC_WATER` (one class, `land_cover.rs`), so at the point where depth is computed and the bed placed, river-vs-lake-vs-ocean is **not known anywhere today**. That defines the work: a river mask must be built upstream and threaded into `compute_big_water_field` / `carve_water_column_with_flags`.
- Kind DOES survive parsing in the element layer (`ProcessedWay.tags` / `ProcessedRelation.tags`):
  - Line rivers: `waterway=river|stream|canal|...` ways — dispatch `data_processing.rs:125-137`, type list `waterways.rs:25-37`, drawability `waterways.rs:127-132`. `compute_waterway_field` (`waterways.rs:214-255`) already rasterizes their block footprint — an existing tile-pure (element-geometry-derived) river mask you can extend.
  - Polygon water: relations with `water=*` or `natural=water|bay` → `water_areas.rs:34-39` / dispatch `data_processing.rs:231-246`. The `water=river` TAG VALUE is present in `rel.tags` but only key-existence is checked — the value is available and unused.
  - `natural=water` WAYS go to `natural.rs` and are flood-filled as surface WATER at per-column ground level (`natural.rs:119`, fill at :249-258) — never carved, never leveled; tags available there.
  - `waterway=riverbank` ways are DROPPED (rejected by `is_channel_waterway`, `waterways.rs:79`; dock-only branch `data_processing.rs:126-137`) — they render only via ESA. A riverbank-polygon river mask would need a new path.
  - Ocean/coastline: `natural=coastline` is excluded from the Overpass query itself (`src/retrieve_data.rs:232`) — oceans exist ONLY as ESA LC_WATER. Convenient for your scope: "untouched lakes/oceans" is the default for anything the river mask doesn't cover, and the confluence blend must happen where the river mask meets unmarked LC_WATER (component connectivity already merges them — see the comp_max sharing noted in (3)).

## Golden-hash note
Every mechanism above feeds block output; any profile change moves `depth`/`bed_y`/dune decisions and cannot be hash-neutral. The clean gate point is `ocean_depth_for_cell` (`water_depth.rs:399`) + the dune gate (`water_depth.rs:639`) + `carve_water_column_with_flags`'s callers (`water_depth.rs:932`, `water_areas.rs:471`), all reachable from a single flag/env plumbed like `water_carve_clearance` (`args.rs:400-411`) — with `estimate_max_carve_depth` (`water_depth.rs:449-484`) kept in sync if the river profile's max depth ever exceeds 6, since the world datum reserves exactly `MAX_WATER_DEPTH`.

## MAP: roads

ROAD SURFACE PIPELINE MAP — arnis-triagefix (all paths under C:\Users\LEGION\Documents\Meld\arnis-triagefix)

## (1) How a road segment picks its Y today

Per **bresenham step, per stamp cell** — and yes, it follows raw quantized elevation contours.

- Node pairs → bresenham centerline: `src/element_processing/highways.rs:1495-1497` (`bresenham_line`), loop over points at `:1471-1474` (`tds = cumulative_distance_from_start + point_index`).
- At each centerline point, `precompute_row_medians` (`highways.rs:70-99`) fills one Y per axial offset in the (2b+1)² stamp; every cell sharing an along-length coordinate gets one Y → flat lateral cross-section.
- The Y itself: `perpendicular_median_ground_y` (`highways.rs:129-166`) = median of `2*block_range+1` `editor.get_ground_level` samples across the width (`perpendicular_median_raw`, `highways.rs:29-54`), then a 3-tap median along the travel axis. Cell Y assignment: `highways.rs:1585-1590` (`row_medians[(axial + block_range)] + offset`).
- `get_ground_level` (`src/world_editor/mod.rs:748-764`) = road override map, else `Ground::level` (`src/ground.rs:614-623`) = **bilinear interpolation then `result.round() as i32`** (`src/ground.rs:694-719`). So terrain input is already quantized to integer Y per column.
- Gate: `flatten_width = !bridge && block_range >= 1` (`highways.rs:1315`). At scale < 1.0, `block_range` is floored (`highways.rs:1186-1188`) and can hit 0 → `flatten_width` false → the road places blocks ground-**relative** per column (`set_block` → `get_absolute_y`, `mod.rs:731-734`), i.e. raw contour-following with no flattening at all.
- Bridges/ramps take absolute deck Y from `y_at(tds, ...)` (`highways.rs:1475-1481`), not terrain.

## (2) Why a 1-block step appears MID-segment

Primary cause: **elevation quantization crossing the segment, unmitigated by design.** `Ground::level` rounds a smooth bilinear field to i32 (`ground.rs:719`). A road climbing a gentle grade crosses the 0.5 rounding contour mid-segment; the width-median passes a step that spans the whole width straight through, and the 3-tap longitudinal median is *explicitly documented* to preserve monotone steps: "A monotone ramp is unaffected because the 3-tap median of any monotonic triple is the middle value" (`highways.rs:110-113`). Nothing anywhere limits ΔY along the road. The step is then cemented into terrain because the ground pass builds the embankment at exactly the stepped override Y (see (3)).

Secondary causes, all real:
- **Diagonal-travel stamp overlap disagreement.** `row_medians` samples the perpendicular strip centered on the *centerline's* cross-axis coordinate (`highways.rs:82-88`: `sz = centerline_z` for horizontal travel). When bresenham steps diagonally, centerline points P and P+1 stamp the same column with strips centered one block apart → different medians → same column gets two Ys. Block placement is first-writer-wins (asphalt is in `ROAD_PROTECTED_SURFACES` blacklist, `highways.rs:190-206`, applied at `:1671-1689`) but the override is **last-writer-wins** (`mod.rs:805-810`), so surface block Y and registered ground Y can disagree by 1 → grass shelf/step beside asphalt after the ground pass places its surface at override Y (`src/ground_generation.rs:844`, `set_block_if_absent_absolute(surface_block, x, ground_y, z)`).
- **Axis flip between segments of one way.** `dir_horizontal` is per segment (`highways.rs:1305`); when a way bends past 45°, the median axis flips at the shared node → different sample sets → possible 1-block jump at the joint.
- **Two overlapping ways disagreeing.** Junction columns get overrides from both ways; last-writer-wins (`mod.rs:805-810` — the "differ by at most ~1 block" comment is only true for stamps of the *same* way; two ways crossing a slope at different angles can disagree by more).
- **Override feedback ordering.** The median samples call `get_ground_level`, which *reads previously registered overrides* (`mod.rs:749-753`). So road B's Y depends on which roads ran before it. Order is deterministic (priority sort, `src/osm_parser.rs:1648-1659`) but this couples output to processing order — see hazards.

## (3) What road_surface_overrides guarantees, and coverage gaps

Guarantee: for every column where a **non-bridge, ground-level (`offset == 0`), width-flattened** road stamped a surface cell, `get_ground_level` returns the road's chosen Y instead of raw terrain (`mod.rs:404-414` doc, `:748-764` read, registration at `highways.rs:1498` + `:1592-1595`). The ground pass then consumes it transparently: `ChunkGroundCache::populate` calls `get_ground_level` (`ground_generation.rs:83`), surface at `ground_y` (`:844`), underfill down to lowest neighbor building the embankment (`:917-959`). Tile editors hand overrides to the main editor for post-merge passes (`mod.rs:813-838`; `src/data_processing.rs:863, 897-902`).

Gaps:
- **Bridges and bridge ramps never register** (`flatten_width` excludes them, `highways.rs:1315`) — at a bridge foot the last deck column meets raw-terrain ground → step at bridge ends. Deliberate (`highways.rs:1316-1323`: registering would turn bridges into embankments), but the transition is unmanaged.
- **Elevated slope sections don't register**: `register_ground_override = flatten_width && offset == 0` (`highways.rs:1498`) — an overpass ramp (offset from `calculate_point_elevation`, `highways.rs:2030-2075`) leaves terrain raw underneath and steps where offset returns to 0.
- **Drowned water crossings skip both surface and override** (`highways.rs:1575-1577`, scale ≤ 0.5 gate at `:1195`).
- **Narrow roads at reduced scale** (block_range floored to 0, `highways.rs:1186-1188`): no overrides at all.
- **Tunnels** excluded (mask collector guard `highways.rs:2540-2542`; renderer equivalent).
- **Junctions**: covered but *conflicting* (last-writer-wins), not a coverage hole — a correctness hole.
- Coverage across the width is otherwise complete: the full (2b+1)² stamp registers, not just the centerline.

## (4) Longitudinal smoothing

**Only** the 3-tap along-length median inside `perpendicular_median_ground_y` (`highways.rs:106-113, 129-166`), which removes single-cell potholes and deliberately passes every monotone step. `calculate_point_elevation` (`highways.rs:2030-2075`) grades only the layer-offset ramps of elevated ways (linear interp over `slope_length`, `:1266`), added *on top of* terrain Y — it never grades terrain-following. **A "grade the road along its length / clamp slope to 1 block per N blocks" pass is not computed anywhere today.** That is the work.

## (5) Where a longitudinal grade pass would go, and tile-invariance anchoring

Placement: per-way, **before the node loop** — the block around `highways.rs:1225-1246` already computes `total_way_length` and the exact `total_bresenham_length` from the full node list, and `tds` (`:1474`) already indexes every centerline point by cumulative arclength. Precompute a `Vec<i32>` profile indexed by `tds`: sample terrain at each bresenham station, slope-limit forward+backward (classic min-clamp both directions), then in the cell loop use `profile[tds]` in place of (or blended with) `row_medians`, and register it via the existing `register_road_surface_y` path — the ground pass builds the embankments/cuts for free (`ground_generation.rs:844, 917-959`).

Inputs required for tile invariance:
- **Full unclipped way geometry** — already available: `assign_elements_to_tiles` assigns whole elements to every tile the AABB+halo intersects (`src/tile.rs:51, 116-127`, assignment fn shown at the `assign_elements_to_tiles` body); tiles process the complete node list and only writes are clipped (`data_processing.rs:596-601, 690-696`). So `tds` and the profile are identical in every tile.
- **Master-origin elevation** — sample `editor.terrain_level` (`mod.rs:777-785`), which uses `ground_origin_x/z` set to the *main* world origin in tile editors (`data_processing.rs:707`, field doc `mod.rs:418-424`). This is what anchors cross-cell agreement today.
- **Critically: sample `terrain_level`, NOT `get_ground_level`.** `get_ground_level` folds in the override map (`mod.rs:749-753`), whose contents at sampling time depend on which other roads a tile was assigned and their order. Today's median code samples `get_ground_level` — this is the existing feedback loop and the thing a new pass must not inherit.
- **Junction boundary conditions**: independently graded ways sharing a node will disagree at the node → new steps at junctions. The endpoint Y must be a pure function of node world coordinates (e.g. raw `terrain_level` at the node, or a deterministic median around it) so every way, in every tile, pins the same Y there. `highway_connectivity` (keyed by node coords, `highways.rs:1978-2010`) is the existing structure to hang this on.

Tile-invariance hazards observed (existing, and traps for the new pass):
1. **Override feedback** (`mod.rs:749-753` read inside median sampling, `highways.rs:45/49`): road Y depends on prior roads' overrides. Within-tile order = ascending global element index in both tiles, and any road whose overrides are within median reach (~9 blocks) of a tile's strict area is itself assigned there (64-block halo, `tile.rs:51`), so it is *usually* consistent — but chained road-over-road dependencies propagate ~9 blocks per link and a chain of 8+ overlapping ways can exceed the 64-block halo, giving tile-dependent Y. Also the sequential path (tiles < 3, `data_processing.rs:589`) accumulates *all* overrides in one map vs per-tile subsets — same-area renders at different bbox sizes can differ.
2. **Last-writer-wins FnvHashMap merge across tiles** (`mod.rs:818-838`): insertion order of tile merges is batch order; two tiles registering different Y for one shared halo column resolve by merge order. Batch order depends on the LPT/band sort (`data_processing.rs:637-656`) — deterministic per run config, but eviction-mode toggles (`ARNIS_NO_BAND`, RAM-dependent `should_stream_to_disk`, `data_processing.rs:617-618`) change it → machine-dependent output where tiles disagree.
3. `calculate_point_elevation` uses `total_way_length = total_segments * segment_length` (`highways.rs:2048`) — an *approximation* assuming uniform segment length; fine per-way, but any new grade pass should use real `tds`/`total_bresenham_length` (`highways.rs:1226-1237`), which is exact and tile-safe.
4. `slope_length`/ramp lengths derive from whole-way length (`highways.rs:1266`) — safe only because ways are never clipped per tile; keep that property.

Determinism notes: all surface randomness is `semirandom_surface` coordinate hashing (`highways.rs:1668`) and `land_cover::coord_hash`/`value_noise_01` in the ground pass (`ground_generation.rs:472-608`) — world-absolute, seeded, safe. No wall-clock anywhere in this path. The only HashMap whose order could matter is the override merge (hazard 2 above); `road_surface_overrides` reads are point lookups, never iterated for output.

Golden-hash consequence: any grade pass changes road Y and therefore embankment terrain — cannot be hash-neutral; must be flag-gated (pattern precedent: `--road-detail` clean/compact/max branches, `highways.rs:1391-1406`).

## DESIGN 1

Verified the map's core claims against the code (`water_depth.rs` DT/depth/carve/dune paths, `waterways.rs` width + footprint field, `water_areas.rs` scanline carve). All cited mechanics check out, including the tag-blindness of the carve pipeline and the "line-waterway carving stays reverted" history note at `waterways.rs:1-8`. Design follows.

# DESIGN — River-only U-shaped bed (rounded parabola), arnis-triagefix

## 0. Governing decisions

- **Compose, don't replace.** `compute_big_water_field` / `depth_from_dt` / `carve_lc_water_pass` stay byte-identical for every non-river column. A new per-render `RiverBedField` overrides depth only for columns inside a river mask. Lakes/oceans (all LC_WATER not covered by the mask) hit the legacy path untouched — the scope rule is satisfied *by construction*, not by a blend everywhere.
- **Depth-only, never carve-creation.** The river field modifies the depth of columns that already carve today (LC_WATER pass at `water_depth.rs:838-943` and OSM polygon fill at `water_areas.rs:397-483`). It never introduces carving where none exists — this is what keeps the historically-failed line-waterway carving (`waterways.rs:1-8`: grooves, cut trees, floaters) dead. Narrow streams absent from ESA LC_WATER remain flat surface ribbons (status quo).
- **Do not reuse `water_distance`** (`land_cover.rs:1243-1296`): grid-resolution, capped at 15 cells, cell-local (`edge_is_shore=false` in master mode). It fails both resolution and tile-invariance requirements. A new block-resolution chamfer DT on a **halo-expanded lattice** replaces it for rivers (below).

## 1. River classification (where "river" comes from)

Built once per render, pre-tiling, next to `compute_waterway_field` (call site alongside `data_processing.rs:464`), from element geometry only:

- **Line rivers**: `ProcessedWay` with `waterway ∈ {river, stream, canal, brook, fairway, flowline}` passing `is_drawable_waterway` (`waterways.rs:127-132` semantics; exclude `ditch`/`drain` — a 2-3-block drain gets no parabola, it's below the mask's useful resolution and today's behavior is fine). Rasterize the channel footprint exactly like `compute_waterway_field` (`waterways.rs:214-255`): bresenham centerline × `scaled_half_width(waterway_width(way), scale)` (`waterways.rs:74-102, 300-310`).
- **Polygon rivers**: relations/ways reaching `water_areas.rs` whose `water` tag value ∈ {`river`, `canal`, `stream`, `oxbow`?—no: oxbow is a lake, exclude} — the value is already in `rel.tags`, currently unread (map §7). Also `waterway=riverbank` ways: currently dropped by `is_channel_waterway` (`waterways.rs:79`); add a *mask-only* rasterization path for them (scanline fill of the polygon into the mask — they still draw nothing themselves).
- **Everything else** (bare `natural=water`, ESA-only LC_WATER, coastline-derived ocean) = not-river ⇒ legacy bed, including its dunes and sqrt terraces, untouched.

## 2. The `RiverBedField` (distance + width + depth), and tile invariance

One struct, built per render on a lattice covering `cell_bbox` **padded by halo `H = ceil(MAX_WATERWAY_WIDTH/2 · scale) + B_max = 64·scale + 32` blocks** (clamp 96). Rasterization uses full unclipped node lists / ring geometry, which extend past the bbox — that is exactly why the halo works.

1. **Mask** `R(x,z)`: 1 bit per lattice cell, from §1, intersected with "would carve today" at apply time (LC_WATER or inside an OSM water polygon fill span).
2. **Distance** `d(x,z)`: chamfer-3-4 DT (`chamfer_3_4_dt`, `water_depth.rs:304-350`, reused verbatim) seeded 0 at every non-mask cell, run over the halo lattice; `d_blocks = d/3.0`. Because seeds come from element geometry rasterized into the halo — not from the cell-cropped LC grid — `d` is a pure function of (way/relation geometry, scale, master-anchored coords): identical in both neighbor cells for every column within the cell proper. This kills the map §6 BWF seam hazard *for river columns* (lake columns keep the existing documented hazard, unchanged).
3. **Local half-width** `hw(x,z)`:
   - Line rivers: per stamped column, `hw = scaled_half_width(waterway_width(way), scale)`; overlapping ways take the **max** (order-independent ⇒ deterministic ⇒ tile-safe). This is the tag/type-derived, tile-safe width the map §3 says doesn't exist yet — the established Overture-hints pattern (element-keyed, unclipped).
   - Polygon rivers: `hw = max(d_blocks over a square window of radius W=16·scale centered on the column)`, floored at own `d_blocks`. Windowed max of a tile-invariant field with halo ≥ W + max hw is tile-invariant.
   - Where both apply: max.
4. **Depth formula** (per column, all in blocks, f64 until the final round):
   - `t = clamp(d_blocks / hw, 0, 1)`
   - bank-roundness exponent `q = 1.0 + 0.5·min(hw/30, 1)·min(scale, 1)` — narrow stream: q≈1 (tight curve); wide 1:1 river: q=1.5 (long soft banks). This is the user's "roundness scales with width and map scale".
   - shape `s = 3τ² − 2τ³` with `τ = t^q` — smoothstep: **zero slope at the shore** (soft entry, no shoal cliff), **zero slope at center** (broad rounded bottom). This *is* the rounded parabola ask; a literal parabola has its steepest slope at the shore, which is the opposite of "banks curve gently".
   - depth cap `D = clamp(6·(hw/30)^0.7, 1.0, D_max)`; `D_max = MAX_WATER_DEPTH = 6` (`water_depth.rs:59`) at scale ≥ 0.5, `SMALL_SCALE_MAX_DEPTH = 5` (`water_depth.rs:99`) below. Examples: hw=3 → D≈1.2 (stream, 1 deep); hw=10 → ≈2.8; hw=20 → ≈4.5; hw≥30 → 6.
   - `depth_f = D·s`, then one 3×3 tent smooth of `depth_f` over the lattice (pure function of the field ⇒ still tile-safe; this is the "smoothed shoreline" anchor — it rounds the DT's chamfer facets), then `depth = round(depth_f).clamp(0, D_max)`.
   - **No shoal ring** (`SHOAL_DT_UNITS` not consulted for river columns — the flat ring *is* the anti-grade, map §5), **no bank wobble** (`ocean_depth_for_cell`'s ±2-unit wobble at `water_depth.rs:412` is bypassed entirely — river columns never call it).
   - Max bank slope = 1.5·D/hw ≤ 0.3 blocks/block ⇒ no adjacent-column step > 1 by construction; still assert `|Δdepth| ≤ 1` in tests.
5. **Confluence blend** (river → untouched lake/sea): second chamfer DT `m(x,z)` over the mask, seeded 0 at every **non-river water** cell (LC_WATER or polygon water outside the mask) adjacent to the mask, run on the same halo lattice. Blend band `B = clamp(2·hw, 8, 32)` blocks. Final river depth `= round(lerp(legacy_f, river_f, smoothstep(clamp(m/B, 0, 1))))` where `legacy_f = f64(bwf.depth_at(x,z))`. At the mask boundary w=0 ⇒ exactly the legacy value ⇒ zero step into the lake bed; the lake side is never written differently at all. (Note the legacy `comp_max` contamination — river connected to lake inherits the lake's max, map §3 — becomes irrelevant for river columns and stays exactly as-is for lake columns.)

Scale ≠ 1:1: everything above is already scale-parameterized (`scaled_half_width` collapses hw at <0.7/<0.3 exactly as today, D_max drops to 5 below 0.5). At scale <0.5, river columns use this formula **instead of** `bowl_depth_small_scale` (`water_depth.rs:433-446`); non-river columns keep the bowl. `estimate_max_carve_depth` (`water_depth.rs:449-484`) needs **no change**: river depth never exceeds the existing caps, so the world-datum reservation holds — state this in the PR and keep the `debug_assert` at `water_depth.rs:499-502` as the enforcement.

## 3. Where it plugs in (exact seams)

- Build: `data_processing.rs` next to `:464` (`compute_big_water_field` call) — `let river_field = compute_river_bed_field(&parsed_elements, &ground, &xzbbox, scale, args.river_bed)`.
- Apply, LC pass: `carve_lc_water_region` (`water_depth.rs:912-941`) — before calling `carve_water_column_with_flags`, `let (depth, is_river) = river_field.depth_override(x, z).map_or((bwf.depth_at(x,z), false), |d| (d, true))`.
- Apply, OSM polygons: `scanline_fill_water` (`water_areas.rs:452-481`) — same substitution.
- Signature: `carve_water_column_with_flags` (`water_depth.rs:490`) gains `is_river: bool`.
- **Noise exemptions** for `is_river` columns: dune gate `water_depth.rs:639-641` → `depth >= 1 && !near_bridge && !is_river`; mirror in `dune_bump_at` (`water_depth.rs:653-681`) so vegetation (gate `:644-646`, kept — it plants above the bed, adds no height noise to it) sits on the smooth bed. Bank wobble is bypassed structurally (§2.4). The bed **palette** noise (`water_depth.rs:534-620`) is block-type-only, zero height effect — keep it, but for river columns force `depth ≤ 1 → SAND` unchanged and skip MAGMA/SOUL_SAND arms (they read as sea floor, wrong in a river). Land-side shore band (`ground_generation.rs:682-810`) untouched — it's palette-only and already fine.
- Interaction with `--water-carve-clearance`: none to change — it fixes the shared datum (`args.rs:400-411` → `ground.rs:907`); since river depth ≤ MAX_WATER_DEPTH the reservation is already sufficient at any clearance mode, including the `max` used in the Bucharest render.
- Known inherited limit, documented not fixed: the bed anchors at per-column `water_y` (`water_depth.rs:895`), so where a *flowing* river's surface steps (per-cell `level_water_surfaces` gradient, `postprocess.rs:362-397`), the whole smooth profile translates 1 block with it. That is a water-*surface* artifact (the unexplained Navodari family) and out of scope; the profile guarantees smoothness *relative to the surface*.

## 4. Flag / versioning / golden hash (hard rule 1)

- New arg `--river-bed <off|v1>`, default `off` at merge, plumbed exactly like `water_carve_clearance` (`args.rs:400-411`) + the `gui.rs` Args field (per the cave-zone-map gotcha: new arg ⇒ `validate_args` + gui field). `off` ⇒ `compute_river_bed_field` returns an empty field ⇒ **byte-identical output, golden hash passes unchanged**.
- `v1` in the name is the version story: any future retune of q/D/B is `v2`, never a silent change under the same flag value. Meld side flips its render invocation to `--river-bed v1` in the release that ships it, with a stated hash break for river-bearing cells (and only those — lake/ocean-only cells stay hash-identical even with the flag on, which is itself a regression test).
- Golden gate: existing golden_hash run stays flag-off (remember: golden_hash.sh never rebuilds — full rebuild before baking any new golden). Add one new golden variant with `--river-bed v1` over a river fixture.

## 5. Test plan

1. **Cross-section unit test** (`water_depth.rs` or new `river_bed.rs` tests): synthetic straight polygon river, widths {6, 12, 40} blocks, scale 1.0. Assert per transect: depth(shore)=0; monotone non-decreasing shore→center; center depth = expected D table; `|Δdepth| ≤ 1` between adjacent columns; mirror-symmetric; dune pass produced zero bumps (bed top exactly `water_y − depth − 1` everywhere).
2. **Tile-invariance A/B (the mandated one)**: master-origin render of a river crossing a cell border, rendered as cell A and cell B (the existing master-origin two-cell rig from the shoreline-era work). Extract bed Y per column in the 128-block band straddling the seam; assert column-for-column equality. Run twice more with `ARNIS_NO_BAND` toggled and small RAM budget to cross the eviction-mode orderings.
3. **Confluence test**: fixture river entering a lake; assert (a) every lake column byte-identical to the flag-off render, (b) `|Δdepth| ≤ 1` across every mask-boundary pair, (c) no column where `m < B` equals the pure river value.
4. **Scale sweep**: same fixtures at 0.3 / 0.5 / 1.0 — depth caps 5/6 respected, `estimate_max_carve_depth` ≥ max carved depth (assert in test).
5. **Golden**: flag-off byte-identity vs current golden; new v1 golden baked after visual sign-off on the Bucharest cells.

## 6. Task list

| # | Task | Files (anchor lines) | Conf | Hrs |
|---|------|------|------|-----|
| 1 | `--river-bed off\|v1` arg + gui field + validate_args | `args.rs` (~:400 pattern), `gui.rs` | 95% | 1.5 |
| 2 | River mask: line-waterway rasterizer (reuse `compute_waterway_field` pattern) + polygon `water=river`/`riverbank` scanline into halo lattice | new `src/river_bed.rs`; `waterways.rs:214-255`, `water_areas.rs:34-39` | 85% | 6 |
| 3 | Halo-lattice chamfer DT (reuse `chamfer_3_4_dt`) + hw field (tag-derived + windowed-max) | `river_bed.rs`; `water_depth.rs:304-350` | 85% | 5 |
| 4 | Depth formula (t, q, s, D, tent smooth, round) + confluence DT `m` + blend | `river_bed.rs` | 90% | 4 |
| 5 | Thread `is_river`/override into both carve callers + `carve_water_column_with_flags` signature; dune/palette exemptions | `water_depth.rs:490, 534-620, 639-646, 653-681, 912-941`; `water_areas.rs:452-481`; build call in `data_processing.rs:~464` | 90% | 4 |
| 6 | Verify/pad OSM fetch bbox covers the halo for near-miss ways (tile-cache + Overpass paths); document residual if not padded | `retrieve_data.rs`, tile-cache read path | 70% | 3 |
| 7 | Tests 1-4 + fixtures | new tests, two-cell rig | 85% | 8 |
| 8 | Golden: flag-off identity check, bake v1 golden (full rebuild first), Bucharest visual pass | `golden_hash.sh` rig | 90% | 3 |
| | **Total** | | | **~34.5 agent-hours** |

Main residual risks: (a) task 6 — if the OSM fetch has no halo, a way passing within 64·scale blocks of a cell without entering it exists in one cell's data only; bounded, rare (river bends near-missing a border), and fixable with fetch padding; (b) riverbank-polygon coverage quality varies by region — cells with ESA-only rivers (no OSM river tagging) fall back to the legacy terraced bed, which is correct-by-scope but visually mixed; note it in the release notes.

## DESIGN 2

All file:line references verified against `C:\Users\LEGION\Documents\Meld\arnis-triagefix` at HEAD. Design follows.

# ROAD LONGITUDINAL GRADE — DESIGN (perf/speed-to-worldgen-phase5)

## 1. Root cause recap (verified)

`Ground::level` rounds a smooth bilinear field to i32 (`src/ground.rs:694-719`). The road's Y machinery is purely cross-sectional: `perpendicular_median_ground_y` (`highways.rs:122-166`) medians across the width and applies only a 3-tap along-length median that is *documented* to pass monotone steps through (`highways.rs:107-111`: "A monotone ramp is unaffected"). No code anywhere limits dY/ds along the road. Every 0.5-contour crossing of the bilinear field becomes a full-width 1-block cliff, and the ground pass then cements it into an embankment at exactly that stepped override Y (`ground_generation.rs:844, 917-959`). Secondary steps come from: diagonal-travel stamp overlap (strips centered on the centerline, `highways.rs:82-88`), per-segment `dir_horizontal` axis flips (`highways.rs:1308`), junction last-writer-wins (`world_editor/mod.rs:809-811`), and override feedback (medians sample `get_ground_level`, which reads prior roads' overrides, `mod.rs:748-756`).

## 2. Design

### 2.1 Profile computed per way, before the node loop

Insert between the way-length block (`highways.rs:1224-1241`, where `total_bresenham_length` is already computed exactly from the full node list) and the node loop (`highways.rs:1290`):

1. **Station list**: concatenate `bresenham_line` output for all segments into `stations: Vec<(i32,i32)>` indexed by `tds`, reproducing the exact `tds = cumulative_distance_from_start + point_index` mapping of the placement loop (`highways.rs:1471-1474`). Note: for non-bridge ways `skip_first = 0` (`highways.rs:1466-1470`), so shared segment endpoints appear TWICE with consecutive tds values — the station list must duplicate them identically (benign: same coord → same profile value).
2. **Base samples** (float): `base[i]` = median of `terrain_level_f64` over the `2*block_range+1` perpendicular strip at each station — the same strip geometry as `perpendicular_median_raw` (`highways.rs:29-54`) but sampling **raw terrain, never `get_ground_level`** (this severs the override-feedback loop at `mod.rs:748-756`). Requires a new pre-round accessor `Ground::level_f64` (bilinear result before `round()`, `ground.rs:694-719`) exposed as `WorldEditor::terrain_level_f64` beside `terrain_level` (`mod.rs:777-785`). Sampling the float field, not the rounded i32, means the profile is built from the smooth surface the rounding destroyed.
3. **Junction pins**: for every way node that is a junction (see 2.2), `pin(node) = median of terrain_level_f64 over a fixed 3x3 window centered on (node.x, node.z)`. Deliberately **width-independent and way-independent**: a pure function of node world coordinates + DEM, so every way at the junction, in every tile and every cell, computes the identical pin with no ordering input.
4. **Slope-limited relaxation** with pins as hard constraints: max grade `g = 1/N` blocks per block of run, `N` by highway class (motorway/trunk/primary 12, secondary/tertiary 8, residential/unclassified/service 6, footway/path/track 4; `highway=steps` excluded entirely — stairs are supposed to step). Scale-aware: `N_eff = max(2, (N * scale_factor).round())`. Algorithm: seed `p = base`, force `p[pin_idx] = pin`, then forward pass `p[i] = clamp(p[i], p[i-1]-g, p[i-1]+g)` and backward pass symmetric, iterated to fixpoint (2 passes suffice for a 1-D chain with fixed pins); where two pins are closer than their dY/g run allows, relax `g` locally to the pin-to-pin linear grade (never worse than today). The relaxation is **symmetric in direction**, so a reversed OSM way yields the mirrored-identical profile — add a unit test for this, since way direction is arbitrary.
5. Result `profile: Vec<f64>`, rounded to i32 only at placement. Rounding a slope-limited monotone profile yields 1-block steps spaced ≥ `N_eff` apart — the voxel-minimum ramp — instead of terrain-contour clusters.

### 2.2 Junction reconciliation — deterministic, order-free

Two layers:

- **Exact agreement at the node**: the pin (2.1.3) makes every way hit the same Y at the shared node column regardless of processing order, tile, or which other ways were parsed. Today's `highway_connectivity` map is keyed by node coords but only records way **endpoints** (`build_highway_connectivity_map`, `highways.rs:365-400`) — extend it (or add a sibling map) to register **all** nodes of each highway way, so mid-way T-junction nodes pin too. Junction-membership itself depends on the parsed element set, but any way sharing a node inside a cell's writable+halo area necessarily intersects that cell's AABB and is whole-element-assigned (`tile.rs:51, 116-127`) / present in Meld's seam-expanded fetch, so both sides see the same junction set; document this as the (existing, established) guarantee the rule leans on.
- **Min-Y-wins override fold**: change `register_road_surface_y` (`mod.rs:809-811`) from insert (last-writer-wins) to `entry.and_modify(|v| *v = (*v).min(y)).or_insert(y)`, and change the tile-merge `extend` in `merge_road_surface_overrides` / `merge_road_surface_overrides_in_regions` (`mod.rs:818-838`) to the same min-fold — flag-gated. Min is commutative/associative, so the final override map is independent of element order, tile batch order, and the LPT/band sort (`data_processing.rs:637-656`) — this deletes tile-invariance hazard 2 (machine-dependent `ARNIS_NO_BAND` / stream-to-disk batch ordering) for road columns outright, and makes the sequential (<3 tiles, `data_processing.rs:589`) vs tiled paths agree.
- **Residual fan lip**: within the overlap fan two ways can still round to Ys differing by 1 (they agree exactly only at the node; at distance d they may differ by ≤ 2·g·d before rounding, ≤1-2 blocks at d=8). The ground pass follows the min-fold override so terrain is consistent; the only artifact is possible asphalt from the higher way one block above the folded ground. Phase-2 optional pass: after highways complete, iterate override columns in **sorted key order** and clear road-surface blocks above the final override Y (hook where overrides are handed to the main editor, `data_processing.rs:863, 897-902`). Ship phase 1 without it; measure whether the ≤1 lip is visible.

### 2.3 Consumption in the placement loop

When the flag is on and the way is non-bridge: replace the `row_medians` base with `profile[tds].round() as i32` at `highways.rs:1585-1590` (`cell_y = profile_y + offset`), skip `precompute_row_medians` (`highways.rs:1508-1519`). Because Y is now a pure function of `tds`, the diagonal-stamp-overlap disagreement and the per-segment axis-flip jump (secondary causes) disappear structurally — overlapping stamps of adjacent stations differ by the profile's ≤g, i.e. round to the same Y except at the spaced ramp steps.

**Coverage extension**: relax the gate `flatten_width = !bridge && block_range >= 1` (`highways.rs:1315`) to allow `block_range == 0` when grading is on — small-scale narrow roads currently get no flattening and no overrides at all (`highways.rs:1186-1188` floor); with the profile they get graded Y and register their 1-wide column. Keep bridges/ramps excluded (deck Y from `y_at`, `highways.rs:1475-1481`) and keep `register_ground_override = flatten && offset == 0` (`highways.rs:1498`) — elevated `offset > 0` sections must still not register (embankment explosion, comment at `highways.rs:1316-1323`). Bridge-foot steps remain an acknowledged, separate gap. `calculate_point_elevation`'s approximate `total_way_length` (`highways.rs:2047`) is untouched (offset ramps only); the profile uses the exact `total_bresenham_length` (`highways.rs:1227-1236`) per hazard note 3.

## 3. Tile-invariance anchors (the profile is a pure function of)

1. **Full unclipped way node list** — whole-element tile assignment (`tile.rs:51, 116-127`; only writes are clipped, `data_processing.rs:596-601, 690-696`) and Meld's seam-expanded parse give every tile/cell the identical node list → identical stations and `tds`.
2. **Master-origin DEM via `terrain_level(_f64)`** (`mod.rs:777-785`; `ground_origin_x/z` set to main origin in tile editors, `data_processing.rs:707`). Never `get_ground_level` — its override-map read (`mod.rs:748-756`) is the existing order/tile-coupled feedback the new pass must not inherit; this design removes that coupling from road Y entirely (hazard 1 dissolved).
3. **Junction pins = f(node coords, DEM only)** — no dependence on element set contents, widths, or order.
4. **Min-fold override merge** — commutative, so cross-tile merge order is irrelevant.
5. **No cell-local accumulation** — profile built fresh per way from (1)+(2); no state survives between ways except the (now order-free) override map. Caveat to state in the PR: in Meld LOCAL-elevation mode (as in the defect render) the per-cell datum differs, so absolute Ys shift per cell — same pre-existing class of seam as the elevation-lock issue, out of scope; in master-origin elevation mode the profile is bit-identical across cells.
6. Determinism: no wall-clock, no RNG (profile is noise-free); the only HashMap is the override map, point-looked-up only, plus the phase-2 pass which must iterate in sorted key order.

## 4. Flag / versioning / golden hash

- New flag `--road-grade off|on`, default `off` (pattern: `--road-detail`, `args.rs:459-472`). `off` = byte-identical to today, so all existing golden hashes stand and golden_hash.sh gates keep passing unchanged (remember: it never rebuilds — rebuild before running).
- The min-fold override semantics are also gated on the flag (off = legacy last-writer insert), keeping `off` strictly hash-neutral.
- `on` changes road Y and embankment terrain → **cannot be hash-neutral**; any config that enables it (Meld schedulers auto-selecting it, like they do `road-detail`) needs a new golden baseline for that config. Record the flag value in the world build-hash metadata (3.1.7 build-hash precedent) so a world self-describes which grading it was rendered with.

## 5. Test plan

1. **Unit — relaxation**: max |Δ| ≤ g everywhere; pins held exactly; steep pin-to-pin feasibility (local g relax); **direction invariance** (reversed node list → mirrored profile); duplicate-endpoint tds handling.
2. **Unit — order independence**: build a 3-way sloped junction fixture, run highway processing with permuted element order, assert the final override map is identical (min-fold) and node-column Y equals the pin from all ways.
3. **Cross-border A/B (required)**: two adjacent master-origin Meld cells with a road crossing the seam; render each cell; decode road columns in the shared halo (block_hash + decode smoke tooling precedent) and assert per-column surface Y and override Y identical from both cells. Repeat with the way's nodes mostly outside one cell (tests the unclipped-geometry anchor).
4. **Tile-count A/B**: same bbox forced sequential (1 tile) vs 4 tiles (and with `ARNIS_NO_BAND` toggled) → identical road-column Y map; this is the regression test for hazards 1+2.
5. **Golden-hash off-run**: full render with `--road-grade off` → existing golden hash byte-identical.
6. **Visual/metric**: re-render the Bucharest defect area 1:1 with `on`; script over the schematic counting Y transitions per 100 m along sampled ways — assert step spacing ≥ N_eff and no |Δ| > 1; before/after screenshots.
7. **Bridge non-regression**: flag on, bridge fixture → deck Y and (absent) overrides byte-identical to flag off.

## 6. Tasks

| # | Task | Where | Conf | Hrs |
|---|------|-------|------|-----|
| T1 | `--road-grade` flag + plumbing | `src/args.rs:459-472` pattern; args already reach highways | 95% | 1 |
| T2 | `Ground::level_f64` + `WorldEditor::terrain_level_f64` | `src/ground.rs:694-719`, `src/world_editor/mod.rs:777-785` | 90% | 1.5 |
| T3 | Station-list builder matching exact tds indexing (incl. duplicated segment endpoints) | `src/element_processing/highways.rs:1227-1236, 1290-1298, 1466-1474` | 85% | 3 |
| T4 | Per-way profile: width-median float sampling, per-class N table, pin constraints, two-direction clamp | insert before `highways.rs:1290`; sampling geometry from `:29-54` | 80% | 6 |
| T5 | Extend connectivity to all way nodes for mid-way junction pins | `highways.rs:365-400`, callers `data_processing.rs:479` | 85% | 2 |
| T6 | Consume `profile[tds]` in cell loop; extend to `block_range==0`; keep bridge/offset paths | `highways.rs:1315, 1498, 1508-1519, 1585-1595` | 80% | 4 |
| T7 | Min-fold override register + tile merge (flag-gated) | `src/world_editor/mod.rs:809-811, 818-838` | 90% | 2 |
| T8 | (Phase 2, optional) junction-fan surface reconciliation post-pass, sorted iteration | hook at `data_processing.rs:863, 897-902` | 55% | 6 |
| T9 | Unit tests: relaxation, direction invariance, order-permutation override | new tests in highways.rs / world_editor | 85% | 4 |
| T10 | Cross-border A/B + tile-count A/B harness + golden off-run | Meld cell harness + decode tooling | 75% | 6 |
| T11 | Docs: flag help, golden-hash consequence note, Meld scheduler wiring, build-hash metadata | args.rs docs, README-CHANGES | 95% | 1 |

Total: 36.5h (30.5h without optional T8). Highest-risk items: T3 tds-mapping fidelity (any off-by-one desyncs profile from placement) and T10 harness plumbing; the core algorithm (T4) is standard 1-D constrained slope limiting with no ordering or cell-local inputs by construction.

## CRITIC

Verified against `C:\Users\LEGION\Documents\Meld\arnis-triagefix` at 1597ce8c (branch confirmed). Most file:line anchors check out (SHOAL_DT_UNITS=9, depth_from_dt sqrt+round, ±2-unit wobble, dune gate, carve callers, tds accumulation at highways.rs:1950, override map at mod.rs:414/809/818-838, TILE_EDITOR_HALO=64). The plan is NOT sound. Numbered corrections, severity order:

**1. Road profile is not tile-invariant — DEM coverage, not anchoring (killer).** `Ground::level` clamps coordinates to the render bbox (`get_data_coordinates`, ground.rs:685-690, ratios clamped 0..1; same for `cover_class` :509-522). A way crossing a Meld cell seam has stations and junction pins outside the cell's bbox; `terrain_level_f64` there returns edge-clamped values, and the slope-limited relaxation propagates them into the cell without bound — the clamp binds continuously on any grade steeper than g, which is exactly the terrain the feature targets. Two cells rendering the same road compute different profiles near the seam. The plan's invariance argument #2 ("master-origin DEM") addresses anchoring only. Test 5's "nodes mostly outside one cell" variant would fail as designed. Fix must bound influence deterministically: hard re-anchor the profile to base at absolute-tds intervals K ≤ available DEM halo, or equivalent windowing. G4/G6's HOLD gate ("only on G3's tds test") is wrong.

**2. The relaxation algorithm is neither direction-symmetric nor pin-preserving as specified.** Counterexample, g=1, no pins: base [0,0,10,10] → forward [0,0,1,2], backward fixpoint [0,0,1,2]; reversed base [10,10,0,0] → [10,10,9,8]; mirror is [8,9,10,10] ≠ [0,0,1,2] (forward-first clamping biases toward the way's start). And pins forced only at seed are destroyed by the first pass (flat base 0, mid-way pin 10 → clamped to 1). "2 passes suffice" and "direction-symmetric (unit-tested)" — the stated algorithm fails its own test. Needs a closed-form symmetric construction: envelopes L[i]=max_j(pin_j − g·d_ij), U[i]=min_j(pin_j + g·d_ij), plus a symmetric Lipschitz projection of base clamped into [L,U].

**3. Lakes/oceans are NOT provably untouched — line-river stamps cross them.** The classification gate sits at apply time (override only where mask R=1, legacy path otherwise) — gate location is fine, gate sufficiency is refuted: OSM `waterway=river` centerlines are routinely mapped through lakes and past the coastline; the stamp (bresenham × half-width ≤ 64) lands on lake/sea columns that are LC_WATER and carve today → in R ∩ would-carve → depth overridden on lake-bed columns. Test 3(a) "every lake column byte-identical" fails by construction. The plan must specify clipping the line mask against non-river water polygons/coastline (element rings are available unclipped, tile-safe) and state the residual for ESA-only lakes, which have no polygon to subtract.

**4. Polygon-river classification premise is factually wrong.** Closed ways tagged `natural=water` + `water=river` never reach water_areas.rs: the dispatch (data_processing.rs:104-112) routes any way with a `natural` key to natural.rs, which paints surface water only; only relations and `waterway=dock` ways reach water_areas (:129, :239). So "relations/ways reaching water_areas.rs whose water tag VALUE..." misses the most common river-polygon form, and the apply-time "inside an OSM water-polygon fill span" membership doesn't exist for those ways (their carve is the LC pass). R2 must classify by tag scan over all elements, and the apply-time intersect for way-polygons must be respecified.

**5. `--water-carve-clearance` claim is false in Measured mode.** `estimate_max_carve_depth` (water_depth.rs:448-485, called ground.rs:263) returns the legacy width-tiered bound (2/3/4/6 from the map's max LC DT), not MAX_WATER_DEPTH. River v1 with tag-derived hw exceeds it: a way tagged width 40 crossing a 12-block LC strip reaches t≈1, depth up to 6 vs a reserved 2-3; the `.min(water_y - MIN_Y - 2)` clamp then silently flattens the bed at the floor. "Needs no change ... at every mode" is wrong; with `--river-bed v1` on, Measured must return max(legacy, river bound). Also missing from the test plan.

**6. The confluence blend re-imports what the plan claims to delete.** `legacy_f = bwf.depth_at` carries (a) the bank wobble + sqrt terraces — so every river column within band B of a lake/sea junction keeps attenuated terraces, contradicting "NO bank wobble ... bypassed structurally"; and (b) BWF's cell-bbox DT + per-cell-fragment comp_max seam hazard — so river columns with m < B near a cell border are not tile-invariant, one paragraph after "this deletes the BWF seam hazard for river columns." Bounded residual, but it must be stated, and test 4 needs a confluence-straddling-the-seam variant (currently absent).

**7. The m-field seeding is not tile-invariant as specified.** "Non-river WATER cell adjacent to the mask" requires water classification in the halo; `cover_class` clamps at the bbox edge, so halo seeds differ between neighbouring cells whenever a confluence lies within B + tent of a seam. Seed m from element-derived polygons only (tile-safe, misses ESA-only lakes) or document the clamped-LC residual.

**8. Mask-edge ≠ water-edge produces a permanent hybrid, and the release note mischaracterizes it.** For line rivers the profile's t=0 anchor is the tag-width footprint edge. The common case — wide LC-water river with a centerline way (default width 10) and no riverbank polygon — yields a narrow override ribbon where m < B everywhere: the bed is a permanent half-blend (never the pure profile) with legacy terraces outside the ribbon, a striped artifact plausibly worse than today. "ESA-only rivers with no OSM river tagging keep the legacy terraced bed" doesn't cover this case. Needs a spec decision (e.g., suppress line-stamp override when footprint ≪ surrounding same-component water, or widen hw from a tile-safe signal) plus a dedicated fixture.

**9. Division by zero / degenerate hw at small scale.** `scaled_half_width` returns 0 below scale 0.3 and ≤1 below 0.7 (waterways.rs:105-113); `t = d_blocks/hw` needs an hw ≥ 1 floor, and the small-scale test matrix (0.3 case) doesn't cover it. Also, if R2 reuses `compute_waterway_field`'s footprint "exactly," note that field gates on `ground_level(x,z) <= seg_water_y + BANK_TOLERANCE` (waterways.rs:246) — a Ground-dependent, bbox-clamped test; the mask must either drop that gate (and say so) or inherit a halo-clamping seam.

**10. Confidence/table corrections.** R2 85→~70 and R4 90→~80 until findings 3/4/6/8 are specified; G4's HOLD gate must include finding 2's respecification, not just G3; R5 (or a new row) must absorb finding 5's estimate change. Test-plan gaps: Measured-clearance mode, confluence-at-seam A/B, through-lake centerline fixture, ESA-wide-river + centerline-only fixture, scale-0.3 line river.

**11. Minor verified nits.** (a) data_processing.rs:466-468's comment references a nonexistent `carve_waterway_region` — stale, don't cite it; no line carve exists (plan's "dead" claim is correct). (b) ground.rs:694-719 is `interpolate_height`; `level` is :614 — the level_f64 refactor lands in interpolate_height (it rounds internally, so the accessor is a real refactor, not a wrapper). (c) Non-bridge shared endpoints repeat with the SAME tds (cumulative += segment_length−1, highways.rs:1950): the station builder must overwrite at equal tds, not append, or indices desync — G3's test should assert station_count == total_bresenham_length. (d) Chamfer reuse caps DT at u8 255 = ~85 blocks; harmless for depth (saturates at t=1) but should be stated as the hw ceiling for the polygon windowed-max. (e) Pre-existing, worth listing beside the Navodari caveat: `water_level`'s steep-terrain snap samples ±3 neighbours which clamp at the bbox edge, so water_y itself can differ within 3 blocks of a seam on steep banks — inherited equally by the new bed.

Direct answers to the two verification asks: the lakes-untouched gate sits at apply time inside the two carve callers (override consulted only where the element-derived river mask is set; all other columns take the byte-identical legacy `bwf.depth_at` path) — structurally correct but pierced by findings 3 and 4. The river-lake/sea blend IS specified (second chamfer DT m, band B = clamp(2·hw, 8, 32), smoothstep lerp to legacy with exact equality at m=0) — specified, but findings 6 and 7 show the specification is neither terrace-free nor tile-invariant near seams.