"""Shareable settings presets (src/presets.py + the /api/presets/* routes).

A preset is one json file a user sends to another user: "my look, your place". The failure
these tests exist to prevent: a preset authored on a 24-core/64 GB desktop carrying
max_workers 24 and master_world_dir D:\\big-disk onto a laptop, which oversubscribes every
core and points the world at a drive that does not exist. So the machine-strip is pinned in
BOTH directions (save and import/apply), alongside the bundled/user split, the unknown-key
forward-compat note, the size cap, and the opt-in selection embed.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
from src import presets as presets_mod  # noqa: E402
from src.project import Project, default_settings  # noqa: E402

HDR = {"Host": "127.0.0.1:5630", "Origin": "http://127.0.0.1:5630"}
REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Isolated user-preset dir + a throwaway project, so a test run can never write into
    the developer's real data dir or active project. The BUNDLED dir stays the real repo
    presets/ on purpose: listing and applying those here is the proof the shipped files
    are valid against this build's default_settings."""
    monkeypatch.setattr(presets_mod, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(server, "PROJECT", Project(tmp_path / "proj"))
    return server.app.test_client()


def _save(client, name="My Look", **body):
    return client.post("/api/presets/save", json={"name": name, **body}, headers=HDR)


def _import(client, obj):
    return client.post("/api/presets/import", data=json.dumps(obj),
                       content_type="application/json", headers=HDR)


# ── round trip ───────────────────────────────────────────────────────────────────────────
def test_round_trip_save_list_apply(client):
    server.PROJECT.update_settings({"scale": 0.25, "snow_mode": "manual"})
    r = _save(client).get_json()
    assert r["ok"] and r["name"] == "My Look"

    listed = client.get("/api/presets", headers=HDR).get_json()["presets"]
    assert listed[0]["name"] == "My Look", "user presets come before the bundled set"
    assert listed[0]["bundled"] is False

    # Change the look, then apply the preset back — the round trip is only real if the
    # values return AND the response carries the refreshed dict for the UI.
    server.PROJECT.update_settings({"scale": 1.0, "snow_mode": "peaks"})
    a = client.post("/api/presets/apply", json={"name": "My Look"}, headers=HDR).get_json()
    assert a["ok"] and a["applied"]["scale"] == 0.25 and a["applied"]["snow_mode"] == "manual"
    assert server.PROJECT.settings()["scale"] == 0.25


# ── the machine strip, both directions ───────────────────────────────────────────────────
def test_machine_keys_are_stripped_on_save(client):
    server.PROJECT.update_settings({"scale": 0.5, "max_workers": 24, "cpu_target_pct": 95,
                                    "master_world_dir": "D:/big-disk/worlds",
                                    "server_ram_gb": 8, "osm_bake_workers": 8})
    r = _save(client, "Beefy").get_json()
    assert "max_workers" in r["stripped"] and "master_world_dir" in r["stripped"]

    saved = json.loads((presets_mod.user_dir() / "beefy.json").read_text(encoding="utf-8"))
    on_disk = set(saved["settings"])
    assert not on_disk & {"max_workers", "cpu_target_pct", "master_world_dir",
                          "server_ram_gb", "osm_bake_workers"}
    assert not any(k.startswith("server_") for k in on_disk), \
        "the whole Leaf-server profile is machine-local"
    assert saved["settings"]["scale"] == 0.5


def test_apply_cannot_touch_this_machines_tuning(client):
    # A hand-edited (or hostile) file dropped straight into the user dir — the strip must
    # hold on APPLY too, not only on the save that our own code performed.
    (presets_mod.user_dir() / "sneaky.json").write_text(json.dumps({
        "meld_preset": 1, "name": "Sneaky",
        "settings": {"scale": 0.3, "max_workers": 64, "master_world_dir": "C:/evil",
                     "server_dir": "C:/evil2", "future_knob": 1}}), encoding="utf-8")
    server.PROJECT.update_settings({"max_workers": 2})

    a = client.post("/api/presets/apply", json={"name": "Sneaky"}, headers=HDR).get_json()
    assert a["ok"] and a["applied"]["scale"] == 0.3
    assert a["applied"]["max_workers"] == 2, "the laptop keeps its own worker count"
    assert a["applied"]["master_world_dir"] == ""
    assert "future_knob" in a["dropped"] and "future_knob" not in a["applied"]


def test_import_strips_machine_keys_and_notes_unknown_ones(client):
    r = _import(client, {"meld_preset": 1, "name": "From a friend",
                         "settings": {"scale": 0.2, "min_threads_per_worker": 8,
                                      "datapack_tile_concurrency": 32,
                                      "wormhole": True}}).get_json()
    assert r["ok"]
    assert "min_threads_per_worker" in r["stripped"]
    assert r["dropped"] == ["wormhole"]
    assert "wormhole" in r["note"], "a dropped key the user cannot see is a silent failure"

    saved = json.loads((presets_mod.user_dir() / r["file"]).read_text(encoding="utf-8"))
    assert saved["settings"] == {"scale": 0.2}


def test_import_accepts_an_uploaded_file(client):
    raw = json.dumps({"meld_preset": 1, "name": "Upload",
                      "settings": {"scale": 0.4}}).encode("utf-8")
    r = client.post("/api/presets/import", data={"file": (io.BytesIO(raw), "p.json")},
                    headers=HDR).get_json()
    assert r["ok"] and r["name"] == "Upload"
    assert client.post("/api/presets/apply", json={"name": "Upload"},
                       headers=HDR).get_json()["applied"]["scale"] == 0.4


# ── bundled presets ──────────────────────────────────────────────────────────────────────
def test_bundled_presets_are_listed_and_apply(client):
    got = {p["name"]: p for p in client.get("/api/presets", headers=HDR).get_json()["presets"]}
    for name in ("Default", "Scaled 1:10", "Extended height"):
        assert got[name]["bundled"] is True, f"{name} must ship with the app"

    a = client.post("/api/presets/apply", json={"name": "Scaled 1:10"}, headers=HDR).get_json()
    assert a["ok"] and a["applied"]["scale"] == 0.1
    assert a["applied"]["road_detail_level"] == "compact"
    assert not any(a["applied"]["props"].values()), "props off at 1:10"

    b = client.post("/api/presets/apply", json={"name": "Extended height"}, headers=HDR).get_json()
    assert b["applied"]["disable_height_limit"] is True
    assert b["applied"]["vertical_exaggeration"] == 1.5


def test_bundled_presets_cannot_be_deleted(client):
    r = client.post("/api/presets/delete", json={"name": "Default"}, headers=HDR)
    assert r.status_code == 403 and not r.get_json()["ok"]
    names = [p["name"] for p in client.get("/api/presets", headers=HDR).get_json()["presets"]]
    assert "Default" in names, "the file must still be there afterwards"


def test_user_presets_can_be_deleted(client):
    _save(client, "Mine")
    r = client.post("/api/presets/delete", json={"name": "Mine"}, headers=HDR).get_json()
    assert r["ok"]
    names = [p["name"] for p in client.get("/api/presets", headers=HDR).get_json()["presets"]]
    assert "Mine" not in names


def test_bundled_settings_only_use_real_keys():
    """The placeholder files are hand-written; a typo'd key would be silently dropped on
    apply and the preset would quietly not do what its description says."""
    known = set(default_settings())
    files = list((REPO / "presets").glob("*.json"))
    assert len(files) == 3
    for p in files:
        obj = json.loads(p.read_text(encoding="utf-8"))
        assert presets_mod.validate(obj) is None, p.name
        assert set(obj["settings"]) <= known, f"{p.name} uses unknown settings keys"


# ── refusals ─────────────────────────────────────────────────────────────────────────────
def test_import_rejects_more_than_a_megabyte(client):
    blob = json.dumps({"meld_preset": 1, "name": "Big", "settings": {},
                       "pad": "x" * (1 << 20)})
    r = client.post("/api/presets/import", data=blob,
                    content_type="application/json", headers=HDR)
    assert r.status_code == 413


def test_import_refuses_a_bundled_name(client):
    r = _import(client, {"meld_preset": 1, "name": "Default", "settings": {"scale": 0.7}})
    assert r.status_code == 409 and not r.get_json()["ok"]


def test_save_refuses_a_bundled_name(client):
    assert _save(client, "Scaled 1:10").status_code == 409


def test_importing_the_same_name_twice_keeps_both(client):
    obj = {"meld_preset": 1, "name": "Twin", "settings": {"scale": 0.5}}
    assert _import(client, obj).get_json()["name"] == "Twin"
    r = _import(client, obj).get_json()
    assert r["name"] == "Twin (2)", "an import must never overwrite a user's own preset"
    assert "Twin (2)" in r["note"]


def test_a_wrong_schema_is_refused_with_a_reason(client):
    r = _import(client, {"meld_preset": 2, "name": "Future", "settings": {}})
    assert r.status_code == 400
    assert "schema" in r.get_json()["error"]


# ── selection: embedded only when asked, applied only when asked ─────────────────────────
def test_selection_embedded_only_when_asked(client):
    server.PROJECT.save_selection({"bbox": {"south": 45.0, "west": 25.0,
                                            "north": 45.2, "east": 25.3}})
    _save(client, "No place")
    r = _save(client, "With place", include_selection=True).get_json()
    assert r["has_selection"] is True

    no = json.loads((presets_mod.user_dir() / "no-place.json").read_text(encoding="utf-8"))
    yes = json.loads((presets_mod.user_dir() / "with-place.json").read_text(encoding="utf-8"))
    assert "selection" not in no, "the place is opt-in; the look alone is the default"
    assert yes["selection"]["bbox"]["north"] == 45.2


def test_selection_applies_only_on_request(client):
    server.PROJECT.save_selection({"bbox": {"south": 45.0, "west": 25.0,
                                            "north": 45.2, "east": 25.3}})
    _save(client, "Placed", include_selection=True)
    server.PROJECT.save_selection(None)

    a = client.post("/api/presets/apply", json={"name": "Placed"}, headers=HDR).get_json()
    assert a["selection_applied"] is False and server.PROJECT.load_selection() is None

    b = client.post("/api/presets/apply", json={"name": "Placed", "apply_selection": True},
                    headers=HDR).get_json()
    assert b["selection_applied"] is True
    assert server.PROJECT.load_selection()["bbox"]["north"] == 45.2


# ── export / apply-by-path (the sharing flow end to end) ─────────────────────────────────
def test_export_downloads_the_exact_file(client):
    _save(client, "Ship it", description="my look")
    r = client.get("/api/presets/export", query_string={"name": "Ship it"}, headers=HDR)
    assert r.status_code == 200
    assert "attachment" in r.headers.get("Content-Disposition", "")
    obj = json.loads(r.get_data(as_text=True))
    assert obj["name"] == "Ship it" and obj["meld_preset"] == 1


def test_apply_by_path_covers_the_downloaded_file_flow(client, tmp_path):
    p = tmp_path / "friend.json"
    p.write_text(json.dumps({"meld_preset": 1, "name": "Friend",
                             "settings": {"scale": 0.6, "max_workers": 48}}),
                 encoding="utf-8")
    server.PROJECT.update_settings({"max_workers": 2})
    a = client.post("/api/presets/apply", json={"path": str(p)}, headers=HDR).get_json()
    assert a["ok"] and a["applied"]["scale"] == 0.6
    assert a["applied"]["max_workers"] == 2, "path-apply gets the same strip as import"
