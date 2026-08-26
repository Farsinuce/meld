"""B1: --canonical-regions must be emitted only when it is safe, and must name the
rectangle the cell actually owns.

The flag tells arnis not to write the seam-halo region ring. That is only correct when a
neighbouring cell will render the adjacent ground, i.e. for a tiled Meld run - never for a
standalone bbox render, and never against an arnis that does not know the flag (clap
rejects an unknown argument outright and the cell would simply fail).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.arnis_cmd import build_arnis_cmd
from src.coords import canonical_region_bounds

BBOX = {"south": 44.4286, "west": 26.1604, "north": 44.4493, "east": 26.1894}
ORIGIN = {"lat": 44.4297, "lon": 26.0848}


def _cmd(settings_extra: dict, **kw) -> list[str]:
    settings = {"scale": 1.0, "timeout": 600, **settings_extra}
    return build_arnis_cmd("arnis.exe", BBOX, "out", settings, ORIGIN, None, 1, **kw)


def _rect(cmd: list[str]) -> str | None:
    """The rectangle as arnis will receive it, whichever spelling is used."""
    for i, a in enumerate(cmd):
        if a.startswith("--canonical-regions="):
            return a.split("=", 1)[1]
        if a == "--canonical-regions":
            return cmd[i + 1]
    return None


def test_off_by_default() -> None:
    assert _rect(_cmd({}, cell_key="3,0,4")) is None


def test_emitted_with_the_cells_own_rectangle() -> None:
    # The rectangle was also derived empirically from a real render: the cell touched
    # regions 11..16 x -5..0 and owned 12..15 x -4..-1.
    assert canonical_region_bounds("3,0,4") == (12, 15, -4, -1)
    assert _rect(_cmd({"canonical_regions": True}, cell_key="3,0,4")) == "12,15,-4,-1"


def test_never_emitted_without_a_cell_key() -> None:
    # A bbox render owns no cell and has no neighbour to generate the ground its edge
    # would lose, so suppressing the ring there would delete real terrain.
    assert _rect(_cmd({"canonical_regions": True})) is None
    assert _rect(_cmd({"canonical_regions": True}, cell_key=None)) is None


def test_an_unparseable_cell_key_is_not_guessed() -> None:
    assert _rect(_cmd({"canonical_regions": True}, cell_key="not-a-cell")) is None


def test_the_rectangle_is_four_ints_arnis_can_parse() -> None:
    rect = _rect(_cmd({"canonical_regions": True}, cell_key="-2,5,8"))
    parts = rect.split(",")
    assert len(parts) == 4 and all(p.lstrip("-").isdigit() for p in parts)
    rx0, rx1, rz0, rz1 = (int(p) for p in parts)
    assert rx0 <= rx1 and rz0 <= rz1, "arnis rejects an inverted rectangle"


def test_the_rectangle_matches_the_cell_size() -> None:
    for size in (1, 4, 8):
        rx0, rx1, rz0, rz1 = canonical_region_bounds(f"0,0,{size}")
        assert (rx1 - rx0 + 1) == size and (rz1 - rz0 + 1) == size


def test_a_negative_rectangle_is_not_mistaken_for_a_flag() -> None:
    """The bug that failed 36 of 81 cells on the first real phase-2 run.

    A cell west or north of the origin owns a rectangle starting with a minus, and clap
    reads a bare "-4,-1,0,3" as an unknown argument: `error: unexpected argument '-4'`.
    The --flag=VALUE spelling is what makes it parse.
    """
    cmd = _cmd({"canonical_regions": True}, cell_key="-1,-1,4")
    rect = _rect(cmd)
    assert rect is not None and rect.startswith("-"), "this cell's rectangle is negative"
    assert f"--canonical-regions={rect}" in cmd, "must use the = form, not a separate argv"
    assert "--canonical-regions" not in cmd, "the bare flag would strand the value"
