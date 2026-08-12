"""Give the app a log file, and give it a stdout that exists.

A windowed build has **no standard streams**: PyInstaller's `--noconsole` mode leaves
`sys.stdout` and `sys.stderr` set to None, so the first `print()` anywhere in the codebase - and
this one prints a great deal, from the banner to every arnis line - raises AttributeError and
takes the app down before the tray icon appears. Handing those attributes a real writer is not a
nicety, it is what makes a windowed build possible at all.

The same writer doubles as the log file behind the tray's "Show log", which is the only way to
see what happened when there is no console to look at.

Rotation is a single size check at startup rather than a logging handler: nothing here needs
per-record filtering or levels, and a rotating handler would add a lock on the hot path that
every arnis output line crosses.
"""
from __future__ import annotations

import io
import sys
import threading
import time
from pathlib import Path

from .paths import logs_dir

MAX_BYTES = 4 * 1024 * 1024
KEEP = 3
LOG_NAME = "arnisxl.log"

_lock = threading.Lock()
_file = None
_path: Path | None = None


class _Tee(io.TextIOBase):
    """Writes to the console (when there is one) and to the log file (always)."""

    def __init__(self, console, fh):
        self._console = console
        self._fh = fh

    def write(self, s):                       # noqa: D102
        if self._console is not None:
            try:
                self._console.write(s)
            except Exception:
                pass
        with _lock:
            try:
                self._fh.write(s)
                self._fh.flush()
            except Exception:
                pass
        return len(s)

    def flush(self):                          # noqa: D102
        for target in (self._console, self._fh):
            if target is not None:
                try:
                    target.flush()
                except Exception:
                    pass

    def isatty(self):                         # noqa: D102
        # Reports the console's answer so the banner still colours itself when one is attached,
        # and stays plain when output is only going to a file.
        try:
            return bool(self._console and self._console.isatty())
        except Exception:
            return False

    @property
    def encoding(self):                       # noqa: D102
        try:
            return self._console.encoding if self._console else "utf-8"
        except Exception:
            return "utf-8"


def _rotate(path: Path) -> None:
    try:
        if not path.exists() or path.stat().st_size < MAX_BYTES:
            return
        for i in range(KEEP - 1, 0, -1):
            older, newer = path.with_suffix(f".{i + 1}.log"), path.with_suffix(f".{i}.log")
            if newer.exists():
                newer.replace(older)
        path.replace(path.with_suffix(".1.log"))
    except Exception:
        pass


def path() -> Path:
    return _path or (logs_dir() / LOG_NAME)


def setup() -> Path:
    """Install the tee on stdout/stderr and return the log file path. Idempotent."""
    global _file, _path
    if _file is not None:
        return path()
    p = logs_dir() / LOG_NAME
    _rotate(p)
    try:
        fh = open(p, "a", encoding="utf-8", errors="replace", buffering=1)
    except Exception:
        return p                                # read-only data dir: keep whatever streams exist
    _file, _path = fh, p
    fh.write(f"\n===== ArnisXL started {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    sys.stdout = _Tee(sys.__stdout__, fh)
    sys.stderr = _Tee(sys.__stderr__, fh)
    return p


def close() -> None:
    global _file
    fh, _file = _file, None
    if fh is not None:
        try:
            fh.flush()
            fh.close()
        except Exception:
            pass
