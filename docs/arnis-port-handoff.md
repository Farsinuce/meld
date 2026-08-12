# Arnis fork port — what Meld needs to know

Status note for anyone (human or agent) working in `light-meld` while the Arnis fork is being
brought up to date with upstream `louis-e/arnis`.

Written 2026-08-12. Fork repo: `../arnis-283-src`.

## Where the fork is

- Trunk is **`origin/main`** (branch `feat/caves` locally points at the same commit). Local
  `main` is stale by 206 commits — do not read it.
- Released: **v3.0.6** (`1cdc3c3`). In progress: **3.0.7**, unreleased, local commits only.
- The port wave takes upstream `af521c9..17cdd62` (31 non-merge commits). Every decision —
  take, adapt or skip, with the reason — is recorded in
  `../.light-meld-docs/UPSTREAM-TRIAGE.tsv`. The plan is
  `../.light-meld-docs/UPSTREAM-PORT-PLAN-3.1.0.mdx`, the log of what actually landed is
  `../.light-meld-docs/UPSTREAM-PORT-LOG.md`.

Landed so far (all local, unpushed): dependency + nix + `NOTICE` hygiene, and three performance
fixes — cubic ring assembly in `merge_way_segments`, memoized relation AABBs in tile assignment,
and non-allocating tag comparisons. All of it is gated on a **byte-identical render**: same bbox,
same flags, `block_hash=3d041c231bad7ff0` before and after. **Nothing in Meld has to change to run
3.0.7.**

## What is coming that *does* touch Meld

| Change | Meld file | When |
|---|---|---|
| `--terrain` → `--mode geo-terrain\|geo-only\|terrain-only` | `src/arnis_cmd.py` (~line 183) | after the fork lands its `--mode` batch |
| Canopy height maps → a baked tile dir + prewarm route, like the elevation/OSM bakes | new route + UI, mirrors the elevation prewarm | after the fork gains `--canopy-dir` |
| Image decals (bus stops, hydrants, recycling) | `src/merge.py` must copy each cell's `data/map_*.dat`, or Meld must run a post-merge `--map-item-only` pass | not scheduled |

Notes on each:

**`--mode`.** Upstream's version defaults to terrain **ON**. The fork deliberately does not take
that default — it keeps `Option<GenerationMode>` plus a hidden `--terrain` alias, so an existing
Meld build, an archived render command and a saved project with `"terrain": false` all keep
working unchanged. That is what makes the two sides updatable in either order. When Meld switches,
it becomes `--mode geo-terrain` / `geo-only`; the saved-project key stays `"terrain": bool`, no
settings migration.

**Canopy.** Taken, but only behind a baked path. Upstream's implementation range-fetches a Meta/WRI
canopy tile from S3 at render time — roughly 18 MB and ~2470 range requests **per cell**, which for
a 2900-cell run is tens of gigabytes, and a partial failure falls back to land cover silently
(invisible forest-density seams). Meld's OSM and elevation bakes are the model to copy.

**Map data.** `src/merge.py` currently walks `region`, `poi` and `entities` only, so per-cell
`data/map_*.dat` never reaches the master world. Note the fork already has `--map-item-only`
(`src/args.rs:451`) which Meld does not use — a post-merge pass over the finished world is probably
the right answer rather than merging per-cell map data, since per-cell map ids collide.

## Two things not to do

1. **Do not run a bare `cargo build --release` in `../arnis-283-src`.** That checkout is mid-port.
   `meld_launch.py`'s `build_arnis()` used to do exactly this and then copy the result over Meld's
   tracked `arnis.exe`; as of 1.8.4 it builds into a launcher-owned directory outside the checkout
   and logs the exact source version it built. If you need a generator binary, take a published
   release or ask for one built deliberately.
2. **Do not "align" the fork to upstream's file layout.** `src/tree_library.rs`, `src/region.rs`,
   `src/schematic.rs` and `src/land_cover*.rs` are flat on purpose and are independent
   implementations — upstream's `src/trees/` and `src/land_cover/` directories are a different
   codebase, not a rename. The non-goals section of the port plan lists the rest.

## Current binaries

`light-meld/arnis.exe` and `Meld/arnis.exe` are the 2026-08-10 build of v3.0.6 and have not been
touched by the port. Port builds go to a scratch target directory; nothing is deployed until a
release is cut.
