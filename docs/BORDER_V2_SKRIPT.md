# Border system v2 — server-side Skript design (prepared, not built)

v1 (shipped in Meld) produces the **geometry + WorldGuard `regions.yml` + per-ring point files +
map preview + cell-trim**. v2 is the **server runtime**: titles, the escalating kill-zone, the
fling-back wall, and packet particles. Meld cannot run or test these — this doc is the spec to hand
to the server. It supersedes the pasted v3 prompt and folds in what we agreed.

## Division of labour (unchanged)
- **WorldGuard** owns geometry + the few build rules. Meld exports it.
- **Skript** owns titles, damage, and the hard wall.
- **Particles** are packet-based (per-player), never world-spawned.
- **Trim** is offline — Meld already trims to the hard ring at cell granularity (`Trim world to ring`).

## What Meld hands you (per zone `<z>` = lower-cased zone name, e.g. `romania`)
From the **Border & zones** panel → Export (written to `<project>/border/`):

| Artifact | Use |
|---|---|
| `<z>_actual.txt` | each country/coast line — identity titles + cyan wall (one per zone) |
| `border_soft.txt` | clump +soft km — ONE ring around the whole clump; edge of the buildable safe zone, orange wall |
| `border_hard.txt` | clump +hard km — ONE ring; outer wall (yellow), no-build/kill-zone edge, trim line |
| `shared_<a>_<b>.txt` | internal line where two zones touch (e.g. Prut) — lime wall + cross-title |
| `regions.yml` | WorldGuard poly2d: `<z>` (identity), `border_soft` (build OK), `border_hard` (no-build wall) |

Each point file: `x,z,lon,lat`, **absolute world block coords** (Meld's origin offset already baked
in — no OFFSET to add). Particles read these files; titles/damage read WorldGuard region events.

## Zones model (multi-zone + owners)
v1 supports **N zones**, each with its own bands, colours, and WorldGuard `owners[]`/`members[]`.
So several admins/factions each define a zone (or co-own one); all land in one `regions.yml`. The
Skript is written **per zone-name** — loop the region names, do not hard-code one country.

## Regions (emitted by Meld; soft + hard are ONE ring each around the WHOLE clump)
The soft + hard buffers are built on the **union of all zones**, so the wall never runs between two
adjacent countries — only around the combined outer edge. Build is allowed out to the **soft** ring
(a few-km safe margin), denied in the **soft→hard** band (the no-build kill-zone) and outside.
```
__global__:                      block-break: deny, block-place: deny   # outside everything = no build
border_hard  priority 5          block-break: deny,  block-place: deny  # soft->hard band: NO build (+ kill-zone), outer wall
border_soft  priority 8          block-break: allow, block-place: allow # country + soft margin: BUILD OK
<z>          priority 12          (no build flag)                        # identity / titles + owners/members
```
Build works inside `border_soft`; the `border_soft`→`border_hard` band is no-build + kill-zone;
past `border_hard` is denied (and void after trim). The internal shared line has **no region** — it
is only a particle wall + title trigger. Kill-zone = in `border_hard` and NOT in `border_soft`.

## Part A — titles (region enter/exit events; never test polygons in Skript)
Country = membership: in `<z>` → that zone's name.
- enter `<z>`: title "Entering <Zone>".
- exit `<z>` while still inside a neighbour zone: title "Entering <Neighbour>".
- exit `<z>` outward (crossed the actual line): subtitle "You left <Zone>". Build still allowed.
- cross the shared line (enter one zone from the other): the two enter/exit events above already fire
  the correct country title — the lime wall is purely visual.

## Part B — kill-zone (in `border_hard` and NOT in `border_soft`, i.e. the soft→hard band)
Escalating, configurable. Default = **double every minute**; expose the interval (e.g. every 10 s for
a harsher server) and the curve.
- on exit `border_soft` while still in `border_hard`: entered the kill-zone. Store `{kz_start::%uuid%}` = now,
  `{kz_tick::%uuid%}` = 0.
- repeating task every 1 s, for each player currently in the kill-zone:
  - `t` = whole **intervals** elapsed since `{kz_start}` (interval default 60 s, config `kz-interval`).
  - if `t` > `{kz_tick}`: set `{kz_tick}` = `t`, deal `kz-base * 2^(t-1)` hearts (config `kz-base` = 1),
    actionbar "Outside <Zone>: %damage% hearts — get back". Minute/interval 1,2,3,4,5 → 1,2,4,8,16 hearts;
    a full player dies in the 5th interval.
- on enter `border_soft` (stepped back inside soft), or exit `border_hard`, or death: clear `{kz_start}` + `{kz_tick}`.

## Part C — hard wall (on exit `border_hard`)
- set the player's velocity strongly back toward inside, deal a few hearts, send "You went too far".
- backstop: track each player's **last location inside `border_hard`** on the same per-second task;
  if they end up clearly outside (or in void), teleport them to that stored location.
- do **not** put `exit: deny` on `border_hard` — Skript owns the fling so it can message + damage.

## Part D — packet particles (the coloured walls)
Per-player, near-segment only — a country wall is hundreds of km, world particles would melt the server.
- every 5–10 ticks, per online player: find border segments within a render radius (~48–64 blocks).
  **Bucket segments into a coarse grid (per chunk / per 64-block cell)**; only check the player's cell +
  neighbours. Never scan all points every tick.
- draw a vertical dust wall along each near segment, a few blocks below feet to above head, stepped
  0.5–1 block, sent only to that player.
- colours (match the Meld preview): `<z>` actual = **CYAN**, `border_soft` = **ORANGE**,
  `border_hard` = **YELLOW**, shared line = faint **LIME** (sparser, lower).
- config: render radius, vertical extent, step spacing, update interval, per-packet particle cap.
- providers: skript-particle preferred, SkBee fallback.

## Part E — trim (done in Meld, noted for completeness)
Meld's **Trim world to ring** restricts generation to cells touching `border_hard` (cell granularity,
~2048 blk @1:10). Void starts ~1 cell past the wall, so the fling-back catches the player before the
void. Chunk-exact (16 blk) trim is a future Meld add (offline MCA pass).

## Plugin compatibility (server, before anything)
Confirm for the server's exact MC version (recent Leaf builds often lag): WorldGuard, WorldEdit,
Skript, a region-event addon (skript-worldguard or similar), and a packet-particle provider
(skript-particle / SkBee). If any is missing for the version — stop and report.

## Config surface (expose in the .sk)
`kz-interval` (s, default 60), `kz-base` (hearts, default 1), `kz-curve` (double | linear),
`wall-knockback`, `render-radius`, `wall-height`, `step`, `update-ticks`, `particle-cap`.

## Acceptance checklist
- [ ] Plugins confirmed for the MC version incl. a packet-particle provider.
- [ ] Point files load; a known coastal point lines up in-game (OFFSET already baked by Meld).
- [ ] Build works inside the country and out to the SOFT ring; soft→hard band is no-build + kill-zone; denied past the hard wall.
- [ ] Entering/leaving each zone shows the right titles; build right up to the shared line both sides.
- [ ] Crossing the actual line shows "left <Zone>"; the 0→soft band is safe.
- [ ] In the soft→hard band, damage lands 1,2,4,8,16 on successive intervals; resets inside soft.
- [ ] Crossing the hard line flings the player back with a message; teleport backstop catches void.
- [ ] Cyan/orange/yellow/lime walls all packet-based, only near the player, spatially bucketed.
- [ ] World trimmed to the hard ring (Meld), seam within ~1 cell.

## Do not
- No polygon containment in Skript (use region events).
- No world-spawned particles for the walls.
- No `exit: deny` on `border_hard` (blocks the fling + damage).
- Do not trim at the coast — both outer bands need terrain; trim at the hard ring (Meld does this).

---
**v2 build note (when approved):** Meld can *generate* this `.sk` from the zone names + config (a
templated `border.sk` written next to `regions.yml`), so the only server step is install plugins +
`/sk reload` + `/rg load`. Geometry, regions, particles-source-data, and trim are all Meld-produced;
only the live runtime is server-side.
