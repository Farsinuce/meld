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


# --- archive naming -------------------------------------------------------------------------
# v1.8.4 published its Windows archive as "Meld-1.8.zip". Path.with_suffix(".zip") on the stem
# "Meld-1.8.4-win-x64" replaces everything after the last dot, so the patch version, the OS and
# the arch all vanished at once. The release notes advertised Meld-*-win-x64.zip and the upload
# glob dist/Meld-*.zip matched the wrong name, so it shipped without a single error.

def _archive_name(mod, monkeypatch, *, win, machine, version="1.8.4"):
    monkeypatch.setattr(mod, "IS_WIN", win)
    monkeypatch.setattr(mod, "IS_MAC", False)
    monkeypatch.setattr(mod, "version", lambda: version)
    monkeypatch.setattr(mod.platform, "machine", lambda: machine)
    return mod.archive_path().name


def test_windows_archive_keeps_the_whole_version_and_platform(build, monkeypatch):
    assert _archive_name(build, monkeypatch, win=True, machine="AMD64") \
        == "Meld-1.8.4-win-x64.zip"


def test_a_three_part_version_survives_on_every_platform(build, monkeypatch):
    """The dots in the version are the whole trap: a two-part version would have hidden it."""
    assert _archive_name(build, monkeypatch, win=False, machine="x86_64") \
        == "Meld-1.8.4-linux-x64.tar.gz"
    assert _archive_name(build, monkeypatch, win=False, machine="aarch64",
                         version="10.20.30") == "Meld-10.20.30-linux-arm64.tar.gz"


def test_the_tarball_keeps_the_app_extension(build, monkeypatch, tmp_path):
    """macOS decides a directory is an application purely by its .app extension.

    v1.8.4 tarred dist/Meld.app with a hardcoded arcname="Meld", so both mac archives extracted
    to a plain folder: Finder drew a folder, LaunchServices would not launch it, and the release
    note's "extract the folder anywhere and run it" dead-ended. Linux was never affected, which
    is why a single hardcoded name looked fine for as long as only Linux and Windows shipped.
    """
    import tarfile

    app = tmp_path / "Meld.app" / "Contents" / "MacOS"
    app.mkdir(parents=True)
    (app / "Meld").write_text("payload", encoding="utf-8")

    out = tmp_path / "out.tar.gz"
    monkeypatch.setattr(build, "IS_WIN", False)
    monkeypatch.setattr(build, "archive_path", lambda: out)
    build.archive(tmp_path / "Meld.app")

    with tarfile.open(out) as t:
        names = t.getnames()
    assert all(n.split("/")[0] == "Meld.app" for n in names), \
        f"the .app extension was stripped: {names[:3]}"


def test_a_linux_folder_is_still_archived_as_plain_Meld(build, monkeypatch, tmp_path):
    """The fix must not rename the Linux payload, whose folder really is called Meld."""
    import tarfile

    folder = tmp_path / "Meld"
    folder.mkdir()
    (folder / "Meld").write_text("payload", encoding="utf-8")

    out = tmp_path / "out.tar.gz"
    monkeypatch.setattr(build, "IS_WIN", False)
    monkeypatch.setattr(build, "archive_path", lambda: out)
    build.archive(folder)

    with tarfile.open(out) as t:
        assert all(n.split("/")[0] == "Meld" for n in t.getnames())


def test_the_name_matches_what_the_release_notes_promise(build, monkeypatch):
    """release.yml's download table names these files literally; a mismatch points users at a
    file that does not exist."""
    import fnmatch
    for win, machine, promised in ((True, "AMD64", "Meld-*-win-x64.zip"),
                                   (False, "x86_64", "Meld-*-linux-x64.tar.gz")):
        got = _archive_name(build, monkeypatch, win=win, machine=machine)
        assert fnmatch.fnmatch(got, promised), f"{got} does not match {promised}"
