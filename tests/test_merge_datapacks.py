"""
The master world must carry the datapacks its level.dat references.

Arnis installs its extended-height pack into each CELL world and registers
`file/arnis_tall` inside that cell's level.dat. Meld copies a cell's level.dat into the
master world — so if the pack files don't come with it, the master world asks for a
datapack that isn't there and Minecraft loads it at vanilla height, silently discarding
every block above y=319. These tests pin that pairing.

Run: python -m pytest light-meld/tests -q   (from the repo root)
"""

import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.merge import merge_cell_into_master


def _write_region(path: Path, rx: int, rz: int) -> None:
    """A minimal but structurally valid .mca: one chunk in sector 2, zlib-compressed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = zlib.compress(b"\x00")               # not real NBT; merge only copies bytes
    header = bytearray(8192)
    header[0:4] = struct.pack(">I", (2 << 8) | 1)  # offset 2 sectors, length 1 sector
    chunk = struct.pack(">IB", len(payload) + 1, 2) + payload
    chunk += b"\x00" * (4096 - (len(chunk) % 4096))
    path.write_bytes(bytes(header) + chunk)


def _cell_world(root: Path, cell_key: str, *, with_pack: bool) -> Path:
    """A cell world as arnis leaves it: regions, a level.dat, and (when extended height
    is on) a datapacks/ tree."""
    from src.coords import canonical_region_bounds

    world = root / f"cell-{cell_key.replace(',', '_')}"
    rx_min, rx_max, rz_min, rz_max = canonical_region_bounds(cell_key)
    for rx in range(rx_min, rx_max + 1):
        for rz in range(rz_min, rz_max + 1):
            _write_region(world / "region" / f"r.{rx}.{rz}.mca", rx, rz)
    (world / "level.dat").write_bytes(b"\x1f\x8b\x08\x00fake-level-dat")
    if with_pack:
        pack = world / "datapacks" / "arnis_tall"
        (pack / "data" / "minecraft" / "dimension_type").mkdir(parents=True)
        (pack / "pack.mcmeta").write_text('{"pack":{"pack_format":61}}', encoding="utf-8")
        (pack / "data" / "minecraft" / "dimension_type" / "overworld.json").write_text(
            '{"min_y":-2032,"height":4064}', encoding="utf-8")
    return world


def test_datapacks_travel_with_level_dat(tmp_path):
    master = tmp_path / "master"
    cell = _cell_world(tmp_path, "0,0,1", with_pack=True)

    res = merge_cell_into_master(str(cell), str(master), "0,0,1")

    assert (master / "level.dat").exists(), "level.dat must be copied"
    pack = master / "datapacks" / "arnis_tall"
    assert pack.is_dir(), "the pack level.dat references must exist in the master world"
    assert (pack / "pack.mcmeta").exists()
    assert (pack / "data" / "minecraft" / "dimension_type" / "overworld.json").exists(), \
        "the dimension_type that defines min_y/height must survive the merge"
    assert "arnis_tall" in res["datapacks"]


def test_datapacks_are_backfilled_when_level_dat_already_exists(tmp_path):
    """A world whose first merge predates this fix (or where the pack appeared later)
    must still end up with the pack, not stay broken."""
    master = tmp_path / "master"
    first = _cell_world(tmp_path, "0,0,1", with_pack=False)
    merge_cell_into_master(str(first), str(master), "0,0,1")
    assert not (master / "datapacks").exists()

    second = _cell_world(tmp_path, "1,0,1", with_pack=True)
    res = merge_cell_into_master(str(second), str(master), "1,0,1")

    assert (master / "datapacks" / "arnis_tall").is_dir(), "pack must be backfilled"
    assert "arnis_tall" in res["datapacks"]


def test_second_merge_does_not_duplicate_or_clobber(tmp_path):
    master = tmp_path / "master"
    a = _cell_world(tmp_path, "0,0,1", with_pack=True)
    merge_cell_into_master(str(a), str(master), "0,0,1")
    marker = master / "datapacks" / "arnis_tall" / "pack.mcmeta"
    marker.write_text("EDITED", encoding="utf-8")

    b = _cell_world(tmp_path, "1,0,1", with_pack=True)
    res = merge_cell_into_master(str(b), str(master), "1,0,1")

    assert marker.read_text(encoding="utf-8") == "EDITED", "existing pack must not be overwritten"
    assert res["datapacks"] == "already present"


def test_no_datapacks_is_not_an_error(tmp_path):
    """The common case — extended height off — must merge exactly as before."""
    master = tmp_path / "master"
    cell = _cell_world(tmp_path, "0,0,1", with_pack=False)

    res = merge_cell_into_master(str(cell), str(master), "0,0,1")

    assert res["datapacks"] == "skipped"
    assert not (master / "datapacks").exists()
    assert (master / "level.dat").exists()


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
