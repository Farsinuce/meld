#!/usr/bin/env python3
"""
Meld one-click launcher (Windows / macOS / Linux).

Run this file (or the meld.command / meld.sh / meld.bat wrapper for your OS) and it will:
  1. create a private virtual environment (.venv) and install Meld's Python dependencies,
  2. make sure a native `arnis` generator binary is present for this OS + CPU
     (download the matching prebuilt from the GitHub release, else build it from the
     bundled Rust source with cargo, else print exact instructions),
  3. start the Meld server and open it in your browser.

Only the standard library is used here, so it runs on any Python 3.9+ with nothing
pre-installed. Re-running is fast: the venv, deps, and binary are reused.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV = HERE / ".venv"
REQ = HERE / "requirements.txt"
PORT = int(os.environ.get("PORT", "5630"))
FORK = os.environ.get("MELD_ARNIS_REPO", "Teddy563/arnis")
IS_WIN = os.name == "nt"

# Core deps Meld cannot run without; the rest of requirements.txt (osmium) is an
# optional feature that is allowed to fail on platforms without a prebuilt wheel.
CORE_DEPS = ["Flask>=3.0", "Pillow>=10.0", "psutil>=5.9", "shapely>=2.1", "zstandard>=0.22"]


def log(msg: str) -> None:
    print(f"[meld] {msg}", flush=True)


# ── virtual environment + Python dependencies ─────────────────────────────────
def venv_python() -> Path:
    return VENV / ("Scripts" if IS_WIN else "bin") / ("python.exe" if IS_WIN else "python")


def venv_pythonw() -> Path:
    """The windowless interpreter (Windows only).

    python.exe is a CONSOLE program: starting it always gives it a console, and when the parent
    has none Windows creates a fresh window for it - which is the black box that kept appearing
    in the taskbar even when the launcher itself was hidden. pythonw.exe is the same interpreter
    built as a GUI subsystem binary, so it gets no console and no taskbar button.
    """
    p = VENV / "Scripts" / "pythonw.exe"
    return p if p.is_file() else venv_python()


def in_target_venv() -> bool:
    try:
        return Path(sys.executable).resolve() == venv_python().resolve()
    except OSError:
        return False


def ensure_venv_and_deps() -> None:
    if not venv_python().exists():
        log("creating a private virtual environment (.venv) ...")
        import venv as _venv
        _venv.EnvBuilder(with_pip=True, clear=False).create(VENV)
    stamp = VENV / ".deps.sha"
    want = hashlib.sha256(REQ.read_bytes()).hexdigest() if REQ.exists() else "none"
    have = stamp.read_text(encoding="utf-8").strip() if stamp.exists() else ""
    if want == have:
        return
    vp = str(venv_python())
    log("installing Python dependencies (first run only) ...")
    subprocess.run([vp, "-m", "pip", "install", "--upgrade", "pip", "wheel"], check=False)
    ok = REQ.exists() and subprocess.run([vp, "-m", "pip", "install", "-r", str(REQ)]).returncode == 0
    if not ok:
        log("some optional dependencies did not install; installing the core set so Meld still runs "
            "(offline .pbf / extra compression features may be unavailable) ...")
        if subprocess.run([vp, "-m", "pip", "install", *CORE_DEPS]).returncode != 0:
            log("ERROR: could not install the core dependencies. Check your internet connection "
                "and that pip works, then re-run.")
            sys.exit(1)
    stamp.write_text(want, encoding="utf-8")


# ── native arnis generator binary ─────────────────────────────────────────────
def arnis_present() -> Path | None:
    names = ["arnis.exe", "arnis"] if IS_WIN else ["arnis"]
    for root in (HERE, HERE.parent):
        for n in names:
            c = root / n
            if c.is_file():
                return c
    return None


def _arnis_asset() -> tuple[str | None, str]:
    """(release asset name for this OS, local filename to save it as)."""
    sysname = platform.system()
    if sysname == "Windows":
        return "arnis-windows.exe", "arnis.exe"
    if sysname == "Linux":
        return "arnis-linux.tar.gz", "arnis"
    if sysname == "Darwin":
        # One universal binary covers both arches, and it is the only mac asset the fork's
        # release workflow actually attaches - the per-arch tarballs stay CI artifacts, so
        # asking for arnis-mac-arm64/intel here failed on every mac.
        return "arnis-mac-universal.tar.gz", "arnis"
    return None, "arnis"


def _mac_unquarantine(path: Path) -> None:
    """A binary downloaded from the internet is flagged com.apple.quarantine, so macOS Gatekeeper
    would block it with 'developer cannot be verified'. Clear the flag so it just runs."""
    if platform.system() == "Darwin":
        subprocess.run(["xattr", "-d", "com.apple.quarantine", str(path)],
                       capture_output=True, check=False)


def _http_get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "meld-launcher"})
    try:
        ctx: ssl.SSLContext | None = ssl.create_default_context()
    except Exception:
        ctx = None
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read()


def download_arnis() -> Path | None:
    asset, outname = _arnis_asset()
    if not asset:
        return None
    try:
        rel = json.loads(_http_get(f"https://api.github.com/repos/{FORK}/releases/latest", 30))
    except Exception as e:  # noqa: BLE001
        log(f"no downloadable release yet ({e})")
        return None
    url = next((a["browser_download_url"] for a in rel.get("assets", [])
                if a.get("name") == asset), None)
    if not url:
        log(f"release '{rel.get('tag_name')}' has no '{asset}' asset")
        return None
    log(f"downloading the arnis binary for this OS from release {rel.get('tag_name')} ...")
    try:
        blob = _http_get(url, 300)
    except Exception as e:  # noqa: BLE001
        log(f"download failed ({e})")
        return None
    target = HERE / outname
    if asset.endswith(".tar.gz"):
        tmp = HERE / asset
        tmp.write_bytes(blob)
        try:
            with tarfile.open(tmp) as t:
                member = next(m for m in t.getmembers() if m.isfile())
                with t.extractfile(member) as f:  # type: ignore[union-attr]
                    target.write_bytes(f.read())
        finally:
            tmp.unlink(missing_ok=True)
    else:
        target.write_bytes(blob)
    if not IS_WIN:
        os.chmod(target, 0o755)
    _mac_unquarantine(target)
    log(f"arnis ready: {target}")
    return target


def _source_provenance(src: Path) -> str:
    """Cargo version + git description of a fork checkout, for the build log."""
    ver = ""
    try:
        for line in (src / "Cargo.toml").read_text(encoding="utf-8").splitlines():
            if line.startswith("version"):
                ver = line.split("=", 1)[1].strip().strip('"')
                break
    except OSError:
        pass
    desc = ""
    try:
        r = subprocess.run(["git", "describe", "--tags", "--always", "--dirty"],
                           cwd=src, capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            desc = r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return " ".join(p for p in (ver, f"({desc})" if desc else "") if p) or "unknown"


def build_arnis() -> Path | None:
    if not shutil.which("cargo"):
        return None
    src = next((c for c in (HERE.parent / "arnis-283-src", HERE.parent / "arnis-source",
                            HERE.parent / "arnis") if (c / "Cargo.toml").is_file()), None)
    if not src:
        return None
    prov = _source_provenance(src)
    log(f"building arnis from source ({src}) at {prov} — this can take a few minutes the "
        "first time ...")
    if prov.endswith("-dirty)"):
        log("NOTE: that checkout has uncommitted changes, so this binary is an unreleased "
            "build. Delete arnis.exe and re-run the launcher to fall back to a published "
            "release.")
    # Build into a launcher-owned target dir outside the checkout. The fork's own target/
    # belongs to whoever is developing it; writing there would hand them a binary they did not
    # build, and hand us one from a half-finished edit. Keeping it out of the repo also leaves
    # `git status` there clean.
    flags = [] if IS_WIN else ["--no-default-features"]   # headless CLI build (no desktop GUI)
    target_dir = Path(tempfile.gettempdir()) / "meld-arnis-build"
    cmd = ["cargo", "build", "--release", "--target-dir", str(target_dir), *flags]
    if subprocess.run(cmd, cwd=src).returncode != 0:
        return None
    binname = "arnis.exe" if IS_WIN else "arnis"
    built = target_dir / "release" / binname
    if not built.is_file():
        return None
    target = HERE / binname
    shutil.copy2(built, target)
    if not IS_WIN:
        os.chmod(target, 0o755)
    log(f"arnis built: {target} ({prov})")
    return target


def ensure_arnis() -> None:
    p = arnis_present()
    if p:
        log(f"arnis binary: {p}")
        return
    log("arnis generator binary not found — fetching it for this OS ...")
    if download_arnis() or build_arnis():
        return
    want = "arnis.exe" if IS_WIN else "arnis"
    log("WARNING: could not obtain the arnis binary automatically. Meld will start, but "
        "generation is disabled until you provide it. Either:\n"
        "        - install Rust (https://rustup.rs) and re-run this launcher (it will build it), or\n"
        f"        - download the matching build from https://github.com/{FORK}/releases and put it\n"
        f"          next to server.py named '{want}'.")


# ── start the server + open the browser ───────────────────────────────────────
def _port_open() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def start_server() -> int:
    # PYTHONUTF8=1 puts the server in UTF-8 mode, so anything that reads a child process or
    # a file WITHOUT naming an encoding uses UTF-8 instead of the machine's locale code page.
    # Meld pins UTF-8 explicitly where it matters, but on a non-cp1252 Windows (cp1250,
    # cp1251, cp932, ...) a single unpinned read of Arnis's output is enough to kill a cell,
    # so the whole process is put on UTF-8 as the floor rather than relying on every call site.
    env = {**os.environ, "PORT": str(PORT), "PYTHONUTF8": "1"}
    url = f"http://127.0.0.1:{PORT}"
    # Prefer the Meld entry point: same server, plus the tray icon, the single-instance
    # guard and the wake lock. It opens the browser itself (with the session token in the URL),
    # so this launcher must not also open one. server.py stays the fallback for a checkout that
    # predates it.
    entry = HERE / "meld_app.py"
    if not entry.is_file():
        entry = HERE / "server.py"
    log(f"starting Meld -> {url}")
    # meld_app.py goes to the tray, so it must start windowless: pythonw plus CREATE_NO_WINDOW,
    # because either one alone still leaves a console window in some launch paths. server.py is
    # the console fallback and keeps the visible window it has always had.
    if entry.name == "meld_app.py":
        interp, flags = venv_pythonw(), (0x08000000 if IS_WIN else 0)   # CREATE_NO_WINDOW
    else:
        interp, flags = venv_python(), 0
    proc = subprocess.Popen([str(interp), str(entry)], env=env, creationflags=flags) if IS_WIN \
        else subprocess.Popen([str(interp), str(entry)], env=env)
    for _ in range(240):                       # up to ~2 min for first-run startup
        if _port_open():
            break
        if proc.poll() is not None:
            return proc.returncode or 1
        time.sleep(0.5)
    if entry.name == "meld_app.py":
        # DETACH. The launcher's job - virtual environment, dependencies, arnis binary - is
        # finished once the port answers, and waiting on the child is what kept a console
        # window pinned to the taskbar for the entire session. Meld lives in the tray from
        # here; quitting it is the tray's Quit item, not closing a window. On Windows the child
        # survives the parent exiting, so there is nothing to keep alive for.
        log("Meld is running in the tray. This window can close; "
            "quit from the tray icon (near the clock).")
        return 0
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass
    log("Meld is running. Close this window (or press Ctrl+C) to stop the server.")
    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        return 0


def check_only() -> int:
    """Report what's installed / missing WITHOUT installing or starting anything (meld_launch.py --check)."""
    ok = True
    log(f"Python {platform.python_version()} ({'ok' if sys.version_info >= (3, 9) else 'TOO OLD, need 3.9+'})")
    if sys.version_info < (3, 9):
        ok = False
    log(f"virtual env: {'present' if venv_python().exists() else 'not created yet (will be made on first run)'}")
    try:
        import flask  # noqa: F401
        log("core deps: importable in this interpreter")
    except Exception:  # noqa: BLE001
        log("core deps: not importable here (the venv installs them on first run)")
    p = arnis_present()
    if p:
        log(f"arnis binary: {p}")
    else:
        asset, _ = _arnis_asset()
        has_cargo = bool(shutil.which("cargo"))
        log(f"arnis binary: MISSING — would try to download '{asset}', else "
            f"{'build with cargo' if has_cargo else 'ask you to install Rust (no cargo found)'}")
        ok = False
    log(f"port {PORT}: {'in use (a Meld may already be running)' if _port_open() else 'free'}")
    log("READY - run this launcher with no arguments to start Meld." if ok
        else "Some pieces are missing; running the launcher normally will fetch/build them.")
    return 0 if ok else 1


def main() -> int:
    if "--check" in sys.argv or "--check-only" in sys.argv:
        return check_only()
    if sys.version_info < (3, 9):
        log(f"Python 3.9+ is required (found {platform.python_version()}).")
        return 1
    # UTF-8 mode for every interpreter Meld starts from here on. It has to travel as an
    # ENVIRONMENT variable, not as `-X utf8`: the venv re-exec below rebuilds argv from
    # scratch, so a `-X` flag given on the command line is silently dropped and the server
    # ends up back on the locale code page. Setting it in os.environ survives both the
    # execv and every subprocess.
    os.environ["PYTHONUTF8"] = "1"
    ensure_venv_and_deps()
    # Re-launch inside the venv so server.py imports the freshly installed dependencies.
    if not in_target_venv():
        os.execv(str(venv_python()), [str(venv_python()), str(HERE / "meld_launch.py")])
    ensure_arnis()
    return start_server()


if __name__ == "__main__":
    raise SystemExit(main())
