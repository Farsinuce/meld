"""Stage a newer Meld next to this one. Download, verify, unpack, prove it starts.

This is the half of updating that cannot lose anything. It writes only into the data directory
and into a NEW sibling folder; it never touches the running install, never deletes, never
rewrites a shortcut. The end state is "1.8.6 is sitting next to you and it works - click to
launch it", and the user moves across on their own schedule.

The destructive half - quitting, swapping, deleting the old folder - is deliberately not here.
It needs a machine that is not the developer's to test on, and it has a failure mode this half
does not: `paths.data_dir()` may resolve INSIDE the application folder for a portable install
(<app>/data), so a swap that deletes the old folder would take the user's projects with it.
Staging cannot hit that, because staging deletes nothing.

Order matters in one place. A new process launched while this one is alive does NOT take the
single-instance lock - meld_app.py sees the lock held, opens a browser at the RUNNING instance
and exits 0. So a hand-off has to be old-quits-then-new-starts, never new-starts-then-old-quits,
which would look like it worked and do nothing. Recorded here because the swap step will need it.

Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

from .paths import data_dir, exe_dir, is_frozen

SMOKE_TIMEOUT = 120        # --check unpacks an embedded generator on first run; 60 s was tight
HEADROOM = 2.5             # archive + extracted payload, with room to be wrong

_lock = threading.Lock()
_state: dict = {"phase": "idle", "pct": 0, "note": "", "error": "", "path": "", "version": ""}


def progress() -> dict:
    with _lock:
        return dict(_state)


def _set(**kw) -> None:
    with _lock:
        _state.update(kw)


def busy() -> bool:
    with _lock:
        return _state["phase"] not in ("idle", "done", "failed")


# ── where things live ─────────────────────────────────────────────────────────────────────────

def install_root() -> Path:
    """The folder a user would drag to the bin to uninstall.

    Not the same as exe_dir() on macOS: there the executable sits in Meld.app/Contents/MacOS, and
    the thing that is "the app" is the .app three levels up. Getting this wrong would stage the
    new version inside the old bundle.
    """
    d = exe_dir()
    if sys.platform == "darwin":
        for p in (d, *d.parents):
            if p.suffix == ".app":
                return p
    return d


def staging_dir() -> Path:
    d = data_dir() / "updates"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _payload_root(extracted: Path) -> Path | None:
    """Find the app folder inside an unpacked archive.

    Archives carry one top-level directory - Meld/ on Windows and Linux, Meld.app on macOS - but
    that is a convention this code should not have to trust, so it looks rather than assumes.
    """
    for c in (extracted / "Meld.app", extracted / "Meld"):
        if c.is_dir():
            return c
    kids = [p for p in extracted.iterdir() if p.is_dir()]
    return kids[0] if len(kids) == 1 else None


def _console_exe(root: Path) -> Path:
    if sys.platform == "win32":
        return root / "Meld-console.exe"
    if root.suffix == ".app":
        return root / "Contents" / "MacOS" / "Meld-console"
    return root / "Meld-console"


# ── the steps ─────────────────────────────────────────────────────────────────────────────────

def _download(url: str, dest: Path, expect_bytes: int) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "meld-updater"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or expect_bytes or 0)
        got = 0
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if total:
                _set(pct=int(got * 100 / total), note=f"{got // 1_000_000} / {total // 1_000_000} MB")


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract(archive: Path, into: Path) -> None:
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as z:
            z.extractall(into)
        return
    # filter="data" refuses absolute paths and ../ members. Without it a hostile archive could
    # write anywhere the user can; with it, extraction stays inside `into`. Python 3.12 warns
    # when it is omitted precisely because the old default was unsafe.
    with tarfile.open(archive) as t:
        try:
            t.extractall(into, filter="data")
        except TypeError:                          # Python < 3.12
            t.extractall(into)


def _mark_executable(root: Path) -> None:
    """Restore the +x bits a zip cannot carry, and clear macOS quarantine.

    tar preserves the mode, so this is mostly a Windows-archive concern - but it is cheap and it
    is exactly the class of bug that shipped in 1.8.4, where the Linux region_converter arrived
    without +x and B_Linear died on a binary that was plainly present.
    """
    if sys.platform == "win32":
        return
    for p in root.rglob("*"):
        try:
            if p.is_file() and (p.suffix in ("", ".sh") or "MacOS" in p.parts):
                p.chmod(p.stat().st_mode | 0o111)
        except Exception:
            pass
    if sys.platform == "darwin":
        # Downloaded bytes carry com.apple.quarantine, which Gatekeeper treats as "from the
        # internet". Without clearing it the staged build refuses to launch.
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(root)],
                       capture_output=True, check=False)


def _carry_identity(new_root: Path) -> list[str]:
    """Point the staged build at this install's data, and bring across what the user added.

    The pointer file is the whole "my projects and my 100 GB of cached tiles are still there"
    guarantee, and it is one line of text. Without it the new folder would resolve its own
    <app>/data and come up looking like a fresh install with no projects - which is indis-
    tinguishable, to the user, from having lost them.
    """
    carried = []
    target = new_root
    if target.suffix == ".app":
        target = target / "Contents" / "MacOS"
    try:
        (target / "meld-data.txt").write_text(str(data_dir()), encoding="utf-8")
        carried.append("data pointer")
    except Exception:
        pass
    old = install_root()
    if old.suffix == ".app":
        old = old / "Contents" / "MacOS"
    # Packs the user dropped in by hand. The bundled copies are already in the new archive; these
    # are the ones that would otherwise silently not come across.
    for name in ("cave-pack", "tree-packs"):
        src = old / name
        if src.is_dir() and not (target / name).exists():
            try:
                shutil.copytree(src, target / name)
                carried.append(name)
            except Exception:
                pass
    return carried


def _smoke(root: Path) -> tuple[bool, str]:
    """Prove the staged build starts before calling it good.

    Same --check the release workflow runs on all four platforms, so a pass here means the same
    thing it means in CI. MELD_DATA_DIR is pointed at a scratch directory: --check must not touch
    the live projects, and a staged build that has not been chosen yet has no business writing to
    them at all.
    """
    exe = _console_exe(root)
    if not exe.is_file():
        return False, f"no {exe.name} in the downloaded build"
    env = dict(os.environ)
    env["MELD_DATA_DIR"] = str(staging_dir() / "smoke-data")
    try:
        r = subprocess.run([str(exe), "--check"], capture_output=True, text=True,
                           timeout=SMOKE_TIMEOUT, env=env, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return False, f"--check did not finish within {SMOKE_TIMEOUT}s"
    except Exception as ex:                                       # noqa: BLE001
        return False, f"could not run --check: {ex}"
    if r.returncode != 0:
        tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-3:]
        return False, "--check failed: " + " / ".join(tail)
    return True, ""


def stage(info: dict, *, render_active: bool = False) -> dict:
    """Download -> verify -> unpack -> smoke test. Returns the final progress dict.

    Nothing here replaces the running install. On success the new version is a sibling folder
    that has already proved it starts.
    """
    if not is_frozen():
        return _fail("a source checkout updates with git, not with this")
    if render_active:
        # A render is hours of work. Even though staging is non-destructive, it competes for disk
        # and bandwidth with the thing the user actually cares about right now.
        return _fail("a render is running - update when it finishes")
    if busy():
        return progress()

    url, sha, version = info.get("download_url"), info.get("sha256"), info.get("latest")
    size = int(info.get("size_mb") or 0) * 1_000_000
    if not url:
        return _fail("no download for this platform in the latest release")
    if not sha:
        # Refusing is the point. Unverified bytes that are about to be executed is the one way
        # an updater becomes worse than no updater.
        return _fail("the release has no checksum for this file - refusing to download it")

    _set(phase="checking", pct=0, note="", error="", path="", version=version or "")

    need = int(max(size, 80_000_000) * HEADROOM)
    try:
        free = shutil.disk_usage(str(staging_dir())).free
    except Exception:
        free = need
    if free < need:
        return _fail(f"not enough free space: need about {need // 1_000_000} MB, "
                     f"{free // 1_000_000} MB available")

    work = staging_dir()
    archive = work / (url.rsplit("/", 1)[-1] or f"Meld-{version}.bin")
    try:
        _set(phase="downloading")
        _download(url, archive, size)

        _set(phase="verifying", pct=100, note="")
        got = _sha256(archive)
        if got.lower() != sha.lower():
            archive.unlink(missing_ok=True)
            return _fail(f"checksum mismatch - discarded the download (expected {sha[:12]}…, "
                         f"got {got[:12]}…)")

        _set(phase="extracting", note="")
        tmp = work / f"_unpack-{version}"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        _extract(archive, tmp)
        payload = _payload_root(tmp)
        if payload is None:
            return _fail("could not find the app folder inside the archive")

        dest = install_root().parent / f"Meld-{version}{'.app' if payload.suffix == '.app' else ''}"
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        shutil.move(str(payload), str(dest))
        shutil.rmtree(tmp, ignore_errors=True)
        _mark_executable(dest)
        carried = _carry_identity(dest)

        _set(phase="testing", note="starting the new build once")
        ok, why = _smoke(dest)
        if not ok:
            # Leave it on disk rather than deleting: a build that fails --check is exactly the
            # one someone will want to look at, and nothing has been replaced.
            return _fail(f"the downloaded build did not start ({why}). Nothing was changed.")

        archive.unlink(missing_ok=True)
        _set(phase="done", pct=100, path=str(dest), error="",
             note="ready - carried: " + (", ".join(carried) or "nothing"))
    except Exception as ex:                                       # noqa: BLE001
        return _fail(str(ex))
    return progress()


def _fail(msg: str) -> dict:
    _set(phase="failed", error=msg)
    return progress()


# ── moving across ─────────────────────────────────────────────────────────────────────────────

def staged_builds() -> list[dict]:
    """Prepared versions sitting next to this install, newest first.

    They are found by looking rather than remembered in a file: staging writes a real folder to
    a real place, and a list that can disagree with the disk is a list that will.
    """
    from .update import parse_version
    here, out = install_root(), []
    try:
        for p in here.parent.iterdir():
            if p == here or not p.is_dir() or not p.name.startswith("Meld-"):
                continue
            v = parse_version(p.name)
            if v and _console_exe(p).is_file():
                out.append({"version": ".".join(map(str, v)), "path": str(p), "_v": v})
    except Exception:
        return []
    out.sort(key=lambda d: d["_v"], reverse=True)
    for d in out:
        d.pop("_v", None)
    return out


def launch_staged(path: str) -> dict:
    """Start a prepared build and hand over to it.

    Order is the whole design. A new build started while this one still holds the single-instance
    lock sees it held, opens a browser at THIS instance and exits 0 - an update that looks like it
    worked and did nothing. It cannot be done the other way either: a process cannot quit and then
    spawn something. So the new build is started first with MELD_WAIT_FOR_LOCK, it blocks on the
    lock, and this process quits - at which point the lock releases and the new one continues.

    The old folder is left alone. If the new build turns out to be worse, the user launches the
    old one again; removing it is a separate, explicit action.
    """
    root = Path(path)
    exe_name = "Meld.exe" if sys.platform == "win32" else "Meld"
    target = (root / "Contents" / "MacOS" / exe_name) if root.suffix == ".app" else root / exe_name
    if not target.is_file():
        return {"ok": False, "error": f"no {exe_name} in {root}"}

    env = dict(os.environ)
    env["MELD_WAIT_FOR_LOCK"] = "45"
    # Not inherited: this process is about to exit, and MELD_DATA_DIR set for a smoke test would
    # otherwise send the new build at a scratch directory instead of the real projects.
    env.pop("MELD_DATA_DIR", None)
    try:
        kw: dict = {"cwd": str(root), "env": env, "close_fds": True}
        if sys.platform == "win32":
            kw["creationflags"] = 0x00000008 | 0x00000200        # DETACHED_PROCESS | NEW_GROUP
        else:
            kw["start_new_session"] = True
        # The successor has to survive us: we exit seconds from now, and the normal spawn
        # policy would have put it inside this instance's kill-on-close job.
        from . import childproc
        with childproc.no_adopt():
            subprocess.Popen([str(target)], **kw)
    except Exception as ex:                                       # noqa: BLE001
        return {"ok": False, "error": f"could not start {target}: {ex}"}
    return {"ok": True, "started": str(target)}


def remove_build(path: str) -> dict:
    """Delete a prepared build, or an old one the user has finished with.

    Explicit and separate because it is the only irreversible step in the whole feature, and
    because of one specific trap: for a portable install paths.data_dir() resolves INSIDE the
    application folder (<app>/data), so deleting that folder would take the user's projects and
    their entire tile cache with it. Refused outright rather than handled cleverly.
    """
    root = Path(path).resolve()
    if root == install_root().resolve():
        return {"ok": False, "error": "that is the version you are running"}
    if not root.is_dir() or not root.name.startswith("Meld-"):
        return {"ok": False, "error": "not a Meld build folder"}
    try:
        d = data_dir().resolve()
        if d == root or root in d.parents:
            return {"ok": False,
                    "error": f"your projects and caches live inside that folder ({d}) - "
                             "move them first, or keep it"}
    except Exception:
        return {"ok": False, "error": "could not confirm where your data lives - not deleting"}
    try:
        shutil.rmtree(root)
    except Exception as ex:                                       # noqa: BLE001
        return {"ok": False, "error": str(ex)}
    return {"ok": True, "removed": str(root)}


# ── the generator, on its own schedule ────────────────────────────────────────────────────────

def stage_arnis(info: dict, *, render_active: bool = False) -> dict:
    """Download a newer arnis into bin_dir(), verified, and prove it runs.

    Separate from the app update, and far cheaper: the generator is one binary in a writable
    directory Meld already owns, so nothing is replaced in place and nothing needs a restart of
    anything. `resolve_arnis_exe()` prefers a bin_dir() copy only when it is strictly NEWER, so
    the moment this lands the next render uses it - and if it turns out to be worse, deleting one
    file reverts to the bundled generator.

    This is what lets a generator fix reach users without shipping a whole new Meld.
    """
    from .paths import bin_dir
    from .arnis_cmd import arnis_version, forget_probe

    if render_active:
        return _fail("a render is running - update the generator when it finishes")
    if busy():
        return progress()

    url, sha, version = info.get("download_url"), info.get("sha256"), info.get("latest")
    if not url:
        return _fail("no generator download for this platform in the latest release")
    if not sha:
        return _fail("the release has no checksum for the generator - refusing to download it")

    _set(phase="downloading", pct=0, note="generator", error="", path="", version=version or "")
    work = staging_dir()
    tmp = work / (url.rsplit("/", 1)[-1] or "arnis.bin")
    try:
        _download(url, tmp, int(info.get("size_mb") or 0) * 1_000_000)

        _set(phase="verifying", pct=100)
        got = _sha256(tmp)
        if got.lower() != sha.lower():
            tmp.unlink(missing_ok=True)
            return _fail(f"generator checksum mismatch - discarded (expected {sha[:12]}…, "
                         f"got {got[:12]}…)")

        name = "arnis.exe" if sys.platform == "win32" else "arnis"
        dest = bin_dir() / name
        _set(phase="extracting")
        if tmp.name.endswith(".tar.gz"):
            unpack = work / "_arnis"
            shutil.rmtree(unpack, ignore_errors=True)
            unpack.mkdir(parents=True)
            _extract(tmp, unpack)
            inner = next((p for p in unpack.rglob("*") if p.is_file()), None)
            if inner is None:
                return _fail("the generator archive was empty")
            shutil.move(str(inner), str(dest))
            shutil.rmtree(unpack, ignore_errors=True)
        else:
            shutil.move(str(tmp), str(dest))
        tmp.unlink(missing_ok=True)
        if sys.platform != "win32":
            dest.chmod(dest.stat().st_mode | 0o755)
        if sys.platform == "darwin":
            subprocess.run(["xattr", "-d", "com.apple.quarantine", str(dest)],
                           capture_output=True, check=False)

        # The file at this path just changed, so anything remembered about the old one is now
        # wrong. Must happen BEFORE the version probe below, or the check reports the version of
        # the binary that was replaced - and, worse, arnis_supports() keeps gating new flags off
        # the old binary's --help for the rest of the session.
        forget_probe(str(dest))

        # Prove it runs before letting it be chosen. A generator that cannot report its version
        # is one resolve_arnis_exe() will refuse to prefer anyway, so a silent failure here would
        # look like "the update did nothing" rather than an error.
        _set(phase="testing", note="checking the new generator")
        got_ver = arnis_version(str(dest))
        if not got_ver:
            dest.unlink(missing_ok=True)
            return _fail("the downloaded generator would not run - kept the existing one")

        _set(phase="done", pct=100, path=str(dest), error="",
             note=f"generator {'.'.join(map(str, got_ver))} installed - the next render uses it")
    except Exception as ex:                                       # noqa: BLE001
        return _fail(str(ex))
    return progress()
