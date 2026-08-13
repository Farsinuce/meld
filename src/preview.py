"""Opens Meld's windows as application windows, not browser tabs.

Chromium's `--app=` mode gives a window with no tab strip, no address bar, no bookmarks and no
browser menu - close enough to a native window that people stop thinking of it as a browser. It
costs nothing to ship: no pywebview, no WebKitGTK, no second UI toolkit to freeze for three
platforms. Edge is on every Windows 11 machine, so the common case needs no install at all.

It runs against a private profile directory. Without one, `--app=` hands the URL to whatever
Chromium process is already running, which then ignores the window size and can put Meld in a
TAB of the user's own browser - and closing their browser would close Meld's window with it.
The private profile also means Meld's window never inherits their extensions, their zoom level
or their session.

The browser is still there for anyone who wants it: the tray has "Open in browser", and
MELD_UI=browser makes that the default everywhere.

Falls back to the default browser when no Chromium is found (a Mac with only Safari, a bare
Linux box); the UI is built to work either way.

This module is only about the FULL UI now. The compact view is src/statusbar.py, which is not a
browser window at all.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from .paths import data_dir

# The full UI: a map, a sidebar and a log. Smaller than this and the map is not worth having.
# (There is no second size any more - the compact view is src/statusbar.py, which is not a
# browser window at all.)
APP_SIZE = (1360, 880)


def _windows_candidates() -> list[str]:
    out = []
    for var in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if not base:
            continue
        out += [
            str(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
            str(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"),
            str(Path(base) / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe"),
        ]
    return out


def _mac_candidates() -> list[str]:
    return [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]


def find_chromium() -> str | None:
    """Path to a Chromium-family browser, or None."""
    override = (os.environ.get("MELD_BROWSER") or "").strip().strip('"')
    if override and Path(override).exists():
        return override
    if sys.platform == "win32":
        candidates = _windows_candidates()
    elif sys.platform == "darwin":
        candidates = _mac_candidates()
    else:
        candidates = []
        for name in ("google-chrome", "google-chrome-stable", "chromium",
                     "chromium-browser", "microsoft-edge", "brave-browser"):
            found = shutil.which(name)
            if found:
                candidates.append(found)
    for c in candidates:
        if c and Path(c).exists():
            return c
    return None


def prefers_browser() -> bool:
    """MELD_UI=browser turns every "open" into a normal tab, for people who want it there."""
    return (os.environ.get("MELD_UI") or "").strip().lower() in ("browser", "tab", "web")


def open_window(url: str, *, size: tuple[int, int] = APP_SIZE, profile: str = "app") -> bool:
    """Open `url` as an application window. True if a real app window was opened.

    `profile` names the profile directory, so the main window and the preview keep separate
    window sizes and positions instead of the second one inheriting the first one's geometry.
    """
    if prefers_browser():
        return open_in_browser(url)
    exe = find_chromium()
    if not exe:
        return open_in_browser(url)
    width, height = size
    profile_dir = data_dir() / "ui-profiles" / profile
    args = [
        exe,
        f"--app={url}",
        f"--window-size={width},{height}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        # Nothing here should nag: no "set me as default", no what's-new tab stealing focus
        # from a window the user opened to watch a render.
        "--disable-features=Translate,ChromeWhatsNewUI,GlobalMediaControls",
        "--disable-background-networking",
    ]
    if sys.platform.startswith("linux"):
        # WM_CLASS is how a Linux desktop matches a window to its .desktop file. Set it to the
        # same id install-shortcut.sh registers and the window gets Meld's icon in the dock and
        # in Alt-Tab instead of the browser's.
        args.append("--class=Meld")
    try:
        profile_dir.mkdir(parents=True, exist_ok=True)
        # Snapshot the existing windows BEFORE launching, so the icon fix can tell which window
        # is ours rather than branding any browser window that happens to be titled "Meld".
        before: set = set()
        if sys.platform == "win32":
            from . import winicon
            before = winicon.find_windows("Meld")
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if sys.platform == "win32":
            _brand_window_async(before)
        return True
    except Exception:
        return open_in_browser(url)


def _brand_window_async(before: set) -> None:
    """Replace the browser's window icon with Meld's, once the window exists.

    A `--app=` window is still a browser window, so Windows draws the BROWSER's icon in its title
    bar and on its taskbar button - which is why it reads as "a stray Edge window" rather than as
    Meld. A favicon does not change that; only an installed web app or WM_SETICON does, and the
    former needs the user to click Install.

    Fire-and-forget on a thread: the window takes a moment to appear, and the tray menu handler
    that called us must not sit and wait for it.
    """
    from . import winicon
    from .paths import resource

    ico = resource("assets", "icons", "meld.ico")
    if not ico.is_file():
        return
    threading.Thread(target=winicon.brand_new_windows, args=(before, ico),
                     kwargs={"title_prefix": "Meld"}, daemon=True).start()


def open_main_window(url: str) -> bool:
    """Show the full Meld UI, reusing the window that is already open.

    Every click used to start another browser process against the same profile, so a few taps on
    the status bar left a stack of identical Meld windows. There is only ever one UI worth
    having: if a window exists, it is raised instead.
    """
    if sys.platform == "win32":
        try:
            from . import winicon
            if winicon.focus_existing("Meld"):
                return True
        except Exception:
            pass
    return _open_main_window(url)


def _open_main_window(url: str) -> bool:
    """The full Meld UI in its own window."""
    return open_window(url, size=APP_SIZE, profile="app")


def open_in_browser(url: str) -> bool:
    """The user's normal browser - the explicit "Open in browser" path, and the fallback."""
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def describe() -> str:
    if prefers_browser():
        return "browser (MELD_UI=browser)"
    exe = find_chromium()
    return f"app window via {Path(exe).name}" if exe else "default browser (no Chromium found)"
