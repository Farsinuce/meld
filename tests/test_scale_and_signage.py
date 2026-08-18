"""The scale envelope, and the signage flag Meld must never let default to on.

Two separate consequences of the generator moving to 3.1.0.

Scale: the fork now rejects `--scale` outside [0.01, 4.0] at the clap parser. Before, an
absurd value reached the fetch stage and produced a hung or empty cell; now it fails the
cell outright with a usage error. Meld builds that argument on every invocation, and a
country render is thousands of invocations, so the value has to be pulled into range
before it is stored rather than discovered per cell.

Signage: upstream 3.1.0 added `--signage`, defaulting to `basic`, i.e. ON. It draws street
plates and billboards as map items, and the map payloads live in the world's `data/`
directory. merge.py copies region/, poi/, entities/, datapacks/ and level.dat - never
data/ - so every cell's maps are dropped at merge and the item frames in the master world
would reference map ids that do not exist. Meld therefore states `none` explicitly instead
of inheriting a default it does not control.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import arnis_cmd  # noqa: E402
from src import project as project_mod  # noqa: E402

BBOX = {"south": 44.0, "west": 26.0, "north": 44.1, "east": 26.1}
ORIGIN = {"lat": 44.0, "lng": 26.0}


@pytest.fixture(autouse=True)
def _clear_help_cache():
    arnis_cmd._HELP_CACHE.clear()
    yield
    arnis_cmd._HELP_CACHE.clear()


def _cmd(monkeypatch, settings, *, supports=()):
    monkeypatch.setattr(arnis_cmd, "arnis_supports", lambda exe, flag: flag in supports)
    return arnis_cmd.build_arnis_cmd("arnis.exe", BBOX, "out", settings, ORIGIN, None, 1)


def _scale_of(cmd):
    return float(cmd[cmd.index("--scale") + 1])


# --- scale ------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 4.0])
def test_in_range_scales_pass_through_untouched(value):
    assert arnis_cmd.clamp_scale(value) == value


def test_the_floor_is_melds_not_upstreams():
    """Upstream arnis floors at 0.05; the fork widened it to 0.01 for planet renders.

    If this ever regresses to 0.05, Meld's 1:100 projects stop working, so pin it.
    """
    assert arnis_cmd.MIN_SCALE == 0.01
    assert arnis_cmd.clamp_scale(0.01) == 0.01


@pytest.mark.parametrize(
    "value,expected",
    [
        (0.001, arnis_cmd.MIN_SCALE),
        (0.0, arnis_cmd.MIN_SCALE),
        (-3.0, arnis_cmd.MIN_SCALE),
        (10.0, arnis_cmd.MAX_SCALE),
        (4.001, arnis_cmd.MAX_SCALE),
    ],
)
def test_out_of_range_scales_are_pulled_back(value, expected):
    assert arnis_cmd.clamp_scale(value) == expected


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_scales_fall_back_rather_than_propagate(value):
    """NaN fails every comparison, so a naive min/max clamp would pass it straight through
    and the generator would reject the cell. Fall back to a usable value instead."""
    assert arnis_cmd.clamp_scale(value) == 1.0


@pytest.mark.parametrize("value", ["", None, "abc", {}])
def test_unparseable_scales_fall_back(value):
    assert arnis_cmd.clamp_scale(value) == 1.0


def test_the_emitted_command_never_carries_an_out_of_range_scale(monkeypatch):
    """The real guarantee: whatever is in the project file, the spawned process gets a
    scale the generator will accept."""
    for stored in [0.0001, 0.0, -1.0, 99.0, float("nan")]:
        emitted = _scale_of(_cmd(monkeypatch, {"scale": stored}))
        assert arnis_cmd.MIN_SCALE <= emitted <= arnis_cmd.MAX_SCALE


def test_a_normal_scale_is_still_emitted_verbatim(monkeypatch):
    assert _scale_of(_cmd(monkeypatch, {"scale": 0.1})) == 0.1


# --- signage ----------------------------------------------------------------------------


def test_signage_defaults_to_none_when_the_generator_has_the_flag(monkeypatch):
    cmd = _cmd(monkeypatch, {}, supports=("--signage",))
    assert cmd[cmd.index("--signage") + 1] == "none"


def test_signage_is_not_passed_to_a_generator_that_lacks_it(monkeypatch):
    """The fork does not carry --signage today. Emitting it ungated would turn every cell
    into `error: unexpected argument '--signage' found`."""
    assert "--signage" not in _cmd(monkeypatch, {"signage": "none"})


def test_an_explicit_signage_choice_round_trips(monkeypatch):
    cmd = _cmd(monkeypatch, {"signage": "basic"}, supports=("--signage",))
    assert cmd[cmd.index("--signage") + 1] == "basic"


def test_the_project_default_is_off():
    """Not merely absent: absent would inherit upstream's `basic`, which is on."""
    assert project_mod.default_settings()["signage"] == "none"
