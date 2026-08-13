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

## The generator auto-download is broken, and the frozen app has none

Measured 2026-08-13 against the GitHub API, not inferred.

**Two separate paths.** `meld_launch.py` → `ensure_arnis()` downloads the right asset for this
OS/CPU from `Teddy563/arnis` `releases/latest`, else builds from source, else warns.
`meld_app.py` (the frozen build) → `paths.unpack_embedded_arnis()` copies the binary **baked in at
build time** and never touches the network. So a packaged Meld ships a pinned generator and can
never pick up a newer one.

**Three defects in the download path:**

1. `releases/latest` was **v3.0.3 with zero assets** — tags v3.0.4/3.0.5/3.0.6 existed but no
   GitHub Release was ever created for them, so the download failed on every OS. Fixed by cutting
   a real Release: `release.yml` fires on `release: created` and attaches all four assets.
   **v3.0.7 is tagged and pushed; someone with repo access still has to publish the Release**
   (GitHub UI → Draft a new release → pick `v3.0.7` → Publish, or `gh release create v3.0.7`).
2. **macOS asset names do not match.** `_arnis_asset()` asks for `arnis-mac-arm64.tar.gz` /
   `arnis-mac-intel.tar.gz`; `release.yml` attaches only `arnis-mac-universal.tar.gz` (the
   per-arch tarballs are internal CI artifacts). Mac fails even against a healthy release. Fix is
   one line: return `arnis-mac-universal.tar.gz` for Darwin on both arches.
3. **The frozen app cannot self-update.** If it should, note the resolution order in
   `server.resolve_arnis_exe()`: `[APP_DIR, APP_DIR.parent, BASE_DIR, BASE_DIR.parent,
   arnis-source/target/release, bin_dir()]`. `bin_dir()` is **last**, so a freshly downloaded
   binary loses to the bundled copy next to the exe. Either write the download over the unpacked
   copy in `bin_dir()`, or move `bin_dir()` ahead of the bundled location — and gate it on a
   version probe so it only downloads when the release is actually newer.

`MELD_ARNIS_REPO` overrides the repo for testing.

## Closing the wave: the `-s ours` merge

The fork shows "N commits behind louis-e/arnis:main" because GitHub compares a fork to its parent.
Porting rewrites commits under new SHAs, so that number never falls by porting — only a merge that
makes upstream an ancestor clears it. There is a precedent: `1453eeb`, *"merge: mark upstream v3.0.0
(af521c9) as incorporated (ours strategy)"* — two parents, **empty diff against the first**.

Repeat it only when **no `plan:*` row is left** in `UPSTREAM-TRIAGE.tsv`. `upstream-triage.sh`
derives its base from `git merge-base`, so it self-heals after the merge and starts a fresh queue —
which is exactly the danger: anything still unresolved at merge time drops out of the queue forever
and survives only in the TSV. Skip and defer rows count as resolved; flip them to their terminal
status first.

```bash
git merge -s ours upstream/main -m "merge: mark upstream wave 1 (<sha>) as incorporated (ours strategy)"
git diff --stat HEAD^1 HEAD    # MUST be empty
```

Never press GitHub's **Sync fork** button. That is a real merge and would pull in the `--mode`
terrain-on default, the canopy network fetch and upstream's `src/trees/` + `src/land_cover/` layout,
straight over the fork's work.

## Current binaries

`light-meld/arnis.exe` and `Meld/arnis.exe` are the 2026-08-10 build of v3.0.6 and have not been
touched by the port. Port builds go to a scratch target directory; nothing is deployed until a
release is cut.
