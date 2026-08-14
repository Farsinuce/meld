"""Planning a .pbf bake before it eats the machine.

The report this comes from: a user rendering one US state dropped eight continent extracts in a
folder (75 GB) and started a bake with four workers. Meld read every file regardless of where the
region was, held a node-location index per worker, exhausted 68 GB of RAM and drove Windows into
a 190 GB pagefile. Nothing warned, because nothing had measured.

Every constant these tests rely on was measured on a real bake, not assumed:
  RAM   2.19 GB per GB of .pbf, per worker (pyosmium 4.3.1 over ukraine-latest, 871 MB -> 1910 MB)
  disk  29x expansion .pbf -> baked JSON (2.03 GB of .pbf -> 59.6 GB of tiles)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import osm_pack as op  # noqa: E402

NH = {"south": 42.7, "west": -72.6, "north": 45.3, "east": -70.6}
EUROPE = {"south": 34, "west": -25, "north": 72, "east": 45}
N_AMERICA = {"south": 5, "west": -170, "north": 84, "east": -52}


def _f(name, gb, bbox):
    return {"name": name, "path": f"C:/pbf/{name}", "size_bytes": int(gb * 1e9), "bbox": bbox}


CONTINENTS = [_f("europe-latest.osm.pbf", 33.1, EUROPE),
              _f("north-america-latest.osm.pbf", 19.3, N_AMERICA)]


# ── which files even get read ─────────────────────────────────────────────────────────────────

def test_files_outside_the_region_are_skipped():
    """The scan already knows every header bbox; the bake used to ignore it and read all of them."""
    need, skip = op.select_pbfs(CONTINENTS, NH)
    assert [f["name"] for f in need] == ["north-america-latest.osm.pbf"]
    assert [f["name"] for f in skip] == ["europe-latest.osm.pbf"]


def test_a_file_with_no_readable_bbox_is_kept():
    """Excluding data because a header would not parse would render a world with holes in it, and
    the hole would be silent. Unknown means keep."""
    need, skip = op.select_pbfs([_f("mystery.osm.pbf", 1.0, None)], NH)
    assert len(need) == 1 and not skip


def test_no_region_means_no_filtering():
    need, skip = op.select_pbfs(CONTINENTS, None)
    assert len(need) == 2 and not skip


def test_touching_edges_still_overlap():
    """A way can sit exactly on the boundary, so a shared edge counts as overlap."""
    a = {"south": 0, "west": 0, "north": 10, "east": 10}
    b = {"south": 10, "west": 10, "north": 20, "east": 20}
    assert op._bbox_overlaps(a, b) is True


# ── the refusal that matters ──────────────────────────────────────────────────────────────────

def test_a_bake_that_cannot_fit_in_memory_is_refused(monkeypatch):
    """The exact reported failure. 19 GB of .pbf needs ~42 GB; with 20 GB free it must refuse
    rather than start and swap."""
    monkeypatch.setattr(op, "RAM_GB_PER_PBF_GB", 2.2)

    class VM:
        available = int(20e9)
    monkeypatch.setattr(op, "psutil", type("m", (), {"virtual_memory": staticmethod(lambda: VM)}),
                        raising=False)
    import psutil
    monkeypatch.setattr(psutil, "virtual_memory", lambda: VM)

    p = op.plan_bake(CONTINENTS, list(range(24)), region_bbox=NH)
    assert p["fits"] is False
    assert "memory" in p["reason"]


def test_the_refusal_names_the_actual_fix(monkeypatch):
    """"Not enough memory" alone leaves the user stuck. The way out is a smaller extract, and the
    message has to say so - it is the difference between 19 GB and 0.07 GB for the same output."""
    import psutil

    class VM:
        available = int(20e9)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: VM)
    p = op.plan_bake(CONTINENTS, list(range(24)), region_bbox=NH)
    assert "country or state extract" in p["reason"]


def test_the_right_extract_fits_easily(monkeypatch):
    """0.07 GB -> ~0.2 GB of RAM. Same rendered result as the 19 GB continent file."""
    import psutil

    class VM:
        available = int(20e9)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: VM)
    p = op.plan_bake([_f("new-hampshire-latest.osm.pbf", 0.07, NH)], list(range(24)),
                     region_bbox=NH)
    assert p["fits"] is True and p["ram_peak_gb"] < 1


# ── worker count ──────────────────────────────────────────────────────────────────────────────

def test_workers_are_fitted_to_free_memory(monkeypatch):
    """Four workers on big files is what turned a slow bake into a 190 GB pagefile."""
    import psutil

    class VM:
        available = int(64e9)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: VM)
    files = [_f(f"c{i}.osm.pbf", 5.0, NH) for i in range(8)]
    p = op.plan_bake(files, list(range(24)), workers_requested=8, region_bbox=NH)
    # 0.6 * 64 GB budget / (5 GB * 2.2) = 3
    assert 1 <= p["workers"] <= 4
    assert p["workers"] < 8, "a requested count must not override what memory allows"


def test_at_least_one_worker_is_always_planned(monkeypatch):
    import psutil

    class VM:
        available = int(2e9)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: VM)
    p = op.plan_bake([_f("tiny.osm.pbf", 0.01, NH)], [1], region_bbox=NH)
    assert p["workers"] >= 1


# ── the estimate the user asked for ───────────────────────────────────────────────────────────

def test_disk_estimate_uses_the_measured_expansion():
    p = op.plan_bake([_f("x.osm.pbf", 1.0, NH)], list(range(10)), region_bbox=NH)
    assert 25 <= p["disk_final_gb"] <= 33, "1 GB of .pbf measured at ~29 GB of baked JSON"


def test_peak_disk_exceeds_the_final_size():
    """The parallel bake writes a temp tree per .pbf and then merges, so both exist at once."""
    p = op.plan_bake([_f("x.osm.pbf", 1.0, NH)], list(range(10)), region_bbox=NH)
    assert p["disk_peak_gb"] > p["disk_final_gb"]


def test_a_bake_that_would_fill_the_disk_is_refused(monkeypatch):
    import psutil

    class VM:
        available = int(64e9)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: VM)
    monkeypatch.setattr(op.shutil, "disk_usage",
                        lambda p: type("d", (), {"free": int(5e9)}), raising=False)
    p = op.plan_bake([_f("x.osm.pbf", 1.0, NH)], list(range(10)), region_bbox=NH,
                     cache_dir=Path("."))
    assert p["fits"] is False and "disk" in p["reason"]


# ── the live correction ───────────────────────────────────────────────────────────────────────

def test_projection_replaces_the_estimate_once_real_tiles_exist():
    """Tile sizes span 88x between sparse and dense (measured median 5.5 MB, max 1089 MB), so a
    constant is only ever a starting point."""
    r = op.project_from_progress(done_tiles=40, bytes_written=40 * 5_500_000, total_tiles=100)
    assert r["per_tile_mb"] == 5.5
    assert r["projected_final_gb"] == 0.6


def test_no_projection_from_too_few_tiles():
    """Two tiles say nothing about a thousand; quoting a number from them would be worse than
    quoting the range."""
    assert op.project_from_progress(2, 10_000_000, 500) == {}
