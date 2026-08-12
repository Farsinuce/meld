"""Process-wide policy for every native child ArnisXL starts.

Two problems, both of which only show up once the app runs without a console:

1. **Console flashing.** A windowed process that starts a console program gets a brand new
   console window for it. ArnisXL launches one `arnis` per cell, so a 3000-cell render would
   pop and close 3000 black windows over the user's desktop. CREATE_NO_WINDOW stops that.

2. **Orphans.** Quit from the tray - or crash, or get killed by the OS - and the `arnis`
   children keep running: eight processes, every core pinned, no window to close them from, and
   the next launch fights them for the same files. On Windows a Job Object with
   KILL_ON_JOB_CLOSE fixes this at the kernel level: whatever kills us, Windows kills them too,
   including a hard `TerminateProcess` that runs no cleanup code of ours. On POSIX the children
   go into their own process group so the whole tree can be signalled at once.

This is applied by wrapping `subprocess.Popen.__init__` once at startup rather than by editing
every call site. That is a deliberate choice: it is a *process-wide policy*, it must hold for
the two dozen places that already spawn something, and - the real reason - it must hold for the
next one somebody adds without reading this file.

Shell-open helpers (`explorer`, `open`, `xdg-open`) are exempt from adoption: they hand off to
an already-running desktop process and exit, and adopting them would mean quitting ArnisXL
slammed shut the file manager window the user just opened.
"""
from __future__ import annotations

import atexit
import os
import subprocess
import sys
import threading
import time

CREATE_NO_WINDOW = 0x08000000

# Started to show the user something, not to compute. Never adopted into the job.
_SHELL_OPENERS = {"explorer", "explorer.exe", "open", "xdg-open", "cmd", "cmd.exe"}

_installed = False
_no_window = False
_job = None                       # Windows HANDLE to the job object
_lock = threading.Lock()
_children: list = []              # live Popen handles, newest last


def has_console() -> bool:
    """Windows: is a console attached? False in a `--noconsole` build, True from a terminal."""
    if os.name != "nt":
        return sys.stdout is not None and sys.stdout.isatty()
    try:
        import ctypes
        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:
        return True


def _create_job():
    """A Job Object whose children die with us. None if the OS refuses (very old Windows, or a
    parent job that forbids nesting) - in that case we still have the explicit kill path."""
    try:
        import ctypes
        import ctypes.wintypes as wt

        k32 = ctypes.windll.kernel32
        k32.CreateJobObjectW.restype = wt.HANDLE
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                        ("WriteOperationCount", ctypes.c_ulonglong),
                        ("OtherOperationCount", ctypes.c_ulonglong),
                        ("ReadTransferCount", ctypes.c_ulonglong),
                        ("WriteTransferCount", ctypes.c_ulonglong),
                        ("OtherTransferCount", ctypes.c_ulonglong)]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wt.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wt.DWORD),
                        ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
                        ("PriorityClass", wt.DWORD),
                        ("SchedulingClass", wt.DWORD)]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                        ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                                           ctypes.byref(info), ctypes.sizeof(info)):
            return None
        return job
    except Exception:
        return None


def _adopt(proc) -> None:
    """Put a freshly started child into the job so it cannot outlive us."""
    if _job is None:
        return
    try:
        import ctypes
        handle = getattr(proc, "_handle", None)
        if handle:
            ctypes.windll.kernel32.AssignProcessToJobObject(_job, int(handle))
    except Exception:
        pass


def _basename(args) -> str:
    try:
        first = args[0] if isinstance(args, (list, tuple)) else str(args).split()[0]
        return os.path.basename(str(first)).lower()
    except Exception:
        return ""


def install(*, no_window: bool | None = None) -> None:
    """Apply the policy. Idempotent; call once at startup, before anything spawns."""
    global _installed, _no_window, _job
    if _installed:
        return
    _installed = True
    _no_window = (os.name == "nt" and not has_console()) if no_window is None else bool(no_window)
    if os.name == "nt":
        _job = _create_job()

    original_init = subprocess.Popen.__init__

    def patched_init(self, args, *a, **kw):
        opener = _basename(args) in _SHELL_OPENERS
        if os.name == "nt":
            if _no_window and "creationflags" not in kw:
                kw["creationflags"] = CREATE_NO_WINDOW
        else:
            # Own process group => one killpg reaches the child and anything it started.
            kw.setdefault("start_new_session", True)
        original_init(self, args, *a, **kw)
        if opener:
            return
        if os.name == "nt":
            _adopt(self)
        with _lock:
            _children.append(self)
            if len(_children) > 64:              # drop finished handles, keep the list small
                _children[:] = [p for p in _children if p.poll() is None]

    subprocess.Popen.__init__ = patched_init      # type: ignore[method-assign]
    atexit.register(kill_all)


def kill_all(timeout: float = 5.0) -> int:
    """Stop every child we started. Returns how many were still alive when asked.

    Order matters: ask politely first so `arnis` can close the region files it has open (a
    half-written .mca is a corrupt chunk, not a lost one), then insist.
    """
    with _lock:
        procs = [p for p in _children if p.poll() is None]
        _children.clear()
    n = len(procs)
    for p in procs:
        try:
            if os.name == "nt":
                p.terminate()
            else:
                os.killpg(os.getpgid(p.pid), 15)    # SIGTERM to the whole group
        except Exception:
            pass
    deadline = time.time() + timeout
    for p in procs:
        try:
            p.wait(timeout=max(0.0, deadline - time.time()))
        except Exception:
            pass
    for p in procs:
        if p.poll() is None:
            try:
                if os.name == "nt":
                    p.kill()
                else:
                    os.killpg(os.getpgid(p.pid), 9)  # SIGKILL
            except Exception:
                pass
    # Last word on Windows: anything the loop missed (a grandchild, a process that ignored
    # terminate) is inside the job, and closing the job takes the lot.
    if os.name == "nt" and _job is not None:
        try:
            import ctypes
            ctypes.windll.kernel32.TerminateJobObject(_job, 1)
        except Exception:
            pass
    return n


def live_children() -> int:
    with _lock:
        return sum(1 for p in _children if p.poll() is None)
