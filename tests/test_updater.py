"""Staging a new version: the refusals, the verification, and the non-destructiveness.

Most of these test that the updater says NO. That is the point of the staging half - it writes
into a new sibling folder and nothing else, so every interesting case is one where it declines
to proceed rather than one where it succeeds.

The single most important test here is the checksum mismatch: bytes that are about to be
executed and were not verified is the one way an updater is worse than no updater at all.
"""
from __future__ import annotations

import hashlib
import io
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import updater  # noqa: E402


@pytest.fixture(autouse=True)
def _reset(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(updater, "exe_dir", lambda: tmp_path / "app" / "Meld")
    monkeypatch.setattr(updater, "is_frozen", lambda: True)
    (tmp_path / "app" / "Meld").mkdir(parents=True)
    with updater._lock:
        updater._state.update(phase="idle", pct=0, note="", error="", path="", version="")
    yield


def _zip_build(dest: Path, *, console="Meld-console.exe") -> bytes:
    """An archive shaped like a real Windows release: one top-level Meld/ directory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"Meld/{console}", "binary")
        z.writestr("Meld/_internal/x.dat", "payload")
    return buf.getvalue()


def _info(blob: bytes, **over):
    d = {"download_url": "https://example.invalid/Meld-9.9.9-win-x64.zip",
         "sha256": hashlib.sha256(blob).hexdigest(),
         "latest": "9.9.9", "size_mb": 1}
    d.update(over)
    return d


def _serve(monkeypatch, blob: bytes):
    def fake_download(url, dest, expect):
        Path(dest).write_bytes(blob)
    monkeypatch.setattr(updater, "_download", fake_download)


# ── refusals ──────────────────────────────────────────────────────────────────────────────────

def test_refuses_from_a_source_checkout(monkeypatch):
    monkeypatch.setattr(updater, "is_frozen", lambda: False)
    r = updater.stage({"download_url": "u", "sha256": "a", "latest": "9.9.9"})
    assert r["phase"] == "failed" and "git" in r["error"]


def test_refuses_during_a_render(monkeypatch):
    """Staging is non-destructive, but it competes for disk and bandwidth with a job that may
    have hours invested in it."""
    r = updater.stage({"download_url": "u", "sha256": "a", "latest": "9"}, render_active=True)
    assert r["phase"] == "failed" and "render is running" in r["error"]


def test_refuses_without_a_checksum(monkeypatch):
    called = []
    monkeypatch.setattr(updater, "_download", lambda *a: called.append(1))
    r = updater.stage({"download_url": "u", "sha256": "", "latest": "9.9.9"})
    assert r["phase"] == "failed" and "checksum" in r["error"]
    assert not called, "it must refuse BEFORE downloading, not after"


def test_refuses_with_no_asset_for_this_platform():
    r = updater.stage({"download_url": "", "sha256": "a", "latest": "9.9.9"})
    assert r["phase"] == "failed" and "no download" in r["error"]


def test_refuses_when_disk_is_short(monkeypatch, tmp_path):
    blob = _zip_build(tmp_path)
    _serve(monkeypatch, blob)

    class DU:
        free = 1_000_000            # 1 MB against a required ~200 MB
    monkeypatch.setattr(updater.shutil, "disk_usage", lambda p: DU)
    r = updater.stage(_info(blob))
    assert r["phase"] == "failed" and "free space" in r["error"]


# ── verification ──────────────────────────────────────────────────────────────────────────────

def test_checksum_mismatch_discards_the_download(monkeypatch, tmp_path):
    """The one that matters. A tampered or truncated archive must never reach the smoke test,
    let alone be executed."""
    blob = _zip_build(tmp_path)
    _serve(monkeypatch, blob)
    smoked = []
    monkeypatch.setattr(updater, "_smoke", lambda root: smoked.append(1) or (True, ""))

    r = updater.stage(_info(blob, sha256="0" * 64))
    assert r["phase"] == "failed" and "checksum mismatch" in r["error"]
    assert not smoked, "a bad archive must not be run"
    assert not list((tmp_path / "data" / "updates").glob("*.zip")), "the bad file must be deleted"


def test_a_good_checksum_proceeds(monkeypatch, tmp_path):
    blob = _zip_build(tmp_path)
    _serve(monkeypatch, blob)
    monkeypatch.setattr(updater, "_smoke", lambda root: (True, ""))
    r = updater.stage(_info(blob))
    assert r["phase"] == "done", r.get("error")
    assert Path(r["path"]).is_dir() and Path(r["path"]).name == "Meld-9.9.9"


# ── non-destructiveness ───────────────────────────────────────────────────────────────────────

def test_the_running_install_is_never_touched(monkeypatch, tmp_path):
    old = tmp_path / "app" / "Meld"
    (old / "Meld.exe").write_text("the running build", encoding="utf-8")
    blob = _zip_build(tmp_path)
    _serve(monkeypatch, blob)
    monkeypatch.setattr(updater, "_smoke", lambda root: (True, ""))

    updater.stage(_info(blob))
    assert (old / "Meld.exe").read_text(encoding="utf-8") == "the running build"


def test_a_build_that_fails_its_smoke_test_changes_nothing(monkeypatch, tmp_path):
    old = tmp_path / "app" / "Meld"
    (old / "Meld.exe").write_text("the running build", encoding="utf-8")
    blob = _zip_build(tmp_path)
    _serve(monkeypatch, blob)
    monkeypatch.setattr(updater, "_smoke", lambda root: (False, "exit 1"))

    r = updater.stage(_info(blob))
    assert r["phase"] == "failed" and "did not start" in r["error"]
    assert (old / "Meld.exe").read_text(encoding="utf-8") == "the running build"


def test_the_data_pointer_is_written_into_the_new_folder(monkeypatch, tmp_path):
    """One line of text, and the whole "my projects and my 100 GB cache are still there"
    guarantee. Without it the new folder resolves its own <app>/data and comes up looking like a
    fresh install - which to a user is indistinguishable from having lost everything."""
    blob = _zip_build(tmp_path)
    _serve(monkeypatch, blob)
    monkeypatch.setattr(updater, "_smoke", lambda root: (True, ""))

    r = updater.stage(_info(blob))
    pointer = Path(r["path"]) / "meld-data.txt"
    assert pointer.is_file()
    assert pointer.read_text(encoding="utf-8").strip() == str(tmp_path / "data")


def test_user_dropped_packs_are_carried_across(monkeypatch, tmp_path):
    old = tmp_path / "app" / "Meld"
    (old / "cave-pack").mkdir()
    (old / "cave-pack" / "mine.schem").write_text("x", encoding="utf-8")
    blob = _zip_build(tmp_path)
    _serve(monkeypatch, blob)
    monkeypatch.setattr(updater, "_smoke", lambda root: (True, ""))

    r = updater.stage(_info(blob))
    assert (Path(r["path"]) / "cave-pack" / "mine.schem").is_file()


# ── archive handling ──────────────────────────────────────────────────────────────────────────

def test_finds_the_app_folder_in_a_mac_style_archive(tmp_path):
    d = tmp_path / "x"
    (d / "Meld.app" / "Contents" / "MacOS").mkdir(parents=True)
    assert updater._payload_root(d).name == "Meld.app"


def test_tar_extraction_refuses_a_path_escape(tmp_path):
    """A member named ../evil must not be able to write outside the extraction directory."""
    arc = tmp_path / "bad.tar.gz"
    with tarfile.open(arc, "w:gz") as t:
        data = b"pwned"
        ti = tarfile.TarInfo("../escaped.txt")
        ti.size = len(data)
        t.addfile(ti, io.BytesIO(data))
    into = tmp_path / "into"
    into.mkdir()
    try:
        updater._extract(arc, into)
    except Exception:
        pass                                    # refusing outright is a fine outcome too
    assert not (tmp_path / "escaped.txt").exists(), "extraction escaped its directory"


def test_install_root_unwraps_a_mac_bundle(monkeypatch, tmp_path):
    """On macOS the executable lives in Meld.app/Contents/MacOS, and the thing to stage beside is
    the .app - not its MacOS folder, which would nest the new version inside the old bundle."""
    monkeypatch.setattr(updater.sys, "platform", "darwin")
    monkeypatch.setattr(updater, "exe_dir",
                        lambda: tmp_path / "Applications" / "Meld.app" / "Contents" / "MacOS")
    assert updater.install_root().name == "Meld.app"
