"""The tray icon and its menu.

Everything the user needs while a render runs, without a window: open the UI, open the compact
preview, stop the job, read the log, find the data folder, quit for real.

Two platform facts shape this file:

* **macOS demands the main thread.** pystray drives NSApplication, and AppKit refuses to run
  anywhere but thread 0. So the tray owns the main thread and the HTTP server is the one pushed
  into a background thread - the opposite of the obvious arrangement.
* **Linux has no reliable tray.** GNOME removed the status area; pystray falls back to AppIndicator,
  which needs an extension the user may not have. `available()` says so honestly instead of the
  app appearing to start and then being invisible and unkillable.

Menu actions talk to the running server over HTTP rather than reaching into its objects: the same
calls the UI makes, already thread-safe, already the tested path.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

from . import applog, preview
from .paths import data_dir, resource

APP_NAME = "Meld"


def available() -> bool:
    """Can a tray icon actually be shown here?"""
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception:
        return False
    return True


def _icon_image(size: int = 64):
    """The tray image: the shipped icon if present, else a drawn placeholder.

    The placeholder is not decoration - it is what keeps the tray working on a build where the
    icon asset is missing or failed to convert, instead of the app starting with no way to reach
    it. A 16 px tray slot cannot show a wordmark anyway, so the fallback is a simple mark.
    """
    from PIL import Image, ImageDraw
    for name in ("meld.png", "icon.png", "meld_icon.png"):
        for p in (resource("assets", "icons", name), resource("web", name)):
            try:
                if p.is_file():
                    img = Image.open(p).convert("RGBA")
                    if img.width == img.height:
                        return img.resize((size, size), Image.LANCZOS)
            except Exception:
                pass
    # The Meld block reduced to three flat faces - the shipped icon's silhouette, drawn from
    # scratch so a build that lost its assets still has a findable tray icon rather than a blank
    # slot. Gold reads against the blues and greys of a normal tray.
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def p(x, y):
        return (s * x, s * y)

    d.polygon([p(.50, .06), p(.94, .31), p(.50, .56), p(.06, .31)], fill=(247, 220, 149, 255))
    d.polygon([p(.06, .31), p(.50, .56), p(.50, .94), p(.06, .69)], fill=(227, 164, 23, 255))
    d.polygon([p(.94, .31), p(.94, .69), p(.50, .94), p(.50, .56)], fill=(169, 114, 15, 255))
    return img


class Tray:
    def __init__(self, url: str, *, on_quit=None, token: str = "") -> None:
        self.url = url.rstrip("/")
        self.token = token
        self._on_quit = on_quit
        self._icon = None
        self._statusbar = None          # the frameless HUD, when the user has it up

    # ── talking to our own server ────────────────────────────────────────────
    def _post(self, path: str) -> dict:
        req = urllib.request.Request(self.url + path, data=b"", method="POST")
        req.add_header("Content-Type", "application/json")
        # Same-origin so the guard's Origin check passes, plus the token for when enforcement
        # is on (the tray is a native client - it gets no cookie for free).
        req.add_header("Origin", self.url)
        if self.token:
            req.add_header("X-Meld-Token", self.token)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read() or b"{}")
        except Exception as ex:
            print(f"[tray] {path} failed: {ex}")
            return {}

    def _get(self, path: str) -> dict:
        req = urllib.request.Request(self.url + path)
        if self.token:
            req.add_header("X-Meld-Token", self.token)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read() or b"{}")
        except Exception:
            return {}

    def _authed(self, path: str = "/") -> str:
        return f"{self.url}{path}?t={self.token}" if self.token else f"{self.url}{path}"

    # ── menu actions ─────────────────────────────────────────────────────────
    def open_ui(self, *_):
        """The default action (left-click / double-click): Meld in its own window."""
        preview.open_main_window(self._authed("/"))

    def open_in_browser(self, *_):
        """For anyone who would rather have it in a tab - devtools, extensions, a second screen
        full of tabs. Same server, same page; only the frame differs."""
        preview.open_in_browser(self._authed("/"))

    def open_preview(self, *_):
        preview.open_app_window(self._authed("/mini"))

    def open_console(self, which: str = "meld"):
        """Open the preview on a console tab.

        The consoles live INSIDE the preview window rather than in a spawned terminal. A
        `powershell -NoExit Get-Content -Wait` tail works, but it puts a console window back in
        the taskbar - the exact thing this app exists to avoid - and it can only ever show the
        log file, never the raw generator output, which is filtered out before anything reaches
        that file's curated feed.
        """
        preview.open_app_window(self._authed("/mini") + f"#{which}")

    def toggle_statusbar(self, *_):
        """Show or hide the frameless status strip.

        Launched as our own executable with --statusbar rather than as an embedded window: see
        src/statusbar.py for why it has to be a separate process (tkinter and the tray both want
        the main thread; macOS insists on it for AppKit).
        """
        proc = self._statusbar
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
            self._statusbar = None
            return
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--statusbar"]
        else:
            # From source, prefer the windowless interpreter so no console flashes behind it.
            exe = Path(sys.executable)
            pyw = exe.with_name("pythonw.exe")
            cmd = [str(pyw if pyw.is_file() else exe),
                   str(Path(__file__).resolve().parent.parent / "meld_app.py"), "--statusbar"]
        cmd += ["--url", self.url]
        if self.token:
            cmd += ["--token", self.token]
        try:
            self._statusbar = subprocess.Popen(cmd)
        except Exception as ex:                                   # noqa: BLE001
            print(f"[tray] could not start the status bar: {ex}")

    def stop_render(self, *_):
        self._post("/api/stop")

    def open_log_file(self, *_):
        """Hand the log file to whatever the OS opens .log/.txt with. No console involved."""
        p = applog.path()
        try:
            if sys.platform == "win32":
                os.startfile(str(p))                  # noqa: S606 - a user-initiated open
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-t", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p)])
        except Exception as ex:
            print(f"[tray] could not open {p}: {ex}")
            self.open_folder(p.parent)

    def open_folder(self, target: Path | None = None, *_):
        d = Path(target) if target else data_dir()
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", str(d)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(d)])
            else:
                subprocess.Popen(["xdg-open", str(d)])
        except Exception as ex:
            print(f"[tray] could not open {d}: {ex}")

    def quit(self, *_):
        proc = self._statusbar
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()          # the HUD is ours; it goes when we go
            except Exception:
                pass
        if self._on_quit:
            threading.Thread(target=self._on_quit, daemon=True).start()
        if self._icon is not None:
            self._icon.stop()

    # ── the icon ─────────────────────────────────────────────────────────────
    def _title(self) -> str:
        d = self._get("/api/mini")
        if not d:
            return APP_NAME
        if d.get("active"):
            return f"{APP_NAME} — {d.get('percent', 0)}% ({d.get('done')}/{d.get('total')})"
        return f"{APP_NAME} — idle"

    def run(self) -> None:
        """Show the icon and block until Quit. MUST be called on the main thread."""
        import pystray
        from pystray import MenuItem as Item

        menu = pystray.Menu(
            Item("Open Meld", self.open_ui, default=True),
            Item("Status bar", self.toggle_statusbar),
            Item("Preview…", self.open_preview),
            Item("Open in browser", self.open_in_browser),
            pystray.Menu.SEPARATOR,
            Item("Meld console", lambda *_: self.open_console("meld")),
            Item("Arnis console", lambda *_: self.open_console("arnis")),
            pystray.Menu.SEPARATOR,
            Item("Stop render", self.stop_render),
            Item("Open log file", self.open_log_file),
            Item("Data folder", lambda *_: self.open_folder()),
            pystray.Menu.SEPARATOR,
            Item("Quit Meld", self.quit),
        )
        self._icon = pystray.Icon(APP_NAME, _icon_image(), APP_NAME, menu)
        # Refresh the tooltip so hovering shows live progress without opening anything.
        threading.Thread(target=self._tooltip_loop, daemon=True).start()
        self._icon.run()

    def _tooltip_loop(self) -> None:
        import time
        while True:
            time.sleep(5)
            if self._icon is None:
                return
            try:
                self._icon.title = self._title()
            except Exception:
                return
