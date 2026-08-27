# Meld 1.10 "Worldgen, faster" - draft release notes

*(pairs with arnis 3.1.8; arnis tags first, Meld bundles it)*

## What this release is

Five phases of measured performance work on world generation. Same city, same settings,
same machine, before and after:

| | wall (81 cells, Bucharest centre, 1:1) | cells/min | region files/min |
|---|---|---|---|
| Meld 1.9.7, default settings | 230.3 s | 21.1 | 338 |
| Meld 1.9.7, expertly hand-tuned | 174.9 s | 27.8 | 445 |
| plain arnis given the whole machine | 181.2 s | - | 318 |
| **this release, default Auto** | **135.8 s** | **35.8** | **573** |

**1.7x faster than a stock install, 1.3x faster than a hand-tuned one, 1.8x faster than
running arnis by itself on the same ground** - while using 30-40 points less RAM at peak,
and with the generated world byte-for-byte identical to what 1.9.7 produces. Every change
in this release was held to that standard: five reference cities are hash-verified on every
build, and any optimization that changed a single block was rejected.

## Why it is faster

**1. Meld now tunes itself (the Governor).** One arnis process cannot keep a modern CPU
busy - measured: 10 of 24 cores on a large render - because parts of every render are
one-threaded. Meld's tiling always fought that by running many cells at once, but the right
number of cells depends on the machine, the scale and the city, and until now it was a
slider you had to guess. The Governor starts low, times every finished cell, grows the
worker count while that actually helps, backs off when it stops helping, and remembers the
answer per project and scale for next time. It never exceeds the limits you set: a worker
ceiling, a CPU percentage, and a RAM headroom it must leave free.

**2. arnis stops doing work that was thrown away.** Each cell used to write 36 region
files of which Meld kept 16 - the rest were a seam-safety ring that the merge deleted
moments later. arnis now knows which regions the cell owns and never writes the ring
(measured: -12% per cell). Repeat renders also skip most of the OSM decoding: each cached
map tile gets a pre-parsed sidecar file, verified against the tile's content on every read
(measured: decode 946 ms -> 429 ms per cell).

**3. The scheduler stops sabotaging itself.** A long list of small correctness bugs fell
out of the measurement work: Stop now actually stops (it used to resubmit the cells it
killed), a stopped prefetch can no longer resurrect a run, a mid-run project switch can no
longer merge cells into the wrong world, and the first run of a new project now converges
as well as the tenth (it used to sit at a third of the machine).

## The new controls (Settings -> Generation performance)

| control | what it does |
|---|---|
| **Mode: Off / Advise / Auto** | Off = your numbers, used exactly as typed. Advise = runs like Off but logs what Auto would have done. Auto = Meld tunes the worker count live, within your limits |
| **Workers** | in Off: the worker count. In Auto: the *ceiling* - Meld may use fewer, never more. A one-time prompt offers to raise a low ceiling when you switch to Auto; it never changes silently |
| **CPU limit %** | the share of the CPU generation may plan for (10-95%) |
| **RAM headroom** | free memory Meld must leave untouched; workers are admitted only while it holds |
| **Flush threads** | region-compression threads during the save phase |
| **live readout** | while a run is active: current -> target workers, threads per cell, measured cores/cell, cells/min, and which limit is currently the binding one |
| **OSM tile sidecars** (OSM drawer) | on by default; pre-parsed tiles for faster repeat renders at ~2/3 extra OSM cache size. Off = 1.9.7 disk behaviour |

Everything ships **default-off**: a fresh install behaves exactly like 1.9.7 until you
switch the Mode away from Off (sidecars are the one default-on item, and they only touch
the cache folder, never the world). Emergency switch: `MELD_GOVERNOR=off` in the
environment overrides everything.

## Expected outcomes by user

| you are | what changes for you |
|---|---|
| default-settings user | flip Mode to Auto once: renders finish in roughly 60% of the time, machine stays responsive (RAM guarded), first runs no longer slower than repeats |
| slider tweaker | your numbers still work exactly as before in Off; Advise will tell you what it would pick without touching anything |
| big-map builder | per-worker memory is capped and constant in area - city-sized batches no longer climb toward swap (measured peak fell 93% -> 56%) |
| tight on disk | untick "OSM tile sidecars"; everything else is unaffected |
| worried about world quality | nothing to worry about: outputs are hash-verified identical to 1.9.7, and every risky idea that changed even one block was rejected and documented |

## For the curious

The full engineering record - every measurement, every dead end, and the three plausible
optimizations that were rejected because the benchmark could not distinguish them from
noise - is in `docs/perf-final-report.md` and the phase documents beside it. Highlights:
the machine's limit turned out to be memory bandwidth, not cores; a "faster" run that
failed 36 cells once masqueraded as a 52-second win until the harness learned to refuse it;
and the seam ring around each cell turned out to be load-bearing - deleting its
*generation* (rather than its files) corrupts cell borders, which is why this release
deletes only the files.

## Also fixed in this release

**Tall worlds + caves: the giant void above y~256 is gone.** The cave engine ported
vanilla's world-top fade with vanilla's hardcoded coordinates, and above y=256 that
arithmetic makes every underground block air regardless of the noise - one huge cavern
under any mountain that reached past it. The fade now follows the world's own ceiling.
Standard-height worlds are unaffected (bit-for-bit - the formula lands on the exact
vanilla numbers there); tall worlds get real rock and real caves all the way up.
Affected worlds heal per cell on regenerate, like every generator fix.

## Versions and compatibility

- arnis **3.1.8**: all new behaviour is opt-in via env/flags (`--canonical-regions`,
  `ARNIS_PHASE_MARKERS`, `ARNIS_FILL_BUDGET`, `ARNIS_OSM_SIDECARS`); a standalone arnis
  run is unchanged.
- Meld: settings are per-project, new keys never travel in shared presets and never enter
  world metadata. Old projects open unchanged in Off mode.
- Worlds generated before and after this release are identical, cell for cell.
