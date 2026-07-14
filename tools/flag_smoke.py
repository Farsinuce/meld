#!/usr/bin/env python3
"""Flag-contract smoke test.

light-meld shells out to arnis.exe by exact flag name, so if the bundled binary
ever stops accepting a flag light-meld emits (e.g. after adopting an upstream
change that renamed/removed one), every cell run would fail at runtime. This test
catches that at build time: it builds the REAL command via build_arnis_cmd (so it
stays in sync with the emitter) plus the batch/prefetch flags, then runs the
binary with --dump-loot-table appended. That flag makes arnis parse + validate all
args and exit 0 BEFORE any generation, so a non-zero exit means the binary rejected
a flag we emit.

Run:  python tools/flag_smoke.py
Exit: 0 = every emitted flag accepted; 1 = a flag was rejected (prints which).
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.arnis_cmd import build_arnis_cmd  # noqa: E402

EXE = ROOT / ("arnis.exe" if sys.platform == "win32" else "arnis")

# A deliberately maximal, internally-consistent settings blob so build_arnis_cmd
# emits as many flags as possible in one go. aws_only is left OFF so it does not
# conflict with regional (validate_args rejects both together).
MAX_SETTINGS = {
    "scale": 1.0, "ground_level": -62, "rotation": 0, "terrain": True,
    "snow_mode": "both", "snow_percent": 50, "snow_y": 80,
    "roof": True, "interior": True, "land_cover": True, "buildings": True,
    "fill_ground": True, "caves": True,
    "cave_biome_amounts": {}, "disable_height_limit": True, "bake_lighting": True,
    "timeout": 60, "aws_only_elevation": False, "regional_elevation_only": True,
    "road_detail": "clean", "overpass_url": ["https://overpass.example/api"],
    "generate_3d_models": False, "vertical_exaggeration": 1.2,
    "trees": True, "tree_size_weights": {"small": 100, "medium": 100, "big": 50,
                                         "tall": 100, "giant": 200},
    "stream_to_disk": True,
}

# Batch / prefetch flags that come from prefetch.py + the CLI contract, not
# build_arnis_cmd. Kept internally consistent so only clap-parse + validate run.
BATCH_FLAGS = [
    "--download-terrain-only",
    "--regional-elevation-only",
    "--overpass-url", "https://overpass.example/api",
    "--osm-tile-z", "11",
    "--prewarm-overture",
]


def run(label: str, argv: list[str]) -> bool:
    tmp = Path(tempfile.gettempdir()) / "meld_flag_smoke_loot.json"
    full = argv + ["--dump-loot-table", str(tmp)]
    r = subprocess.run(full, capture_output=True, text=True)
    if r.returncode == 0:
        print(f"  ok   {label}  ({len(argv)} flags)")
        return True
    print(f"  FAIL {label}  exit={r.returncode}")
    err = (r.stderr or r.stdout or "").strip().splitlines()
    for line in err[-6:]:
        print("       " + line)
    print("       full argv: " + " ".join(full))
    return False


def main() -> int:
    if not EXE.exists():
        print(f"arnis binary not found at {EXE}; build + deploy it first")
        return 1
    print(f"flag-contract smoke test against {EXE.name}")

    bbox = {"south": 0.0, "west": 0.0, "north": 0.001, "east": 0.001}
    origin = {"lat": 0.0005, "lon": 0.0005}
    elevation = {"min_m": 0, "max_m": 100}

    gen = build_arnis_cmd(str(EXE), bbox, tempfile.gettempdir(),
                          MAX_SETTINGS, origin, elevation, seed=12345,
                          osm_file=None, loot_table=str(EXE))  # loot_table just proves --loot-table parses

    batch = [str(EXE), "--bbox", "0,0,0.001,0.001"] + BATCH_FLAGS

    ok = True
    ok &= run("generation flags (build_arnis_cmd)", gen)
    ok &= run("batch/prefetch flags", batch)
    print("PASS: every emitted flag accepted" if ok else "FAIL: a flag was rejected")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
