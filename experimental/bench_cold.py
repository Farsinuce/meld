#!/usr/bin/env python3
"""
COLD benchmark: end-to-end time INCLUDING the network, so we count the download too.

Real world: Arnis downloads OSM (Overpass) and terrain (AWS) WHILE it runs, every run. Meld
downloads once up front (the prefetch) and then builds from the local cache, fully offline, so a
re-run of the same area pays no network at all. This test starts from an EMPTY cache (its own temp
folder, the real cache is untouched) so the download actually happens.

  arnis cold : one arnis run, no pre-cache  -> it fetches OSM + terrain itself, then builds.
  meld cold  : timed prefetch (OSM grid + terrain) + the 8x4 parallel build from that cache.

Config: 8 workers x 4 threads, buildings off, sizes 256 and 1024 regions, Bucharest 1:1.
Results -> experimental/bench_cold.json ; log -> experimental/bench_cold.log
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

MELD = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MELD))
os.chdir(MELD)

from src.coords import cell_bbox, expand_bbox_for_seam            # noqa: E402
from src.arnis_cmd import build_arnis_cmd, find_world_dir         # noqa: E402
from src.merge import merge_cell_into_master                      # noqa: E402
from src.prefetch import run_prefetch, run_terrain_prefetch       # noqa: E402
from src.survey import survey_elevation                           # noqa: E402

ORIGIN = {"lat": 44.4268, "lon": 26.1025}
SCALE = 1.0
SEED = 1337
CELL = 4
W, T = 8, 4
SIZES = [16, 32]
EXE = str(MELD / "arnis.exe")
OUT = MELD / "experimental" / "_cold"
TMPA = OUT / "cacheA"        # cold cache for the Arnis side
TMPM = OUT / "cacheM"        # cold cache for the Meld side
LOGF = MELD / "experimental" / "bench_cold.log"
RESF = MELD / "experimental" / "bench_cold.json"

SETTINGS = {
    "scale": SCALE, "ground_level": -62, "rotation": 0, "terrain": True, "roof": True,
    "interior": False, "land_cover": True, "buildings": False, "fill_ground": True,
    "bake_lighting": True, "seam_buffer_chunks": 8, "elevation_mode": "global",
    "tile_invariant_rendering": True, "elevation_zoom": 13,
}


def log(m):
    line = f"[{time.strftime('%H:%M:%S')}] {m}"
    print(line, flush=True)
    with open(LOGF, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def arnis_cold(size, full, elev):
    out = str(OUT / f"arnis_{size}")
    shutil.rmtree(out, ignore_errors=True)
    shutil.rmtree(TMPA, ignore_errors=True)
    TMPA.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["ARNIS_CACHE_ROOT"] = str(TMPA)        # empty -> terrain fetched from AWS during the run
    env["ARNIS_ELEV_ZOOM"] = "13"
    env["ARNIS_STREAM_TO_DISK"] = "1"
    env.pop("RAYON_NUM_THREADS", None)
    cmd = build_arnis_cmd(EXE, full, out, SETTINGS, ORIGIN, elev, SEED, osm_file=None)  # no tile dir -> Overpass
    t0 = time.time()
    p = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    sec = round(time.time() - t0, 1)
    ok = p.returncode == 0 and bool(find_world_dir(out))
    if not ok:
        log("    arnis cold stderr tail: " + (p.stderr or "")[-400:])
    shutil.rmtree(out, ignore_errors=True)
    return sec, ok


def meld_cold(size, cks, elev):
    shutil.rmtree(TMPM, ignore_errors=True)
    TMPM.mkdir(parents=True, exist_ok=True)
    osm_dir = str((TMPM / "osm").resolve())
    full = cell_bbox(0, 0, size, ORIGIN["lat"], ORIGIN["lon"], SCALE)
    # prefetch (download) into the empty temp cache, timed
    os.environ["ARNIS_CACHE_ROOT"] = str(TMPM)
    pf_cells = [{"cell_key": ck, "bbox": cell_bbox(int(ck.split(',')[0]), int(ck.split(',')[1]),
                 CELL, ORIGIN["lat"], ORIGIN["lon"], SCALE)} for ck in cks]
    t0 = time.time()
    try:
        run_prefetch(pf_cells, ORIGIN, SETTINGS, EXE, str(TMPM), log, lambda c: None)
        run_terrain_prefetch([full], EXE, log)
    except Exception as ex:
        log(f"    meld prefetch warning: {ex}")
    pf = round(time.time() - t0, 1)
    # build, timed, reading the just-downloaded cache (offline)
    master = OUT / f"meld_{size}"
    shutil.rmtree(master, ignore_errors=True)
    master.mkdir(parents=True, exist_ok=True)
    seam = int(SETTINGS["seam_buffer_chunks"])
    osm = osm_dir if os.path.isdir(osm_dir) else None

    def gen_merge(ck):
        rx, rz, sz = (int(v) for v in ck.split(","))
        bb = expand_bbox_for_seam(cell_bbox(rx, rz, sz, ORIGIN["lat"], ORIGIN["lon"], SCALE), seam, ORIGIN, SCALE)
        work = OUT / "_cells" / f"{size}_{ck.replace(',', '_')}"
        env = dict(os.environ)
        env["ARNIS_CACHE_ROOT"] = str(TMPM)
        env["ARNIS_ELEV_ZOOM"] = "13"
        env["RAYON_NUM_THREADS"] = str(T)
        try:
            c = build_arnis_cmd(EXE, bb, str(work), SETTINGS, ORIGIN, elev, SEED, osm_file=osm)
            pp = subprocess.run(c, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if pp.returncode != 0:
                return ck, "gen-fail"
            wd = find_world_dir(str(work))
            if not wd:
                return ck, "no-world"
            merge_cell_into_master(wd, str(master), ck, seam_buffer_chunks=seam,
                                   world_name="Bench", overwrite_collisions=True)
            return ck, "ok"
        except Exception as ex:  # noqa: BLE001
            return ck, f"err:{ex}"
        finally:
            shutil.rmtree(work, ignore_errors=True)

    failed = []
    t1 = time.time()
    with ThreadPoolExecutor(max_workers=W) as ex:
        for fut in as_completed({ex.submit(gen_merge, ck): ck for ck in cks}):
            ck, st = fut.result()
            if st != "ok":
                failed.append((ck, st))
    gen = round(time.time() - t1, 1)
    shutil.rmtree(master, ignore_errors=True)
    return pf, gen, (not failed), failed


def main():
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True, exist_ok=True)
    LOGF.write_text("", encoding="utf-8")
    res = {"place": "Bucharest", "scale": SCALE, "buildings": False, "workers": W, "threads": T,
           "note": "COLD: empty cache, download counted. Arnis fetches during the run; Meld prefetches once then builds offline.",
           "cores": os.cpu_count() or 4, "runs": []}
    RESF.write_text(json.dumps(res, indent=2), encoding="utf-8")

    sv = survey_elevation(cell_bbox(0, 0, max(SIZES), ORIGIN["lat"], ORIGIN["lon"], SCALE), zoom=10)
    elev = {"min_m": sv.get("min_m"), "max_m": sv.get("max_m"), "seed": SEED, "zoom": 13}
    log(f"elevation min={elev['min_m']} max={elev['max_m']}")

    for size in SIZES:
        regions = size * size
        ncw = size // CELL
        cks = [f"{i},{j},{CELL}" for i in range(ncw) for j in range(ncw)]
        full = cell_bbox(0, 0, size, ORIGIN["lat"], ORIGIN["lon"], SCALE)
        log(f"=== SIZE {size}x{size} = {regions} regions (COLD, network counted) ===")
        a_sec, a_ok = arnis_cold(size, full, elev)
        log(f"  ARNIS COLD (fetch + build): {a_sec}s ({a_sec/60:.1f} min) ok={a_ok}")
        pf, gen, m_ok, failed = meld_cold(size, cks, elev)
        m_total = round(pf + gen, 1)
        sp = round(a_sec / m_total, 2) if m_total else None
        log(f"  MELD COLD: prefetch {pf}s + build {gen}s = {m_total}s ({m_total/60:.1f} min) ok={m_ok} speedup={sp}x")
        res["runs"].append({"size": size, "regions": regions, "cells": len(cks),
                            "arnis_cold_s": a_sec, "arnis_ok": a_ok,
                            "meld_prefetch_s": pf, "meld_build_s": gen, "meld_cold_s": m_total,
                            "meld_ok": m_ok, "meld_failed": failed, "speedup_cold": sp})
        RESF.write_text(json.dumps(res, indent=2), encoding="utf-8")
    log("COLD DONE -> experimental/bench_cold.json")


if __name__ == "__main__":
    main()
