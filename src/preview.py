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
Linux box). Both pages are built to work either way.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

from .paths import data_dir

# The preview: a panel meant to sit in a corner of the screen.
PREVIEW_SIZE = (430, 640)
# The full UI: a map, a sidebar and a log. Smaller than this and the map is not worth having.
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
    try:
        profile_dir.mkdir(parents=True, exist_ok=True)
        subprocess.Popen([
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
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return open_in_browser(url)


def open_app_window(url: str, *, width: int | None = None, height: int | None = None) -> bool:
    """The compact preview panel."""
    size = (width or PREVIEW_SIZE[0], height or PREVIEW_SIZE[1])
    return open_window(url, size=size, profile="preview")


def open_main_window(url: str) -> bool:
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
