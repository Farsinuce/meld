"""Where ArnisXL reads its bundled files from, and where it writes its state to.

Two roots that are deliberately NOT the same thing:

  resource_dir()  READ-ONLY.  web/, assets/, site/, tree-packs/, cave-pack/ - everything that
                  ships inside the app and never changes at runtime.
  data_dir()      WRITABLE.   projects/, cache/, logs/ - everything the user creates.

Running from a source checkout the two are the same folder (the repo root), so an existing
install keeps its `projects/` and `cache/` exactly where they are and nothing moves. The split
only becomes visible once the app is frozen with PyInstaller, where `Path(__file__).parent`
points inside the unpacked bundle: that location is read-only, is a temp directory on some
platforms, and is wiped between runs. Writing 100 GB of tile cache in there would fail, or
worse, silently disappear.

Data-dir resolution order (first hit wins):
  1. $ARNISXL_DATA_DIR  (or the older $MELD_DATA_DIR)
  2. a pointer file `arnisxl-data.txt` next to the executable - one line, a path. This is how a
     portable install parks its 100 GB cache on a different drive without any env-var setup.
  3. source checkout  -> the repo root (today's layout, unchanged)
  4. frozen + the app folder is writable -> <app>/data  (the portable-ZIP case)
  5. frozen + read-only app folder -> the OS user-data directory

Note (4): a portable app extracted to Downloads or a USB stick keeps its data beside it, which
is what "portable" means. Extracted into Program Files, the write test fails and it falls back
to (5) instead of dying.

Stdlib only, no imports from the rest of the package: everything else imports this.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "ArnisXL"
POINTER_FILE = "arnisxl-data.txt"

_DATA_CACHE: Path | None = None


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle (ArnisXL.exe) rather than from source."""
    return bool(getattr(sys, "frozen", False))


def exe_dir() -> Path:
    """The folder a user would call "where the app is".

    Frozen: the directory holding ArnisXL.exe - NOT sys._MEIPASS, which is the unpacked
    payload. In onedir mode the exe sits next to _internal/; in onefile mode _MEIPASS is a
    temp dir that vanishes on exit, so a pointer file or data folder must never live there.
    Source: the repo root.
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_dir() -> Path:
    """Root of the read-only bundled files. Never write here."""
    if is_frozen():
        # _MEIPASS is where PyInstaller unpacked the bundle. It exists in both onedir and
        # onefile mode; falling back to the exe dir keeps this honest if that ever changes.
        return Path(getattr(sys, "_MEIPASS", None) or exe_dir()).resolve()
    return Path(__file__).resolve().parent.parent


def resource(*parts: str) -> Path:
    """A path inside the bundled files, e.g. resource("web", "index.html")."""
    return resource_dir().joinpath(*parts)


def _writable(p: Path) -> bool:
    """Can we actually create files under p? Asked by making the directory and writing a probe -
    os.access() lies on Windows (it reports the read-only ATTRIBUTE, not the ACL) and would
    happily green-light Program Files."""
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def _os_user_data_dir() -> Path:
    r"""Per-user data directory for this OS.

    Windows uses LOCALAPPDATA, never APPDATA: the roaming profile is synced to a domain/school
    server on managed machines, and a 100 GB tile cache in there is an incident, not a feature.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME.lower()


def _pointer_target() -> Path | None:
    """Read `arnisxl-data.txt` next to the executable, if present and non-empty."""
    try:
        f = exe_dir() / POINTER_FILE
        if not f.is_file():
            return None
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip().strip('"').strip("'")
            if line and not line.startswith("#"):
                return Path(line).expanduser()
    except Exception:
        pass
    return None


def data_dir() -> Path:
    """Writable root for projects/, cache/ and logs/. Created if missing. Cached per process."""
    global _DATA_CACHE
    if _DATA_CACHE is not None:
        return _DATA_CACHE

    candidate: Path | None = None
    for var in ("ARNISXL_DATA_DIR", "MELD_DATA_DIR"):
        env = (os.environ.get(var) or "").strip().strip('"').strip("'")
        if env:
            candidate = Path(env).expanduser()
            break
    if candidate is None:
        candidate = _pointer_target()
    if candidate is None:
        if not is_frozen():
            candidate = exe_dir()                       # source checkout: today's layout
        else:
            portable = exe_dir() / "data"
            candidate = portable if _writable(portable) else _os_user_data_dir()

    try:
        candidate = candidate.resolve()
    except Exception:
        pass
    try:
        candidate.mkdir(parents=True, exist_ok=True)
    except Exception:
        # An unusable explicit override (bad drive letter, revoked share) must not be fatal at
        # import time - fall back to the OS directory so the app still starts and can say so.
        candidate = _os_user_data_dir()
        candidate.mkdir(parents=True, exist_ok=True)
    _DATA_CACHE = candidate
    return candidate


def projects_root() -> Path:
    return data_dir() / "projects"


def cache_root() -> Path:
    """Shared tile/OSM cache root. $MELD_CACHE_DIR still wins - it is the documented escape
    hatch for parking the cache on a big drive and predates the data-dir pointer."""
    env = (os.environ.get("MELD_CACHE_DIR") or "").strip().strip('"').strip("'")
    if env:
        return Path(env).expanduser().resolve()
    return data_dir() / "cache"


def logs_dir() -> Path:
    d = data_dir() / "logs"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def bin_dir() -> Path:
    """Where a single-file build unpacks the arnis generator on first launch.

    It cannot stay inside the executable: arnis is a separate program, and the OS will only run
    a program that exists on disk. It cannot live in the PyInstaller payload either - that is a
    temp directory in onefile mode, wiped between runs, so the binary would be re-extracted (45
    MB) on every start. The data directory is stable and writable, which is what is needed.
    """
    d = data_dir() / "bin"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def user_assets_dir() -> Path:
    """Writable twin of the bundled assets/ - user-saved loot presets, generated caches. Read
    paths check here first, then fall back to the bundled read-only copy."""
    return data_dir() / "user-assets"


def describe() -> dict:
    """Resolved paths, for the banner / diagnostics / the About panel."""
    return {
        "frozen": is_frozen(),
        "app": str(exe_dir()),
        "resources": str(resource_dir()),
        "data": str(data_dir()),
        "projects": str(projects_root()),
        "cache": str(cache_root()),
        "logs": str(logs_dir()),
    }


def unpack_embedded_arnis() -> Path | None:
    """Write the bundled generator out of a single-file build. Returns its path, or None.

    Only does anything for a build made with --onefile-embed-arnis; for every other layout the
    binary is already a real file next to the app. Re-extracts when the sizes differ, which is
    how an upgraded ArnisXL replaces the generator it unpacked a version ago.
    """
    if not is_frozen():
        return None
    name = "arnis.exe" if sys.platform == "win32" else "arnis"
    src = resource_dir() / "arnis-bundled" / name
    if not src.is_file():
        return None
    dst = bin_dir() / name
    try:
        if dst.is_file() and dst.stat().st_size == src.stat().st_size:
            return dst
        import shutil
        shutil.copy2(src, dst)
        if sys.platform != "win32":
            dst.chmod(0o755)
        return dst
    except Exception:
        return None
