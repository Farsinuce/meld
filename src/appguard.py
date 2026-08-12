"""Keeps other programs on the machine out of Meld's API.

Binding to 127.0.0.1 stops the network, not the browser. Any web page the user has open can
POST to http://127.0.0.1:5630/api/... - the browser sends the request happily, and this API
writes files, deletes worlds and launches processes. As a tab you open for ten minutes that is a
narrow window; as a tray app running all day it is a permanent one, so the daemon needs the
checks a long-lived local service normally has:

  Host check    rejects DNS rebinding. An attacker points evil.com at 127.0.0.1, so the request
                really does arrive here - but it arrives with `Host: evil.com`, and only
                localhost names are accepted.
  Origin check  rejects cross-site writes. Browsers always attach `Origin` to a cross-origin
                POST, so a page on any other site is refused. Costs the real UI nothing: its
                fetches are same-origin and pass untouched.
  Token         optional, off by default. Blocks *other native programs* on the machine, which
                send no Origin at all and are unaffected by the two checks above. The packaged
                app turns this on because the tray always opens the URL with the token in it;
                running `python server.py` by hand leaves it off so typing the address into a
                browser still works.

The token rides in a cookie after the first request, so the UI's plain `fetch('/api/...')` calls
keep working without a single change to index.html.
"""
from __future__ import annotations

import os
import secrets
from urllib.parse import urlsplit

from flask import g, jsonify, redirect, request

COOKIE = "meld_token"
HEADER = "X-Meld-Token"
QUERY = "t"

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def new_token() -> str:
    return secrets.token_urlsafe(24)


def _host_ok(host_header: str, port: int) -> bool:
    """`Host` must name this machine. Anything else means the request was aimed at a name that
    happens to resolve here - the DNS-rebinding shape."""
    if not host_header:
        return False
    h = host_header.strip()
    if h.startswith("["):                       # [::1]:5630
        name, _, p = h.rpartition("]")
        name, p = name + "]", p.lstrip(":")
    else:
        name, _, p = h.partition(":")
    if p and p != str(port):
        return False
    return name.lower() in _LOCAL_HOSTS


def _origin_ok(origin: str, port: int) -> bool:
    if not origin:
        return True                             # absent on same-origin GETs and native clients
    if origin == "null":
        return False                            # sandboxed iframe / file:// page
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https"):
        return False
    return _host_ok(parts.netloc, port)


def install(app, *, port: int, token: str = "", require_token: bool = False) -> None:
    """Wire the checks into a Flask app. Call once, before serving."""

    @app.before_request
    def _guard():                               # noqa: ANN202 - Flask hook
        if not _host_ok(request.headers.get("Host", ""), port):
            return jsonify({"ok": False, "error": "bad host"}), 403

        if request.method not in _SAFE_METHODS:
            if not _origin_ok(request.headers.get("Origin", ""), port):
                return jsonify({"ok": False,
                                "error": "cross-site request refused"}), 403

        if not require_token or not token:
            return None

        supplied = (request.args.get(QUERY) or request.headers.get(HEADER)
                    or request.cookies.get(COOKIE) or "")
        # compare_digest, not ==: a plain comparison returns early on the first wrong byte, which
        # leaks the token one character at a time to anything that can time the reply.
        if secrets.compare_digest(supplied, token):
            if request.args.get(QUERY):
                g.set_token_cookie = True
                # Land on a clean address so the token is not left in the address bar, in
                # history, or in the Referer of anything the page links to.
                if request.method == "GET" and request.path == "/":
                    g.strip_token_redirect = True
            return None
        return jsonify({"ok": False, "error": "unauthorized: open Meld from the tray icon"}), 403

    @app.after_request
    def _stamp(resp):                           # noqa: ANN202 - Flask hook
        if getattr(g, "set_token_cookie", False):
            resp.set_cookie(COOKIE, token, httponly=True, samesite="Strict", path="/")
        if getattr(g, "strip_token_redirect", False) and resp.status_code < 400:
            clean = redirect("/", code=302)
            clean.set_cookie(COOKIE, token, httponly=True, samesite="Strict", path="/")
            return clean
        return resp


def require_token_default() -> bool:
    """Token enforcement is opt-in via MELD_REQUIRE_TOKEN; the tray entry point sets it."""
    return (os.environ.get("MELD_REQUIRE_TOKEN") or "").strip().lower() in ("1", "true", "yes", "on")
