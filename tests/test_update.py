"""The update checker: version maths, asset picking, caching, and the states.

No network in any test - every one stubs the fetch. A test suite that reaches GitHub would be
flaky offline and would spend the same 60-requests-per-hour budget the module exists to protect.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import update  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Every test gets its own cache dir and a frozen 1.8.4 build stamp."""
    monkeypatch.setattr(update, "cache_root", lambda: tmp_path)
    monkeypatch.setattr(update, "is_frozen", lambda: True)
    monkeypatch.setattr(update, "build_info",
                        lambda: {"version": "1.8.4", "built": "2026-08-13", "commit": "abc"})
    with update._lock:
        update._state.clear()
    yield


def _release(tag="meld-v1.8.5", assets=None):
    return {
        "tag_name": tag,
        "html_url": f"https://github.com/Teddy563/meld/releases/tag/{tag}",
        "body": "notes",
        "assets": assets if assets is not None else [
            {"name": "Meld-1.8.5-win-x64.zip", "size": 72_000_000},
            {"name": "Meld-1.8.5-linux-x64.tar.gz", "size": 99_000_000},
            {"name": "Meld-1.8.5-mac-arm64.tar.gz", "size": 84_000_000},
        ],
    }


def _serve(monkeypatch, rel, etag="W/\"x\"", not_modified=False):
    calls = []

    def fake(etag_in=""):
        calls.append(etag_in)
        return (None, etag, True) if not_modified else (rel, etag, False)

    monkeypatch.setattr(update, "_fetch", fake)
    return calls


# ── version comparison ────────────────────────────────────────────────────────────────────────

def test_ten_is_newer_than_nine():
    """The bug a string compare would ship: "1.8.10" < "1.8.9" as text, so one release in ten
    would announce an update to an older build."""
    assert update.is_newer("meld-v1.8.10", "1.8.9") is True
    assert update.is_newer("meld-v1.8.9", "1.8.10") is False


def test_equal_is_not_newer():
    assert update.is_newer("meld-v1.8.4", "1.8.4") is False


def test_differing_component_counts():
    assert update.is_newer("meld-v1.9", "1.8.4") is True
    assert update.is_newer("meld-v1.8", "1.8.0") is False


def test_unparseable_never_claims_an_update():
    """Silence beats a badge that can never be cleared."""
    assert update.is_newer("nightly", "1.8.4") is False
    assert update.is_newer("meld-v1.8.5", "") is False


# ── asset picking ─────────────────────────────────────────────────────────────────────────────

def test_picks_this_platform(monkeypatch):
    monkeypatch.setattr(update, "platform_tag", lambda: ("linux", "x64", ".tar.gz"))
    assert update.pick_asset(_release()["assets"])["name"] == "Meld-1.8.5-linux-x64.tar.gz"


def test_falls_back_for_the_misnamed_v184_windows_asset(monkeypatch):
    """v1.8.4 published `Meld-1.8.zip` because with_suffix() ate the version, OS and arch. An
    updater matching only the documented scheme would report "no download for your platform"
    against a release that plainly has one."""
    monkeypatch.setattr(update, "platform_tag", lambda: ("win", "x64", ".zip"))
    assets = [{"name": "Meld-1.8.zip", "size": 72_000_000},
              {"name": "Meld-1.8.4-linux-x64.tar.gz", "size": 99_000_000}]
    assert update.pick_asset(assets)["name"] == "Meld-1.8.zip"


def test_ambiguous_fallback_picks_nothing(monkeypatch):
    """Two candidate zips and no exact match is not a guess worth making."""
    monkeypatch.setattr(update, "platform_tag", lambda: ("win", "x64", ".zip"))
    assets = [{"name": "Meld-1.8.zip", "size": 1}, {"name": "Meld-other.zip", "size": 2}]
    assert update.pick_asset(assets) is None


# ── states ────────────────────────────────────────────────────────────────────────────────────

def test_available(monkeypatch):
    _serve(monkeypatch, _release())
    r = update.check()
    assert r["state"] == "available" and r["latest"] == "1.8.5" and r["current"] == "1.8.4"


def test_the_asset_digest_is_carried_through(monkeypatch):
    """GitHub returns a per-asset `digest` on every release in both repos, so the installer half
    can verify what it downloaded without any SHA256SUMS asset being published first."""
    monkeypatch.setattr(update, "platform_tag", lambda: ("linux", "x64", ".tar.gz"))
    rel = _release(assets=[{"name": "Meld-1.8.5-linux-x64.tar.gz", "size": 99_000_000,
                            "digest": "sha256:d8da0446",
                            "browser_download_url": "https://example/a.tar.gz"}])
    _serve(monkeypatch, rel)
    r = update.check()
    assert r["sha256"] == "d8da0446" and r["download_url"].endswith("a.tar.gz")


def test_a_missing_digest_is_empty_not_a_crash(monkeypatch):
    """Older releases predate the field. An installer must see "no digest" and refuse, rather
    than the checker dying and the whole update surface going dark."""
    monkeypatch.setattr(update, "platform_tag", lambda: ("linux", "x64", ".tar.gz"))
    _serve(monkeypatch, _release(assets=[{"name": "Meld-1.8.5-linux-x64.tar.gz", "size": 1}]))
    assert update.check()["sha256"] == ""


def test_up_to_date(monkeypatch):
    _serve(monkeypatch, _release(tag="meld-v1.8.4"))
    assert update.check()["state"] == "up-to-date"


def test_older_release_is_not_an_update(monkeypatch):
    _serve(monkeypatch, _release(tag="meld-v1.8.3"))
    assert update.check()["state"] == "up-to-date"


def test_offline(monkeypatch):
    monkeypatch.setattr(update, "_fetch", lambda etag="": (None, "", False))
    assert update.check()["state"] == "offline"


def test_source_checkout_says_nothing(monkeypatch):
    monkeypatch.setattr(update, "is_frozen", lambda: False)
    called = []
    monkeypatch.setattr(update, "_fetch", lambda etag="": called.append(1) or (None, "", False))
    assert update.check()["state"] == "source"
    assert not called, "a source checkout must not spend rate-limit quota"


# ── caching ───────────────────────────────────────────────────────────────────────────────────

def test_second_check_is_served_from_disk(monkeypatch):
    calls = _serve(monkeypatch, _release())
    update.check()
    update.check()
    assert len(calls) == 1, "the 60/hour budget is shared with everything else on the machine"


def test_force_bypasses_the_cache(monkeypatch):
    calls = _serve(monkeypatch, _release())
    update.check()
    update.check(force=True)
    assert len(calls) == 2


def test_a_new_build_invalidates_the_cache(monkeypatch, tmp_path):
    """After the user updates, the cached 'available' must not survive and keep nagging."""
    _serve(monkeypatch, _release())
    assert update.check()["state"] == "available"
    monkeypatch.setattr(update, "build_info",
                        lambda: {"version": "1.8.5", "built": "2026-08-14", "commit": "d"})
    _serve(monkeypatch, _release())
    assert update.check()["state"] == "up-to-date"


def test_304_refreshes_the_timestamp_without_new_data(monkeypatch, tmp_path):
    _serve(monkeypatch, _release())
    update.check()
    (tmp_path / "update.json").write_text(json.dumps({
        **update._read_cache(), "checked_at": time.time() - update.TTL_OK - 1}), encoding="utf-8")
    _serve(monkeypatch, None, not_modified=True)
    assert update.check()["state"] == "available", "a 304 must reuse the cached answer"


def test_a_failed_check_retries_sooner_than_a_good_one(monkeypatch, tmp_path):
    monkeypatch.setattr(update, "_fetch", lambda etag="": (None, "", False))
    update.check()
    (tmp_path / "update.json").write_text(json.dumps({
        **update._read_cache(), "checked_at": time.time() - update.TTL_FAIL - 1}), encoding="utf-8")
    calls = _serve(monkeypatch, _release())
    assert update.check()["state"] == "available"
    assert len(calls) == 1, "an offline answer must not be pinned for a full day"


def test_cached_state_never_blocks(monkeypatch):
    """/api/status takes this path on every hit and must not make a network call."""
    monkeypatch.setattr(update, "_fetch",
                        lambda etag="": pytest.fail("cached_state() must not fetch"))
    assert update.cached_state()["state"] in ("source", "up-to-date", "available", "offline")


def test_an_unwritable_cache_dir_is_survivable(monkeypatch):
    monkeypatch.setattr(update, "_write_cache", lambda d: (_ for _ in ()).throw(OSError("ro")))
    _serve(monkeypatch, _release())
    with pytest.raises(OSError):
        update._write_cache({})
    monkeypatch.setattr(update, "_write_cache", lambda d: None)
    assert update.check()["state"] == "available"
