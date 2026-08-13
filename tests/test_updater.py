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


# ── removing a build: the only irreversible step ──────────────────────────────────────────────

def test_never_removes_the_running_version(tmp_path):
    r = updater.remove_build(str(tmp_path / "app" / "Meld"))
    assert r["ok"] is False and "running" in r["error"]


def test_never_removes_a_folder_that_holds_the_data(tmp_path, monkeypatch):
    """The portable case, and the one that would be unrecoverable.

    paths.data_dir() resolves to <app>/data for a portable install, so "delete the old version"
    would take the projects and the entire tile cache with it. Refused outright rather than
    handled cleverly - there is no clever version of deleting someone's 100 GB cache.
    """
    old = tmp_path / "app" / "Meld-1.8.5"
    (old / "data" / "projects").mkdir(parents=True)
    monkeypatch.setattr(updater, "data_dir", lambda: old / "data")
    r = updater.remove_build(str(old))
    assert r["ok"] is False and "projects" in r["error"]
    assert (old / "data" / "projects").is_dir(), "it must still be there"


def test_refuses_anything_that_is_not_a_meld_build(tmp_path):
    other = tmp_path / "Documents"
    other.mkdir()
    assert updater.remove_build(str(other))["ok"] is False


def test_removes_a_spent_build(tmp_path):
    old = tmp_path / "app" / "Meld-1.8.4"
    (old / "_internal").mkdir(parents=True)
    r = updater.remove_build(str(old))
    assert r["ok"] is True and not old.exists()


# ── handing over ──────────────────────────────────────────────────────────────────────────────

def test_the_new_build_is_told_to_wait_for_the_lock(tmp_path, monkeypatch):
    """The hand-off only works in one order.

    A new build started while the old one holds the lock sees it held, opens a browser at the OLD
    instance and exits 0 - an update that looks successful and changes nothing. So the new one is
    started first and must WAIT; the old one quitting is what releases the lock.
    """
    staged = tmp_path / "app" / "Meld-9.9.9"
    staged.mkdir(parents=True)
    (staged / "Meld.exe").write_text("new", encoding="utf-8")
    monkeypatch.setattr(updater.sys, "platform", "win32")

    seen = {}

    def fake_popen(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw.get("env") or {}
        return object()

    monkeypatch.setattr(updater.subprocess, "Popen", fake_popen)
    r = updater.launch_staged(str(staged))
    assert r["ok"] is True
    assert float(seen["env"]["MELD_WAIT_FOR_LOCK"]) > 0


def test_the_smoke_test_data_dir_is_not_inherited(tmp_path, monkeypatch):
    """staging points MELD_DATA_DIR at a scratch directory. Inheriting that would send the new
    build at an empty data folder - it would come up with no projects, which to a user is
    indistinguishable from having lost them."""
    staged = tmp_path / "app" / "Meld-9.9.9"
    staged.mkdir(parents=True)
    (staged / "Meld.exe").write_text("new", encoding="utf-8")
    monkeypatch.setattr(updater.sys, "platform", "win32")
    monkeypatch.setenv("MELD_DATA_DIR", str(tmp_path / "scratch"))

    seen = {}
    monkeypatch.setattr(updater.subprocess, "Popen",
                        lambda cmd, **kw: seen.update(env=kw.get("env") or {}) or object())
    updater.launch_staged(str(staged))
    assert "MELD_DATA_DIR" not in seen["env"]


def test_staged_builds_are_listed_newest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(updater.sys, "platform", "win32")
    for v in ("1.8.4", "1.8.10", "1.8.9"):
        d = tmp_path / "app" / f"Meld-{v}"
        d.mkdir(parents=True)
        (d / "Meld-console.exe").write_text("x", encoding="utf-8")
    got = [b["version"] for b in updater.staged_builds()]
    assert got == ["1.8.10", "1.8.9", "1.8.4"], "numeric order, not string order"


def test_install_root_unwraps_a_mac_bundle(monkeypatch, tmp_path):
    """On macOS the executable lives in Meld.app/Contents/MacOS, and the thing to stage beside is
    the .app - not its MacOS folder, which would nest the new version inside the old bundle."""
    monkeypatch.setattr(updater.sys, "platform", "darwin")
    monkeypatch.setattr(updater, "exe_dir",
                        lambda: tmp_path / "Applications" / "Meld.app" / "Contents" / "MacOS")
    assert updater.install_root().name == "Meld.app"


# ── probe caches must not outlive the binary they describe ────────────────────────────────────

def test_installing_a_generator_forgets_what_was_probed_about_the_old_one(tmp_path, monkeypatch):
    """Both probe caches key on the PATH, and an update replaces the file at an unchanged path.

    The visible symptom was a success message naming the version it had just replaced. The real
    damage is arnis_supports(): it gates every new flag, so a freshly installed 3.0.8 would keep
    being told it has no --overture, and the checkbox the update just enabled would go on doing
    nothing until Meld was restarted.
    """
    from src import arnis_cmd

    exe = tmp_path / "arnis.exe"
    exe.write_text("old", encoding="utf-8")
    arnis_cmd._VER_CACHE[str(exe)] = (3, 0, 7)
    arnis_cmd._HELP_CACHE[str(exe)] = "no such flag here"

    assert arnis_cmd.arnis_version(str(exe)) == (3, 0, 7)
    assert arnis_cmd.arnis_supports(str(exe), "--overture") is False

    arnis_cmd.forget_probe(str(exe))
    assert str(exe) not in arnis_cmd._VER_CACHE
    assert str(exe) not in arnis_cmd._HELP_CACHE
