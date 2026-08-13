"""The names packaging/build.py asks the arnis release for must be names it actually publishes.

The bug this pins down: build.py asked for `arnis-mac-arm64.tar.gz`. The fork builds that file,
but only as a CI artifact - the release job lipos the two arches together and attaches
`arnis-mac-universal.tar.gz` alone. So both macOS jobs failed with

    [build] release v3.0.7 has no 'arnis-mac-arm64.tar.gz'
    [build] ERROR: no arnis binary to bundle.

and v1.8.4 shipped with no mac archives. meld_launch.py had already been fixed for exactly this;
build.py, the copy that runs in CI, had not.

These assert against the asset names in arnis-283-src/.github/workflows/release.yml. If that
workflow's `files:` list changes, this test is where it should be noticed.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# Published by the fork's release job. Kept literal on purpose: the point is to fail when the two
# lists drift apart, which a shared constant would hide.
PUBLISHED = {
    "arnis-windows.exe",
    "arnis-linux.tar.gz",
    "arnis-linux-appimage.tar.gz",
    "arnis-mac-universal.tar.gz",
}


@pytest.fixture
def build():
    spec = importlib.util.spec_from_file_location("meld_build", ROOT / "packaging" / "build.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["meld_build"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _as(mod, *, win=False, mac=False, machine="x86_64", platform_name="linux"):
    mod.IS_WIN, mod.IS_MAC = win, mac
    mod.sys = type("s", (), {"platform": platform_name})
    mod.platform = type("p", (), {"machine": staticmethod(lambda: machine)})
    return mod.arnis_assets()


def test_every_platform_asks_for_something_that_exists(build, monkeypatch):
    """The first choice for each platform must be a file the release really has - a later
    fallback is not good enough, because a build that has to fall back is a build that noticed
    too late."""
    for kwargs in (dict(win=True, platform_name="win32"),
                   dict(mac=True, machine="arm64", platform_name="darwin"),
                   dict(mac=True, machine="x86_64", platform_name="darwin"),
                   dict(platform_name="linux")):
        names, _out = _as(build, **kwargs)
        assert names, f"no asset defined for {kwargs}"
        assert names[0] in PUBLISHED, (
            f"{names[0]} is not attached to any arnis release; the mac jobs failed for "
            f"precisely this reason. Published: {sorted(PUBLISHED)}")


def test_mac_prefers_the_universal_binary(build):
    """Both arches take the same file. It carries x86_64 and arm64, so there is one right
    answer and it does not depend on which runner picked up the job."""
    for machine in ("arm64", "x86_64"):
        names, out = _as(build, mac=True, machine=machine, platform_name="darwin")
        assert names[0] == "arnis-mac-universal.tar.gz"
        assert out == "arnis"


def test_windows_saves_with_an_exe_extension(build):
    names, out = _as(build, win=True, platform_name="win32")
    assert names == ["arnis-windows.exe"] and out == "arnis.exe", \
        "Windows will not execute a file without the extension"
