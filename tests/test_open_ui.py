"""/api/open-ui must open an AUTHENTICATED window.

The bug this pins down: the status bar authenticates with the X-Meld-Token header, so there was
no cookie and no ?t= on the request to copy into the URL being opened. The window opened at a
bare address and the page it loaded was

    {"error": "unauthorized: open Meld from the tray icon", "ok": false}

The server knows its own token; it never needed the caller to hand it back.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def srv(monkeypatch):
    """The real server module, with window-opening stubbed so nothing appears on screen."""
    import server as server_mod
    from src import preview

    opened = {}
    monkeypatch.setattr(preview, "open_main_window",
                        lambda url: opened.__setitem__("window", url) or True)
    monkeypatch.setattr(preview, "open_in_browser",
                        lambda url: opened.__setitem__("browser", url) or True)
    monkeypatch.setattr(server_mod, "_UI_TOKEN", "test-token-123")
    return server_mod, server_mod.app.test_client(), opened


def test_opened_url_carries_the_session_token(srv):
    _mod, client, opened = srv
    r = client.post("/api/open-ui", headers={"Host": "127.0.0.1:5630"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert "?t=test-token-123" in opened["window"], \
        "the window would load unauthenticated and show the 'unauthorized' JSON"


def test_header_token_is_not_required_to_be_echoed(srv):
    """The caller sending its token as a header (as the status bar does) is enough; the server
    does not depend on a cookie or a query parameter being present."""
    _mod, client, opened = srv
    r = client.post("/api/open-ui",
                    headers={"Host": "127.0.0.1:5630", "X-Meld-Token": "test-token-123"})
    assert r.status_code == 200
    assert "?t=test-token-123" in opened["window"]


def test_browser_variant_is_authenticated_too(srv):
    _mod, client, opened = srv
    r = client.post("/api/open-ui?browser=1", headers={"Host": "127.0.0.1:5630"})
    assert r.status_code == 200
    assert "?t=test-token-123" in opened["browser"]
    assert "window" not in opened


def test_no_token_configured_opens_a_plain_url(srv, monkeypatch):
    """`python server.py` runs with enforcement off and no token; the URL must simply have no
    query string rather than a dangling '?t='."""
    mod, client, opened = srv
    monkeypatch.setattr(mod, "_UI_TOKEN", "")
    r = client.post("/api/open-ui", headers={"Host": "127.0.0.1:5630"})
    assert r.status_code == 200
    assert opened["window"].endswith("/")
