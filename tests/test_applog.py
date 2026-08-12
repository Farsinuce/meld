"""Log streams and the on-demand console (src/applog.py).

The reason this module exists: a windowed PyInstaller build has `sys.stdout = None`, so the
first `print()` in the codebase raises AttributeError and the app dies before its tray icon
appears. Everything here guards that, plus one regression that was introduced and caught while
building the single-file variant.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import applog  # noqa: E402


@pytest.fixture(autouse=True)
def _restore(monkeypatch, tmp_path):
    """Keep the real stdout and the real log file out of this."""
    monkeypatch.setattr(applog, "logs_dir", lambda: tmp_path)
    real_out, real_err = sys.stdout, sys.stderr
    applog.close()
    applog._file = None
    applog._path = None
    yield
    applog.close()
    applog._file = None
    applog._path = None
    sys.stdout, sys.stderr = real_out, real_err


def test_setup_gives_a_windowed_build_working_streams(monkeypatch, tmp_path):
    """The whole point: with stdout None, printing must still work rather than raise."""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(sys, "__stdout__", None)
    monkeypatch.setattr(sys, "__stderr__", None)

    p = applog.setup()
    print("hello from a windowed build")        # would be AttributeError without setup()
    sys.stdout.flush()

    assert p.is_file()
    assert "hello from a windowed build" in p.read_text(encoding="utf-8")


def test_output_reaches_both_console_and_file(monkeypatch, tmp_path):
    import io

    class Console(io.StringIO):
        encoding = "utf-8"

        def isatty(self):
            return True

    console = Console()
    monkeypatch.setattr(sys, "__stdout__", console)
    monkeypatch.setattr(sys, "__stderr__", console)
    applog.setup()
    print("both places")
    sys.stdout.flush()

    assert "both places" in console.getvalue()
    assert "both places" in applog.path().read_text(encoding="utf-8")


def test_attach_console_leaves_a_redirected_stdout_alone(monkeypatch):
    """Regression: attaching a console rebound the streams onto it, so
    `ArnisXL.exe --check > out.txt` wrote to a console window and left the file empty.

    A windowed build DOES get a real stdout handle when the caller redirects it, so the presence
    of a usable stream - not the absence of a console - is what decides.
    """
    import io

    class Redirected(io.StringIO):
        encoding = "utf-8"

        def fileno(self):
            return 1

    target = Redirected()
    monkeypatch.setattr(sys, "__stdout__", target)
    monkeypatch.setattr(sys, "stdout", target)

    assert applog._has_real_stdout() is True
    assert applog.attach_console() is True
    assert sys.stdout is target, "attach_console must not steal a redirected stdout"


def test_has_real_stdout_false_when_windowed(monkeypatch):
    monkeypatch.setattr(sys, "__stdout__", None)
    assert applog._has_real_stdout() is False


def test_rotation_keeps_the_current_log_small(monkeypatch, tmp_path):
    big = tmp_path / applog.LOG_NAME
    big.write_bytes(b"x" * (applog.MAX_BYTES + 10))
    applog.setup()
    assert (tmp_path / "arnisxl.1.log").is_file(), "the oversized log should have been rotated"
    assert applog.path().stat().st_size < applog.MAX_BYTES
