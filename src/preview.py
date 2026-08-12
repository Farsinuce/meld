"""Opens the small always-there preview as a real window, not a browser tab.

Chromium's `--app=` mode gives a window with no tab strip, no address bar and no bookmarks -
close enough to a native window that people do not think of it as a browser, and it costs
nothing: no pywebview, no WebKitGTK, no second UI toolkit to freeze and ship. Edge is present on
every Windows 11 machine, so the common case needs no install at all.

It runs against a private profile directory. Without one, `--app=` hands the URL to whatever
Chromium process is already running, which then ignores the window size and can put the preview
in a tab of the user's own browser - and closing their browser would close the preview with it.

Falls back to the default browser when no Chromium is found (a Mac with only Safari, a bare
Linux box); the preview page is built to be usable either way.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

from .paths import data_dir

WIDTH, HEIGHT = 430, 580


def _windows_candidates() -> list[str]:
    out = []
    for var in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if not base:
            continue
        out += [
            str(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
            str(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"),
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


def open_app_window(url: str, *, width: int = WIDTH, height: int = HEIGHT) -> bool:
    """Open `url` as a chrome-less window. True if a real app window was opened."""
    exe = find_chromium()
    if not exe:
        return open_in_browser(url)
    profile = data_dir() / "preview-profile"
    try:
        profile.mkdir(parents=True, exist_ok=True)
        subprocess.Popen([
            exe,
            f"--app={url}",
            f"--window-size={width},{height}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate,ChromeWhatsNewUI",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return open_in_browser(url)


def open_in_browser(url: str) -> bool:
    """The user's normal browser - used for the full UI, and as the preview's fallback."""
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def describe() -> str:
    exe = find_chromium()
    return f"app window via {Path(exe).name}" if exe else "default browser (no Chromium found)"
