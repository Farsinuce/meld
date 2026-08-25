"""Governor settings: the new keys, the legacy migration, and the never-travels lists.

Three failures these pin down:

1. A key added to default_settings but forgotten in presets._MACHINE_KEYS ships one machine's
   scheduling policy inside a shared preset. governor_history is the sharpest version of that
   bug — it carries a 24-core desktop's measured knee, so an imported preset would warm-start a
   laptop at 24 workers. (gpu_accel was exactly this leak, live, until this change.)
2. The same key forgotten in server.py's _META_SKIP_SETTINGS does it again through the OTHER
   door: world-meta import, which is how a downloaded world's settings reach a project.
3. A project that opted into the pre-governor worker_autoscale silently falling back to the
   legacy formulas — the user asked for adaptive scheduling once and must keep it.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.presets import _MACHINE_KEYS, clean_settings  # noqa: E402
from src.project import (  # noqa: E402
    Project, default_settings, migrate_governor_settings,
)

REPO = Path(__file__).resolve().parent.parent

# Every key the governor work introduced, plus the legacy flag it replaces. Machine-shaped,
# all of them: a scheduling policy and the numbers measured on one particular box.
GOVERNOR_KEYS = (
    "governor_mode", "governor_history", "ram_headroom_mb",
    "flush_threads_cap", "governor_max_workers", "worker_autoscale",
)


# ── defaults ─────────────────────────────────────────────────────────────────────────────
def test_defaults_present_and_legacy_safe():
    d = default_settings()
    assert d["governor_mode"] == "off"        # legacy scheduling formulas, byte-identical
    assert d["governor_history"] == {}
    assert d["ram_headroom_mb"] == 2048
    assert d["flush_threads_cap"] == 12
    assert d["governor_max_workers"] == 0
    assert d["worker_autoscale"] is False     # legacy key stays, one more release
    assert d["cpu_target_pct"] == 90          # the one true fallback (not 100)


# ── the never-travels lists ──────────────────────────────────────────────────────────────
def test_governor_keys_are_machine_keys():
    missing = [k for k in GOVERNOR_KEYS if k not in _MACHINE_KEYS]
    assert not missing, f"would travel inside a shared preset: {missing}"


def test_gpu_accel_never_travels():
    """Its tooltip always said so; the list did not. Regression pin for that leak."""
    assert "gpu_accel" in _MACHINE_KEYS


def test_machine_keys_stripped_in_both_directions():
    blob = {k: default_settings()[k] for k in GOVERNOR_KEYS}
    blob["gpu_accel"] = "dgpu"
    blob["scale"] = 0.5                       # a real world key must survive
    for known_only in (False, True):          # save direction, then import/apply
        kept, machine, _unknown = clean_settings(blob, known_only=known_only)
        assert kept == {"scale": 0.5}
        assert set(machine) == set(GOVERNOR_KEYS) | {"gpu_accel"}


def _meta_skip_settings() -> set[str]:
    """server.py's _META_SKIP_SETTINGS, read from SOURCE — no import, so this test does not
    depend on the flask app booting (or on server.py being mid-edit)."""
    tree = ast.parse((REPO / "server.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_META_SKIP_SETTINGS" for t in node.targets):
            return set(ast.literal_eval(node.value))
    return set()


def test_governor_keys_skipped_on_world_meta_import():
    skip = _meta_skip_settings()
    assert skip, "server.py no longer defines a literal _META_SKIP_SETTINGS"
    missing = [k for k in GOVERNOR_KEYS if k not in skip]
    assert not missing, f"world-meta import would overwrite local scheduling: {missing}"


# ── the legacy migration ─────────────────────────────────────────────────────────────────
def test_migration_maps_legacy_autoscale_to_auto(tmp_path):
    """A pre-governor project: worker_autoscale stored True, governor_mode never written
    (the key did not exist in that release's default_settings)."""
    proj = Project(tmp_path / "legacy")
    proj.save({"settings": {"worker_autoscale": True, "max_workers": 6}})
    patch = migrate_governor_settings(proj.settings())
    assert patch == {"governor_mode": "auto", "worker_autoscale": False}


def test_migration_is_a_noop_without_the_legacy_flag():
    assert migrate_governor_settings(default_settings()) == {}
    assert migrate_governor_settings({}) == {}
    assert migrate_governor_settings(None) == {}          # tolerates a missing blob
    assert migrate_governor_settings({"governor_mode": "auto"}) == {}


def test_migration_is_idempotent(tmp_path):
    proj = Project(tmp_path / "legacy")
    proj.save({"settings": {"worker_autoscale": True}})
    proj.update_settings(migrate_governor_settings(proj.settings()))
    assert proj.settings()["governor_mode"] == "auto"
    assert migrate_governor_settings(proj.settings()) == {}   # second load changes nothing


def test_migration_does_not_resurrect_a_mode_the_user_turned_off(tmp_path):
    """The whole reason the patch CONSUMES worker_autoscale: a stale True must not flip the
    governor back to auto on the next load after the user deliberately set it off."""
    proj = Project(tmp_path / "legacy")
    proj.save({"settings": {"worker_autoscale": True}})
    proj.update_settings(migrate_governor_settings(proj.settings()))
    proj.update_settings({"governor_mode": "off"})            # user turns it off later
    assert migrate_governor_settings(proj.settings()) == {}


def test_migration_respects_an_explicit_off_even_with_the_legacy_flag_still_set(tmp_path):
    """Finding 15. The old rule migrated on any mode outside (advise, auto), so a project
    whose owner had chosen "off" while a stale worker_autoscale=True sat in the blob was
    dragged back to "auto" — at boot AND on every project switch, i.e. every single load.
    A written mode is the user's; the migration may only retire the legacy flag."""
    proj = Project(tmp_path / "chose-off")
    proj.save({"settings": {"worker_autoscale": True, "governor_mode": "off"}})
    assert migrate_governor_settings(proj.settings()) == {"worker_autoscale": False}
    proj.update_settings({"worker_autoscale": False})
    assert proj.settings()["governor_mode"] == "off"          # and it STAYS off
    assert migrate_governor_settings(proj.settings()) == {}


def test_settings_reports_which_keys_were_stored(tmp_path):
    """The provenance the migration needs: Project.settings() merges the defaults under the
    stored blob, so a default "off" and a chosen "off" are the same string. stored_keys is
    what tells them apart; a plain copy drops it and readers must fall back safely."""
    proj = Project(tmp_path / "prov")
    proj.save({"settings": {"max_workers": 6}})
    s = proj.settings()
    assert s["governor_mode"] == "off" and s["max_workers"] == 6
    assert s.stored_keys == frozenset({"max_workers"})
    assert dict(s) == {**default_settings(), "max_workers": 6}   # still a plain dict of values
    # No provenance (hand-built blob / a copy): a recognised mode counts as written, which is
    # the conservative direction — never overwrite a mode that might be the user's.
    assert migrate_governor_settings({"worker_autoscale": True, "governor_mode": "off"})         == {"worker_autoscale": False}
    assert migrate_governor_settings({"worker_autoscale": True})         == {"governor_mode": "auto", "worker_autoscale": False}


def test_migration_retires_the_flag_when_already_governed():
    s = {"worker_autoscale": True, "governor_mode": "advise"}
    assert migrate_governor_settings(s) == {"worker_autoscale": False}


def test_migration_does_not_mutate_its_input():
    s = {**default_settings(), "worker_autoscale": True}
    before = dict(s)
    migrate_governor_settings(s)
    assert s == before
