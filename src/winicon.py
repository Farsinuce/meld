"""Puts Meld's icon on Meld's window (Windows).

A Chromium `--app=` window is still a browser window: Windows draws the BROWSER's icon in its
title bar and on its taskbar button, so Meld looks like a stray Edge window. The page favicon
does not change this - a favicon only becomes the window icon for an *installed* web app, which
is a deliberate user action we cannot perform for them.

What does change it is WM_SETICON, which is how every native app sets its own window icon. So
the window is found after it opens and handed the same .ico the executable and the shortcut use.

Finding it is done by DIFFERENCE, not by title: matching on the title alone would also hit a
browser window the user happens to have open on a page called "Meld". The set of candidate
windows is captured before the browser is launched, and only windows that appear afterwards are
touched.
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

# Chromium's top-level window class, unchanged for over a decade and shared by Chrome, Edge and
# Brave (all are the same browser underneath).
CHROMIUM_CLASS = "Chrome_WidgetWin_1"

WM_SETICON = 0x0080
ICON_SMALL, ICON_BIG = 0, 1
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040

_ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def available() -> bool:
    return sys.platform == "win32"


def _user32():
    u = ctypes.windll.user32
    u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.SendMessageW.restype = wintypes.LPARAM
    return u


def _window_titles(hwnd) -> tuple[str, str]:
    u = ctypes.windll.user32
    buf = ctypes.create_unicode_buffer(512)
    u.GetWindowTextW(hwnd, buf, 512)
    title = buf.value
    cls = ctypes.create_unicode_buffer(256)
    u.GetClassNameW(hwnd, cls, 256)
    return title, cls.value


def find_windows(title_prefix: str = "") -> set[int]:
    """Visible Chromium top-level windows, optionally filtered by title prefix."""
    if not available():
        return set()
    u = ctypes.windll.user32
    out: set[int] = set()

    def cb(hwnd, _lparam):
        try:
            if not u.IsWindowVisible(hwnd):
                return True
            title, cls = _window_titles(hwnd)
            if cls != CHROMIUM_CLASS or not title:
                return True
            if title_prefix and not title.startswith(title_prefix):
                return True
            out.add(int(hwnd))
        except Exception:
            pass
        return True

    try:
        u.EnumWindows(_ENUM_PROC(cb), 0)
    except Exception:
        pass
    return out


def focus_window(hwnd: int) -> bool:
    """Bring an existing window to the front, un-minimising it first.

    SetForegroundWindow alone is unreliable: Windows refuses foreground changes from a process
    that does not own the current foreground window, and silently flashes the taskbar button
    instead. Restoring first, then asking, covers the common cases without the AttachThreadInput
    trickery that fights the OS for focus.
    """
    if not available():
        return False
    try:
        u = ctypes.windll.user32
        SW_RESTORE = 9
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, SW_RESTORE)
        u.SetForegroundWindow(hwnd)
        u.BringWindowToTop(hwnd)
        return True
    except Exception:
        return False


def focus_existing(title_prefix: str = "Meld") -> bool:
    """Focus an already-open Meld window if there is one. True if something was focused."""
    for hwnd in find_windows(title_prefix):
        if focus_window(hwnd):
            return True
    return False


def _load_icon(path: Path, size: int):
    try:
        return ctypes.windll.user32.LoadImageW(
            None, str(path), IMAGE_ICON, size, size,
            LR_LOADFROMFILE | (LR_DEFAULTSIZE if size == 0 else 0))
    except Exception:
        return None


def set_icon(hwnd: int, ico: Path) -> bool:
    """Give one window Meld's icon, small (title bar) and big (Alt-Tab, taskbar)."""
    if not available() or not ico.is_file():
        return False
    u = _user32()
    small = _load_icon(ico, 16)
    big = _load_icon(ico, 32)
    if not small and not big:
        return False
    try:
        if small:
            u.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, small)
        if big:
            u.SendMessageW(hwnd, WM_SETICON, ICON_BIG, big)
        return True
    except Exception:
        return False


def brand_new_windows(before: set[int], ico: Path, *, title_prefix: str = "Meld",
                      timeout: float = 20.0, poll: float = 0.25) -> int:
    """Wait for windows that were not open before, and brand them. Returns how many.

    Runs on a background thread: the browser takes a second or two to map its window, and
    blocking the caller for that would stall the tray's menu handler.

    It keeps watching for the whole timeout rather than stopping at the first hit - Chromium
    sometimes maps a window and then re-creates it while the page settles, which would leave the
    second one wearing the browser's icon.
    """
    if not available() or not ico.is_file():
        return 0
    branded: set[int] = set()
    deadline = time.time() + timeout
    while time.time() < deadline:
        for hwnd in find_windows(title_prefix) - before - branded:
            if set_icon(hwnd, ico):
                branded.add(hwnd)
        time.sleep(poll)
    return len(branded)
