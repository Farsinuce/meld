"""The resource/data split (src/paths.py).

The bug this guards against only appears in a frozen build: `Path(__file__).parent` points
inside the PyInstaller payload, which is read-only, so anything that writes there works
perfectly from source and dies for every packaged user. These tests fake a frozen environment
so that failure mode is visible from a normal `pytest` run.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import paths  # noqa: E402


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Every test starts with an unresolved data dir and no inherited overrides."""
    monkeypatch.setattr(paths, "_DATA_CACHE", None)
    for var in ("MELD_DATA_DIR", "MELD_CACHE_DIR"):
        monkeypatch.delenv(var, raising=False)
    yield
    paths._DATA_CACHE = None


def _fake_frozen(monkeypatch, app: Path, payload: Path):
    """Make paths.py believe it is running as Meld.exe out of `app`, unpacked into `payload`."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(payload), raising=False)
    monkeypatch.setattr(sys, "executable", str(app / "Meld.exe"), raising=False)


def test_source_checkout_keeps_todays_layout():
    """An existing install must not have its 120 GB of projects/ and cache/ relocated."""
    root = Path(__file__).resolve().parent.parent
    assert paths.data_dir() == root
    assert paths.resource_dir() == root
    assert paths.projects_root() == root / "projects"
    assert paths.cache_root() == root / "cache"


def test_frozen_splits_resources_from_data(tmp_path, monkeypatch):
    app, payload = tmp_path / "app", tmp_path / "payload"
    app.mkdir()
    payload.mkdir()
    _fake_frozen(monkeypatch, app, payload)

    assert paths.resource_dir() == payload          # read-only bundle
    assert paths.exe_dir() == app
    assert paths.data_dir() == app / "data"         # portable: beside the app
    assert paths.resource_dir() != paths.data_dir()


def test_frozen_read_only_app_dir_falls_back_to_user_dir(tmp_path, monkeypatch):
    """Installed into Program Files, the app folder is not writable - the data dir must move to
    the OS user-data directory instead of raising."""
    app, payload = tmp_path / "app", tmp_path / "payload"
    app.mkdir()
    payload.mkdir()
    _fake_frozen(monkeypatch, app, payload)
    user_dir = tmp_path / "userdata"
    monkeypatch.setattr(paths, "_writable", lambda p: False)
    monkeypatch.setattr(paths, "_os_user_data_dir", lambda: user_dir)

    assert paths.data_dir() == user_dir.resolve()


def test_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("MELD_DATA_DIR", str(tmp_path / "elsewhere"))
    assert paths.data_dir() == (tmp_path / "elsewhere").resolve()
    assert paths.projects_root() == (tmp_path / "elsewhere").resolve() / "projects"


def test_pointer_file_moves_the_data_dir(tmp_path, monkeypatch):
    """The portable escape hatch: one line in meld-data.txt parks the cache on another drive."""
    app, payload, target = tmp_path / "app", tmp_path / "payload", tmp_path / "D-drive"
    app.mkdir()
    payload.mkdir()
    (app / paths.POINTER_FILE).write_text(f"# where the big cache lives\n{target}\n", encoding="utf-8")
    _fake_frozen(monkeypatch, app, payload)

    assert paths.data_dir() == target.resolve()


def test_cache_env_var_still_wins(tmp_path, monkeypatch):
    """MELD_CACHE_DIR predates the data-dir pointer and is documented; it must keep working."""
    monkeypatch.setenv("MELD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MELD_CACHE_DIR", str(tmp_path / "bigdrive"))
    assert paths.cache_root() == (tmp_path / "bigdrive").resolve()


def test_no_module_writes_under_the_resource_dir(tmp_path, monkeypatch):
    """Guard rail for the whole split: with a frozen layout, every path the app writes to must
    live under data_dir(), never under the read-only bundle."""
    app, payload = tmp_path / "app", tmp_path / "payload"
    app.mkdir()
    payload.mkdir()
    _fake_frozen(monkeypatch, app, payload)

    data = paths.data_dir()
    write_targets = [paths.projects_root(), paths.cache_root(),
                     paths.logs_dir(), paths.user_assets_dir()]
    for t in write_targets:
        assert data in t.parents or t == data, f"{t} is not under the data dir"
        assert paths.resource_dir() not in t.parents, f"{t} would write into the bundle"
