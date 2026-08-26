#!/bin/bash
# Contention curve: the same cell, run N-way concurrent, measuring per-cell CPU and wall.
# cpu_s is the discriminator - a core stalled on memory still burns CPU time, so
# memory/cache contention inflates cpu_s; waiting on disk or a lock does not.
ARNIS="c:/Users/LEGION/Documents/Meld/arnis-triagefix/target/release/arnis.exe"
TAIL="--scale 1.0 --ground-level=-56 --rotation 0 --region-format blinear --blinear-level 6 --osm-tile-dir C:/tmp/meld-ab-data/cache/osm --osm-tile-z 11 --master-origin-lat 44.429752066115704 --master-origin-lng 26.08477631813682 --seed 1 --terrain --snow-mode peaks --snow-percent 6.0 --roof true --interior false --land-cover true --no-buildings --fillground --water-carve-clearance max --bake-lighting --timeout 600 --road-detail clean --no-3d --gamemode creative --world-time 6000 --tree-pack c:/Users/LEGION/Documents/Meld/meld-triagefix/tree-packs/eur --rocks --bushes --grass-texture --grass-mix coarse=6,plains=64,flower=22,moss=8 --land-texture --land-mix coarse=15,plains=40,flower=10,farm=25,moss=10"
BBOX="44.428602227811716,26.16045437288697,44.44929931728351,26.1894374576849"
OUT="$(pwd -W)/phase/cont"
for N in "$@"; do
  rm -rf "phase/cont/n$N"; mkdir -p "phase/cont/n$N"
  for i in $(seq 1 $N); do
    ARNIS_PHASE_MARKERS=1 ARNIS_FILL_BUDGET=1 RAYON_NUM_THREADS=2 ARNIS_FLUSH_THREADS=2 \
      "$ARNIS" --bbox $BBOX --output-dir "$OUT/n$N/c$i" $TAIL --benchmark \
      --canonical-regions=12,15,-4,-1 > "phase/cont/n$N/c$i.txt" 2>&1 &
  done
  wait
  echo "done N=$N"
done
