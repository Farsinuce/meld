"""The Additional Buildings toggle, and the capability gate that keeps it safe.

Overture footprints come from satellite imagery, so a few land where no building exists. Until
now the only way to avoid them was --no-buildings, which also deletes every real OSM building,
wall, mast and pylon. `--overture=false` is the narrow version of that request.

The gate matters as much as the flag. Meld and the generator ship separately, so a user can run
a new Meld against the arnis binary already sitting next to it. clap rejects unknown arguments
outright, so passing --overture to a 3.0.7 binary turns every cell into

    error: unexpected argument '--overture' found

Asking the binary what it accepts is version-independent, which matters because a locally built
or side-loaded generator may not report a version at all.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import arnis_cmd  # noqa: E402

BBOX = {"south": 44.0, "west": 26.0, "north": 44.1, "east": 26.1}
ORIGIN = {"lat": 44.0, "lng": 26.0}


@pytest.fixture(autouse=True)
def _clear_help_cache():
    arnis_cmd._HELP_CACHE.clear()
    yield
    arnis_cmd._HELP_CACHE.clear()


def _cmd(monkeypatch, settings, *, supports=("--overture", "--props-min-scale")):
    monkeypatch.setattr(arnis_cmd, "arnis_supports", lambda exe, flag: flag in supports)
    return arnis_cmd.build_arnis_cmd("arnis.exe", BBOX, "out", settings, ORIGIN, None, 1)


def test_on_by_default_emits_nothing(monkeypatch):
    """Absent setting must mean the previous behaviour exactly: Overture on, no flag passed.

    Every existing project predates this key. If a missing key read as False, reopening any of
    them would silently drop buildings the user has already rendered with.
    """
    assert not [a for a in _cmd(monkeypatch, {}) if "overture" in a]


def test_explicitly_on_emits_nothing(monkeypatch):
    assert not [a for a in _cmd(monkeypatch, {"overture": True}) if "overture" in a]


def test_off_emits_the_flag(monkeypatch):
    assert "--overture=false" in _cmd(monkeypatch, {"overture": False})


def test_off_is_not_passed_to_a_generator_that_lacks_it(monkeypatch):
    """The whole point of the gate: an older binary must get no flag rather than a fatal one."""
    cmd = _cmd(monkeypatch, {"overture": False}, supports=())
    assert not [a for a in cmd if "overture" in a]


def test_off_still_keeps_osm_buildings(monkeypatch):
    """--overture=false is the narrow request; it must never imply --no-buildings."""
    cmd = _cmd(monkeypatch, {"overture": False, "buildings": True})
    assert "--overture=false" in cmd and "--no-buildings" not in cmd


def test_props_min_scale_is_passed_when_set(monkeypatch):
    """arnis defaults this to 0.35 while Meld's default scale is 0.1, so every prop family was
    dropped at the default while the UI showed the checkboxes ticked."""
    cmd = _cmd(monkeypatch, {"props_min_scale": 0})
    assert "--props-min-scale" in cmd and cmd[cmd.index("--props-min-scale") + 1] == "0.0"


def test_props_min_scale_absent_leaves_the_fork_default(monkeypatch):
    assert "--props-min-scale" not in _cmd(monkeypatch, {})


def test_unreadable_help_is_treated_as_an_old_binary(monkeypatch):
    """A generator that cannot be run at all must not be assumed capable - omitting a flag loses
    one behaviour, passing an unknown one loses the whole run."""
    def boom(*a, **k):
        raise OSError("cannot exec")
    monkeypatch.setattr(arnis_cmd.subprocess, "run", boom)
    assert arnis_cmd.arnis_supports("arnis.exe", "--overture") is False


def test_help_is_probed_once_per_exe(monkeypatch):
    """Once per exe, not once per cell: a country render spawns thousands of cells."""
    calls = []

    class R:
        stdout, stderr = "--overture  Add building footprints", ""

    monkeypatch.setattr(arnis_cmd.subprocess, "run", lambda *a, **k: calls.append(a) or R())
    for _ in range(5):
        assert arnis_cmd.arnis_supports("arnis.exe", "--overture") is True
    assert len(calls) == 1
