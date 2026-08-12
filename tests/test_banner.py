"""The console banner (src/banner.py).

A banner is decoration; the only way it can matter is by breaking startup. Two ways it could:
writing block characters to a console that cannot encode them (UnicodeEncodeError on a legacy
Windows code page), or emitting ANSI escapes to something that prints them literally. Both are
checked here, plus the alignment of the ASCII fallback, since a crooked box is the one bug that
never shows up on the developer's machine.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import banner  # noqa: E402


class _Stream(io.StringIO):
    """A stdout stand-in with a chosen encoding and tty-ness."""

    def __init__(self, encoding="utf-8", tty=False):
        super().__init__()
        self._encoding = encoding
        self._tty = tty

    @property
    def encoding(self):
        return self._encoding

    def isatty(self):
        return self._tty


def test_ascii_fallback_box_is_square():
    lines = banner.PLAIN.strip("\n").split("\n")
    assert len({len(x) for x in lines}) == 1, "the fallback box is not rectangular"
    assert lines[0].startswith("  +") and lines[0].endswith("+")


def test_block_art_used_on_utf8(monkeypatch):
    monkeypatch.setattr(sys, "stdout", _Stream("utf-8"))
    assert "█" in banner.art()


def test_plain_art_used_on_legacy_codepage(monkeypatch):
    """cp1252 - the default on a Western Windows console - has no U+2588, so printing the block
    art there raises UnicodeEncodeError. (cp437, the old DOS page, *does* have it: that page was
    built for box drawing. The check has to be a real encode attempt, not a guess about which
    code pages look old.)"""
    monkeypatch.setattr(sys, "stdout", _Stream("cp1252"))
    assert "█" not in banner.art()
    assert "M E L D" in banner.art()


def test_render_is_encodable_on_legacy_codepage(monkeypatch):
    """The whole banner, not just the art: the subtitle's separator is also non-ASCII."""
    monkeypatch.setattr(sys, "stdout", _Stream("cp1252"))
    text = banner.render(version="1.8.3", arnis="3.0.6", url="http://127.0.0.1:5630")
    text.encode("cp1252")           # raises if any character slipped through


def test_no_ansi_when_not_a_tty(monkeypatch):
    """Output redirected to a file or a pipe must not collect escape sequences."""
    monkeypatch.setattr(sys, "stdout", _Stream("utf-8", tty=False))
    assert "\x1b[" not in banner.render(url="http://127.0.0.1:5630")


def test_no_color_env_respected(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(sys, "stdout", _Stream("utf-8", tty=True))
    assert "\x1b[" not in banner.render(url="http://127.0.0.1:5630")


def test_show_never_raises(monkeypatch):
    class Exploding(io.StringIO):
        encoding = "utf-8"

        def write(self, s):
            raise OSError("the console went away")

    monkeypatch.setattr(sys, "stdout", Exploding())
    banner.show(version="1.0")      # must swallow it: a banner may not stop the app booting
