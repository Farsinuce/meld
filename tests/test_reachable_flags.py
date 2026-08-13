"""Generator capabilities that existed but had no way to reach them from Meld.

Each of these flags was already plumbed to the CLI from a project key that no UI control ever
wrote, so the only way to use one was to hand-edit project.json. The tests pin the emission rules,
and in particular the difference between "blank" and "zero" - collapsing those would silently
change behaviour for every project that has never touched the setting.
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
def _all_flags_supported(monkeypatch):
    monkeypatch.setattr(arnis_cmd, "arnis_supports", lambda exe, flag: True)
    arnis_cmd._HELP_CACHE.clear()


def _cmd(settings):
    return arnis_cmd.build_arnis_cmd("arnis.exe", BBOX, "out", settings, ORIGIN, None, 1)


def _val(cmd, flag):
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


# ── cached-elevation-only ─────────────────────────────────────────────────────────────────────

def test_offline_is_off_by_default():
    assert "--offline" not in _cmd({})


def test_offline_is_passed_when_set():
    assert "--offline" in _cmd({"offline_elevation": True})


def test_offline_is_pointless_without_terrain():
    """Nothing fetches elevation when terrain is off, so the flag would only be noise in the
    command line and in the logs."""
    assert "--offline" not in _cmd({"offline_elevation": True, "terrain": False})


def test_offline_is_not_passed_to_a_generator_without_it(monkeypatch):
    monkeypatch.setattr(arnis_cmd, "arnis_supports", lambda exe, flag: False)
    assert "--offline" not in _cmd({"offline_elevation": True})


# ── overpass endpoints ────────────────────────────────────────────────────────────────────────

def test_overpass_endpoints_are_joined_with_commas():
    """The generator takes a comma-delimited list, tried in order."""
    cmd = _cmd({"overpass_url": ["https://a/api", "https://b/api"]})
    assert _val(cmd, "--overpass-url") == "https://a/api,https://b/api"


def test_an_empty_endpoint_list_is_omitted():
    """An empty box must fall back to the built-in public endpoints, not pass a blank one."""
    assert "--overpass-url" not in _cmd({"overpass_url": []})


def test_no_overpass_key_is_omitted():
    assert "--overpass-url" not in _cmd({})


# ── cell timeout ──────────────────────────────────────────────────────────────────────────────

def test_timeout_is_passed_when_set():
    assert _val(_cmd({"timeout": 1200}) or [], "--timeout") == "1200"


def test_blank_timeout_leaves_the_generator_default():
    """Blank means "use the generator's 600 s", which is not the same as 0 - and 0 would be a
    timeout of zero seconds, i.e. every cell fails instantly."""
    assert "--timeout" not in _cmd({})
    assert "--timeout" not in _cmd({"timeout": None})
    assert "--timeout" not in _cmd({"timeout": 0})
