"""Native B_Linear generation: the pipeline pieces that had to learn a second container.

The fork can write Leaf's `r.X.Z.b_linear` directly instead of Anvil `r.X.Z.mca`. Nothing
in Meld reads region bytes, so the changes are all about recognising the other file name —
and about refusing the one state that would produce a world no server can open: a master
holding both containers at once.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import arnis_cmd, finalcheck, merge  # noqa: E402

# A B_Linear v3 file with no chunks: 14-byte header + a 16-entry all-zero offset table.
EMPTY_BLINEAR = b"\xff\xff\xdf\xf7\xed\xda\xfd\x97" + b"\x03" + b"\x06" + b"\x00\x00\x07\x21" + bytes(128)


def write_region(world: Path, name: str, size: int = 4096) -> Path:
    region_dir = world / "region"
    region_dir.mkdir(parents=True, exist_ok=True)
    path = region_dir / name
    path.write_bytes(b"\x00" * size)
    return path


class TestWorldContainer:
    def test_detects_each_container(self, tmp_path):
        anvil, blinear = tmp_path / "a", tmp_path / "b"
        write_region(anvil, "r.0.0.mca")
        write_region(blinear, "r.0.0.b_linear")
        assert merge.world_container(anvil) == "mca"
        assert merge.world_container(blinear) == "b_linear"

    def test_empty_and_missing_worlds_have_no_container(self, tmp_path):
        empty = tmp_path / "empty"
        (empty / "region").mkdir(parents=True)
        assert merge.world_container(empty) is None
        assert merge.world_container(tmp_path / "nope") is None

    def test_both_containers_present_is_mixed(self, tmp_path):
        world = tmp_path / "w"
        write_region(world, "r.0.0.mca")
        write_region(world, "r.0.1.b_linear")
        assert merge.world_container(world) == "mixed"


class TestMergeRefusesMismatch:
    """A cell in the other container must never land in the master: the file names differ,
    so it would not even register as a collision — it would just quietly produce a world
    that is half unreadable."""

    # "rx,rz,size" — the grid cell at origin, four regions on a side.
    CELL_KEY = "0,0,4"

    def _merge(self, cell, master):
        return merge.merge_cell_into_master(str(cell), str(master), self.CELL_KEY)

    def test_blinear_cell_into_anvil_master_is_refused(self, tmp_path):
        cell, master = tmp_path / "cell", tmp_path / "master"
        write_region(cell, "r.0.0.b_linear")
        write_region(master, "r.5.5.mca")
        with pytest.raises(merge.MeldCollisionError, match="b_linear"):
            self._merge(cell, master)

    def test_anvil_cell_into_blinear_master_is_refused(self, tmp_path):
        cell, master = tmp_path / "cell", tmp_path / "master"
        write_region(cell, "r.0.0.mca")
        write_region(master, "r.5.5.b_linear")
        with pytest.raises(merge.MeldCollisionError, match="mca"):
            self._merge(cell, master)

    def test_mixed_master_is_refused(self, tmp_path):
        cell, master = tmp_path / "cell", tmp_path / "master"
        write_region(cell, "r.0.0.b_linear")
        write_region(master, "r.5.5.b_linear")
        write_region(master, "r.6.6.mca")
        with pytest.raises(merge.MeldCollisionError, match="both"):
            self._merge(cell, master)

    def test_empty_master_accepts_either_container(self, tmp_path):
        # First cell of a run merges into a master that has no regions yet.
        for name in ("r.0.0.mca", "r.0.0.b_linear"):
            cell, master = tmp_path / f"cell-{name}", tmp_path / f"master-{name}"
            write_region(cell, name)
            (master / "region").mkdir(parents=True)
            assert merge.world_container(master) is None
            assert merge.world_container(cell) == name.split(".")[-1]


class TestFinalcheckScan:
    def test_blinear_regions_count_as_present(self, tmp_path):
        world = tmp_path / "w"
        write_region(world, "r.0.0.b_linear", size=5000)
        write_region(world, "r.1.0.mca", size=20000)
        present, empty = finalcheck._scan_present(world / "region")
        assert present == {(0, 0), (1, 0)}
        assert empty == set()

    def test_chunkless_blinear_counts_as_empty(self, tmp_path):
        world = tmp_path / "w"
        region_dir = world / "region"
        region_dir.mkdir(parents=True)
        (region_dir / "r.2.3.b_linear").write_bytes(EMPTY_BLINEAR)
        present, empty = finalcheck._scan_present(region_dir)
        assert present == {(2, 3)}
        assert empty == {(2, 3)}, "a 142-byte b_linear holds no chunks — it is a hole to retry"

    def test_one_byte_of_bucket_data_is_not_empty(self, tmp_path):
        world = tmp_path / "w"
        region_dir = world / "region"
        region_dir.mkdir(parents=True)
        (region_dir / "r.2.3.b_linear").write_bytes(EMPTY_BLINEAR + b"\x00")
        present, empty = finalcheck._scan_present(region_dir)
        assert present == {(2, 3)}
        assert empty == set()


class TestArnisCommand:
    def _cmd(self, settings):
        base = {"ground_level": -62, "rotation": 0, "terrain": False, "scale": 1.0}
        base.update(settings)
        return arnis_cmd.build_arnis_cmd(
            arnis_exe="arnis.exe",
            bbox={"south": 52.0, "west": 5.0, "north": 52.01, "east": 5.01},
            output_path="out",
            settings=base,
            origin={},
            elevation=None,
            seed=1,
        )

    def test_flag_absent_by_default(self):
        assert "--region-format" not in self._cmd({})

    def test_flag_passed_when_native_blinear_is_on(self):
        cmd = self._cmd({"native_region_format": "blinear", "native_blinear_level": 9})
        assert cmd[cmd.index("--region-format") + 1] == "blinear"
        assert cmd[cmd.index("--blinear-level") + 1] == "9"

    def test_explicit_mca_stays_off(self):
        assert "--region-format" not in self._cmd({"native_region_format": "mca"})
