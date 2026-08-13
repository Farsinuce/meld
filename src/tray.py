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


def use_dark_menus() -> bool:
    """Ask Windows to draw this process's menus dark.

    pystray builds a NATIVE Win32 popup menu (CreatePopupMenu/TrackPopupMenu), which Windows
    paints itself - none of its colours are reachable from Python, so a tray menu comes up as a
    bright white slab hanging off a black status bar.

    What does work is telling Windows the whole process prefers dark mode. `SetPreferredAppMode`
    and `FlushMenuThemes` live in uxtheme.dll and are exported BY ORDINAL ONLY (135 and 136) -
    they have no public names, which is why this is done with GetProcAddress on an ordinal rather
    than a normal ctypes call. Every widely-used dark-mode app on Windows does exactly this.

    Undocumented means it can stop working, so the whole thing is best-effort: if the ordinals
    move or the OS is too old (pre-1809), the menu stays light and nothing else changes.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        uxtheme = ctypes.WinDLL("uxtheme")
        # GetProcAddress needs the ordinal as a pointer-sized value, not a string.
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetProcAddress.restype = ctypes.c_void_p
        kernel32.GetProcAddress.argtypes = [wintypes.HMODULE, ctypes.c_char_p]

        def by_ordinal(ordinal: int, restype, argtypes):
            addr = kernel32.GetProcAddress(uxtheme._handle, ctypes.c_char_p(ordinal))
            if not addr:
                return None
            return ctypes.WINFUNCTYPE(restype, *argtypes)(addr)

        PREFERRED_APP_MODE_FORCE_DARK = 2
        set_preferred = by_ordinal(135, ctypes.c_int, (ctypes.c_int,))
        flush = by_ordinal(136, None, ())
        if set_preferred is None:
            return False
        set_preferred(PREFERRED_APP_MODE_FORCE_DARK)
        if flush is not None:
            flush()
        return True
    except Exception:
        return False


def available() -> bool:
    """Can a tray icon actually be shown here?"""
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception:
        return False
    return True


def _pip(img):
    """Stamp a small badge in the corner: "there is something new", without a notification.

    Drawn rather than shipped as a second asset so it works on the procedural fallback icon too.
    A ring of background colour separates it from whatever is underneath, because a bare dot on a
    gold icon is invisible at the 16 px the tray actually renders.
    """
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    s = img.width
    r = max(3, s // 5)
    box = (s - r * 2 - 1, s - r * 2 - 1, s - 1, s - 1)
    d.ellipse(box, fill=(20, 20, 22, 255))
    d.ellipse((box[0] + 2, box[1] + 2, box[2] - 2, box[3] - 2), fill=(247, 220, 149, 255))
    return img


def _icon_image(size: int = 64, pip: bool = False):
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
                        img = img.resize((size, size), Image.LANCZOS)
                        return _pip(img) if pip else img
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
    return _pip(img) if pip else img


class Tray:
    def __init__(self, url: str, *, on_quit=None, token: str = "") -> None:
        self.url = url.rstrip("/")
        self.token = token
        self._on_quit = on_quit
        self._icon = None
        self._statusbar = None          # the frameless HUD, when the user has it up
        self._sb_lock = threading.Lock()
        # Filled by the tooltip poll. Empty means the menu row stays hidden and the icon stays
        # plain, which is what a fresh launch and an offline machine should both look like.
        self._update: dict = {}

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
        """Meld's full UI in its own window.

        Deliberately NOT what clicking the tray icon does. A stray click on a tray icon should
        not throw a 1360x880 window over whatever you were working on; the click toggles the
        status bar, and the big window is a menu item you choose on purpose.
        """
        preview.open_main_window(self._authed("/"))

    def open_in_browser(self, *_):
        """For anyone who would rather have it in a tab - devtools, extensions, a second screen
        full of tabs. Same server, same page; only the frame differs."""
        preview.open_in_browser(self._authed("/"))

    def statusbar_running(self) -> bool:
        p = self._statusbar
        return p is not None and p.poll() is None

    def toggle_statusbar(self, *_):
        """Show or hide the frameless status strip.

        Launched as our own executable with --statusbar rather than as an embedded window: see
        src/statusbar.py for why it has to be a separate process (tkinter and the tray both want
        the main thread; macOS insists on it for AppKit).
        """
        with self._sb_lock:
            if self.statusbar_running():
                try:
                    self._statusbar.terminate()
                except Exception:
                    pass
                self._statusbar = None
                return
            self._spawn_statusbar()

    def show_statusbar(self, *_):
        """Start the status bar if it is not already up.

        Serialised, not merely guarded: the check and the spawn are two steps, and the startup
        timer and a tray click can hit them at the same moment - which is how two bars got
        launched in the same second, both seeing `self._statusbar` still None.
        """
        with self._sb_lock:
            if self.statusbar_running():
                return
            self._spawn_statusbar()

    def _spawn_statusbar(self) -> None:
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
            print(f"[tray] status bar started (pid {self._statusbar.pid})")
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
        # Cached off the same poll the tooltip already makes, so the menu's visible= gate and the
        # icon pip both read it without a request of their own.
        self._update = d.get("update") or {}
        if d.get("active"):
            base = f"{APP_NAME} — {d.get('percent', 0)}% ({d.get('done')}/{d.get('total')})"
        else:
            base = f"{APP_NAME} — idle"
        if self._update.get("state") == "available" and self._update.get("latest"):
            base += f" · update {self._update['latest']} available"
        return base

    def run(self) -> None:
        """Show the icon and block until Quit. MUST be called on the main thread."""
        import pystray
        from pystray import MenuItem as Item

        # Before the menu exists: the preference is process-wide and is read when Windows paints.
        use_dark_menus()

        # `default=True` is what a plain left-click on the icon runs. It toggles the status bar,
        # NOT the full UI: the overlay is the thing you flick in and out of view all day, and a
        # mis-click that opens a 1360x880 window over your work is the wrong cost for that.
        menu = pystray.Menu(
            Item(lambda _: "Hide status bar" if self.statusbar_running() else "Show status bar",
                 self.toggle_statusbar, default=True),
            pystray.Menu.SEPARATOR,
            Item("Open Meld", self.open_ui),
            Item("Open in browser", self.open_in_browser),
            # Same dynamic-label pattern as the toggle above, plus a visible gate so the row is
            # absent rather than greyed when there is nothing to update to. It opens the UI's
            # update panel; nothing here starts a download.
            Item(lambda _: f"Update to {self._update.get('latest', '')}…",
                 self.open_ui,
                 visible=lambda _: self._update.get("state") == "available"
                 and bool(self._update.get("latest"))),
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
                was = self._update.get("state")
                self._icon.title = self._title()
                # Repaint only on the transition. The icon is the one surface that is always on
                # screen, so a pip appearing there is the least intrusive way to say "there is
                # something new" - and, unlike a balloon notification, it does not interrupt.
                # Left-click still toggles the status bar: the notice must not cost the click.
                if self._update.get("state") != was:
                    self._icon.icon = _icon_image(pip=self._update.get("state") == "available")
                    self._icon.update_menu()
            except Exception:
                return
