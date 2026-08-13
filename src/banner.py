"""The Meld console banner.

Two versions of the wordmark. The block-drawing one needs a console that can render U+2588 and
the box-drawing set; Windows Terminal, the macOS terminal and every Linux terminal can, but a
legacy conhost window on a raster font shows a column of question marks, and a redirected pipe
on a non-UTF-8 code page raises UnicodeEncodeError outright. So the encoding is inspected first
and the plain-ASCII version is used when there is any doubt - a banner is decoration, and
decoration that can crash the process on boot is a bug.

Colour goes through ANSI escapes with the same treatment: virtual-terminal processing is enabled
explicitly on Windows (it is on by default in Windows Terminal, off in some older hosts), and if
that fails the escapes are dropped rather than printed literally.
"""
from __future__ import annotations

import os
import sys

BLOCK = r"""
███╗   ███╗███████╗██╗     ██████╗
████╗ ████║██╔════╝██║     ██╔══██╗
██╔████╔██║█████╗  ██║     ██║  ██║
██║╚██╔╝██║██╔══╝  ██║     ██║  ██║
██║ ╚═╝ ██║███████╗███████╗██████╔╝
╚═╝     ╚═╝╚══════╝╚══════╝╚═════╝
"""

PLAIN = r"""
  +-------------------------------+
  |     M E L D                   |
  +-------------------------------+
"""

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
GREEN = "\x1b[38;5;77m"
GREY = "\x1b[38;5;245m"


def _enable_vt() -> bool:
    """Turn on ANSI escape handling for this console. True if colour is safe to emit."""
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout or not sys.stdout.isatty():
        return False
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.GetStdHandle(-11)              # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        return bool(k32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING))
    except Exception:
        return False


def _unicode_ok() -> bool:
    """Can this stream actually encode the block characters?"""
    enc = getattr(sys.stdout, "encoding", None) or ""
    if not enc:
        return False
    try:
        "█╗".encode(enc)
        return True
    except Exception:
        return False


def art() -> str:
    return BLOCK if _unicode_ok() else PLAIN


def render(*, version: str = "", arnis: str = "", url: str = "",
           data_dir: str = "", extra: list[str] | None = None) -> str:
    """The full banner as one string, ready to print."""
    colour = _enable_vt()

    def c(text: str, code: str) -> str:
        return f"{code}{text}{RESET}" if colour else text

    lines = [c(art().strip("\n"), GREEN)]
    sub = "   Arnis at scale"
    if version:
        sub += f"  ·  {version}" if _unicode_ok() else f"  |  {version}"
    if arnis:
        sub += f"  ·  arnis {arnis}" if _unicode_ok() else f"  |  arnis {arnis}"
    lines.append(c(sub, BOLD))
    if url:
        lines.append(c(f"   {url}", GREY))
    if data_dir:
        lines.append(c(f"   data: {data_dir}", DIM if colour else GREY))
    for line in (extra or []):
        lines.append(c(f"   {line}", GREY))
    return "\n".join(lines) + "\n"


def show(**kw) -> None:
    """Print the banner, and never let it be the reason the app fails to start."""
    try:
        sys.stdout.write(render(**kw))
        sys.stdout.flush()
    except Exception:
        try:
            print("Meld")
        except Exception:
            pass
