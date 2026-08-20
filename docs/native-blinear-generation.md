# Native B_Linear generation — arnis writes Leaf's `.b_linear` directly

**Status:** proposal (branch `meld-linear`, local only — do not push)
**Date:** 2026-08-20
**Owner:** Teddy563
**Scope:** arnis fork (`arnis-283-src`, v3.1.1) + Meld (`light-meld`)

## Decision summary

Add an **experimental** `--region-format blinear` toggle to the arnis fork that writes
Leaf's **B_Linear v3** region container (`r.<x>.<z>.b_linear`) directly at save time,
instead of Anvil `.mca` followed by a conversion pass. Default stays `.mca`; the toggle
is opt-in and labeled experimental in the Meld UI.

Why B_Linear and not Linear:

- Leaf's own docs: *"Due to Linear v2 having many design flaws and being very dangerous
  to production, we strongly recommend that you use Buffered Linear."* Leaf's runtime
  even logs a data-loss warning when LINEAR_V2 is selected (`RegionFormatConfig.java:68`).
- B_Linear v3 is the format Leaf's B_LINEAR reader consumes natively; no on-load
  migration happens (LINEAR_V2 worlds get rewritten to linear-v3 by the server; B_Linear
  worlds are read as-is).
- The classic Linear v1 export path in Meld (`src/export.py`, Python codec) stays as-is
  for users who pick `.linear` — no arnis work needed there.

The extension is **`.b_linear`**, not `.blinear`. Triple-confirmed:

1. Leaf source `EnumRegionFormat.java` (ver/26.2): `MCA → ".mca"`, `LINEAR_V2 → ".linear"`,
   `B_LINEAR → ".b_linear"`.
2. A Leaf 26.1.2 server run locally by Meld (projects/carved-e2e) wrote its own
   `entities/r.X.Z.b_linear` files.
3. region-convert and Meld's existing blinear export already produce `.b_linear`.

## Ground truth (verified against Leaf ver/26.2 source + local Leaf 26.1.2 install)

Implementation classes inside the Leaf jar:

- B_LINEAR = `me.earthme.luminol.data.BufferedLinearRegionFile` (Luminol lineage,
  added via Leaf PR #754, 2026-06-01; ships in 1.21.11 June-2026+ builds and all 26.x).
- LINEAR_V2 = `abomination.LinearRegionFile` (Xymb's Abomination lineage).

Reader strictness (what an external writer MUST get right):

| Field | Behavior on read |
|---|---|
| Superblock `-0x2008_1225_0269` (i64 BE) | STRICT — mismatch fails the whole region |
| Version byte `0x03` | STRICT (0x02 accepted as legacy blinear-v2 and migrated) |
| Per-chunk XXHash32, **seed `0x0721`** | STRICT — Leaf hardcodes the seed and **ignores the header seed field**; hash mismatch throws on that chunk's load |
| Header compression-level byte | ignored on read |
| Header hash-seed field | ignored by Leaf (region-convert *does* use it — write `0x0721` so both agree) |
| Chunk timestamp (i64 millis) | read and ignored (`// TODO use this timestamp`) — 0 is tolerated |
| Inner `payloadLen` | ignored; outer slot length drives parsing |
| File < 142 bytes | silently treated as an empty region |

Leaf write-side behavior (why B_Linear is the "safe" one): server saves buffer into a
`.swp` sidecar (LZ4 sectors), a background flusher (`io-flush-delay`, default 3000 ms)
rewrites the master as `.tmp` → `force(true)` fsync → atomic rename, byte-copying
untouched buckets. A crash never tears the master file. Our writer mirrors the same
tmp-then-rename discipline.

Leaf config Meld already writes (`src/mcserver.py:194-211`):
`misc.region-format.format-name: B_LINEAR`, `compress-level: 6` — plus Leaf-side keys
`io-thread-count`, `io-flush-delay`, `linear-use-virtual-thread` left at defaults.
`REGION_FORMAT_BY_EXPORT` already maps blinear → B_LINEAR; **no mcserver.py changes needed**.

## Byte-exact format spec (B_Linear v3)

All integers big-endian. Mirrors `region-convert/src/formats/blinear_v3.rs` (the
production-verified writer that converted the 13k-region Romania world) and Leaf's
`BufferedLinearRegionFile.java` §919-1185.

```
File: region/r.<x>.<z>.b_linear          (exactly 4 dot-separated segments)

[0,8)    i64  superblock = -0x2008_1225_0269
[8]      u8   version    = 0x03
[9]      u8   zstd compression level (informational)
[10,14)  u32  hash seed  = 0x0000_0721 (informational for Leaf; used by region-convert)
[14,142) 16 × u64 absolute file offset of each bucket record; 0 = empty bucket
[142,EOF) bucket records: i32 originalLen | i32 compressedLen | zstd(rawBucket)
```

- `chunk_index = (x & 31) + (z & 31) * 32` (Anvil header order);
  `bucket_index = chunk_index >> 6` → 16 buckets × 64 chunks (two Z-rows each).
- Raw bucket = 64 slots in ascending index. Slot: `i32 N` (0 = absent) then N bytes:
  `i32 rawLen | i64 timestampMillis | u32 xxh32(rawNBT, seed 0x0721) | rawNBT`.
  `N = rawLen + 16`. Payload = **uncompressed** chunk NBT (same bytes as a
  zlib-inflated `.mca` chunk; no per-chunk compression).
- Bucket with all 64 slots empty: not written at all; offset stays 0.
- Compression: `zstd::bulk::compress(rawBucket, level)`, one frame per bucket. No footer.
- Offset table backfilled by seek after buckets are written.
- Empty region = legal 142-byte file (header + zero table).
- Timestamps: write now-in-**millis** (or 0). Never seconds — values < 1e10 get ×1000
  by re-encoders (`mod.rs:393-399` normalization), and the codec_roundtrip test locks that.

## arnis changes (`arnis-283-src`)

Design rule: blinear is a **container option on the JavaAnvil path**, NOT a fourth
`WorldFormat` variant — parallel tiles, streaming eviction (`FlushWorker`), golden-hash
gate (in-memory `content_hash`, container-agnostic) and block-entity schemas are all
gated `== JavaAnvil` and must stay untouched.

| # | File | Change |
|---|---|---|
| 1 | `src/args.rs:77-83` | `--region-format {mca,blinear}` clap ValueEnum next to `--bedrock`/`--luanti`; default mca |
| 2 | `src/args.rs:719-721` | mutual exclusion with `--bedrock`/`--luanti` (modifier flag — the early-exit `validate_args` exemption rule does NOT apply here) |
| 3 | `src/gui.rs:1098-1207` | MANDATORY: init the new field in the exhaustive `Args` struct literal or the build breaks. Do NOT surface a GUI toggle (b_linear is server-only; GUI writes into `.minecraft/saves`) |
| 4 | `src/world_editor/mod.rs:123-169` | new `java_container` field on `WorldEditor` + setter; init in all 3 constructors (mod.rs:176/204/240) |
| 5 | `src/data_processing.rs:23-30, 360-383` | thread container through `GenerationOptions` (set at main.rs:639-646 and gui.rs:1087-1094) |
| 6 | `src/world_editor/java.rs:284-388` | the actual swap in `write_region_to_disk`: branch before `create_region_file` (:297); collect the raw `ser_buffer` NBT (:347) + base-chunk NBT (:381) into the bucket encoder instead of `region.write_chunk`; write `.b_linear` via same-dir dot-temp + rename. ~200 lines, portable nearly verbatim from `region-convert/src/formats/blinear_v3.rs:270-354` + `mod.rs:380-392` |
| 7 | `src/world_editor/java.rs:392-447` | `RegionWriteCtx` carries the container flag → streaming-eviction FlushWorker path covered for free (single funnel) |
| 8 | `src/world_utils.rs:153-173` | `scaffold_world`: skip the `r.0.0.mca` template write + `restamp_region_data_version` under blinear; keep region/ dir, level.dat, icon.png |
| 9 | `src/data_processing.rs:1166-1173` | gate `--map-item` on container == mca (user-accepted disable). GUI preview call sites gui.rs:1240-1244, :1349-1355 likewise (otherwise the renderer silently emits an all-white map). `--map-item-only` on a blinear world already fails cleanly ("world has no saved regions") |
| 10 | `src/world_editor/mod.rs:1517-1551` | metadata.json: add `region_container: "mca" \| "b_linear_v3"` — additive; gives Meld/tools a detection hook |
| 11 | `Cargo.toml` | add `xxhash-rust = { version = "0.8", features = ["xxh32"] }` (region-convert pins 0.8.15). `zstd = "0.13"` already a direct dep. Nothing else |

Timestamp note: fastanvil 0.32 never writes the `.mca` timestamp table, so today's mca
timestamps come from the 4 MB template. The blinear writer synthesizes
`SystemTime::now()` millis per chunk — Leaf ignores it either way.

Atomic-write convention to mirror (matters for Meld's progress poller, which counts
atomically-renamed `r.*.b_linear` files): temp name `.r.<x>.<z>.b_linear.tmp-<pid>-<counter>`
in the same directory, write + flush, rename (Windows fallback: remove-then-rename).
Dot-prefix keeps it out of the `r.*.b_linear` glob until commit.

## Meld changes (`light-meld`)

| # | File | Change |
|---|---|---|
| 1 | `src/project.py` | new setting `native_region_format: "mca" \| "blinear"` (default mca), UI-labeled **Experimental** with plain-text warning: server-only world, no `.mca` fallback copy, map item disabled |
| 2 | `src/arnis_cmd.py:248+` | pass `--region-format blinear` when enabled |
| 3 | `src/merge.py:30,103,227` | extend `_MCA_RE`/globs to `r.X.Z.(mca\|b_linear)`. Merge stays whole-file `shutil.copy2` — verified: merge.py reads zero file bytes, so nothing else changes. Cell + master must be the SAME container (guard: error on mixed extensions) |
| 4 | `src/finalcheck.py:23-27` | count `.b_linear` as present; empty-region heuristic = file size ≤ 142 bytes (header + zero table), exact analogue of the ≤ 8192 `.mca` check |
| 5 | `server.py` | skip `_maybe_run_export`/overlap-linear pool when native blinear (world is already final); skip the `--map-item-only` post-pass (server.py:2434) under native blinear; safeguard-D analogue: on cell re-merge delete stale `.b_linear` siblings (it already does this for `.linear`) |
| 6 | `src/mcserver.py` | none — `blinear → B_LINEAR` mapping + `pick_world_source` validation of `r.*.b_linear` already exist |
| 7 | export UI | when native blinear is on, the export dropdown's blinear option becomes "already native"; zip/tarzst of a blinear world still works (plain files) |

Retry/repair path: regenerated cells re-run arnis with the same toggle → fresh
`.b_linear` regions merge in. No mixed-format master as long as the toggle is a
per-project (not per-run) setting; enforce that in project.py.

Root `meld/` package tools (mca.py, chunk_protection.py, subworld.py, …) glob `*.mca`
only and would silently no-op — out of scope; they are not part of the light-meld run
path. The metadata.json `region_container` field is their future detection hook.

## Test plan

**T0 — instrumentation baseline (do FIRST, closes the only measurement gap).**
The fork already prints `save_ms` under `--benchmark` (`data_processing.rs:1131-1135`,
`bench.rs`). Run the Flevoland reference bbox + one 256-region Meld run with
`--benchmark` on current main to record the `.mca` save-phase share. No code changes.

**T1 — unit: writer golden tests (Rust, arnis repo).**
- Round-trip: encode a synthetic region (sparse/dense/empty slots) → decode with a
  test-local reader → byte-equal NBT, correct offsets, xxh32 pass.
- Empty region = exactly 142 bytes; empty bucket = offset 0; slot order; bucket math
  (index 63/64 boundary, negative region coords).
- Header constants (superblock, 0x03, seed field 0x0721).

**T2 — cross-validation against region_converter (the killer test, zero new tooling).**
`region_converter --info <world>` fully decodes every region and verifies every chunk's
xxh32, exiting non-zero on any problem. Every arnis-written world in CI/E2E goes through
`--info`. Then `region_converter --to mca` the same world back and parse with the Python
codec.

**T3 — equivalence gate (extends the existing golden-hash discipline).**
Same seed/bbox generated twice: (a) `.mca` → `region_converter --to blinear-v3`,
(b) direct `.b_linear`. Decode both with region_converter; **chunk NBT must be
byte-identical** per chunk index (timestamps excluded). The fork's in-memory
`content_hash` gate already proves the generator side is container-agnostic.

**T4 — Leaf live-boot E2E (extends carved-e2e).**
Stage a Leaf 26.1.2/26.2 server on the direct-written world with
`format-name: B_LINEAR` (Meld's existing one-click path), boot, then via the stdin
console: `forceload add` a spread of chunks incl. region corners + a far region, watch
the log for `XXHash32 check failed` / any `BufferedLinearRegionFile` errors, stop server
cleanly, confirm Leaf's own resave still passes `region_converter --info`.

**T5 — crash-safety.**
Kill arnis mid-run (during flush). Assert: no `r.*.b_linear` is ever partial (only
dot-temps may be orphaned), master world still `--info`-clean for completed regions.

**T6 — Meld pipeline E2E.**
16-cell run with toggle on: merge picks up `.b_linear` cells, seam-buffer strip works,
finalcheck reports correct present/empty counts, retry of a deleted cell re-merges,
export dropdown behaves, server plan/stage picks B_LINEAR.

**T7 — perf A/B (the numbers the summary promises).**
Same bbox, 3 runs each, report medians: (a) current pipeline gen→convert→verify,
(b) direct write. Record wall, save_ms, peak RAM, peak disk, final disk. Do this at
256 regions and once at ≥ 2000 regions.

## Performance estimate (evidence-based, to be confirmed by T7)

Measured inputs:
- Blinear conversion post-pass: ~1000 regions/min post-mimalloc (13,044 regions in
  ~13 min, commit dce09c9); it also doubles disk residency (Carved E2E: 2.96 GB `.mca`
  + 1.25 GB `.b_linear` simultaneously).
- Generation: 256 regions ≈ 86-421 s; Bucharest-scale 9,408 regions ≈ 30 min at
  8 workers.
- zstd vs zlib: ~5× faster codec (upstream Linear README); save-phase share of a run is
  unmeasured (hence T0).
- Real blinear disk ratio on our data: 2.36× (Carved E2E cave world) — do NOT promise
  the 4.3× planning constant; ratio is world-dependent.

Expected end-to-end (generation start → Leaf-ready world):

| World size | Convert pass eliminated | Est. end-to-end gain |
|---|---|---|
| 256 regions | ~15-30 s | ~5-10 % |
| 2,304 regions (24k×24k) | ~2-3 min | ~15-20 % |
| 9,408 regions (country) | ~9-13 min | **~25-30 %** |

Plus, in all cases: write bytes roughly halved (one world written instead of two, and
the one written is ~2.4× smaller than Anvil), peak disk ~0.3× of Anvil-then-convert
(~1.3×), and the arnis save phase itself likely gets faster (zstd-6 per bucket replaces
zlib per chunk — T0/T7 confirm). RAM delta ≈ negligible: the whole region is already
resident at write time today; the bucket staging buffer adds low tens of MB.

## Risks

| Risk | Mitigation |
|---|---|
| Writer bug corrupts worlds with no `.mca` fallback | experimental default-off toggle; T2 `--info` verify after every E2E; T3 byte-equivalence gate; users keep the export path |
| Leaf format evolves (v4?) | version byte is checked strictly on both sides; T4 pins the supported Leaf builds (1.21.11 Jun-2026+, 26.x); region_converter remains the escape hatch both directions |
| Seed mismatch (Leaf hardcodes 0x0721, ignores header) | write 0x0721 in header AND hash with 0x0721 — satisfies Leaf and region-convert simultaneously (T2/T4 both catch regressions) |
| FlushWorker stall from per-bucket zstd at high level | default level 6 (Leaf's own default); optional `--blinear-level 1..22`; T7 watches eviction backpressure |
| Mixed-container master (mca cells + blinear cells) | per-project setting + merge.py extension guard errors on mismatch |
| Vanilla client can't open the world | documented in the toggle's warning text; `region_converter --to mca` converts back |

## Milestones

1. **M0** — T0 instrumentation runs on current main (half a day, no code).
2. **M1** — arnis writer + flag + gates + unit tests (T1), `--info`-clean world (~1.5-2 days).
3. **M2** — Meld integration (merge/finalcheck/server/project/UI) + T6 (~1 day).
4. **M3** — T3 equivalence + T4 Leaf boot + T5 crash test wired into carved-e2e (~1 day).
5. **M4** — T7 A/B numbers, doc the results here, decide whether the toggle graduates
   from experimental.

## Open questions

- Entities/POI: arnis embeds entities in terrain chunks and writes no `entities/`/`poi/`
  regions — Leaf creates its own `entities/r.*.b_linear` on first run (observed in
  carved-e2e). Nothing to do, recorded for completeness.
- Should native-blinear runs also offer a one-click "archive .mca copy" (run the
  converter in reverse post-gen) for users who want the fallback? Cheap to add via
  existing meldconvert path; decide at M4.
