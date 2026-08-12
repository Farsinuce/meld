"""Stop the machine sleeping while a render is running.

A country-scale render is hours of work with no keyboard or mouse activity, so every default
power policy treats the machine as idle and suspends it. The render does not fail cleanly - the
worker pool wakes up to half-written region files and cells that timed out - and the user comes
back to a job that stopped twenty minutes after they walked away.

Each OS is asked in its own way, and none of them are fatal if unavailable:
  Windows  SetThreadExecutionState, a flag on the calling thread - no child process, nothing to
           clean up if we crash (the flag dies with the process).
  macOS    `caffeinate -s`, a child process held for the duration.
  Linux    `systemd-inhibit`, likewise. Without systemd there is no portable answer, so the
           call is a no-op rather than a guess.

The display is deliberately left alone: keeping the screen lit for six hours would burn a
laptop's battery and an OLED panel to prevent nothing.
"""
from __future__ import annotations

import subprocess
import sys
import threading

# Windows SetThreadExecutionState flags
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

_lock = threading.Lock()
_depth = 0                       # nested acquire() calls; release only on the way back to zero
_proc: subprocess.Popen | None = None
_reason = "Meld is generating a world"


def _begin() -> None:
    global _proc
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        except Exception:
            pass
    elif sys.platform == "darwin":
        try:
            _proc = subprocess.Popen(["caffeinate", "-s"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            _proc = None
    else:
        try:
            _proc = subprocess.Popen(
                ["systemd-inhibit", "--what=sleep:idle", "--who=Meld",
                 f"--why={_reason}", "--mode=block", "sleep", "infinity"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            _proc = None


def _end() -> None:
    global _proc
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        except Exception:
            pass
        return
    p, _proc = _proc, None
    if p is not None:
        try:
            p.terminate()
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass


def acquire() -> None:
    """Ask the OS to stay awake. Reference-counted: safe to call per queued job."""
    global _depth
    with _lock:
        _depth += 1
        if _depth == 1:
            _begin()


def release() -> None:
    """Give the request back. The machine may sleep once the last holder releases."""
    global _depth
    with _lock:
        if _depth == 0:
            return
        _depth -= 1
        if _depth == 0:
            _end()


def reset() -> None:
    """Drop every outstanding request at once.

    A run that is stopped part-way never reaches the "run finished" path that would release it,
    so without this the machine would refuse to sleep until Meld was quit - a power bug that
    outlives the job that caused it. Called from Stop and from shutdown.
    """
    global _depth
    with _lock:
        if _depth == 0:
            return
        _depth = 0
        _end()


def active() -> bool:
    with _lock:
        return _depth > 0


class keep_awake:
    """`with keep_awake():` around a long job."""

    def __enter__(self):
        acquire()
        return self

    def __exit__(self, *exc):
        release()
        return False


def describe() -> str:
    if not active():
        return "sleep allowed"
    how = {"win32": "SetThreadExecutionState", "darwin": "caffeinate"}.get(sys.platform,
                                                                          "systemd-inhibit")
    return f"sleep blocked ({how})"
