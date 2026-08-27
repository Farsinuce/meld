"""A7 — OSM sidecar lifecycle reaper (perf phase 5).

Arnis bakes a bincode `.osmbin` sidecar next to every grid tile `.json` (WS-A).
The Rust reader is fail-open (content-hash check on every read), so a stale
sidecar is never MIS-read — but a sidecar whose .json is pruned, TTL-expired or
cleaned away is never read at all and would sit on disk forever, roughly
doubling the OSM cache footprint. These tests pin the lifecycle contract in
osm_grid.py:

  * remove_tile deletes the .json and its paired .osmbin as one operation;
  * sweep_orphan_sidecars deletes any .osmbin with no .json sibling;
  * a sweep over a healthy cache is a strict no-op (valid pairs and bare
    .json tiles untouched, zero reported);
  * merge_tiles republishing a tile .json reaps the now-stale sidecar in the
    same operation.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import osm_grid  # noqa: E402


def _mk_pair(cache: Path, x: int, y: int, elements=None) -> tuple[Path, Path]:
    """A grid tile .json (real filename, valid Overpass shape) + its .osmbin."""
    j = cache / osm_grid.tile_filename(x, y)
    j.write_text(json.dumps({"version": 0.6, "elements": elements or []}),
                 encoding="utf-8")
    b = osm_grid.sidecar_path(j)
    b.write_bytes(b"OSMB\x01fake-bincode-payload")
    return j, b


# ---------------------------------------------------------------- pairing rule

def test_sidecar_path_is_suffix_swap_same_dir(tmp_path):
    j = tmp_path / osm_grid.tile_filename(1189, 739)
    b = osm_grid.sidecar_path(j)
    assert b.parent == j.parent
    assert b.name == j.name[: -len(".json")] + ".osmbin"


# ---------------------------------------------------------- reap with the json

def test_remove_tile_deletes_json_and_sidecar_together(tmp_path):
    j, b = _mk_pair(tmp_path, 1189, 739)
    osm_grid.remove_tile(j)
    assert not j.exists(), ".json must be gone"
    assert not b.exists(), "paired .osmbin must be reaped in the same operation"


def test_remove_tile_without_sidecar_is_quiet(tmp_path):
    j, b = _mk_pair(tmp_path, 1189, 739)
    b.unlink()
    osm_grid.remove_tile(j)          # must not raise
    assert not j.exists()


def test_remove_tile_on_missing_tile_is_quiet(tmp_path):
    j = tmp_path / osm_grid.tile_filename(5, 5)
    osm_grid.remove_tile(j)          # neither file exists — still a no-op, no raise
    assert not j.exists()


def test_reap_sidecar_reports_whether_one_existed(tmp_path):
    j, b = _mk_pair(tmp_path, 1189, 739)
    assert osm_grid.reap_sidecar(j) is True
    assert not b.exists()
    assert j.exists(), "reap_sidecar touches ONLY the sidecar"
    assert osm_grid.reap_sidecar(j) is False, "second reap finds nothing"


# ---------------------------------------------------------------- orphan sweep

def test_sweep_deletes_orphan_sidecars_only(tmp_path):
    # healthy pair
    j1, b1 = _mk_pair(tmp_path, 1189, 739)
    # orphan: .osmbin whose .json was deleted directly (the accumulation bug)
    j2, b2 = _mk_pair(tmp_path, 1190, 739)
    j2.unlink()
    # bare .json with no sidecar yet (not baked) — must be ignored
    j3 = tmp_path / osm_grid.tile_filename(1191, 739)
    j3.write_text('{"version":0.6,"elements":[]}', encoding="utf-8")

    assert osm_grid.sweep_orphan_sidecars(tmp_path) == 1
    assert not b2.exists(), "orphan .osmbin deleted"
    assert j1.exists() and b1.exists(), "valid pair untouched"
    assert j3.exists(), "bare .json untouched"


def test_sweep_on_healthy_cache_is_noop(tmp_path):
    pairs = [_mk_pair(tmp_path, x, 700) for x in (10, 11, 12)]
    before = sorted(p.name for p in tmp_path.iterdir())
    assert osm_grid.sweep_orphan_sidecars(tmp_path) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_sweep_missing_dir_returns_zero(tmp_path):
    assert osm_grid.sweep_orphan_sidecars(tmp_path / "nope") == 0


# ---------------------------------------- rewrite-in-place reaps (merge_tiles)

def test_merge_tiles_reaps_stale_sidecar_of_rewritten_tile(tmp_path):
    src1, _ = _mk_pair(tmp_path, 1, 1, elements=[{"type": "node", "id": 7}])
    src2, _ = _mk_pair(tmp_path, 2, 1, elements=[{"type": "node", "id": 7},
                                                 {"type": "way", "id": 8}])
    out, out_sc = _mk_pair(tmp_path, 3, 1)   # pre-existing tile + now-stale sidecar
    n = osm_grid.merge_tiles([src1, src2], out)
    assert n == 2, "dedup on (type,id): node 7 kept once + way 8"
    assert json.loads(out.read_text(encoding="utf-8"))["version"] == 0.6
    assert not out_sc.exists(), "republish must reap the stale .osmbin in the same op"
