<div align="center">

<img src="assets/banner.png?v=181" alt="Meld, Arnis at scale" width="100%">

# Turn the real world into one seamless Minecraft world

From a single city block to a whole continent, in one build.
Built on a fork of [Arnis](https://github.com/louis-e/arnis) by louis-e.

&nbsp;![version](https://img.shields.io/badge/version-1.8.2-blue)
&nbsp;![Minecraft](https://img.shields.io/badge/Minecraft%20Java-1.21%2B-brightgreen)
&nbsp;![Python](https://img.shields.io/badge/Python-3.10%2B-yellow)
&nbsp;![built on](https://img.shields.io/badge/built%20on-Arnis%20fork-orange)

**Windows · macOS · Linux** &nbsp;·&nbsp; Latest: **[1.8.2 Field reports](CHANGELOG.md)** &nbsp;·&nbsp;
[Docs](https://meldmc.com/docs) &nbsp;·&nbsp; [Live demo](https://meldmc.com/demo)

</div>

**Arnis builds one area at a time. Meld runs it over a whole region.** Draw an area on a map and
Meld splits it into tiles, builds them in parallel with a custom
[Arnis fork](https://github.com/Teddy563/arnis), and joins them into one world. Every seam lands on
a Minecraft region boundary, so the join is exact. About 2x faster than a single Arnis pass on the
same area, and it scales to areas a single pass cannot finish.

---

## Quickstart

```bash
git clone https://github.com/Teddy563/meld
cd meld
```

Then start it. **Windows:** double-click `meld.bat`. **macOS:** double-click `meld.command` (first
time: right-click, Open). **Linux:** run `./meld.sh`.

The launcher makes a virtual environment, installs the dependencies, fetches the matching `arnis`
binary, and opens `http://127.0.0.1:5630`. Only **Python 3.9+** is required. Run
`python meld_launch.py --check` to see what is installed without changing anything.

Prefer manual: `pip install -r requirements.txt` then `python server.py`, with an `arnis` binary
next to it. Prebuilt binaries: [Teddy563/arnis releases](https://github.com/Teddy563/arnis/releases).

Then: draw an area, set the cell size, press **Generate world**.

---

## What it does

**Scale**

| | |
|---|---|
| Parallel tiles | Builds many Arnis instances at once, bounded by a worker pool. [docs](https://meldmc.com/docs/parallel-generation) |
| Region perfect merge | Cell edges snap to Minecraft region boundaries, so tiles join exactly. |
| One elevation lock | A single global height range and seed, so terrain matches across every border. |
| Resume and retry | Re-run only unfinished cells, or click one cell to rebuild it. |
| Recommend | One click sets cell size and workers for your machine. Keep workers x threads at or under your cores. |
| Live tuning | Change workers, threads and CPU budget mid-run. No restart. |
| Projects and queue | Keep many worlds side by side, group them in folders, and render a whole folder one after another. |
| Benchmark report | Every run writes an HTML report: machine specs, CPU and RAM graphs, per-worker timeline. |

**The world itself**

| | |
|---|---|
| Build height (1.8.1) | Pick your Minecraft version and let the world's height follow the terrain. Mountains keep their peaks, and ground can go below vanilla's -64. [docs](https://meldmc.com/docs/build-height) |
| Farmlands (1.8.0) | Field parcels that follow the roads, one crop per plot at varying growth, meadows, tracks, hay bales, rocks and bushes. |
| Caves (1.5.0) | One toggle carves a full cave system into every cell: caverns, tunnels, underground water, ores, geodes, eight themed cave biomes. [docs](https://meldmc.com/docs/caves) |
| Region trees (1.4.0) | 1,959 hand-made models across 10 world regions by [paleozoey](https://www.planetminecraft.com/member/paleozoey/), placed by location, five size tiers. [docs](https://meldmc.com/docs/tree-packs) |
| Terrain height and snow (1.4.0) | Make mountains taller without widening the map. Snow by real latitude, by peak percentage, or at a fixed height. [docs](https://meldmc.com/docs/terrain-and-snow) |
| Props and facades (1.6.0) | Boats, cars, cranes, tractors and more placed at real map features, plus six skyscraper styles and editable chest loot. |
| Buildings and roads | Toggle buildings off for a roads and land cover world. Road detail modes keep small scales legible. |
| LOD ready | Chunk lighting is baked, so Distant Horizons and Voxy render distant chunks lit. |

**Map data**

| | |
|---|---|
| Shared OSM prefetch | The area is downloaded once and reused by every cell, so parallel runs never hit the rate limit. |
| OSM data packs | Bake a region from a local `.pbf` file and generate with no Overpass calls at all. |
| Elevation packs | Download a region's elevation once into a shared cache, then build offline. [docs](https://meldmc.com/docs/elevation) |
| Reusable cache | Tiles are cached on a fixed grid, so overlapping selections share them. |
| Height preview | See cached elevation on the map. Red means a tile is not cached yet. |
| Hole repair | Rebuilds the source data's no-data gaps that showed as dark bands and dips in game. |

**Shipping the world**

| | |
|---|---|
| Export and compression | zip and tar.zst (~1.85x), Linear (~4.85x, servers), or B_Linear. The raw world is never deleted before the copy verifies. |
| Server setup | World to a running Leaf server in five confirmed steps, with hash-verified downloads, a live console and a crash watchdog. [docs](https://meldmc.com/docs/server-setup) |
| Border and zones | Turn a country into WorldGuard regions, point files and a ready Skript, then trim the world to that border. [docs](https://meldmc.com/docs/border-zones) |
| Getting it onto a server | Open it in single-player once, stop the server, then move the whole folder or import it with Multiverse. Never paste over a running world. [docs](https://meldmc.com/docs/troubleshooting) |

---

## How it works

1. **Origin.** One lat/lon snapped to a region corner. Every cell measures from it.
2. **Survey.** Lock one elevation range and seed for the whole area.
3. **Plan.** Split the selection into region aligned cells.
4. **Prefetch.** Download the map data once, then feed it to every cell.
5. **Generate.** Build cells in parallel with the Arnis fork.
6. **Merge.** Write each cell's own regions into the master world, with a guard against overlap.

More detail: [how it works](https://meldmc.com/docs/how-it-works) and [the Arnis fork](https://meldmc.com/docs/the-arnis-fork).

---

## Good to know

- **Generation is CPU bound.** Keep workers x threads at or under your cores. Going over slows the
  build. Disk speed and RAM are secondary limits that **Recommend** accounts for.
- **One Arnis binary is required**, named `arnis` on macOS and Linux. The app says so on startup if
  it is missing.

---

## More

- [Docs](https://meldmc.com/docs) and the [live demo](https://meldmc.com/demo)
- [CHANGELOG.md](CHANGELOG.md) for the full history, [RELEASE-NOTES.md](RELEASE-NOTES.md) for
  highlights
- [`docs/README-detailed.md`](docs/README-detailed.md) is the long form of this page
- [`docs/`](docs/) holds the per-release deep dives

---

## Credits

Built on the open source [Arnis](https://github.com/louis-e/arnis) generator by louis-e. Meld drives
a [custom Arnis fork](https://github.com/Teddy563/arnis) for the shared OSM prefetch and the
position based rendering that makes tiles line up. Respect the upstream Arnis license.

Tree models by **[paleozoey](https://www.planetminecraft.com/member/paleozoey/)**. Meld bundles and
places them with attribution; the artistry is theirs.

Not affiliated with Mojang AB or Minecraft.
