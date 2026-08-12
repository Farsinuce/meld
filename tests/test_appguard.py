"""Localhost hardening (src/appguard.py).

Scenario being defended: ArnisXL sits in the tray all day. The user browses the web. A page they
visit runs `fetch('http://127.0.0.1:5630/api/project/delete', {method:'POST'})`, or resolves its
own domain to 127.0.0.1 to dodge the origin rules. Neither may reach a handler.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from flask import Flask, jsonify

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import appguard  # noqa: E402

PORT = 5630
TOKEN = "test-token-value"


def make_app(*, require_token=False, token=""):
    app = Flask(__name__)

    @app.route("/")
    def index():
        return "ui"

    @app.route("/api/thing", methods=["GET", "POST"])
    def thing():
        return jsonify({"ok": True})

    appguard.install(app, port=PORT, token=token, require_token=require_token)
    return app.test_client()


# ── host check (DNS rebinding) ────────────────────────────────────────────────
@pytest.mark.parametrize("host", ["127.0.0.1:5630", "localhost:5630", "[::1]:5630"])
def test_localhost_hosts_allowed(host):
    assert make_app().get("/api/thing", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("host", ["evil.com", "evil.com:5630", "192.168.1.20:5630"])
def test_foreign_host_header_refused(host):
    """The rebinding case: the packet really does arrive here, the Host header gives it away."""
    assert make_app().get("/api/thing", headers={"Host": host}).status_code == 403


def test_wrong_port_in_host_refused():
    assert make_app().get("/api/thing", headers={"Host": "127.0.0.1:9999"}).status_code == 403


# ── origin check (cross-site writes) ──────────────────────────────────────────
def test_cross_site_post_refused():
    r = make_app().post("/api/thing", headers={"Host": "127.0.0.1:5630",
                                               "Origin": "https://evil.com"})
    assert r.status_code == 403


def test_same_origin_post_allowed():
    r = make_app().post("/api/thing", headers={"Host": "127.0.0.1:5630",
                                               "Origin": "http://127.0.0.1:5630"})
    assert r.status_code == 200


def test_post_without_origin_allowed():
    """curl, the launcher's own health checks, and older same-origin form posts send no Origin.
    The token is what covers native clients; refusing here would break normal use for nothing."""
    r = make_app().post("/api/thing", headers={"Host": "127.0.0.1:5630"})
    assert r.status_code == 200


def test_null_origin_refused():
    r = make_app().post("/api/thing", headers={"Host": "127.0.0.1:5630", "Origin": "null"})
    assert r.status_code == 403


def test_cross_site_get_allowed_by_origin_rule():
    """Only writes are origin-checked; a cross-origin GET is stopped by the token, not here."""
    r = make_app().get("/api/thing", headers={"Host": "127.0.0.1:5630",
                                              "Origin": "https://evil.com"})
    assert r.status_code == 200


# ── token ─────────────────────────────────────────────────────────────────────
def test_token_off_by_default():
    assert make_app().get("/api/thing", headers={"Host": "127.0.0.1:5630"}).status_code == 200


def test_token_required_blocks_unauthenticated():
    c = make_app(require_token=True, token=TOKEN)
    assert c.get("/api/thing", headers={"Host": "127.0.0.1:5630"}).status_code == 403


def test_token_in_query_then_cookie():
    """The tray opens /?t=<token>; the cookie it sets carries every later fetch()."""
    c = make_app(require_token=True, token=TOKEN)
    r = c.get(f"/?t={TOKEN}", headers={"Host": "127.0.0.1:5630"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/")   # token stripped from the URL
    assert c.get("/api/thing", headers={"Host": "127.0.0.1:5630"}).status_code == 200


def test_token_via_header():
    c = make_app(require_token=True, token=TOKEN)
    r = c.get("/api/thing", headers={"Host": "127.0.0.1:5630", appguard.HEADER: TOKEN})
    assert r.status_code == 200


def test_wrong_token_refused():
    c = make_app(require_token=True, token=TOKEN)
    r = c.get("/api/thing", headers={"Host": "127.0.0.1:5630", appguard.HEADER: "nope"})
    assert r.status_code == 403


def test_require_token_default_reads_env(monkeypatch):
    monkeypatch.delenv("ARNISXL_REQUIRE_TOKEN", raising=False)
    assert appguard.require_token_default() is False
    monkeypatch.setenv("ARNISXL_REQUIRE_TOKEN", "1")
    assert appguard.require_token_default() is True
