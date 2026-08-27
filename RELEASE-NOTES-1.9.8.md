# Meld 1.9.8

*(ships with arnis 3.1.8)*

World generation is substantially faster and tunes itself, a long list of
scheduling bugs is fixed, and two new generator options reshape river beds
and road surfaces.

Nothing in this release changes what your worlds look like unless you ask
for it. With default settings the output is identical to 1.9.7, verified by
hashing generated blocks on every build.

---

## Generation is much faster

Same city, same settings, same machine, before and after:

| | wall clock | cells/min | region files/min |
|---|---|---|---|
| 1.9.7, default settings | 230.3 s | 21.1 | 338 |
| 1.9.7, expertly hand-tuned | 174.9 s | 27.8 | 445 |
| the generator alone, given the whole machine | 181.2 s | - | 318 |
| **1.9.8, Auto** | **135.8 s** | **35.8** | **573** |

*(81 cells of central Bucharest at 1:1, warm caches.)*

**1.7x a stock install, 1.3x a hand-tuned one, and 1.8x what the generator
manages by itself on the same ground** - while using 30-40 points less
memory at peak. Every change was held to producing identical output:
reference cities are hash-verified on each build, and any optimisation that
altered a single block was rejected.

### Meld now tunes itself

One generator process cannot keep a modern CPU busy - measured, 10 of 24
cores on a large render - because parts of every render are single-threaded.
Running many cells at once has always been the answer, but the right number
depends on the machine, the scale and the city, and until now it was a
slider you had to guess.

Meld now starts low, times every finished cell, adds workers while that
actually improves throughput, backs off when it stops helping, and remembers
the answer per project and scale for next time. It never exceeds the limits
you set: a worker ceiling, a CPU percentage, and an amount of memory it must
leave free. Peak memory fell from 82-93% to 52-56% *while getting faster*,
and per-worker memory is now capped and constant in area, so city-sized
batches no longer climb toward swap.

### The generator stops doing work that was thrown away

Each cell used to write 36 region files of which Meld kept 16 - the rest a
seam-safety ring the merge deleted moments later. Meld now tells the
generator which regions each cell owns, so the ring is never written
(-12% per cell). Repeat renders also skip most map decoding: each cached
OSM tile gets a pre-parsed sidecar, verified against the tile's own contents
on every read, cutting decode from 946 ms to 429 ms per cell.

---

## Bug fixes

**Stop now actually stops.** The flag that halts a run was never set, so the
guard was dead: cancelled cells matched the retry filter and were quietly
resubmitted - twice. Prefetch had the same problem from the other side; it
accepted a stop signal that was never passed to it, so a stopped run could be
brought back to life by its own prefetcher.

**A project switch mid-run could merge a cell into the wrong world.** The
destination world path was re-resolved while the run was in flight, so
switching projects at the wrong moment merged finished cells into whichever
world was active by then. The path is now frozen when the run starts. A
second path through the export hook re-opened half of the same race and is
closed too.

**The first run of a new project was much slower than later ones.** Startup
staggering armed once per worker thread rather than once per run, so runs
after the first began in lockstep; and the worker-count search could not
recognise a good step early, leaving a fresh project running at about a third
of the machine. Cold runs improved from 173.9 s to 144.8 s, and CPU use from
60% to 79%.

**The worker-count search could stop for the wrong reason.** Throughput was
measured over however many cells had just finished rather than over a real
window of time, so a burst of quick cells read as a rate spike and a lull as
a collapse. The idle-machine guard had the same weakness in reverse: it could
make the search settle sooner but never keep it alive, which is the direction
that actually mattered. A stop is now only trusted once the machine is
genuinely busy.

**The memory guard was four times too pessimistic** whenever streaming to
disk was on, holding back workers there was room for.

**The CPU budget had two different defaults** (100% in one path, 90% in
another) depending on how it was reached; it is 90% everywhere now.

**GPU acceleration no longer travels in shared presets** - it is a property
of a machine, not of a world, and importing someone else's preset should not
flip it.

**Generation no longer depends on machine load.** The generator's flood-fill
work limiter varied with system load, so the same input could produce
slightly different output on a busy machine. Meld now sets it explicitly on
every child process.

**Caves at any world height** (generator fix, applies to worlds you make
here). Worlds taller or deeper than vanilla broke at both limits: above
roughly y=256 every underground block became air - one giant void under any
mountain reaching past it - and below y=-64 no caves generated at all, just
solid rock. Both limits now follow the world's own floor and ceiling.
Standard-height worlds are unaffected, bit for bit. Affected worlds heal per
cell on regenerate.

---

## New generation options

Both are **off by default** and both deliberately change the world, so a
project regenerated with one enabled will not match cells rendered earlier.

**Smooth river beds** (Settings, Generation). River beds were built from
integer depth steps, which showed up as concentric terraced shelves under
the water, with a flat shallow ring at the bank and dune bumps on the floor.
With this on, a river bed grades gently down from the shore, is broadly
rounded across the middle and comes back up symmetrically, with the bank
curve scaling to the river's width - a wide river gets long soft banks, a
narrow stream a tighter curve.

Lakes and oceans are left exactly as they are, including their own bed
noise, and a river blends into them rather than stepping. Worth knowing
before you enable it: where land-cover water data overhangs the mapped river
outline - which is common, since the two rarely align exactly - part of the
old terracing survives, so beds come out smoother rather than perfectly
clean. Rivers with no river tagging in the map data keep the old bed.

**Road grading** (Settings, Generation). Road surfaces followed the terrain
height directly beneath them, and because that height is rounded to whole
blocks, a road on a gentle slope crosses a rounding boundary partway along a
straight - and the full width of the carriageway steps by a block right
there. Nothing smoothed a road along its length.

With this on, each road gets a single height profile computed before
placement from the unrounded terrain, limited to at most one block of climb
per 4-12 blocks travelled depending on road class. Steps become evenly
spaced ramps instead of contour cliffs. Junction heights are pinned so
crossing roads agree, and road heights no longer depend on the order roads
happened to be processed in. Stairs, bridges, elevated sections and tunnels
are untouched.

---

## Settings

| control | what it does |
|---|---|
| **Mode: Off / Advise / Auto** | Off = your numbers, exactly as typed. Advise = runs like Off but logs what Auto would have done. Auto = Meld tunes the worker count live, within your limits |
| **Workers** | in Off, the worker count. In Auto, the *ceiling* - Meld may use fewer, never more. Switching to Auto offers once to raise a low ceiling; it never changes silently |
| **CPU limit %** | share of the CPU generation may plan for (10-95%) |
| **RAM headroom** | memory Meld must leave free; workers are admitted only while it holds |
| **Flush threads** | region-compression threads during the save phase |
| **Live readout** | during a run: current and target workers, threads per cell, measured cores per cell, cells/min, and which limit is currently binding |
| **OSM tile sidecars** | on by default; pre-parsed map tiles for faster repeat renders, at roughly two thirds more OSM cache on disk. Off = 1.9.7 disk behaviour |
| **Smooth river beds** | off by default; see above |
| **Road grading** | off by default; see above |

A fresh install behaves exactly like 1.9.7 until you switch Mode away from
Off. Sidecars are the one default-on item and they only touch the cache
folder, never the world. `MELD_GOVERNOR=off` in the environment overrides
everything.

---

## What changes for you

| if you are | what changes |
|---|---|
| using default settings | switch Mode to Auto once: renders finish in roughly 60% of the time, the machine stays responsive, and first runs are no longer slower than repeats |
| a slider tweaker | your numbers still work exactly as before in Off. Advise reports what it would have chosen without touching anything |
| building big maps | per-worker memory is capped and constant in area, so city-sized batches no longer creep toward swap |
| short on disk | turn off OSM tile sidecars; nothing else is affected |
| concerned about world quality | default output is hash-verified identical to 1.9.7, and every idea that changed even one block was rejected and recorded. The two new generation options are opt-in and clearly marked |
