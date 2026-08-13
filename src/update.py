"""Is there a newer Meld than the one running?

One server-side checker, four surfaces. The web UI, the status bar, the tray menu and the tray
icon all read the answer out of /api/status, which every one of them already polls - so nothing
here adds a poll loop, and the four surfaces cannot disagree with each other about the version.

What this module deliberately does NOT do: download or install anything. Telling a user a new
version exists is safe and reversible; overwriting a running application is neither, and it needs
a published checksum, a smoke gate and a rollback path before it is worth shipping. The button
this feeds opens the release page.

Rate limits shape the design. Unauthenticated GitHub allows 60 requests per hour per IP, shared
with every other tool on that machine, and Meld already spends that budget elsewhere. So: one
check ~10 s after boot on a daemon thread, the answer cached on disk for 24 h, and an ETag on the
revalidation so a check that finds nothing new costs no quota at all. A failed check is cached
too, briefly - an offline laptop must not retry every time a surface repaints.

Stdlib only. This runs inside a frozen bundle where adding a dependency means rebuilding.
"""
from __future__ import annotations

import json
import platform
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .paths import build_info, cache_root, is_frozen

REPO = "Teddy563/meld"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"

TTL_OK = 24 * 3600          # a good answer is fresh for a day
TTL_FAIL = 3600             # a failed one is retried in an hour, not on the next repaint
BOOT_DELAY = 10.0           # never on the startup path; the UI must come up first

_lock = threading.Lock()
_state: dict = {}


# ── version comparison ────────────────────────────────────────────────────────────────────────

def parse_version(text: str) -> tuple[int, ...]:
    """"meld-v1.8.10" -> (1, 8, 10). Unparseable -> ().

    Numeric per component, never a string compare: "1.8.10" sorts BEFORE "1.8.9" as text, so a
    string compare would announce an update to an older build for one release out of every ten.
    """
    m = re.search(r"(\d+(?:\.\d+)*)", text or "")
    if not m:
        return ()
    return tuple(int(p) for p in m.group(1).split("."))


def is_newer(latest: str, current: str) -> bool:
    """Strictly newer, and only when BOTH parse. An unreadable version on either side means we
    say nothing - a false 'update available' that never resolves is worse than silence."""
    lv, cv = parse_version(latest), parse_version(current)
    if not lv or not cv:
        return False
    n = max(len(lv), len(cv))
    return lv + (0,) * (n - len(lv)) > cv + (0,) * (n - len(cv))


# ── which download belongs to this machine ────────────────────────────────────────────────────

def platform_tag() -> tuple[str, str, str]:
    """(os, arch, extension) as packaging/build.py spells them in the archive name."""
    mach = (platform.machine() or "").lower()
    arch = "arm64" if mach in ("arm64", "aarch64") else "x64"
    if sys.platform == "win32":
        return "win", arch, ".zip"
    if sys.platform == "darwin":
        return "mac", arch, ".tar.gz"
    return "linux", arch, ".tar.gz"


def pick_asset(assets: list[dict]) -> dict | None:
    """The archive for this OS and CPU, by name.

    Exact tag match first: `Meld-1.8.5-win-x64.zip`. The fallback exists because v1.8.4 published
    its Windows archive as `Meld-1.8.zip` - Path.with_suffix() had eaten the version, OS and arch
    - so an updater that only matched the documented scheme would find nothing at all on Windows
    and report "no download for your platform" against a release that plainly has one. Matching
    on extension alone is enough to disambiguate there, since exactly one .zip is published.
    """
    osname, arch, ext = platform_tag()
    named = [a for a in assets if a.get("name", "").endswith(f"-{osname}-{arch}{ext}")]
    if named:
        return named[0]
    loose = [a for a in assets
             if a.get("name", "").startswith("Meld-") and a.get("name", "").endswith(ext)]
    return loose[0] if len(loose) == 1 else None


# ── disk cache ────────────────────────────────────────────────────────────────────────────────

def _cache_file() -> Path:
    return cache_root() / "update.json"


def _read_cache() -> dict:
    try:
        d = json.loads(_cache_file().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_cache(d: dict) -> None:
    try:
        f = _cache_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(d, indent=1), encoding="utf-8")
    except Exception:
        pass                      # a read-only cache dir must not break the check


# ── the check ─────────────────────────────────────────────────────────────────────────────────

def _fetch(etag: str = "") -> tuple[dict | None, str, bool]:
    """(release json, etag, not_modified). Raises nothing - network failure returns (None, '', False)."""
    req = urllib.request.Request(API, headers={
        "User-Agent": "meld-update-check",
        "Accept": "application/vnd.github+json",
    })
    if etag:
        req.add_header("If-None-Match", etag)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read()), r.headers.get("ETag", ""), False
    except urllib.error.HTTPError as e:
        if e.code == 304:
            # Revalidated: unchanged, and on GitHub a 304 does not spend rate-limit quota.
            return None, etag, True
        return None, "", False
    except Exception:
        return None, "", False


def check(force: bool = False) -> dict:
    """{state, current, latest, url, asset, size_mb, notes}.

    state:
      source      running from a git checkout - says nothing, those users have git
      up-to-date  checked, nothing newer
      available   a newer release exists
      offline     could not reach GitHub (or the answer was unusable)
    """
    info = build_info()
    current = (info.get("version") or "").strip()

    # A source checkout has no build stamp and no archive to replace. Announcing releases to
    # someone whose working tree IS the build would be noise at best and wrong at worst.
    if not is_frozen() or info.get("built") == "source" or not current:
        return {"state": "source", "current": current or "source", "latest": "",
                "url": RELEASES_PAGE, "asset": "", "size_mb": 0, "notes": ""}

    cached = _read_cache()
    age = time.time() - float(cached.get("checked_at") or 0)
    ttl = TTL_FAIL if cached.get("state") == "offline" else TTL_OK
    if not force and cached.get("current") == current and age < ttl:
        return {k: v for k, v in cached.items() if k not in ("checked_at", "etag")}

    rel, etag, not_modified = _fetch(cached.get("etag", "") if not force else "")
    if not_modified and cached:
        cached["checked_at"] = time.time()
        _write_cache(cached)
        return {k: v for k, v in cached.items() if k not in ("checked_at", "etag")}

    if not rel:
        out = {"state": "offline", "current": current, "latest": "",
               "url": RELEASES_PAGE, "asset": "", "size_mb": 0, "notes": ""}
        _write_cache({**out, "checked_at": time.time(), "etag": ""})
        return out

    tag = (rel.get("tag_name") or "").strip()
    latest = ".".join(str(p) for p in parse_version(tag)) or tag
    asset = pick_asset(rel.get("assets") or []) or {}
    out = {
        "state": "available" if is_newer(tag, current) else "up-to-date",
        "current": current,
        "latest": latest,
        "url": rel.get("html_url") or RELEASES_PAGE,
        "asset": asset.get("name", ""),
        "size_mb": round((asset.get("size") or 0) / 1e6, 1),
        # Trimmed hard: this lands in a tkinter HUD and a small modal, not a browser.
        "notes": (rel.get("body") or "").strip()[:1200],
    }
    _write_cache({**out, "checked_at": time.time(), "etag": etag})
    return out


# ── background use ────────────────────────────────────────────────────────────────────────────

def cached_state() -> dict:
    """The last answer, for a caller that must not block - every /api/status hit takes this path.

    Before the first background check completes this reports 'source', which renders as no badge
    anywhere. Showing nothing briefly is the right failure: a surface that flickered 'offline' for
    ten seconds on every launch would train users to ignore it.
    """
    with _lock:
        if _state:
            return dict(_state)
    c = _read_cache()
    if c:
        return {k: v for k, v in c.items() if k not in ("checked_at", "etag")}
    return {"state": "source", "current": (build_info().get("version") or ""), "latest": "",
            "url": RELEASES_PAGE, "asset": "", "size_mb": 0, "notes": ""}


def refresh(force: bool = False) -> dict:
    r = check(force=force)
    with _lock:
        _state.clear()
        _state.update(r)
    return r


def start_background_check() -> None:
    """One check, shortly after boot, on a daemon thread.

    Delayed because startup is the one moment the user is watching: a DNS lookup on a captive
    portal can hang for the full socket timeout, and nothing about an update notice justifies
    holding the first paint. Daemon so it can never keep the process alive at quit.
    """
    def run():
        time.sleep(BOOT_DELAY)
        try:
            refresh()
        except Exception:
            pass
    threading.Thread(target=run, name="meld-update-check", daemon=True).start()
