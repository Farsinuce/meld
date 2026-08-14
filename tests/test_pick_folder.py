"""Browse must never turn a stray line of child output into a folder path.

The bug, reported from a shipped exe: the .pbf folder field came back containing

    Meld is already running: http://127.0.0.1:5630/?t=0zxDLQ2tiFpv-jQza0fzCP9xg-bRXVdZ

Frozen, sys.executable is Meld.exe rather than a Python interpreter. The route passed it
`-c <tkinter source>`; the bootloader ignored -c and started a SECOND Meld, which found the
single-instance lock held, printed that line and exited 0. The route then took the last line of
stdout as the chosen folder - so every Browse button in the exe was broken, and the session token
was printed into a UI field and screenshotted onto a public Discord.

Two independent defences, one test each: spawn the right thing when frozen, and accept a path
only from a sentinel-prefixed line so no other output can ever qualify.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402

HDR = {"Host": "127.0.0.1:5630", "Origin": "http://127.0.0.1:5630"}


class _Out:
    def __init__(self, stdout):
        self.stdout, self.stderr, self.returncode = stdout, "", 0


@pytest.fixture
def client():
    return server.app.test_client()


def _run(monkeypatch, stdout):
    seen = {}

    def fake(cmd, **kw):
        seen["cmd"] = cmd
        return _Out(stdout)

    monkeypatch.setattr(server.subprocess, "run", fake)
    return seen


def test_the_already_running_line_is_never_a_path(client, monkeypatch):
    """The exact reported failure, verbatim."""
    _run(monkeypatch, "Meld is already running: http://127.0.0.1:5630/?t=SECRETTOKEN\n")
    r = client.post("/api/pick-folder", json={"title": "x"}, headers=HDR)
    assert r.status_code == 200
    assert r.get_json()["path"] == "", "a status message must not become a folder"


def test_a_token_can_never_reach_the_field(client, monkeypatch):
    """Whatever a child prints, nothing unprefixed is passed through - so a leaked token cannot
    be rendered into the UI even if some future code path prints one."""
    _run(monkeypatch, "http://127.0.0.1:5630/?t=SECRETTOKEN\nwarning: something\n")
    body = r"{}".format(client.post("/api/pick-folder", json={}, headers=HDR).get_data(as_text=True))
    assert "SECRETTOKEN" not in body


def test_a_real_pick_is_returned(client, monkeypatch):
    _run(monkeypatch, f"some noise\n{server.PICK_SENTINEL}C:/Users/me/pbf\n")
    assert client.post("/api/pick-folder", json={}, headers=HDR).get_json()["path"] \
        == "C:/Users/me/pbf"


def test_noise_after_the_answer_does_not_win(client, monkeypatch):
    """Scanned in reverse for the sentinel, not for the last line - a trailing warning must not
    displace a real answer."""
    _run(monkeypatch, f"{server.PICK_SENTINEL}C:/picked\nBye: some trailing chatter\n")
    assert client.post("/api/pick-folder", json={}, headers=HDR).get_json()["path"] == "C:/picked"


def test_cancelling_gives_an_empty_path(client, monkeypatch):
    _run(monkeypatch, f"{server.PICK_SENTINEL}\n")
    assert client.post("/api/pick-folder", json={}, headers=HDR).get_json()["path"] == ""


def test_frozen_runs_a_mode_of_the_app_not_a_python_c(client, monkeypatch):
    """The root cause. Frozen, sys.executable is Meld.exe: handing it -c starts a second Meld."""
    monkeypatch.setattr(server, "is_frozen", lambda: True)
    seen = _run(monkeypatch, f"{server.PICK_SENTINEL}C:/x\n")
    client.post("/api/pick-folder", json={"title": "Pick"}, headers=HDR)
    assert "-c" not in seen["cmd"], "a frozen build must never be passed -c"
    assert "--pick-folder" in seen["cmd"]


def test_source_still_uses_the_interpreter(client, monkeypatch):
    """From source sys.executable really is python, and -c is the right call - the fix must not
    regress the path that always worked."""
    monkeypatch.setattr(server, "is_frozen", lambda: False)
    seen = _run(monkeypatch, f"{server.PICK_SENTINEL}C:/x\n")
    client.post("/api/pick-folder", json={}, headers=HDR)
    assert "-c" in seen["cmd"]


def test_the_title_is_data_not_code(client, monkeypatch):
    """A hostile title must reach the dialog as an argument, never as source.

    Sanitising a string before pasting it into code you are about to run is a defence that has
    to keep being right every time someone edits the template. Passing it as argv cannot be got
    wrong, so that is what the generated program reads.
    """
    monkeypatch.setattr(server, "is_frozen", lambda: False)
    seen = _run(monkeypatch, f"{server.PICK_SENTINEL}\n")
    client.post("/api/pick-folder", json={"title": "x'); import os; os.system('calc'); ('"},
                headers=HDR)
    src = seen["cmd"][2]                       # the -c program itself
    assert "os.system" not in src, "the title must not appear in the executed source at all"
    assert "sys.argv" in src, "the title has to be read as an argument"
    assert len(seen["cmd"]) == 4, "and passed as one"
