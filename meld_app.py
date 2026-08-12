#!/usr/bin/env python3
"""Meld - the background app.

Double-click it and nothing opens: it goes to the tray, the server comes up behind it, and the
browser lands on the UI. Close the window and the render keeps going, because the window was
never the thing doing the work.

Layout of the process:

    main thread   the tray icon (macOS refuses to run AppKit anywhere else)
    worker thread the HTTP server + the arnis worker pool

Two entry points are built from this one file. `Meld.exe` is frozen with --noconsole, so it
has no console and no taskbar button - just the tray icon. `meld.exe` keeps its console and
prints the banner, for anyone who wants to watch it work or script it. Same code, one flag apart.

    python meld_app.py                 tray + server
    python meld_app.py --no-tray       server only, banner in the terminal
    python meld_app.py --console       open a console window too (single-file builds)
    python meld_app.py --check         report what is installed and exit

Quitting is the tray's Quit item. Closing the browser tab, the preview window or a console does
not stop it - none of them are doing the work, and a render that died because someone tidied
their taskbar would be the whole point of this app thrown away.
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from src import applog, banner, childproc, paths, preview, tray  # noqa: E402
from src.appguard import new_token  # noqa: E402
from src.single_instance import SingleInstance, running_url  # noqa: E402

DEFAULT_PORT = 5630
PORT_TRIES = 20


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) != 0


def pick_port(preferred: int = DEFAULT_PORT) -> int:
    """First free port at or after `preferred`.

    Reached only when we already hold the single-instance lock, so a busy 5630 is somebody
    else's program, not another Meld. Refusing to start because an unrelated app took the
    port would be a bad trade for a tool people leave running all day.
    """
    for i in range(PORT_TRIES):
        if _port_free(preferred + i):
            return preferred + i
    return preferred


def version() -> str:
    """Read the version out of the changelog heading; cosmetic, never fatal."""
    for name in ("CHANGELOG.md",):
        try:
            for line in (paths.resource_dir() / name).read_text(encoding="utf-8",
                                                                errors="replace").splitlines():
                line = line.strip()
                if line.startswith("## "):
                    return line[3:].split()[0].strip("[]v")
        except Exception:
            pass
    return ""


def check() -> int:
    """`--check`: say what is present without starting or installing anything."""
    import platform
    # A single-file build is windowed, so it has no stdout to print this report to. Borrow the
    # terminal it was launched from (or open one), and mirror to the log file either way -
    # a diagnostic command that produces nothing visible is worse than no diagnostic command.
    if paths.is_frozen():
        applog.attach_console()
        applog.setup()
    import server  # noqa: PLC0415 - imported late; it is the expensive import
    print(f"Meld {version() or '(unknown version)'}")
    print(f"python      {platform.python_version()} ({'frozen' if paths.is_frozen() else 'source'})")
    for k, v in paths.describe().items():
        print(f"{k:<11} {v}")
    exe = server.resolve_arnis_exe()
    print(f"arnis       {exe or 'NOT FOUND'}")
    print(f"tray        {'available' if tray.available() else 'unavailable (pystray/Pillow missing)'}")
    print(f"preview     {preview.describe()}")
    print(f"port {DEFAULT_PORT}   {'free' if _port_free(DEFAULT_PORT) else 'in use'}")
    return 0 if exe else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--check" in argv:
        return check()
    want_tray = "--no-tray" not in argv and tray.available()

    # UTF-8 everywhere, for this process and every child. Without it a single accented place
    # name in arnis output decodes against the machine's code page and kills a cell.
    os.environ.setdefault("PYTHONUTF8", "1")

    # Before anything else: a windowed build has no stdout at all, so print() would raise on
    # the very first line. This gives it one, and gives the console view something to show.
    log_path = applog.setup()
    if "--console" in argv:
        applog.attach_console()

    # A single-file build carries the generator inside itself; write it out once so the OS has
    # a real file to execute. A no-op for every other layout.
    paths.unpack_embedded_arnis()

    inst = SingleInstance()
    if not inst.acquire():
        # Already running. Open that copy instead of dying on a busy port - a second
        # double-click should feel like "bring it up", not like an error.
        other = running_url()
        print(f"Meld is already running: {other or 'http://127.0.0.1:5630'}")
        if other:
            preview.open_in_browser(other)
        return 0

    try:
        port = pick_port(int(os.environ.get("PORT") or DEFAULT_PORT))
        token = new_token()
        url = f"http://127.0.0.1:{port}"
        # The tray always opens URLs carrying the token, so enforcement costs the user nothing
        # here and shuts out other native programs on the machine.
        os.environ.setdefault("MELD_REQUIRE_TOKEN", "1")
        childproc.install()

        banner.show(version=version(), url=url, data_dir=str(paths.data_dir()),
                    extra=[f"log: {log_path}"] +
                          (["tray icon active — right-click it for controls"] if want_tray
                           else ["no tray (headless) — Ctrl+C to stop"]))

        import server  # noqa: PLC0415 - after the banner, so the slow import is visible as a pause

        opened = threading.Event()

        def on_ready(u: str) -> None:
            if opened.is_set():
                return
            opened.set()
            if "--no-open" not in argv:
                preview.open_in_browser(f"{u}/?t={token}")

        def serve() -> None:
            try:
                server.run_server(port, token=token, require_token=True, on_ready=on_ready)
            except Exception as ex:                       # noqa: BLE001
                print(f"[meld] server stopped: {type(ex).__name__}: {ex}")
            finally:
                if want_tray and t is not None and t._icon is not None:
                    try:
                        t._icon.stop()                    # server died -> take the tray with it
                    except Exception:
                        pass

        def shutdown() -> None:
            print("[meld] shutting down…")
            try:
                server.stop_server()
            except Exception:
                pass
            childproc.kill_all()

        t = tray.Tray(url, on_quit=shutdown, token=token) if want_tray else None
        worker = threading.Thread(target=serve, name="meld-server", daemon=True)
        worker.start()

        if t is not None:
            t.run()             # blocks on the main thread until Quit
            shutdown()
            worker.join(timeout=10)
        else:
            try:
                while worker.is_alive():
                    time.sleep(0.5)
            except KeyboardInterrupt:
                shutdown()
                worker.join(timeout=10)
        return 0
    finally:
        childproc.kill_all()
        inst.release()
        applog.close()


if __name__ == "__main__":
    raise SystemExit(main())
