"""One Meld at a time, and a way for the second launch to find the first.

A background tray app gets launched again by accident constantly - double-clicked twice, started
from a shortcut while it is already running in the tray, opened at login while a manual copy is
up. Without a guard the second process races the first for port 5630, loses, and dies with a
stack trace; worse, if the port were free it would run a second worker pool over the same project
folder and both would write the same cells.

The lock is an OS file lock held for the whole process lifetime, not a PID file: a PID file left
behind by a crash (or a hard power-off mid-render, which is exactly the case Meld is built to
survive) locks the user out until they find and delete it. An OS lock is released by the kernel
when the holder dies, however it dies.

Alongside it sits session.json, which the second launch reads to open the browser at the copy
that is already running instead of just refusing.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from .paths import data_dir

LOCK_NAME = "meld.lock"
SESSION_NAME = "session.json"


def lock_path() -> Path:
    return data_dir() / LOCK_NAME


def session_path() -> Path:
    return data_dir() / SESSION_NAME


class SingleInstance:
    """Hold the lock, or report that someone else has it.

    Usage:
        inst = SingleInstance()
        if not inst.acquire():
            other = read_session()   # -> open other["url"] and exit
    """

    def __init__(self, name: str = LOCK_NAME) -> None:
        self._fh = None
        self._name = name

    def acquire_waiting(self, seconds: float) -> bool:
        """acquire(), but keep trying while the previous instance shuts down.

        This exists for the update hand-off, and the hand-off only works in one order. A new
        build started while the old one is still alive does NOT take the lock: it sees the lock
        held, opens a browser at the OLD instance and exits 0 - looking like a successful update
        that changed nothing. The old process therefore has to quit first, which it cannot do
        *and then* spawn anything. So the new build is started first and waits here for the lock
        the old one is about to release.

        Bounded, and falls through to a normal "already running" if the wait runs out - a hung
        old instance must not leave the new one spinning forever.
        """
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            if self.acquire():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.25)

    def acquire(self) -> bool:
        try:
            path = data_dir() / self._name
            path.parent.mkdir(parents=True, exist_ok=True)
            # Opened r+ (not w) so an existing lock file is never truncated before we know
            # whether we can actually take the lock.
            fh = open(path, "a+")
            if sys.platform == "win32":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False                     # someone else holds it
        except Exception:
            # No locking primitive at all (exotic filesystem): degrade to "allowed" rather than
            # refusing to start. A duplicate instance is a worse failure than no guard, but
            # refusing to launch on a network share would be worse still.
            return True
        self._fh = fh
        return True

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            fh.close()
        except Exception:
            pass
        if self._name == LOCK_NAME:
            # Only the app itself publishes a session; a secondary holder (the status bar) must
            # not delete the running app's session file when it exits.
            try:
                session_path().unlink(missing_ok=True)
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()


def write_session(*, port: int, url: str, token: str = "") -> None:
    """Publish where this instance is listening, for the next launch to find."""
    p = session_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "pid": os.getpid(), "port": port, "url": url,
            "token": token, "started": int(time.time()),
        }, indent=1), encoding="utf-8")
        if sys.platform != "win32":
            # The token is in here; keep it out of other users' reach on a shared machine.
            os.chmod(p, 0o600)
    except Exception:
        pass


def read_session() -> dict | None:
    try:
        return json.loads(session_path().read_text(encoding="utf-8"))
    except Exception:
        return None


def running_url() -> str | None:
    """URL of the already-running instance, token included so the browser lands authenticated."""
    s = read_session()
    if not s or not s.get("url"):
        return None
    url = str(s["url"])
    tok = s.get("token")
    return f"{url}/?t={tok}" if tok else url
