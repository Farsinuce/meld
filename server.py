#!/usr/bin/env python3
"""
light-meld orchestrator.

Flow: set origin -> survey (lock elevation) -> grid (split selection) ->
queue (parallel Arnis) -> per-cell merge into the master world.

See ../light-docs/ for the full spec. The coordinate convention lives in
src/coords.py and is matched by the Arnis fork's transform_point fix
(light-docs/03).
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

# Windows consoles default to cp1252; Arnis stdout and log lines can contain
# Unicode (arrows, degree signs, accented place names). Without this, a single
# non-cp1252 character printed by log() would crash the whole server. Force
# UTF-8 with replacement so logging can never take the process down.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from flask import Flask, request, jsonify, send_from_directory, send_file, abort, Response

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from src.project import Project, default_settings
from src.grid import cells_for_bbox, cells_for_polygons, _point_in_poly
from src.coords import expand_bbox_for_seam, cell_bbox, snap_to_region_grid
from src.arnis_cmd import (build_arnis_cmd, run_arnis, find_world_dir, clean_output_dir,
                           parse_progress, effective_elev_zoom)
from src import arnis_cmd
from src.prefetch import (run_prefetch, preview_clumps, run_terrain_prefetch,
                          run_mapterhorn_bake, purge_small_tiles)
from src import datapack as dp
from src import finalcheck
from src import osm_pack as op
from src import osm_grid
from src import border
from src import mcserver as mcs
from src import runreport
from src.merge import (merge_cell_into_master, strip_buffer_regions,
                        MeldCoordinateDriftError, MeldCollisionError)
from src.survey import survey_elevation
from src.workers import WorkerPool
from src import export as exportmod

# psutil powers the live CPU/RAM gauges. Optional: Flask must boot without it (disk still
# works via shutil, RAM via the ctypes fallback).
try:
    import psutil
except Exception:
    psutil = None

app = Flask(__name__)   # no static catch-all — assets served via /assets/<f> below

# ── projects ─────────────────────────────────────────────────────────────────
# Each project is a self-contained folder under projects/<slug>/ (project.json, grid.json,
# cells/, logs/, osm_cache/, cell_health.json). One project = one world's workspace, so you
# can keep a small "test" project and a big "country" project side by side and switch between
# them without losing either's settings/origin/grid/suspects.
PROJECTS_ROOT = BASE_DIR / "projects"
_ACTIVE_FILE = PROJECTS_ROOT / ".active"


def _setup_shared_cache() -> None:
    """Point ALL Arnis caches (OSM + terrain + land-cover) at one shared Meld-local folder so
    they're visible and reused by every project/world, instead of hidden in AppData. Sets
    ARNIS_CACHE_ROOT process-wide (every child arnis inherits it), and one-time MOVES any
    existing AppData caches into the new root (same-drive only -> instant rename; cross-drive is
    skipped so we never silently copy tens of GB)."""
    from src.prefetch import meld_cache_root
    root = meld_cache_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        os.environ["ARNIS_CACHE_ROOT"] = str(root)
    except Exception as ex:
        log(f"[Cache] could not set up shared cache at {root}: {ex}")
        return

    # One-time migration only: a sentinel makes this idempotent so a re-import / second process
    # can never re-run the move (and so it never fights a running generation after the first time).
    sentinel = root / ".cache_migrated"
    if sentinel.exists():
        return
    appdata = os.environ.get("LOCALAPPDATA")
    if not appdata:
        try: sentinel.write_text("")
        except Exception: pass
        return
    same_drive = os.path.splitdrive(os.path.abspath(appdata))[0].lower() == \
        os.path.splitdrive(os.path.abspath(root))[0].lower()
    # (legacy AppData name) -> (new name under the Meld cache root)
    moves = [("arnis-tile-cache", "arnis-tile-cache"),
             ("arnis-landcover-cache", "arnis-landcover-cache"),
             ("meld-osm-cache", "osm")]
    for old_name, new_name in moves:
        src = Path(appdata) / old_name
        dst = root / new_name
        if not src.exists() or dst.exists():
            continue
        if not same_drive:
            log(f"[Cache] {old_name} is on a different drive than {root}; leaving it "
                f"(set MELD_CACHE_DIR on the same drive to migrate, or it re-downloads).")
            continue
        try:
            shutil.move(str(src), str(dst))   # same drive => atomic rename, instant even for 500k files
            log(f"[Cache] moved {old_name} -> {dst}")
        except Exception as ex:
            log(f"[Cache] could not move {old_name} (left in place, will re-download): {ex}")
    try:
        sentinel.write_text("")   # done — never migrate again
    except Exception:
        pass


def _slugify(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip()).strip("-._")
    return (s or "world").lower()[:64]


def _read_active_slug() -> str:
    try:
        s = _ACTIVE_FILE.read_text(encoding="utf-8").strip()
        if s and (PROJECTS_ROOT / s / "project.json").exists():
            return s
    except Exception:
        pass
    return "default"


def _write_active_slug(slug: str) -> None:
    try:
        PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
        _ACTIVE_FILE.write_text(slug, encoding="utf-8")
    except Exception:
        pass


ACTIVE_SLUG = _read_active_slug()
PROJECT = Project(PROJECTS_ROOT / ACTIVE_SLUG)
POOL = WorkerPool(max_workers=PROJECT.settings().get("max_workers", 4))
POOL.stagger_seconds = (float(PROJECT.settings().get("cpu_stagger_seconds", 2) or 0)
                        if PROJECT.settings().get("cpu_stagger_enabled", True) else 0.0)
POOL.stagger_adaptive = bool(PROJECT.settings().get("cpu_stagger_adaptive", True))

_LOG: list[str] = []

# Generation run stats (for the live timer + final report).
_RUN_LOCK = threading.Lock()
# phase: idle | prefetch (OSM/terrain warm-up, counts toward elapsed) | generating
_RUN = {"started": None, "ended": None, "total": 0, "done": 0, "failed": 0,
        "est_regions": 0, "est_mb": 0, "actual_mb": None, "phase": "idle"}
MB_PER_REGION = 4   # rough estimate for the size report

# ── post-generation export/compression status (drives the progress view) ──────
# Populated by the export post-pass that runs once all cells are merged. See
# src/export.py + repo MELD_EXPORT_PLAN.md. Reset at the start of each run.
_EXPORT_LOCK = threading.Lock()
_EXPORT = {"format": "none", "phase": "idle", "total": 0, "done": 0, "failed": 0,
           "raw_mb": 0.0, "out_mb": 0.0, "ratio": 0.0, "message": "", "out_name": None,
           "rate_per_min": 0.0, "eta_s": -1, "elapsed_s": 0}
_EXPORT_STARTED = {"run": None}   # guards one export per finished run (by _RUN['started'])

# ── one-click Leaf server setup status (drives the Server setup card) ─────────
# Same shape/convention as _EXPORT: a lock-guarded dict folded into /api/status.
# `proc` holds the live mcserver.ServerProc when running (not serialized).
_MCSERVER_LOCK = threading.Lock()
_MCSERVER = {"phase": "idle", "message": "", "version": None, "mode": "main",
             "server_dir": None, "target_world": None, "plan": None, "eula": False,
             "console": [], "running": False, "port": 25565,
             # voxy = plan resolved the vss plugin
             "voxy": False,
             # crash watchdog: auto_restart flips via the UI toggle; restarts = recent count
             "auto_restart": True, "restarts": 0, "world_choice": "auto"}
_MCSERVER_PROC = {"proc": None}
# crash-restart bookkeeping (epoch seconds of recent auto-restarts, max 3 per 10 min)
_MCSERVER_RESTARTS: list = []


def _machine_specs() -> dict:
    """Total RAM (GB) + logical cores, for adapting the server's RAM/CPU knobs."""
    try:
        import psutil
        ram = round(psutil.virtual_memory().total / (1024 ** 3))
    except Exception:  # noqa: BLE001
        ram = 8
    return {"ram_gb": ram, "cores": os.cpu_count() or 4}


def _mcs_resources() -> tuple[str, int]:
    """(heap string, JVM cpu count) from the project profile, clamped to this machine."""
    st = PROJECT.settings()
    m = _machine_specs()
    return mcs.launch_resources(int(st.get("server_ram_gb") or 0),
                                int(st.get("server_cpu_pct") or 100),
                                m["ram_gb"], m["cores"])


def _mcs_set(**kw) -> None:
    with _MCSERVER_LOCK:
        _MCSERVER.update(**kw)


def _mcs_console(line: str) -> None:
    with _MCSERVER_LOCK:
        tail = _MCSERVER["console"]
        tail.append(line)
        if len(tail) > 40:
            del tail[: len(tail) - 40]


def _reset_export_status() -> None:
    with _EXPORT_LOCK:
        _EXPORT.update(format="none", phase="idle", total=0, done=0, failed=0,
                       raw_mb=0.0, out_mb=0.0, ratio=0.0, message="", out_name=None,
                       rate_per_min=0.0, eta_s=-1, elapsed_s=0)
    _EXPORT_STARTED["run"] = None
    # Drop any lingering streaming session from a prior (e.g. stopped) run. Raws are intact
    # (keep-both is forced for archive streaming), so a later post-pass / Compress-now sweep
    # finalizes via the manifest. The orphaned daemon threads idle harmlessly.
    with _STREAM_LOCK:
        _STREAM["session"] = None

# Streaming-overlap session (linear pool or single-writer archive). Lives only while a
# generate run with export_overlap is in flight. See src/export.py.
_STREAM_LOCK = threading.Lock()
_STREAM = {"session": None}


def _world_locked(world_dir) -> bool:
    """Safeguard E: is THIS world open in Minecraft right now? Minecraft holds an
    exclusive lock on `<world>/session.lock` while a world is loaded, so we test that file —
    NOT 'is any javaw.exe running' (which false-positives on the launcher or any other Java
    app). Fail-open (return False on doubt): the export is safe-by-verify anyway, so a missed
    detection only means a locked region write fails gracefully and keeps its source."""
    try:
        lock = Path(world_dir) / "session.lock"
        if not lock.exists():
            return False                       # never opened / not open
        try:
            f = open(lock, "r+b")              # MC's share mode denies this if the world is open
        except PermissionError:
            return True
        except OSError:
            return False
        try:
            if sys.platform == "win32":
                import msvcrt
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                    return False               # acquired → world not open
                except OSError:
                    return True                # couldn't acquire → world open
            else:
                import fcntl
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    return False
                except OSError:
                    return True
        finally:
            f.close()
    except Exception:  # noqa: BLE001
        return False


def _run_had_failures() -> bool:
    with _RUN_LOCK:
        return int(_RUN.get("failed", 0) or 0) > 0


def _export_destination(s: dict) -> str:
    d = str(s.get("export_destination", "in_place") or "in_place").strip().lower()
    return "separate" if d == "separate" else "in_place"


def _export_overlap_on(s: dict) -> bool:
    fmt = str(s.get("export_format", "none") or "none").strip().lower()
    if _export_destination(s) == "separate" or fmt == exportmod.FMT_BLINEAR:
        return False   # separate-folder + blinear build a sibling world as a post-pass (never overlap)
    return bool(s.get("export_overlap", False)) and fmt in exportmod.VALID_FORMATS and fmt != "none"


def _stream_on_progress(p) -> None:
    with _EXPORT_LOCK:
        _EXPORT.update(format=p.format, phase=p.phase, total=p.total, done=p.done,
                       failed=p.failed, raw_mb=round(p.raw_bytes / 1048576, 2),
                       out_mb=round(p.out_bytes / 1048576, 2),
                       ratio=round(p.ratio, 2), message=p.message)


def _post_merge_export_hook(cell_key: str) -> None:
    """Run right after a cell merges its canonical regions into the master. Two jobs:
      • Safeguard D — a regenerated region invalidates any prior .linear sibling, so drop it
        (keeps a converted world from going half-mca/half-linear).
      • Overlap — if export_overlap is on, lazily start the streaming session on the first
        merge and enqueue this cell's finalized (disjoint) regions into it.
    Never raises into the generation path."""
    try:
        from src.coords import canonical_region_bounds
        b = canonical_region_bounds(cell_key)
        if not b:
            return
        rx0, rx1, rz0, rz1 = b
        region_dir = master_world_path(create=False) / "region"
        s = PROJECT.settings()
        want_stream = _export_overlap_on(s)
        with _STREAM_LOCK:
            sess = _STREAM["session"]
            if sess is None and want_stream:
                sess = _make_stream_session(s)
                _STREAM["session"] = sess
        for rx in range(rx0, rx1 + 1):
            for rz in range(rz0, rz1 + 1):
                mca = region_dir / f"r.{rx}.{rz}.mca"
                if not mca.exists():
                    continue
                lin = mca.with_suffix(".linear")
                if lin.exists():
                    try:
                        lin.unlink()        # D: stale .linear from a prior export
                    except OSError:
                        pass
                if sess is not None:
                    sess.submit(mca)
    except Exception as e:  # noqa: BLE001
        log(f"[Export] post-merge hook warning: {e}")


def _make_stream_session(s: dict):
    """Build + start a streaming session for the current format. linear → RegionExportPool
    (parallel per-region); zip/tarzst → ArchiveStreamWriter (single container, keep_both
    forced)."""
    fmt = str(s.get("export_format")).strip().lower()
    world = master_world_path(create=True)
    level = exportmod.resolve_level(fmt, s.get("export_level", 0))
    workers = exportmod.resolve_workers(s.get("export_compression_workers", 0))
    keep_both = bool(s.get("export_keep_both", True))
    if bool(s.get("export_stream_and_free", False)):
        keep_both = False
    manifest = exportmod.Manifest(world, meta={"format": fmt, "overlap": True})
    if fmt == exportmod.FMT_LINEAR:
        sess = exportmod.RegionExportPool(
            level=level, workers=workers, keep_both=keep_both,
            manifest=manifest, on_progress=_stream_on_progress)
    else:
        # Archive streaming keeps raws through the whole stream (a container is only safe once
        # closed + verified). If the user chose delete-raw, the raws are removed ONLY at the
        # end, after the verified archive — never mid-stream.
        sess = exportmod.ArchiveStreamWriter(
            world, fmt, level=level, threads=workers, manifest=manifest,
            on_progress=_stream_on_progress, free_raw_at_end=(not keep_both))
    sess.start()
    with _EXPORT_LOCK:
        _EXPORT.update(format=fmt, phase=("compressing" if fmt == exportmod.FMT_LINEAR else "archiving"),
                       total=0, done=0, failed=0, raw_mb=0.0, out_mb=0.0, ratio=0.0,
                       message="", out_name=None)
    log(f"[Export] overlap streaming started ({fmt}, workers={workers}, keep_both={keep_both})")
    return sess


def _finish_stream_session():
    """Finalize the streaming session (drain + close + verify). Returns its ExportProgress,
    or None if there was no session."""
    with _STREAM_LOCK:
        sess = _STREAM["session"]
        _STREAM["session"] = None
    if sess is None:
        return None
    prog = sess.finish()
    if isinstance(sess, exportmod.ArchiveStreamWriter):
        out_name = sess.dst.name
    else:
        out_name = f"{prog.done} .linear region(s)"
    with _EXPORT_LOCK:
        _EXPORT.update(format=prog.format, phase=prog.phase, total=prog.total, done=prog.done,
                       failed=prog.failed, raw_mb=round(prog.raw_bytes / 1048576, 2),
                       out_mb=round(prog.out_bytes / 1048576, 2),
                       ratio=round(prog.ratio, 2), message=prog.message, out_name=out_name)
    log(f"[Export] overlap streaming finished: {prog.phase} {prog.done}/{prog.total}")
    return prog

# ── per-cell timing + activity timeline (squares graph + end-of-run benchmark) ──
# Collected live during a run, reset when a fresh batch is submitted. Read by /api/status
# (the live squares graph) and by _write_run_report (assembled via src/runreport.py).
_RUN_TIMING_LOCK = threading.Lock()
_CELL_TIMING: dict[str, dict] = {}   # cell_key -> {queued, started, ended, duration, attempts, worker, status, reason}
_RUN_TIMELINE: list[dict] = []       # [{t: bucket_epoch, active: peak running, done, failed}] (done/failed cumulative)
_TIMELINE_BUCKET_S = 20              # seconds per activity square — finer than a minute = a richer graph
_LAST_REPORT = {"html": None, "json": None, "world": None, "ts": None}


def _timing_reset() -> None:
    with _RUN_TIMING_LOCK:
        _CELL_TIMING.clear()
        _RUN_TIMELINE.clear()


def _timing_queued(cell_key: str) -> None:
    with _RUN_TIMING_LOCK:
        t = _CELL_TIMING.setdefault(cell_key, {"attempts": 0})
        if not t.get("queued"):
            t["queued"] = time.time()


def _timing_started(cell_key: str, worker_id) -> None:
    with _RUN_TIMING_LOCK:
        t = _CELL_TIMING.setdefault(cell_key, {"attempts": 0})
        t["started"] = time.time()
        t["worker"] = worker_id
        t["attempts"] = int(t.get("attempts", 0)) + 1


def _timing_finished(cell_key: str, status: str, reason: str | None = None) -> None:
    with _RUN_TIMING_LOCK:
        t = _CELL_TIMING.setdefault(cell_key, {"attempts": 1})
        now = time.time()
        t["ended"] = now
        t["status"] = status
        if t.get("started"):
            t["duration"] = round(now - t["started"], 2)
        if reason:
            t["reason"] = reason


def _timeline_sample(n_running: int, done: int, failed: int, cpu=None, ram=None) -> None:
    """Fold one observation into the current time bucket (called from /api/status while a run is
    active). active = peak running in the bucket; done/failed cumulative; cpu/ram = latest sample
    (so the report can chart CPU and RAM over the run)."""
    with _RUN_TIMING_LOCK:
        m = int(time.time() // _TIMELINE_BUCKET_S) * _TIMELINE_BUCKET_S
        if _RUN_TIMELINE and _RUN_TIMELINE[-1]["t"] == m:
            b = _RUN_TIMELINE[-1]
            b["active"] = max(b["active"], n_running)
            b["done"], b["failed"] = done, failed
        else:
            b = {"t": m, "active": n_running, "done": done, "failed": failed,
                 "cpu": None, "ram": None, "_cs": 0.0, "_cn": 0, "_rs": 0.0, "_rn": 0}
            _RUN_TIMELINE.append(b)
            if len(_RUN_TIMELINE) > 360:
                del _RUN_TIMELINE[:len(_RUN_TIMELINE) - 360]
        # Average every sample in the bucket (not the last one) so the CPU/RAM chart reads the true
        # ~20s level instead of a single instantaneous spike.
        if cpu is not None:
            b["_cs"] += cpu; b["_cn"] += 1; b["cpu"] = round(b["_cs"] / b["_cn"])
        if ram is not None:
            b["_rs"] += ram; b["_rn"] += 1; b["ram"] = round(b["_rs"] / b["_rn"])


def _report_exists() -> bool:
    """True if a benchmark report is available to open: the one written this session, or a
    meld-report.html left in the current world folder by any prior run (survives a restart)."""
    p = _LAST_REPORT.get("html")
    if p and Path(p).exists():
        return True
    try:
        return (master_world_path(create=False) / runreport.REPORT_HTML_NAME).exists()
    except Exception:
        return False


def _write_run_report() -> None:
    """Assemble + write the end-of-run benchmark (meld-report.json + .html) into the world
    folder. Best-effort: never raises into the run path."""
    try:
        with _RUN_LOCK:
            run = dict(_RUN)
        with _RUN_TIMING_LOCK:
            timing = {k: dict(v) for k, v in _CELL_TIMING.items()}
            timeline = [{k: v for k, v in b.items() if not k.startswith("_")} for b in _RUN_TIMELINE]
        with _PREFETCH_LOCK:
            pf_timings = dict(_PREFETCH.get("timings", {}))
        name = PROJECT.load().get("name", "Meld World")
        stats = _sys_stats()
        hw = _hw_specs(stats.get("drive"))
        machine = {"cores": os.cpu_count() or 0,   # logical CPUs / hardware threads (the parallelism budget)
                   "cores_phys": (psutil.cpu_count(logical=False) if psutil is not None else None),
                   "ram_gb": stats.get("ram_total_gb") or _total_ram_gb(),
                   "drive": stats.get("drive"),
                   "disk_free_gb": stats.get("disk_free_gb"),
                   "disk_total_gb": stats.get("disk_total_gb"),
                   "cpu_model": hw.get("cpu_model"), "ram_kind": hw.get("ram_kind"),
                   "ram_speed": hw.get("ram_speed"), "ram_modules": hw.get("ram_modules"),
                   "drive_type": hw.get("drive_type")}
        rep = runreport.build_report(
            world_name=name, meld_version="1.6.1", run=run, timing=timing,
            timeline=timeline, grid=PROJECT.load_grid(), prefetch_timings=pf_timings,
            settings=PROJECT.settings(), actual_mb=run.get("actual_mb"),
            max_workers=POOL.max_workers, machine=machine)
        paths = runreport.write_report(master_world_path(), rep)
        if paths.get("html"):
            _LAST_REPORT.update(html=str(paths["html"]), json=str(paths.get("json") or ""),
                                world=name, ts=time.time())
            log(f"[Report] benchmark written to the world folder ({Path(paths['html']).name})")
    except Exception as ex:  # noqa: BLE001
        log(f"[Report] could not write benchmark: {ex}")

# OSM prefetch state (for the live cyan-chunk overlay + status). Populated while a
# selection's OSM is being downloaded once and shared to all cells (src/prefetch.py).
_PREFETCH_LOCK = threading.Lock()
_PREFETCH = {"active": False, "done": False, "chunks": [], "started": None, "note": "",
             # phase: idle | osm | terrain | generating. terrain = the elevation-tile warm-up.
             "phase": "idle",
             "terrain": {"done": 0, "total": 0, "ok": 0, "failed": 0}}

# Region data-pack build progress (bulk elevation download). Separate from _PREFETCH so a pack
# build never collides with a generation's per-run prefetch overlay.
_DATAPACK_LOCK = threading.Lock()
_DATAPACK = {"active": False, "done": False, "note": "", "total": 0, "done_n": 0,
             "ok": 0, "absent": 0, "fail": 0, "region": None}
_DATAPACK_STOP = {"flag": False}

# OSM data-pack bake progress (slice a local .pbf into the shared OSM grid). Its own lock/dict/stop
# so an OSM bake and an elevation build are independent jobs and never report each other's progress.
_OSMPACK_LOCK = threading.Lock()
_OSMPACK = {"active": False, "done": False, "note": "", "total": 0, "done_n": 0,
            "ok": 0, "absent": 0, "fail": 0, "region": None}
_OSMPACK_STOP = {"flag": False}


def _osm_cache_dir() -> Path:
    """GLOBAL OSM prefetch cache (shared across all projects/worlds) so a new world over an
    already-fetched area reuses the verified OSM instead of re-downloading. Legacy per-project
    files at projects/<slug>/osm_cache stay on disk but are no longer written to; they're
    harmless (content-keyed names) and a re-fetch repopulates the global cache."""
    from src.prefetch import meld_osm_cache_dir
    return meld_osm_cache_dir()


# Per-cell health: after a cell merges, its log is scanned for markers that predict a
# visible artifact (truncated terrain-tile retries -> possible flat seam; ESA 404 ->
# missing land cover). Suspect cells are ringed in the UI and can be redone in one click.
_CELL_HEALTH_LOCK = threading.Lock()
_CELL_HEALTH: dict[str, dict] = {}

# Post-generation missing-region final check: interior holes / dropped cells detected on disk
# after a run, surfaced on the map (black square + ⚠️) and re-queueable through the normal retry
# path. Reset per project. See src/finalcheck.py.
_MISSING_LOCK = threading.Lock()
_MISSING: list[dict] = []


def _cell_health_path() -> Path:
    return PROJECT.root / "cell_health.json"


def _load_cell_health() -> None:
    global _CELL_HEALTH
    try:
        _CELL_HEALTH = json.loads(_cell_health_path().read_text(encoding="utf-8"))
    except Exception:
        _CELL_HEALTH = {}


def _save_cell_health() -> None:
    try:
        _cell_health_path().write_text(json.dumps(_CELL_HEALTH), encoding="utf-8")
    except Exception:
        pass


def _scan_cell_health(cell_key: str, out: str) -> None:
    """Scan a just-merged cell's log for artifact-predicting markers and record suspects."""
    tag = (cell_key or "").replace(",", "_")
    log_path = Path(out).parent.parent / "logs" / f"cell-{tag}.log"
    reasons = []
    try:
        txt = log_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        txt = ""
    if "is too small" in txt and "Re-downloading" in txt:
        reasons.append("terrain-tile-retry")     # truncated AWS elevation tile -> possible flat seam
    if "Failed to read ESA tile" in txt:          # the actual ESA WorldCover failure line (not the
        reasons.append("landcover-404")           # always-printed "Fetching ... ESA" banner)
    with _CELL_HEALTH_LOCK:
        if reasons:
            _CELL_HEALTH[cell_key] = {"suspect": True, "reasons": reasons}
        else:
            _CELL_HEALTH.pop(cell_key, None)
        _save_cell_health()


# Why a cell FAILED (distinct from the suspect markers above, which flag merged-but-risky cells).
# Surfaced in /api/status so the UI tooltip can say WHY a red cell failed instead of nothing.
_CELL_FAIL: dict = {}
_FAIL_MARKERS = [
    ("out of memory", "out of memory"), ("memoryerror", "out of memory"),
    ("no space left", "disk full"), ("os error 112", "disk full"), ("not enough space", "disk full"),
    ("rate limit", "Overpass rate limit"), ("too many requests", "Overpass rate limit"),
    ("timed out", "network timeout"), ("timeout", "network timeout"),
    ("panicked", "Arnis crashed (panic)"), ("failed to fetch", "data fetch failed"),
    ("overpass", "Overpass error"), ("connection", "network error"),
]


def _record_fail(cell_key: str, reason: str, out: str | None = None) -> None:
    """Store a concise failure reason. If `out` is given, scan the cell log tail for a more
    specific cause (OOM / disk full / rate limit / panic / network) before the generic fallback."""
    label = (reason or "failed").strip()
    if out is not None:
        tag = (cell_key or "").replace(",", "_")
        try:
            txt = (Path(out).parent.parent / "logs" / f"cell-{tag}.log").read_text(
                encoding="utf-8", errors="replace")[-6000:].lower()
            for marker, lab in _FAIL_MARKERS:
                if marker in txt:
                    label = lab
                    break
        except Exception:
            pass
    with _CELL_HEALTH_LOCK:
        _CELL_FAIL[cell_key] = label[:120]


def _clear_fail(cell_key: str) -> None:
    with _CELL_HEALTH_LOCK:
        _CELL_FAIL.pop(cell_key, None)


def _prefetch_on_chunk(chunk: dict) -> None:
    """Upsert a chunk by id so the UI can recolor it live as state changes."""
    with _PREFETCH_LOCK:
        chunks = _PREFETCH["chunks"]
        for i, c in enumerate(chunks):
            if c["id"] == chunk["id"]:
                chunks[i] = chunk
                return
        chunks.append(chunk)


def _safe_world_name(name: str) -> str:
    name = (name or "Meld World").strip() or "Meld World"
    return "".join(c for c in name if c not in '<>:"/\\|?*').strip() or "Meld World"


def _world_icon_src() -> Path | None:
    """Optional custom Minecraft world icon (icon.png, 64x64). Drop one of these
    in and every generated world gets it in its world-selection list."""
    for c in (BASE_DIR / "web" / "world_icon.png", BASE_DIR / "world_icon.png"):
        if c.exists():
            return c
    return None


def _apply_world_icon(world_path) -> None:
    src = _world_icon_src()
    if src:
        try:
            shutil.copy2(src, Path(world_path) / "icon.png")
        except Exception:
            pass


def master_world_path(create: bool = True) -> Path:
    """Path of the merged Minecraft world.

    Save location (settings.master_world_dir) is the PARENT FOLDER where worlds are
    kept — e.g. .minecraft/saves. The world itself is a SUBFOLDER named by the
    World Name, so several worlds (Meld World, Meld World 2, …) can live in one
    folder. Blank save location → the project folder."""
    s = PROJECT.settings()
    name = _safe_world_name(PROJECT.load().get("name", "Meld World"))
    d = (s.get("master_world_dir") or "").strip()
    parent = Path(d) if d else PROJECT.root
    p = parent / name
    if create:
        p.mkdir(parents=True, exist_ok=True)
        (p / "region").mkdir(parents=True, exist_ok=True)
        _apply_world_icon(p)
    return p


def _output_drive_ok() -> tuple[bool, str]:
    """Is the master-world save location reachable AND writable RIGHT NOW? Returns (ok, reason).

    Run BEFORE a generation so an offline/disconnected save drive (e.g. a flaky external/USB drive
    that dropped) fails the whole run fast with ONE clear message, instead of every cell reaching
    the merge step and throwing a cryptic per-cell '[WinError 433] A device which does not exist'."""
    try:
        parent = master_world_path(create=False).parent
    except Exception as ex:  # noqa: BLE001
        return False, f"cannot resolve the save location: {ex}"
    try:
        parent.mkdir(parents=True, exist_ok=True)
        probe = parent / f".meld_write_test.{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True, ""
    except OSError as ex:  # device offline / read-only / no space → WinError 433/21/112…
        return False, (f"Save drive not reachable or not writable: {parent} ({ex}). "
                       f"Reconnect the drive (or change the save location), then generate again.")


def _dir_size_mb(p: Path) -> float:
    try:
        total = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        return round(total / (1024 * 1024), 1)
    except Exception:
        return 0.0


def _cache_targets() -> dict:
    from src.prefetch import meld_cache_root
    root = meld_cache_root()
    return {"_root": root, "osm": root / "osm",
            "terrain": root / "arnis-tile-cache", "landcover": root / "arnis-landcover-cache"}


def _cache_info() -> dict:
    """Location + per-type size/file-count of the shared Meld cache. Computed on demand (the
    terrain dir can be 500k files, ~1-2s), NOT in the status poll."""
    t = _cache_targets()
    def info(p: Path) -> dict:
        try:
            files = sum(1 for f in p.rglob("*") if f.is_file()) if p.exists() else 0
        except Exception:
            files = 0
        return {"mb": _dir_size_mb(p), "files": files}
    return {"root": str(t["_root"]),
            "osm": info(t["osm"]), "terrain": info(t["terrain"]), "landcover": info(t["landcover"])}


@app.route("/api/cache", methods=["GET"])
def api_cache():
    """Where the shared cache lives + how big each part is (OSM / terrain / land-cover)."""
    return jsonify({"ok": True, **_cache_info()})


@app.route("/api/cache/clear", methods=["POST"])
def api_cache_clear():
    """Delete a cache type (osm | terrain | landcover | all). Refused while a generation runs
    (a child arnis may be reading it). Tiles/OSM just re-download next time they're needed."""
    if _run_active():
        return jsonify({"ok": False, "error": "stop the generation before clearing the cache"}), 409
    what = (request.json or {}).get("what", "")
    t = _cache_targets()
    sel = [t["osm"], t["terrain"], t["landcover"]] if what == "all" else \
        ([t[what]] if what in ("osm", "terrain", "landcover") else None)
    if sel is None:
        return jsonify({"ok": False, "error": "what must be osm | terrain | landcover | all"}), 400
    freed = 0.0
    for p in sel:
        if p.exists():
            freed += _dir_size_mb(p)
            shutil.rmtree(p, ignore_errors=True)
    log(f"[Cache] cleared {what}: freed ~{round(freed, 1)} MB")
    return jsonify({"ok": True, "freed_mb": round(freed, 1)})


# ── region data packs: bulk elevation download + coverage + preview + import ──────────────
def _datapack_selection():
    """Resolve the request's selection -> (bbox, rings, name). bbox derived from rings if absent."""
    d = request.json or {}
    bbox = d.get("bbox")
    rings = d.get("polygons") or ([d.get("polygon")] if d.get("polygon") else None)
    if not bbox and rings:
        bbox = dp.rings_bbox(rings)
    return bbox, rings, (d.get("name") or "").strip()


def _pack_zoom(bbox: dict | None = None) -> int:
    """The terrarium zoom the pack + preview + Arnis all use for the current project (auto = matched
    to scale). Uses the selection's centre latitude when available, else the project origin, else 45."""
    settings = PROJECT.settings()
    lat = 45.0
    try:
        if bbox:
            lat = (float(bbox["south"]) + float(bbox["north"])) / 2.0
        else:
            o = PROJECT.origin() or {}
            if o.get("lat") is not None:
                lat = float(o["lat"])
    except (TypeError, ValueError, KeyError):
        lat = 45.0
    return effective_elev_zoom(settings, lat)


@app.route("/api/datapack/coverage", methods=["POST"])
def api_datapack_coverage():
    """How much of the selection's elevation is already cached (covered% + missing tiles)."""
    bbox, rings, _ = _datapack_selection()
    if not bbox:
        return jsonify({"ok": False, "error": "bbox or polygon required"}), 400
    pz = _pack_zoom(bbox)
    cov = dp.coverage_elevation(bbox, zoom=pz)
    log(f"[Datapack] coverage: {cov['pct']}% ({cov['cached']}/{cov['total']} z{pz} elevation tiles, "
        f"{len(cov['missing'])} missing)")
    return jsonify({"ok": True, "elevation": cov, "bbox": bbox, "zoom": pz})


@app.route("/api/datapack/build", methods=["POST"])
def api_datapack_build():
    """Bulk-download the selection's missing z15 elevation tiles into the global cache."""
    if _run_active():
        return jsonify({"ok": False, "error": "stop the generation before building a data pack"}), 409
    with _DATAPACK_LOCK:
        if _DATAPACK["active"]:
            return jsonify({"ok": False, "error": "a data pack build is already running"}), 409
    bbox, rings, name = _datapack_selection()
    if not bbox:
        return jsonify({"ok": False, "error": "bbox or polygon required"}), 400
    # force=true re-downloads EVERY tile in the bbox (not just the missing ones) to replace stale /
    # flat / corrupt tiles that decode fine so coverage counts them as present. Use it on a small
    # bbox (e.g. the current map view) over a bad area, not a whole country.
    force = bool((request.json or {}).get("force"))
    pz = _pack_zoom(bbox)
    if force:
        missing = dp.tiles_for_bbox(bbox, zoom=pz)
    else:
        cov = dp.coverage_elevation(bbox, zoom=pz)
        missing = [(t["x"], t["y"]) for t in cov["missing"]]
    rid = dp.region_id(bbox, name)
    _DATAPACK_STOP["flag"] = False
    verb = "re-fetching" if force else "downloading"
    with _DATAPACK_LOCK:
        _DATAPACK.update(active=True, done=False, note=f"{verb} {len(missing)} z{pz} elevation tiles…",
                         total=len(missing), done_n=0, ok=0, absent=0, fail=0, region=name or rid)

    _logged = [0]

    def _prog(done_n, total, ok, skip, absent, fail):
        with _DATAPACK_LOCK:
            _DATAPACK.update(done_n=done_n, total=total, ok=ok, absent=absent, fail=fail)
        # Log to the web LOG card on ~5% steps (and at the end) so progress is visible there too.
        if total and (done_n - _logged[0] >= max(2000, total // 20) or done_n >= total):
            _logged[0] = done_n
            log(f"[Datapack] {done_n}/{total} tiles · {ok} new, {absent} off-grid, {fail} failed")

    def _worker():
        _t0 = time.time()
        try:
            conc = int(PROJECT.settings().get("datapack_tile_concurrency", 16) or 16)
            log(f"[Datapack] {verb} {len(missing)} z{pz} elevation tiles ({conc} at a time)…")
            res = dp.download_tiles(missing, _prog, zoom=pz, concurrency=conc, force=force,
                                    should_stop=lambda: _DATAPACK_STOP["flag"])
            cov2 = dp.coverage_elevation(bbox, zoom=pz)
            dp.write_manifest(rid, name=name, bbox=bbox, cov=cov2, polygons=rings)
            dp.clear_preview_cache()   # drop rendered overviews so re-fetched tiles show fresh
            _el = time.time() - _t0
            with _DATAPACK_LOCK:
                _DATAPACK.update(active=False, done=True, elapsed=round(_el, 1),
                                 note=f"done in {_el:.0f}s: {cov2['cached']}/{cov2['total']} tiles cached ({cov2['pct']}%)")
            log(f"[Datapack] {name or rid}: {res['ok']} new, {res['skip']} cached, "
                f"{res['absent']} off-grid, {res['fail']} failed -> {cov2['pct']}% covered in {_el:.0f}s")
        except Exception as ex:  # noqa: BLE001
            with _DATAPACK_LOCK:
                _DATAPACK.update(active=False, done=True, note=f"error: {ex}")
            log(f"[Datapack] error: {ex}")

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"ok": True, "region_id": rid, "missing": len(missing),
                    "total": (len(missing) if force else cov["total"])})


@app.route("/api/datapack/tile-info")
def api_datapack_tile_info():
    """One tile's facts for the click popup: cached?, size, decoded height min/max/mean, flat/no-data,
    lat/lon bbox. Lets you click a dark band and see whether it's a real hole."""
    try:
        z = int(request.args.get("z", _pack_zoom()))
        x = int(request.args.get("x")); y = int(request.args.get("y"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "z, x, y required"}), 400
    return jsonify({"ok": True, **dp.tile_info(x, y, z)})


@app.route("/api/datapack/repair", methods=["POST"])
def api_datapack_repair():
    """Scan the selection's cached tiles and overzoom-fix any all-black no-data holes (the terrarium
    z14/z15 gaps that show as dark bands + flat in-game dips). Doesn't re-download good tiles."""
    if _run_active():
        return jsonify({"ok": False, "error": "stop the generation before repairing tiles"}), 409
    with _DATAPACK_LOCK:
        if _DATAPACK["active"]:
            return jsonify({"ok": False, "error": "a data pack job is already running"}), 409
    # global=true: scan EVERY cached z15 tile (one scandir) and fix every no-data hole anywhere in the
    # cache in a single pass — no selection, no clicking holes. Otherwise repair the current selection.
    is_global = bool((request.json or {}).get("global"))
    if is_global:
        pz = _pack_zoom()
        tiles = sorted(dp._cached_xy(zoom=pz))
        name = "repair-all"
        if not tiles:
            return jsonify({"ok": False, "error": "no cached tiles to repair"}), 400
    else:
        bbox, rings, name = _datapack_selection()
        if not bbox:
            return jsonify({"ok": False, "error": "bbox or polygon required"}), 400
        pz = _pack_zoom(bbox)
        tiles = dp.tiles_for_bbox(bbox, zoom=pz)
    _DATAPACK_STOP["flag"] = False
    scope = "whole cache" if is_global else "selection"
    with _DATAPACK_LOCK:
        _DATAPACK.update(active=True, done=False, note=f"scanning {len(tiles)} tiles ({scope}) for no-data holes…",
                         total=len(tiles), done_n=0, ok=0, absent=0, fail=0, region=name or "repair")
    _logged = [0]

    def _prog(done_n, total, fixed, unfixable):
        with _DATAPACK_LOCK:
            _DATAPACK.update(done_n=done_n, total=total, ok=fixed, fail=unfixable)
        if total and (done_n - _logged[0] >= max(2000, total // 20) or done_n >= total):
            _logged[0] = done_n
            log(f"[Datapack] repair {done_n}/{total} · {fixed} holes fixed, {unfixable} unfixable")

    def _worker():
        try:
            conc = int(PROJECT.settings().get("datapack_tile_concurrency", 16) or 16)
            log(f"[Datapack] repairing no-data holes across {len(tiles)} z{pz} tiles ({scope}, {conc} at a time)…")
            res = dp.repair_nodata(tiles, _prog, zoom=pz, concurrency=conc,
                                   should_stop=lambda: _DATAPACK_STOP["flag"])
            dp.clear_preview_cache()   # re-render the fixed tiles
            with _DATAPACK_LOCK:
                _DATAPACK.update(active=False, done=True,
                                 note=f"done: {res['fixed']} holes fixed, {res['unfixable']} unfixable")
            log(f"[Datapack] repair done: {res['fixed']} holes fixed, {res['unfixable']} unfixable, "
                f"{res['checked']} checked")
        except Exception as ex:  # noqa: BLE001
            with _DATAPACK_LOCK:
                _DATAPACK.update(active=False, done=True, note=f"error: {ex}")
            log(f"[Datapack] repair error: {ex}")

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"ok": True, "tiles": len(tiles)})


def _bbox_grid(bbox: dict, step_deg: float = 0.06) -> list:
    """Split a bbox into a grid of <=step_deg sub-bboxes, so the regional warm runs as many
    bounded child processes (visible per-sweep progress + a stop point between each) instead
    of one giant process. A ~0.06° cell is a few km — one polite regional fetch each."""
    s, n = float(bbox["south"]), float(bbox["north"])
    w, e = float(bbox["west"]), float(bbox["east"])
    out = []
    y = s
    while y < n - 1e-9:
        y2 = min(n, y + step_deg)
        x = w
        while x < e - 1e-9:
            x2 = min(e, x + step_deg)
            out.append({"south": y, "north": y2, "west": x, "east": x2})
            x = x2
        y = y2
    return out or [bbox]


@app.route("/api/datapack/prefetch-regional", methods=["POST"])
def api_datapack_prefetch_regional():
    """Pre-warm the regional high-res elevation cache (IGN / USGS / GSI) for the selection,
    the regional-provider counterpart to 'Download elevation' (which fills the AWS terrarium
    cache). Runs the fork's terrain warm in regional-only mode over a grid of the selection so
    the later parallel cells read tiles from disk instead of rate-limiting the provider."""
    if _run_active():
        return jsonify({"ok": False, "error": "stop the generation first"}), 409
    with _DATAPACK_LOCK:
        if _DATAPACK["active"]:
            return jsonify({"ok": False, "error": "a data-pack job is already running"}), 409
    bbox, rings, name = _datapack_selection()
    if not bbox:
        return jsonify({"ok": False, "error": "bbox or polygon required"}), 400
    exe = resolve_arnis_exe()
    if exe is None:
        return jsonify({"ok": False, "error": "arnis binary not found"}), 400
    settings = PROJECT.settings()
    scale = float(settings.get("scale", 1.0) or 1.0)
    lat = (float(bbox["south"]) + float(bbox["north"])) / 2.0
    ez = effective_elev_zoom(settings, lat)
    tiles = _bbox_grid(bbox)
    _DATAPACK_STOP["flag"] = False
    with _DATAPACK_LOCK:
        _DATAPACK.update(active=True, done=False, total=len(tiles), done_n=0, ok=0, absent=0, fail=0,
                         region=name or "regional-prefetch",
                         note=f"warming regional elevation over {len(tiles)} area(s)…")

    def _prog(done_n, total, ok, failed):
        with _DATAPACK_LOCK:
            _DATAPACK.update(done_n=done_n, total=total, ok=ok, fail=failed)

    def _worker():
        _t0 = time.time()
        try:
            log(f"[Regional] warming IGN/USGS/GSI elevation over {len(tiles)} area(s) at scale {scale}…")
            res = run_terrain_prefetch(tiles, str(exe), log, _prog, elev_zoom=ez, scale=scale,
                                       regional_only=True,
                                       should_stop=lambda: _DATAPACK_STOP["flag"])
            _el = time.time() - _t0
            prov = res.get("regional_provider") or "regional provider"
            rok, rfail = res.get("regional_ok", 0), res.get("regional_failed", 0)
            if rok == 0 and rfail == 0:
                msg = ("no regional provider covers this area (US/France/Spain/Japan only) — "
                       "nothing warmed")
            else:
                msg = f"done in {_el:.0f}s: {prov} warmed for {rok}/{len(tiles)} area(s)" \
                      + (f", {rfail} failed" if rfail else "")
            with _DATAPACK_LOCK:
                _DATAPACK.update(active=False, done=True, elapsed=round(_el, 1), note=msg)
            log(f"[Regional] {msg}")
        except Exception as ex:  # noqa: BLE001
            with _DATAPACK_LOCK:
                _DATAPACK.update(active=False, done=True, note=f"error: {ex}")
            log(f"[Regional] error: {ex}")

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"ok": True, "areas": len(tiles), "zoom": ez})


@app.route("/api/datapack/bake-mapterhorn", methods=["POST"])
def api_datapack_bake_mapterhorn():
    """Pre-download the elevation TILE cache generation reads (Mapterhorn global terrain, or the
    regional/AWS provider the fork picks for the area) for the selection, so generation runs
    offline and is never rate-limited. Runs the fork's --prewarm-elevation over a grid of the
    selection at the generation scale so the cached zoom matches what the cells later request."""
    if _run_active():
        return jsonify({"ok": False, "error": "stop the generation first"}), 409
    with _DATAPACK_LOCK:
        if _DATAPACK["active"]:
            return jsonify({"ok": False, "error": "a data-pack job is already running"}), 409
    bbox, rings, name = _datapack_selection()
    if not bbox:
        return jsonify({"ok": False, "error": "bbox or polygon required"}), 400
    exe = resolve_arnis_exe()
    if exe is None:
        return jsonify({"ok": False, "error": "arnis binary not found"}), 400
    settings = PROJECT.settings()
    scale = float(settings.get("scale", 1.0) or 1.0)
    aws_only = bool(settings.get("aws_only_elevation"))
    tiles = _bbox_grid(bbox)
    _DATAPACK_STOP["flag"] = False
    with _DATAPACK_LOCK:
        _DATAPACK.update(active=True, done=False, total=len(tiles), done_n=0, ok=0, absent=0, fail=0,
                         region=name or "mapterhorn-bake",
                         note=f"baking elevation tiles over {len(tiles)} area(s)…")

    def _prog(done_n, total, ok, failed):
        with _DATAPACK_LOCK:
            _DATAPACK.update(done_n=done_n, total=total, ok=ok, fail=failed)

    def _worker():
        _t0 = time.time()
        try:
            log(f"[Mapterhorn] baking elevation tiles over {len(tiles)} area(s) at scale {scale}…")
            res = run_mapterhorn_bake(tiles, str(exe), log, _prog, scale=scale, aws_only=aws_only,
                                      should_stop=lambda: _DATAPACK_STOP["flag"])
            _el = time.time() - _t0
            prov = res.get("provider") or "elevation"
            msg = (f"done in {_el:.0f}s: {prov} - {res.get('ok', 0)} tile(s) cached, "
                   f"{res.get('absent', 0)} ocean/absent"
                   + (f", {res.get('failed', 0)} failed" if res.get("failed") else ""))
            with _DATAPACK_LOCK:
                _DATAPACK.update(active=False, done=True, elapsed=round(_el, 1), note=msg)
            log(f"[Mapterhorn] {msg}")
        except Exception as ex:  # noqa: BLE001
            with _DATAPACK_LOCK:
                _DATAPACK.update(active=False, done=True, note=f"error: {ex}")
            log(f"[Mapterhorn] error: {ex}")

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"ok": True, "areas": len(tiles)})


@app.route("/api/datapack/status")
def api_datapack_status():
    with _DATAPACK_LOCK:
        return jsonify({"ok": True, **_DATAPACK})


@app.route("/api/datapack/stop", methods=["POST"])
def api_datapack_stop():
    _DATAPACK_STOP["flag"] = True
    return jsonify({"ok": True})


@app.route("/api/datapack/list")
def api_datapack_list():
    """Every downloaded pack + its live elevation coverage% (reused across all projects)."""
    try:
        return jsonify({"ok": True, "packs": dp.list_packs()})
    except Exception as ex:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(ex), "packs": []})


@app.route("/api/datapack/import", methods=["POST"])
def api_datapack_import():
    """Drop-in: import an external folder of pack files (tiles + osm json) into the global cache."""
    if _run_active():
        return jsonify({"ok": False, "error": "stop the generation before importing"}), 409
    folder = ((request.json or {}).get("folder") or "").strip()
    if not folder:
        return jsonify({"ok": False, "error": "folder required"}), 400
    res = dp.import_pack_folder(folder, log=log)
    return jsonify(res), (200 if res.get("ok") else 400)


@app.route("/api/terrain-tile/<int:z>/<int:x>/<int:y>.png")
def api_terrain_tile(z, x, y):
    """Decoded height preview tile (grayscale|hillshade). Native z15 + downsampled overviews z12-14
    so it shows when zoomed out. Normalized by the GLOBAL elevation range (the project lock, or the
    ?lo=&hi= override) so a flat tile reads as its true gray instead of solid black. Missing -> red."""
    if z < dp.PREVIEW_MIN_ZOOM or z > dp.PACK_ZOOM:
        return ("", 204)

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    lo = _f(request.args.get("lo"))
    hi = _f(request.args.get("hi"))
    if lo is None or hi is None:                      # default to the project's locked elevation range
        ev = PROJECT.elevation() or {}
        if ev.get("min_m") is not None and ev.get("max_m") is not None:
            lo = float(ev["min_m"]) if lo is None else lo
            hi = float(ev["max_m"]) if hi is None else hi
    # Always normalize against a SINGLE global range, never per-tile — otherwise each tile uses its
    # own min/max and adjacent tiles render at different brightness, which looks like stripes / a
    # grid / black flat tiles even though the underlying data is continuous. Fall back to a wide
    # fixed range when there's no elevation lock yet.
    if lo is None:
        lo = -100.0
    if hi is None:
        hi = 3000.0
    try:
        png = dp.render_tile(z, x, y, lo=lo, hi=hi, mode=request.args.get("mode", "grayscale"),
                             pack_zoom=_pack_zoom())
    except Exception:
        return ("", 204)
    return Response(png, mimetype="image/png")


@app.route("/api/datapack/zoom")
def api_datapack_zoom():
    """The effective elevation zoom for the current project (auto = scale-matched) + the recommended
    one, so the UI can label the dropdown and set the preview layer's maxNativeZoom."""
    from src.coords import recommended_elev_zoom
    s = PROJECT.settings()
    o = PROJECT.origin() or {}
    lat = float(o["lat"]) if o.get("lat") is not None else 45.0
    scale = float(s.get("scale", 1.0) or 1.0)
    return jsonify({"ok": True, "effective": effective_elev_zoom(s, lat),
                    "recommended": recommended_elev_zoom(scale, lat),
                    "setting": s.get("elevation_zoom", "auto"), "scale": scale})


# ── region OSM packs: bake a local .pbf into the shared OSM grid (offline OSM) ─────────────
def _osm_gen_bbox(bbox: dict) -> dict:
    """Expand a drawn selection by the SAME seam buffer + prefetch margin the generator adds to
    every cell, so OSM coverage/bake target exactly the z9 tiles generation will request — not just
    the drawn rectangle. Without this, edge cells' seam-expanded bboxes reach into tiles the bake
    skipped, so coverage reads 100% yet generation still fetches the ring (the user-seen gap)."""
    from src.coords import mpd_lon
    from src.constants import METERS_PER_DEG_LAT, REGION_BLOCKS, CHUNK_BLOCKS
    s = PROJECT.settings()
    scale = float(s.get("scale", 1.0) or 1.0)
    seam = int(s.get("seam_buffer_chunks", 8) or 0)
    margin = float(s.get("prefetch_margin_m", 256) or 0)
    # Edge cells snap OUTWARD to the global region grid (up to one 512-block region past the drawn
    # edge), THEN get the seam buffer, THEN the prefetch margin. Pad by all three so the baked tiles
    # are a superset of every tile generation will request — no live-fetched ring.
    pad_blocks = REGION_BLOCKS + seam * CHUNK_BLOCKS
    pad_m = pad_blocks / scale + margin if scale > 0 else margin
    try:
        clat = (float(bbox["south"]) + float(bbox["north"])) / 2.0
        d_lat = pad_m / METERS_PER_DEG_LAT
        d_lon = pad_m / (mpd_lon(clat) or METERS_PER_DEG_LAT)
        return {"south": bbox["south"] - d_lat, "west": bbox["west"] - d_lon,
                "north": bbox["north"] + d_lat, "east": bbox["east"] + d_lon}
    except (TypeError, ValueError, KeyError):
        return bbox


@app.route("/api/osmpack/coverage", methods=["POST"])
def api_osmpack_coverage():
    """How much of the selection's OSM is already baked/cached on the stable grid (covered% +
    missing tiles). Pure disk, no pyosmium needed."""
    bbox, rings, _ = _datapack_selection()
    if not bbox:
        return jsonify({"ok": False, "error": "bbox or polygon required"}), 400
    cov = op.coverage_osm(_osm_gen_bbox(bbox))   # seam-expanded → matches what generation needs
    log(f"[OSM pack] coverage: {cov['pct']}% ({cov['cached']}/{cov['total']} z{cov['grid_z']} OSM "
        f"tiles, {len(cov['missing'])} missing)")
    return jsonify({"ok": True, "osm": cov, "bbox": bbox})


@app.route("/api/osmpack/scan", methods=["POST"])
def api_osmpack_scan():
    """List the .pbf files in a drop folder + their header bbox, so the UI can confirm before baking."""
    folder = ((request.json or {}).get("folder") or "").strip()
    if not folder:
        return jsonify({"ok": False, "error": "folder required"}), 400
    return jsonify(op.scan_pbf_folder(folder))


@app.route("/api/osmpack/bake", methods=["POST"])
def api_osmpack_bake():
    """Slice the .pbf file(s) in `folder` into the selection's missing OSM grid tiles. Offline:
    no Overpass. Mirrors the elevation build's lock + daemon-thread + cooperative-stop pattern."""
    if _run_active():
        return jsonify({"ok": False, "error": "stop the generation before baking OSM"}), 409
    # Reserve the slot AND reset the stop flag atomically under the lock, so two near-simultaneous
    # bake POSTs can't both pass the active-check and start two workers over the same tiles.
    with _OSMPACK_LOCK:
        if _OSMPACK["active"]:
            return jsonify({"ok": False, "error": "an OSM bake is already running"}), 409
        _OSMPACK.update(active=True, done=False, note="preparing OSM bake…",
                        total=0, done_n=0, ok=0, absent=0, fail=0, region=None)
        _OSMPACK_STOP["flag"] = False

    def _release(err, code):
        with _OSMPACK_LOCK:
            _OSMPACK.update(active=False, done=True, note=f"error: {err}")
        return jsonify({"ok": False, "error": err}), code

    bbox, rings, name = _datapack_selection()
    if not bbox:
        return _release("bbox or polygon required", 400)
    folder = ((request.json or {}).get("folder") or "").strip()
    if not folder:
        return _release("a folder of .pbf files is required", 400)
    scan = op.scan_pbf_folder(folder)
    if not scan.get("ok") or not scan.get("files"):
        return _release(scan.get("error") or "no .pbf files in folder", 400)
    pbf_paths = [f["path"] for f in scan["files"]]
    force = bool((request.json or {}).get("force"))
    gbb = _osm_gen_bbox(bbox)                     # seam-expanded → bake the ring generation will need
    cov = op.coverage_osm(gbb)
    tiles = (osm_grid.grid_tiles_for_bbox(gbb) if force
             else [(t["x"], t["y"]) for t in cov["missing"]])
    rid = dp.region_id(bbox, name)
    with _OSMPACK_LOCK:
        _OSMPACK.update(total=len(tiles), region=name or rid,
                        note=f"baking {len(tiles)} OSM tile(s) from {len(pbf_paths)} .pbf…")

    _logged = [0]

    def _prog(done_n, total, ok, skip, absent, fail):
        with _OSMPACK_LOCK:
            _OSMPACK.update(done_n=done_n, total=total, ok=ok, absent=absent, fail=fail)
        if total and (done_n - _logged[0] >= max(50, total // 20) or done_n >= total):
            _logged[0] = done_n
            log(f"[OSM pack] {done_n}/{total} tiles · {ok} baked, {skip} cached, {fail} failed")

    def _worker():
        _t0 = time.time()
        try:
            log(f"[OSM pack] baking {len(tiles)} z{osm_grid.OSM_GRID_Z} tile(s) from "
                f"{len(pbf_paths)} .pbf file(s)…")
            _s = PROJECT.settings()
            _bw = int(_s.get("osm_bake_workers", 4) or 4)
            # Parallel front end (one process per .pbf, then merge seams) — bake_tiles_parallel falls
            # back to the sequential bake_tiles for <2 overlapping .pbf or any pool error. Set bake
            # workers to 1 to force the sequential path. Output is identical either way (verified).
            _bake = op.bake_tiles_parallel if (_bw > 1 and _s.get("osm_bake_parallel", True)) else op.bake_tiles
            _kw = {"workers": _bw} if _bake is op.bake_tiles_parallel else {}
            res = _bake(pbf_paths, tiles, on_progress=_prog,
                        should_stop=lambda: _OSMPACK_STOP["flag"], log=log, force=force, **_kw)
            cov2 = op.coverage_osm(gbb)
            try:
                el = dp.coverage_elevation(bbox, zoom=_pack_zoom(bbox))
                dp.write_manifest(rid, name=name, bbox=bbox, cov=el, polygons=rings, osm=cov2)
            except Exception:  # noqa: BLE001
                pass
            _el = time.time() - _t0
            with _OSMPACK_LOCK:
                _OSMPACK.update(active=False, done=True, elapsed=round(_el, 1),
                                note=f"done in {_el:.0f}s: {cov2['cached']}/{cov2['total']} OSM tiles cached ({cov2['pct']}%)")
            log(f"[OSM pack] {name or rid}: {res['baked']} baked, {res['skip']} cached, "
                f"{res['elements']} elements -> {cov2['pct']}% covered in {_el:.0f}s")
        except Exception as ex:  # noqa: BLE001
            with _OSMPACK_LOCK:
                _OSMPACK.update(active=False, done=True, note=f"error: {ex}")
            log(f"[OSM pack] error: {ex}")

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"ok": True, "region_id": rid, "tiles": len(tiles), "pbf": len(pbf_paths)})


@app.route("/api/osmpack/status")
def api_osmpack_status():
    with _OSMPACK_LOCK:
        return jsonify({"ok": True, **_OSMPACK})


@app.route("/api/osmpack/stop", methods=["POST"])
def api_osmpack_stop():
    _OSMPACK_STOP["flag"] = True
    return jsonify({"ok": True})


# ── Overture buildings pre-warm (data-pack style) ─────────────────────────────
# Buildings come from Overture Maps GeoParquet — a per-cell HTTP byte-range fetch in the fork. The fork
# caches each range to <cache>/arnis-overture-cache/ranges/, but on a cold cache the FIRST cell stalls
# downloading them. Pre-warm fetches the region's ranges ONCE, in parallel, up front (one
# `arnis --prewarm-overture --bbox` per sub-tile), so the parallel cells read them from disk. Only
# matters with buildings ON; with --no-buildings the fork skips Overture entirely.
_OVERTURE_LOCK = threading.Lock()
_OVERTURE = {"active": False, "done": False, "note": "", "total": 0, "done_n": 0,
             "ok": 0, "fail": 0, "mb": 0.0}
_OVERTURE_STOP = {"flag": False}


def _overture_ranges_dir() -> Path:
    from src.prefetch import meld_cache_root
    return meld_cache_root() / "arnis-overture-cache" / "ranges"


def _overture_cached_mb() -> float:
    d = _overture_ranges_dir()
    if not d.exists():
        return 0.0
    total = 0
    try:
        for f in d.glob("*.bin"):
            try:
                total += f.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return round(total / 1_048_576, 1)


def _split_bbox_grid(bb: dict, target: int = 96) -> list[dict]:
    """Split a bbox into ~`target` sub-tiles so the pre-warm runs in parallel and each sub stays under
    the fork's per-fetch building cap. Step is adaptive so a city and a country both yield ~target."""
    s, w, n, e = bb["south"], bb["west"], bb["north"], bb["east"]
    span_lat, span_lon = max(n - s, 1e-6), max(e - w, 1e-6)
    step = max(0.04, math.sqrt(span_lat * span_lon / max(target, 1)))
    out = []
    z = s
    while z < n - 1e-9:
        z2 = min(z + step, n)
        x = w
        while x < e - 1e-9:
            x2 = min(x + step, e)
            out.append({"south": z, "west": x, "north": z2, "east": x2})
            x = x2
        z = z2
    return out[:512]   # hard cap so a huge selection never spawns thousands of processes


@app.route("/api/overture/coverage", methods=["POST"])
def api_overture_coverage():
    """How much Overture building data is cached locally (MB + range-file count). Overture has no tile
    grid, so this is a size, not a percent — it grows as cells (or a pre-warm) fetch ranges."""
    return jsonify({"ok": True, "mb": _overture_cached_mb(),
                    "files": sum(1 for _ in _overture_ranges_dir().glob("*.bin")) if _overture_ranges_dir().exists() else 0})


@app.route("/api/overture/prewarm", methods=["POST"])
def api_overture_prewarm():
    """Download the selection's Overture building ranges up front, in parallel, into the shared cache,
    so a later buildings-ON build never stalls on a cold fetch. Mirrors the OSM-pack lock + daemon +
    cooperative-stop pattern; drives `arnis --prewarm-overture --bbox` per sub-tile."""
    if _run_active():
        return jsonify({"ok": False, "error": "stop the generation before pre-warming"}), 409
    with _OVERTURE_LOCK:
        if _OVERTURE["active"]:
            return jsonify({"ok": False, "error": "an Overture pre-warm is already running"}), 409
        _OVERTURE.update(active=True, done=False, note="preparing Overture pre-warm…",
                         total=0, done_n=0, ok=0, fail=0, mb=_overture_cached_mb())
        _OVERTURE_STOP["flag"] = False

    def _release(err, code):
        with _OVERTURE_LOCK:
            _OVERTURE.update(active=False, done=True, note=f"error: {err}")
        return jsonify({"ok": False, "error": err}), code

    exe = resolve_arnis_exe()
    if not exe:
        return _release("arnis.exe not found", 400)
    bbox, rings, name = _datapack_selection()
    if not bbox:
        return _release("bbox or polygon required", 400)
    scale = float(PROJECT.settings().get("scale", 1.0) or 1.0)
    subs = _split_bbox_grid(_osm_gen_bbox(bbox))
    from src.prefetch import meld_cache_root
    root = str(meld_cache_root())
    with _OVERTURE_LOCK:
        _OVERTURE.update(total=len(subs), note=f"pre-warming Overture over {len(subs)} tile(s)…")

    def _one(sub):
        if _OVERTURE_STOP["flag"]:
            return False
        cmd = [str(exe), "--prewarm-overture", "--scale", str(scale),
               "--bbox", f"{sub['south']},{sub['west']},{sub['north']},{sub['east']}"]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                               env={**os.environ, "ARNIS_CACHE_ROOT": root})
            return p.returncode == 0
        except Exception:  # noqa: BLE001
            return False

    def _worker():
        from concurrent.futures import ThreadPoolExecutor, as_completed
        done_n = ok = fail = 0
        t0 = time.time()
        try:
            log(f"[Overture] pre-warming buildings over {len(subs)} tile(s) (4 at a time)…")
            with ThreadPoolExecutor(max_workers=4) as pool:
                futs = [pool.submit(_one, sub) for sub in subs]
                for fut in as_completed(futs):
                    done_n += 1
                    if fut.result():
                        ok += 1
                    else:
                        fail += 1
                    mb = _overture_cached_mb()
                    with _OVERTURE_LOCK:
                        _OVERTURE.update(done_n=done_n, ok=ok, fail=fail, mb=mb)
                    if done_n % 4 == 0 or done_n == len(subs):
                        log(f"[Overture] pre-warm {done_n}/{len(subs)} tile(s) · {mb:.0f} MB cached")
                    if _OVERTURE_STOP["flag"]:
                        break
            mb = _overture_cached_mb()
            el = time.time() - t0
            with _OVERTURE_LOCK:
                _OVERTURE.update(active=False, done=True, mb=mb, elapsed=round(el, 1),
                                 note=f"done in {el:.0f}s: {ok}/{len(subs)} tile(s), {mb:.0f} MB cached")
            log(f"[Overture] pre-warm done in {el:.0f}s — {mb:.0f} MB of building data cached locally")
        except Exception as ex:  # noqa: BLE001
            with _OVERTURE_LOCK:
                _OVERTURE.update(active=False, done=True, note=f"error: {ex}")
            log(f"[Overture] pre-warm error: {ex}")

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"ok": True, "tiles": len(subs)})


@app.route("/api/overture/status")
def api_overture_status():
    with _OVERTURE_LOCK:
        return jsonify({"ok": True, **_OVERTURE})


@app.route("/api/overture/stop", methods=["POST"])
def api_overture_stop():
    _OVERTURE_STOP["flag"] = True
    return jsonify({"ok": True})


# ── world metadata sidecar ────────────────────────────────────────────────────
# Saved INTO the world folder so the exact origin, elevation lock + seed, and the
# generation settings travel with the world. Load it later (api/world/load-meta) to
# regenerate or CONTINUE the same world with identical coordinates and terrain.
WORLD_META_NAME = "meld-world.json"

# Settings that define how the world LOOKS/tiles (reproducible). Host/run-specific
# settings (where it saves, how many workers, prefetch/timeout) are intentionally
# excluded so loading a world's meta never hijacks the current machine's setup.
_META_SKIP_SETTINGS = {
    "master_world_dir", "max_workers", "prefetch_enabled", "prefetch_margin_m",
    "timeout", "overpass_url", "prune_cell_after_merge",
}


def _world_meta_dict() -> dict:
    data = PROJECT.load()
    return {
        "meld_version": "1.6.1",
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "name": data.get("name", "Meld World"),
        "origin": data.get("origin", {}),                  # lat, lon, locked
        "elevation": data.get("elevation", {}),            # min_m, max_m, seed, locked
        "settings": PROJECT.settings(),                    # scale, cell size, ground level, etc.
        "merged_cells": sorted(k for k, v in PROJECT.load_grid().items() if v == "merged"),
    }


def write_world_meta(world_path=None) -> Path | None:
    """Write meld-world.json into the saved world folder (origin + elevation lock +
    seed + settings). Best-effort: never raises into the merge/save path."""
    try:
        wp = Path(world_path) if world_path else master_world_path(create=True)
        wp.mkdir(parents=True, exist_ok=True)
        out = wp / WORLD_META_NAME
        out.write_text(json.dumps(_world_meta_dict(), indent=2), encoding="utf-8")
        return out
    except Exception:
        return None


def read_world_meta(path):
    """Read a meld-world.json given either the world folder or the json file path."""
    try:
        p = Path(path)
        if p.is_dir():
            p = p / WORLD_META_NAME
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def log(msg: str) -> None:
    line = str(msg)
    _LOG.append(line)
    if len(_LOG) > 2000:
        del _LOG[:1000]
    print(line, flush=True)


# Now that log() exists, point all Arnis caches at the shared Meld-local folder + migrate any
# legacy AppData caches into it (runs once at startup, before any generation spawns a child).
_setup_shared_cache()


# Arnis prints hundreds of lines per cell (one per tile, per element). We can't
# dump all of that into the live log tab without burying the RUN/MERGE lines, so
# every line is captured to a persistent per-cell file (logs/cell-*.log) and only
# the diagnostically useful lines are surfaced into the _LOG buffer the UI polls.
# These keywords are chosen to make the elevation-fetch path visible: tile
# downloads, retries, rate-limit recovery, provider fallback, and flat-ground.
_ARNIS_LOG_KEYWORDS = (
    "warning", "error", "failed", "panic", "fallback", "falling back",
    "elevation", "still missing", "retry", "could not be fetched",
    "flat ground", "unavailable", "corrupted", "land cover", "downloading",
)
# Per-tile chatter that matches a keyword above but is pure noise at scale.
_ARNIS_LOG_EXCLUDE = (
    "fetching tile x=", "loading cached tile", "bilinear sampling",
)


def _arnis_should_surface(text: str) -> bool:
    low = text.lower()
    if any(x in low for x in _ARNIS_LOG_EXCLUDE):
        return False
    return any(k in low for k in _ARNIS_LOG_KEYWORDS)


# ── arnis binary resolution ────────────────────────────────────────────────

def resolve_arnis_exe() -> Path | None:
    """Find the Arnis binary next to Meld. Platform-aware: on Linux/macOS we look for `arnis`
    (no extension) and NEVER pick up a stray Windows `arnis.exe`, which would die with
    '[Errno 8] Exec format error'. On Windows we prefer `arnis.exe` then a bare `arnis`."""
    roots = [BASE_DIR, BASE_DIR.parent,
             BASE_DIR.parent / "arnis-source" / "target" / "release"]
    if sys.platform == "win32":
        names = ["arnis.exe", "arnis"]
    else:
        names = ["arnis"]                  # a .exe on Linux/macOS is the wrong arch — skip it
    for root in roots:
        for name in names:
            c = root / name
            if c.exists() and c.is_file():
                return c
    return None


_ARNIS_HELP_CACHE: dict = {}


def _arnis_supports(flag: str) -> bool:
    """Whether the arnis binary advertises `flag` in --help (cached per exe path). Lets Meld
    pass new flags like --stream-to-disk only when the deployed binary actually has them, so an
    older binary never dies on an unknown argument."""
    exe = resolve_arnis_exe()
    if not exe:
        return False
    key = str(exe)
    if key not in _ARNIS_HELP_CACHE:
        try:
            out = subprocess.run([str(exe), "--help"], capture_output=True, text=True,
                                 timeout=20, encoding="utf-8", errors="replace")
            _ARNIS_HELP_CACHE[key] = (out.stdout or "") + (out.stderr or "")
        except Exception:
            _ARNIS_HELP_CACHE[key] = ""
    return flag in _ARNIS_HELP_CACHE[key]


# ── worker runner: generate one cell, then merge it ────────────────────────

def _runner(job: dict, state: dict) -> bool:
    cell_key = job["cell_key"]
    out = job["output_path"]
    settings = job["settings"]
    origin = job["origin"]
    elevation = job["elevation"]
    seed = int((elevation or {}).get("seed", 1) or 1)
    world_name = job.get("world_name", "Meld World")

    exe = resolve_arnis_exe()
    if not exe:
        want = "arnis.exe" if sys.platform == "win32" else "arnis"
        build = "cargo build --release" + ("" if sys.platform == "win32" else " --no-default-features")
        state.update(message=f"Arnis binary not found. Put '{want}' next to server.py "
                             f"(or in the parent folder), or build it: {build}.")
        log(state["message"])
        return False

    PROJECT.set_cell_status(cell_key, "running")
    _timing_started(cell_key, state.get("worker_id"))
    clean_output_dir(out)
    Path(out).mkdir(parents=True, exist_ok=True)

    seam = int(settings.get("seam_buffer_chunks", 8) or 0)
    scale_f = float(settings.get("scale", 1.0) or 1.0)
    # Anchor generation to the cell's REGION corner + region size derived from the
    # cell_key — NOT the raw user selection. cell_bbox() places the SW corner on an
    # exact region boundary (rx*size, rz*size), so Arnis always generates whole,
    # region-aligned cells that the canonical merge keeps cleanly. Falls back to the
    # passed bbox only for a bare bbox job with no cell_key.
    base_bbox = job["bbox"]
    cell_size = 1   # regions per axis; >=2 makes Arnis take its in-process tile-parallel path
    parts = cell_key.split(",") if cell_key else []
    if len(parts) == 3 and origin.get("lat") is not None:
        rx, rz, size = int(parts[0]), int(parts[1]), int(parts[2])
        cell_size = size
        base_bbox = cell_bbox(rx, rz, size, origin["lat"], origin["lon"], scale_f)
    arnis_bbox = expand_bbox_for_seam(base_bbox, seam, origin, scale_f)

    _lt = PROJECT.root / "loot_table.json"
    cmd = build_arnis_cmd(str(exe), arnis_bbox, out, settings, origin, elevation, seed,
                          osm_file=job.get("osm_file"),
                          loot_table=str(_lt) if _lt.exists() else None)
    if job.get("osm_file"):
        log(f"  [{cell_key}] using pre-fetched OSM (no Overpass call)")
    log("RUN " + " ".join(cmd))

    # Full Arnis stdout/stderr capture → persistent per-cell file (survives the
    # post-merge prune that wipes the cell output dir), plus filtered surfacing
    # into the live Meld log tab. `arnis_log_verbose` setting pushes every line.
    cell_tag = (cell_key or "bbox").replace(",", "_")
    cell_log_fp = None
    try:
        logs_dir = Path(out).parent.parent / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        cell_log_fp = open(logs_dir / f"cell-{cell_tag}.log", "w",
                           encoding="utf-8", errors="replace")
        cell_log_fp.write("RUN " + " ".join(cmd) + "\n")
        cell_log_fp.flush()
    except Exception:
        cell_log_fp = None
    verbose = bool(settings.get("arnis_log_verbose", False))
    _last_surfaced = {"line": None}

    def on_line(text: str):
        if not text:
            return
        state["message"] = text[:140]
        state["progress"] = parse_progress(text, state.get("progress", 0))
        if cell_log_fp is not None:
            try:
                cell_log_fp.write(text + "\n")
            except Exception:
                pass
        # De-duplicate consecutive identical lines (retry spam) before surfacing.
        if (verbose or _arnis_should_surface(text)) and text != _last_surfaced["line"]:
            _last_surfaced["line"] = text
            log(f"[{cell_tag}] {text}")

    def on_proc(p):
        state["process"] = p   # published so /api/stop can terminate this run

    # Per-child env (merged Arnis reads these; an older binary ignores them):
    #  - RAYON_NUM_THREADS: a size>=2 cell uses Arnis's in-process tile parallelism. We
    #    divide a core budget across workers, BUT never collapse a worker to 1 thread:
    #    workers are in different phases (fetch/prep/tiles/save) at any instant, so only a
    #    fraction are CPU-bound at once. A per-worker floor (min_threads_per_worker, default
    #    2) keeps each cell's tile parallelism alive — mild oversubscription that the OS
    #    shares across phases, not thrash. cpu_target_pct is the budget (default 100% of
    #    cores; set >100 to oversubscribe harder, <100 to leave headroom).
    #  - ARNIS_STREAM_TO_DISK=1: region eviction so big test cells (8x8/16x16) don't OOM.
    #    Env, not a CLI flag (upstream removed the flag). Forced for size>=8 or when the
    #    user enables the setting; smaller cells let Arnis's own RAM heuristic decide.
    # Live CPU budget: re-read these from the CURRENT settings (NOT the job snapshot), so
    # changing CPU budget / threads-per-worker / worker count MID-RUN flows to the next cells a
    # worker picks up. The world invariants (scale, origin, seed, elevation, bbox) stay frozen in
    # the snapshot above, so a mid-run tweak never desyncs the world. max_workers is already live
    # (the pool resizes), and POOL.max_workers below reflects the current value.
    _live = PROJECT.settings()
    cpu_pct = float(_live.get("cpu_target_pct", settings.get("cpu_target_pct", 100)) or 100)
    min_threads = max(1, int(_live.get("min_threads_per_worker", settings.get("min_threads_per_worker", 2)) or 1))
    core_budget = max(1, int((os.cpu_count() or 4) * cpu_pct / 100.0))
    rayon_threads = max(min_threads, core_budget // max(1, POOL.max_workers))
    log(f"  [{cell_key}] {rayon_threads} threads/cell (cpu {int(cpu_pct)}% · {POOL.max_workers} workers, live)")
    child_env = {"RAYON_NUM_THREADS": str(rayon_threads)}
    if settings.get("stream_to_disk") or cell_size >= 8:
        child_env["ARNIS_STREAM_TO_DISK"] = "1"
    # Elevation source zoom: caps Arnis's terrain zoom so the whole world generates at the chosen
    # detail (auto = scale-matched). Matches the zoom the data pack downloaded, so it's a cache hit.
    child_env["ARNIS_ELEV_ZOOM"] = str(effective_elev_zoom(settings, float(origin.get("lat", 45.0))))

    ok = run_arnis(cmd, cwd=str(BASE_DIR), on_line=on_line, on_proc=on_proc, env=child_env)
    if cell_log_fp is not None:
        try:
            cell_log_fp.write(f"\n=== arnis exit ok={ok} ===\n")
            cell_log_fp.close()
        except Exception:
            pass
    if not ok:
        PROJECT.set_cell_status(cell_key, "failed")
        _record_fail(cell_key, "Arnis generation failed", out=out)
        state.update(message="Arnis generation failed.")
        return False

    world_dir = find_world_dir(out)
    if not world_dir:
        PROJECT.set_cell_status(cell_key, "failed")
        _record_fail(cell_key, "no world produced", out=out)
        state.update(message="No world dir produced.")
        return False

    # Name the per-cell subregion world "Meld Sub World N" (stable, no duplicates).
    try:
        from src.level_dat import patch_level_name
        n = PROJECT.subworld_number(cell_key)
        patch_level_name(Path(world_dir) / "level.dat", f"Meld Sub World {n}")
    except Exception:
        pass

    state.update(progress=96, message="Merging…")
    try:
        # overwrite_collisions=True is safe under the v1 uniform grid: each cell
        # owns a disjoint canonical region rectangle, so any collision is the
        # SAME cell re-merging its own regions (a re-run/repair), never two
        # different cells fighting over one region.
        res = None
        for _attempt in range(3):
            try:
                master = str(master_world_path())
                res = merge_cell_into_master(
                    world_dir, master, cell_key,
                    seam_buffer_chunks=seam, world_name=world_name,
                    overwrite_collisions=True,
                )
                break
            except OSError as _oe:
                # A flaky external save drive can briefly drop mid-merge (WinError 433 no-such-device
                # / 21 not-ready / 112 / 1167 not-connected). Retry a couple times with short backoff
                # so a transient blip self-heals; a truly-removed drive still fails fast after ~1.5s
                # (and the queue-time pre-flight already rejects a persistently-offline drive).
                if getattr(_oe, "winerror", None) in (433, 21, 112, 1167) and _attempt < 2:
                    log(f"  [Merge] {cell_key} save-drive blip ({_oe}); retry {_attempt + 1}/2…")
                    time.sleep(0.5 * (_attempt + 1))
                    continue
                raise
        log(f"MERGE {cell_key}: +{res['regions_copied']} regions, "
            f"-{res['regions_skipped']} seam, level.dat={res['level_dat']}")
    except MeldCoordinateDriftError as ex:
        PROJECT.set_cell_status(cell_key, "drift")
        _record_fail(cell_key, "coordinate drift (scale/origin changed)")
        state.update(message=f"DRIFT GUARD: {ex}")
        log("ERROR " + str(ex))
        return False
    except MeldCollisionError as ex:
        PROJECT.set_cell_status(cell_key, "collision")
        _record_fail(cell_key, "region collision (overlapping cells)")
        state.update(message=f"COLLISION: {ex}")
        log("ERROR " + str(ex))
        return False
    except Exception as ex:  # noqa: BLE001
        PROJECT.set_cell_status(cell_key, "failed")
        _record_fail(cell_key, f"merge error: {ex}", out=out)
        state.update(message=f"Merge error: {ex}")
        log("ERROR " + str(ex))
        return False

    PROJECT.set_cell_status(cell_key, "merged")
    _clear_fail(cell_key)              # succeeded — drop any prior failure reason
    _scan_cell_health(cell_key, out)   # flag the cell if its log predicts an artifact
    _post_merge_export_hook(cell_key)  # D: drop stale .linear; overlap: stream the new regions
    # Prune the per-cell subregion world now that its canonical regions live in the
    # master world — avoids doubling storage. Toggle via settings.prune_cell_after_merge.
    if settings.get("prune_cell_after_merge", True):
        try:
            shutil.rmtree(out, ignore_errors=True)
            log(f"  [Prune] removed cell subregion {cell_key} (merged into master)")
        except Exception:
            pass
    else:
        # Keeping the subregion: strip its seam-buffer region files so it holds ONLY
        # its canonical regions. Kept subregions are then disjoint, so their region/
        # files can be drag-and-dropped straight into one master world.
        try:
            n = strip_buffer_regions(world_dir, cell_key)
            log(f"  [Keep] {cell_key}: stripped {n} seam-buffer region files (canonical-only, drag-drop ready)")
        except Exception:
            pass
    # Refresh the world's reproducibility sidecar so origin + elevation + seed +
    # settings stay current with the latest merged state.
    write_world_meta(Path(master))
    state.update(progress=100, message="Merged.")
    return True


# A cell whose failure reason contains any of these is treated as TRANSIENT and auto-retried
# (network blips, rate limits, transient OOM). Deterministic failures (drift / collision / disk
# full / panic / merge error) are NOT retried — a retry would just fail the same way.
_RETRYABLE_FAIL = ("timeout", "rate limit", "network", "fetch failed",
                   "out of memory", "generation failed", "no world produced", "overpass",
                   # strict regional elevation: a rate-limited IGN/USGS fetch errors the
                   # cell instead of silently using AWS; retrying usually succeeds off
                   # the (by then) warm tile cache
                   "elevation fetch failed")
_MAX_CELL_RETRIES = 2


def _export_in_flight() -> bool:
    with _EXPORT_LOCK:
        return _EXPORT["phase"] in ("starting", "compressing", "archiving", "converting")


def _start_export_job(kind: str, *, force_keep_both: bool = False,
                      force: bool = False) -> dict:
    """Spawn an export/compression job in a daemon thread. kind:
      'compress' → run_world_export with the selected format (resumable post-pass).
      'convert'  → convert_linear_world: every region/*.linear back to vanilla .mca.
    Returns a small dict describing what started. Refuses to start a second job while one
    is in flight. Honours the safety contract + resumable manifest in src/export.py.
    Safeguards: E (refuse if THIS world is open in Minecraft), B (force keep-both when asked),
    A (disk preflight on compress). `force` overrides E + A (the op is safe-by-verify)."""
    s = PROJECT.settings()
    world = master_world_path(create=False)
    if not (world / "region").is_dir():
        return {"ok": False, "error": "no world to export yet"}
    if _export_in_flight():
        return {"ok": False, "error": "an export is already running"}
    if not force and _world_locked(world):   # safeguard E: this world's session.lock is held
        return {"ok": False, "locked": True,
                "error": "This world is open in Minecraft — close it first (its files are "
                "locked). If it's not actually open, retry to force."}

    keep_both = bool(s.get("export_keep_both", True))
    stream_and_free = bool(s.get("export_stream_and_free", False))
    if stream_and_free:
        keep_both = False   # low-disk: free each raw after its compressed copy verifies
    if force_keep_both:     # safeguard B: never delete raw of an incomplete/failed world
        keep_both = True
        stream_and_free = False
    # Separate-folder linear builds a sibling world and never touches the source: keep-both
    # is implicit, and delete-raw / stream-free do not apply.
    _separate_linear = (kind == "compress"
                        and _export_destination(s) == "separate"
                        and str(s.get("export_format", "none") or "none").strip().lower() == exportmod.FMT_LINEAR)
    if _separate_linear:
        keep_both = True
        stream_and_free = False

    # Safeguard A: disk preflight before a compress (convert shrinks, so it's skipped).
    if kind == "compress" and not force:
        _fmt = str(s.get("export_format", "none") or "none").strip().lower()
        if _fmt in exportmod.VALID_FORMATS and _fmt != "none":
            pf = exportmod.preflight_export(world, _fmt, keep_both=keep_both)
            if not pf["enough"]:
                return {"ok": False, "error":
                        f"low disk: ~{pf['needed'] // 1048576} MB needed, "
                        f"{(pf['free'] or 0) // 1048576} MB free. Turn on "
                        f"'Delete raw after compressing', free space, or retry to force.",
                        "preflight": {k: pf[k] for k in
                                      ("raw", "out", "additional", "needed", "free", "enough")}}

    def _on_prog(p):
        with _EXPORT_LOCK:
            _EXPORT.update(format=p.format, phase=p.phase, total=p.total, done=p.done,
                           failed=p.failed, raw_mb=round(p.raw_bytes / 1048576, 2),
                           out_mb=round(p.out_bytes / 1048576, 2),
                           ratio=round(p.ratio, 2), message=p.message,
                           rate_per_min=p.rate_per_min, eta_s=p.eta_s, elapsed_s=p.elapsed_s)

    if kind == "convert":
        def _worker():
            with _EXPORT_LOCK:
                _EXPORT.update(format="linear2mca", phase="starting", total=0, done=0,
                               failed=0, raw_mb=0.0, out_mb=0.0, ratio=0.0, message="", out_name=None,
                               rate_per_min=0.0, eta_s=-1, elapsed_s=0)
            log(f"[Export] linear→mca: starting (keep_both={keep_both})")
            try:
                prog = exportmod.convert_linear_world(
                    world, keep_both=keep_both,
                    workers=s.get("export_compression_workers", 0), on_progress=_on_prog)
                with _EXPORT_LOCK:
                    _EXPORT["out_name"] = f"{prog.done} .mca region(s)"
                log(f"[Export] linear→mca: {prog.phase} — {prog.done}/{prog.total} ok, {prog.failed} failed")
            except Exception as e:
                with _EXPORT_LOCK:
                    _EXPORT.update(phase="error", message=str(e))
                log(f"[Export] linear→mca: ERROR {e}")
        threading.Thread(target=_worker, daemon=True, name="export-convert").start()
        return {"ok": True, "kind": "convert"}

    # kind == 'compress'
    fmt = str(s.get("export_format", "none") or "none").strip().lower()
    if fmt == "none" or fmt not in exportmod.VALID_FORMATS:
        return {"ok": False, "error": "export format is None — nothing to compress"}
    if fmt == exportmod.FMT_BLINEAR and exportmod.resolve_region_converter() is None:
        return {"ok": False, "error":
                "B_Linear needs the region-convert binary — none found for this OS in "
                "region-convert/bin (or run region-convert/build.sh|build.ps1)."}

    # Separate-folder builds a sibling world, leaving the source untouched. linear honours the
    # destination toggle; blinear is ALWAYS a sibling "<name> [BLinear]" world (the Rust tool
    # only writes to --output). keep-both is implicit for both (the original is never modified).
    dest_world = None
    if fmt == exportmod.FMT_BLINEAR:
        dest_world = world.parent / exportmod.format_folder_name(world.name, fmt)
    elif _export_destination(s) == "separate" and fmt == exportmod.FMT_LINEAR:
        dest_world = world.parent / exportmod.format_folder_name(world.name, fmt)
    blinear_variant = str(s.get("export_blinear_variant", "v3") or "v3").strip().lower()
    if blinear_variant not in ("v2", "v3"):
        blinear_variant = "v3"
    # What to do with the master .mca after the [BLinear] world verifies.
    blinear_keep = str(s.get("export_blinear_keep", "both") or "both").strip().lower()
    if blinear_keep not in ("both", "blinear_only", "archive_mca"):
        blinear_keep = "both"
    if force_keep_both:        # safeguard B: a failed/partial world keeps its raw .mca
        blinear_keep = "both"

    def _worker():
        nworkers = exportmod.resolve_workers(s.get("export_compression_workers", 0))
        with _EXPORT_LOCK:
            _EXPORT.update(format=fmt, phase="starting", total=0, done=0, failed=0,
                           raw_mb=0.0, out_mb=0.0, ratio=0.0, message="", out_name=None,
                           rate_per_min=0.0, eta_s=-1, elapsed_s=0)
        _dest_note = f", -> {dest_world.name}/" if dest_world else ""
        log(f"[Export] {fmt}: post-pass starting (workers={nworkers}, keep_both={keep_both}{_dest_note})")
        try:
            prog = exportmod.run_world_export(
                world, fmt,
                level=s.get("export_level", 0),
                workers=s.get("export_compression_workers", 0),
                keep_both=keep_both, stream_and_free=stream_and_free,
                dest_world=dest_world, blinear_variant=blinear_variant,
                on_progress=_on_prog)
            if fmt == exportmod.FMT_ZIP:
                out_name = world.name + ".zip"
            elif fmt == exportmod.FMT_TARZST:
                out_name = world.name + ".tar.zst"
            elif fmt == exportmod.FMT_BLINEAR:
                out_name = f"{prog.done} .b_linear region(s) -> {dest_world.name}/"
            elif dest_world is not None:
                out_name = f"{prog.done} .linear region(s) -> {dest_world.name}/"
            else:
                out_name = f"{prog.done} .linear region(s)"
            # After B_Linear VERIFIES, apply the keep-mode to the master .mca. Never runs on a
            # failed/partial blinear (prog.phase != done) — the .mca is only touched once the
            # [BLinear] world (and, for archive_mca, the zip) is proven good.
            if fmt == exportmod.FMT_BLINEAR and prog.phase == "done" and blinear_keep != "both":
                if blinear_keep == "blinear_only":
                    freed = exportmod._free_world_raws(world)
                    out_name += f" · deleted {freed} master .mca (B_Linear only)"
                    log(f"[Export] blinear keep=blinear_only — removed {freed} master .mca")
                elif blinear_keep == "archive_mca":
                    log("[Export] blinear keep=archive_mca — zipping master .mca then freeing")
                    zprog = exportmod.run_world_export(
                        world, exportmod.FMT_ZIP, level=6,
                        workers=s.get("export_compression_workers", 0),
                        keep_both=False, on_progress=_on_prog)   # builds <name>.zip + frees .mca on verify
                    if zprog.phase == "done":
                        out_name += f" · archived .mca -> {world.name}.zip"
                    else:
                        out_name += " · .mca archive FAILED, kept .mca"
                        log(f"[Export] blinear archive_mca: zip {zprog.phase} {zprog.message}")
            with _EXPORT_LOCK:
                _EXPORT["out_name"] = out_name
            log(f"[Export] {fmt}: {prog.phase} — {prog.done}/{prog.total} ok, "
                f"{prog.failed} failed, {prog.ratio:.2f}x → {out_name}")
        except Exception as e:
            with _EXPORT_LOCK:
                _EXPORT.update(phase="error", message=str(e))
            log(f"[Export] {fmt}: ERROR {e}")
    threading.Thread(target=_worker, daemon=True, name="export-pass").start()
    return {"ok": True, "kind": "compress", "format": fmt,
            "dest": (dest_world.name if dest_world else None)}


def _maybe_write_map_item() -> None:
    """Once per finished run: if the 'map_item' setting is on, add a locked filled-map of the
    whole world to the player's inventory. Runs a single post-merge `--map-item-only` arnis pass
    over the assembled master world (Meld builds it from many per-cell worlds, so a per-cell map
    would be wrong). Must run BEFORE any export converts the .mca regions to .linear. Best-effort:
    never raises into the run-completion path, and a missing/failed pass just skips the map."""
    try:
        s = PROJECT.settings()
        if not s.get("map_item"):
            return
        exe = resolve_arnis_exe()
        if exe is None:
            log("[MapItem] skipped: arnis binary not found")
            return
        world = master_world_path(create=False)
        if not (world / "region").is_dir():
            log("[MapItem] skipped: master world has no region data")
            return
        # --bbox is required by the CLI but ignored in map-item-only mode (the footprint is
        # read from the saved regions), so a tiny placeholder is fine.
        cmd = [str(exe), "--bbox", "0.0,0.0,0.001,0.001",
               "--output-dir", str(world), "--map-item-only"]
        log("[MapItem] writing world map item into the player inventory…")
        r = subprocess.run(cmd, cwd=str(BASE_DIR), capture_output=True, text=True, timeout=1800)
        if r.returncode == 0:
            log("[MapItem] done — locked filled-map added to the world")
        else:
            log(f"[MapItem] failed (exit {r.returncode}): {(r.stderr or '').strip()[:300]}")
    except Exception as e:  # noqa: BLE001
        log(f"[MapItem] warning: {e}")


def _maybe_run_export() -> None:
    """Once per finished run: if a streaming-overlap session is live, finalize it (then a
    cheap idempotent linear sweep catches any straggler); otherwise run the post-pass. A run
    with any failed cell forces keep-both (safeguard B) so an incomplete world's raw survives."""
    s = PROJECT.settings()
    fmt = str(s.get("export_format", "none") or "none").strip().lower()
    with _STREAM_LOCK:
        has_session = _STREAM["session"] is not None
    if fmt == "none" or fmt not in exportmod.VALID_FORMATS:
        if has_session:
            threading.Thread(target=_finish_stream_session, daemon=True, name="export-finish").start()
        return
    with _RUN_LOCK:
        run_id = _RUN.get("started")
    if _EXPORT_STARTED.get("run") == run_id and not has_session:
        return   # already exported this run
    _EXPORT_STARTED["run"] = run_id
    force_keep = _run_had_failures()
    if force_keep:
        log("[Export] run had failures — forcing keep-both so the raw world survives (safeguard B)")

    if has_session:
        def _finish():
            _finish_stream_session()
            if fmt == exportmod.FMT_LINEAR:
                # idempotent sweep: compress any region the stream missed (manifest skips done)
                _start_export_job("compress", force_keep_both=force_keep, force=True)
        threading.Thread(target=_finish, daemon=True, name="export-finish").start()
        return
    _start_export_job("compress", force_keep_both=force_keep)


def _scan_missing_regions() -> list[dict]:
    """Scan the merged world for interior missing/empty regions (finalcheck) and store the result
    in _MISSING for /api/status. Best-effort: never raises into the run-completion path. Should run
    BEFORE any export converts .mca -> .linear (finalcheck tolerates .linear too, but .mca lets it
    detect header-only 'empty' regions)."""
    try:
        origin = PROJECT.origin()
        if origin.get("lat") is None:
            return []
        scale = float(PROJECT.settings().get("scale", 1.0) or 1.0)
        world = master_world_path(create=False)
        missing = finalcheck.find_missing_regions(PROJECT.load_grid(), world, origin, scale)
        with _MISSING_LOCK:
            _MISSING.clear()
            _MISSING.extend(missing)
        if missing:
            cells = finalcheck.missing_cell_keys(missing)
            log(f"[FinalCheck] ⚠️ {len(missing)} missing region(s) across {len(cells)} cell(s) — "
                f"use 'Retry missing' to regenerate")
        else:
            log("[FinalCheck] no missing regions — world looks complete")
        return missing
    except Exception as e:  # noqa: BLE001
        log(f"[FinalCheck] warning: {e}")
        return []


def _on_complete(job, ok, err):
    if not ok:
        ck = job.get("cell_key")
        with _CELL_HEALTH_LOCK:
            reason = _CELL_FAIL.get(ck, "")
        rlow = reason.lower()
        status = PROJECT.load_grid().get(ck)
        deterministic = (status in ("drift", "collision")
                         or any(x in rlow for x in ("disk full", "panic", "merge error")))
        transient = any(t in rlow for t in _RETRYABLE_FAIL)
        retries = int(job.get("_retries", 0))
        if (transient and not deterministic and retries < _MAX_CELL_RETRIES
                and not getattr(POOL, "_stopped", False)):
            job = {**job, "_retries": retries + 1}
            PROJECT.set_cell_status(ck, "queued")
            _clear_fail(ck)
            log(f"[Retry] {ck} failed ({reason or 'transient'}) — retry {retries + 1}/{_MAX_CELL_RETRIES}")
            POOL.submit(job)
            return   # re-queued: don't count it done/failed yet (the retry will)

    # Terminal outcome (a retry returned above): stamp the cell's wall-time + final status/reason.
    ck = job.get("cell_key")
    if ok:
        _timing_finished(ck, "merged")
    else:
        with _CELL_HEALTH_LOCK:
            _fin_reason = _CELL_FAIL.get(ck, "")
        _timing_finished(ck, PROJECT.load_grid().get(ck) or "failed", _fin_reason or None)

    run_done = False
    with _RUN_LOCK:
        if ok:
            _RUN["done"] += 1
        else:
            _RUN["failed"] += 1
        if _RUN["total"] and (_RUN["done"] + _RUN["failed"]) >= _RUN["total"] and not _RUN["ended"]:
            _RUN["ended"] = time.time()
            try:
                _RUN["actual_mb"] = _dir_size_mb(master_world_path(create=False))
            except Exception:
                _RUN["actual_mb"] = None
            # Final snapshot of the world's origin/elevation/settings sidecar.
            write_world_meta()
            run_done = True
    if run_done:
        _write_run_report()   # benchmark JSON + HTML into the world folder (best-effort)
        _maybe_write_map_item()  # add the world map item (before export may convert regions)
        _scan_missing_regions()  # ⚠️ flag interior holes (before export converts .mca -> .linear)
        _maybe_run_export()   # compress/export the finished world if a format is selected


POOL.configure(_runner, _on_complete)


# ── routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    # No-cache so a server update is never hidden behind a browser-cached copy of the page (a plain
    # refresh would otherwise re-serve a stale index.html and the new UI/buttons "wouldn't appear").
    resp = send_from_directory(str(BASE_DIR / "web"), "index.html")
    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/assets/<path:fname>")
def assets(fname):
    """Serve static web assets (logo, etc.) from web/ under an explicit prefix so
    there is no catch-all rule that could shadow the /api POST routes."""
    if "/" in fname or "\\" in fname:
        abort(404)
    target = BASE_DIR / "web" / fname
    if not target.is_file():
        abort(404)
    return send_from_directory(str(BASE_DIR / "web"), fname)


@app.route("/docs/")
def docs_index():
    """Serve the bundled docs site (site/) so the in-app Guide can link to it."""
    return send_from_directory(str(BASE_DIR / "site"), "index.html")


@app.route("/docs/<path:fname>")
def docs_file(fname):
    return send_from_directory(str(BASE_DIR / "site"), fname)   # safe_join guards traversal


@app.route("/api/state")
def api_state():
    return jsonify({
        "origin": PROJECT.origin(),
        "settings": PROJECT.settings(),
        "elevation": PROJECT.elevation(),
        "grid": PROJECT.load_grid(),
        "selection": PROJECT.load_selection(),   # drawn area, per-project, so a restart redraws it
        "name": PROJECT.load().get("name", "Meld World"),
        "arnis_found": resolve_arnis_exe() is not None,
        "master_world": str(master_world_path(create=False)),   # the world subfolder
        "save_location": (PROJECT.settings().get("master_world_dir") or "").strip() or str(PROJECT.root),
        "world_icon": _world_icon_src() is not None,
        "ram_gb": _total_ram_gb(),   # lets the UI set RAM-based worker warning thresholds
    })


@app.route("/api/name", methods=["POST"])
def api_name():
    d = request.json or {}
    name = PROJECT.set_name(d.get("name") or "Meld World")
    # Patch the master world's LevelName in place if it already exists.
    dat = master_world_path(create=False) / "level.dat"
    if dat.exists():
        from src.level_dat import patch_level_name, gold_name
        patch_level_name(dat, gold_name(name))
        write_world_meta()   # keep the sidecar's name in sync with the rename
    return jsonify({"ok": True, "name": name})


@app.route("/api/worlds")
def api_worlds():
    grid = PROJECT.load_grid()
    cells = []
    for cell_key, status in sorted(grid.items()):
        sub = PROJECT.cells_dir / cell_key.replace(",", "_")
        cells.append({
            "cell_key": cell_key,
            "status": status,
            "has_source": sub.exists(),
            "size_mb": _dir_size_mb(sub) if sub.exists() else 0.0,
        })
    mp = master_world_path(create=False)
    region_dir = mp / "region"
    master = {
        "path": str(mp),
        "name": PROJECT.load().get("name", "Meld World"),
        "exists": region_dir.exists(),
        "regions": len(list(region_dir.glob("*.mca"))) if region_dir.exists() else 0,
        "size_mb": _dir_size_mb(mp) if mp.exists() else 0.0,
        "has_meta": (mp / WORLD_META_NAME).exists(),
    }
    return jsonify({"cells": cells, "master": master})


@app.route("/api/world/meta")
def api_world_meta():
    """Read the meld-world.json sidecar for a world folder (defaults to the current
    master world). Pass ?path=<world folder or json file> to read another world's."""
    src = (request.args.get("path") or "").strip()
    meta = read_world_meta(src) if src else read_world_meta(master_world_path(create=False))
    if not meta:
        return jsonify({"ok": False, "error": f"no {WORLD_META_NAME} found"}), 404
    return jsonify({"ok": True, "meta": meta})


@app.route("/api/world/load-meta", methods=["POST"])
def api_world_load_meta():
    """Load a saved world's origin + elevation lock + seed + settings into THIS
    project, so you can regenerate or continue/extend that world with identical
    coordinates and terrain. `path` = the world folder or its meld-world.json.

    Save location and worker/prefetch settings are intentionally NOT applied, they
    stay whatever this machine is set to. To extend the SAME world in place, point
    the save location + world name at it first, then load-meta."""
    d = request.json or {}
    # Accept either the parsed JSON object directly (UI file upload) or a path on
    # disk to a world folder / meld-world.json.
    if isinstance(d.get("meta"), dict):
        meta = d["meta"]
    else:
        src = (d.get("path") or "").strip()
        if not src:
            return jsonify({"ok": False, "error": "meta object or path required (world folder or meld-world.json)"}), 400
        meta = read_world_meta(src)
        if not meta:
            return jsonify({"ok": False, "error": f"no {WORLD_META_NAME} found at {src}"}), 404
    if not isinstance(meta, dict) or not (meta.get("origin") or meta.get("elevation") or meta.get("settings")):
        return jsonify({"ok": False, "error": "not a Meld world file (need origin/elevation/settings)"}), 400

    applied = []
    o = meta.get("origin") or {}
    if o.get("lat") is not None and o.get("lon") is not None:
        PROJECT.set_origin(float(o["lat"]), float(o["lon"]), force=True)
        applied.append("origin")

    s = {k: v for k, v in (meta.get("settings") or {}).items() if k not in _META_SKIP_SETTINGS}
    if s:
        PROJECT.update_settings(s)
        applied.append("settings")

    ev = meta.get("elevation") or {}
    if ev.get("min_m") is not None and ev.get("max_m") is not None:
        PROJECT.set_elevation_lock(float(ev["min_m"]), float(ev["max_m"]), ev.get("seed"))
        applied.append("elevation")
    elif ev.get("seed") is not None:
        PROJECT.set_seed(ev.get("seed"))
        applied.append("seed")

    if d.get("apply_name") and meta.get("name"):
        PROJECT.set_name(meta["name"])
        applied.append("name")

    src_label = (d.get("path") or "").strip() or "imported file"
    log(f"  [Load] applied {', '.join(applied) or 'nothing'} from {src_label}")
    return jsonify({"ok": True, "applied": applied, "meta": meta})


@app.route("/api/world/delete", methods=["POST"])
def api_world_delete():
    """Delete one cell's subregion: its source world AND its canonical regions in
    the master world. Resets the cell to 'planned' so it can be regenerated."""
    from src.coords import canonical_region_bounds
    d = request.json or {}
    cell_key = d.get("cell_key")
    if not cell_key:
        return jsonify({"ok": False, "error": "cell_key required"}), 400

    sub = PROJECT.cells_dir / cell_key.replace(",", "_")
    if sub.exists():
        shutil.rmtree(sub, ignore_errors=True)

    removed = 0
    b = canonical_region_bounds(cell_key)
    mregion = master_world_path(create=False) / "region"
    if b and mregion.exists():
        rx_min, rx_max, rz_min, rz_max = b
        for rx in range(rx_min, rx_max + 1):
            for rz in range(rz_min, rz_max + 1):
                f = mregion / f"r.{rx}.{rz}.mca"
                if f.exists():
                    try:
                        f.unlink()
                        removed += 1
                    except Exception:
                        pass

    grid = PROJECT.load_grid()
    if cell_key in grid:
        grid[cell_key] = "planned"
        PROJECT.save_grid(grid)
    log(f"  [Delete] {cell_key}: removed source + {removed} master region(s)")
    return jsonify({"ok": True, "cell_key": cell_key, "removed_regions": removed})


@app.route("/api/pick-folder", methods=["POST"])
def api_pick_folder():
    """Open a native folder-select dialog on the local machine (the server runs on
    the user's box) and return the chosen path. Uses a throwaway tkinter subprocess
    so it can't block or crash the server. An optional `title` labels the dialog (used
    for the Save location, the .pbf folder, and the import folder browse buttons)."""
    raw_title = ((request.json or {}).get("title") or "Select a folder")
    # The title is interpolated into the subprocess source, so keep it to a safe, quote-free set.
    title = re.sub(r"[^A-Za-z0-9 ._/()-]", "", str(raw_title))[:80] or "Select a folder"
    code = (
        "import tkinter as tk, tkinter.filedialog as fd\n"
        "r=tk.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
        f"p=fd.askdirectory(title='{title}')\n"
        "print(p or '')\n"
    )
    try:
        out = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True, timeout=180)
        lines = [ln for ln in (out.stdout or "").splitlines() if ln.strip()]
        return jsonify({"ok": True, "path": lines[-1].strip() if lines else ""})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/open-folder", methods=["POST"])
def api_open_folder():
    """Open the world's save folder in the OS file browser (server runs locally). Climbs to the
    first folder that ACTUALLY exists — the world, else its save location, else the Meld project
    folder — so it always opens something instead of silently failing on a not-yet-created or
    moved/disconnected path (e.g. a save location left pointing at an old drive)."""
    target = master_world_path(create=False)
    while not target.exists() and target.parent != target:
        target = target.parent
    if not target.exists():
        target = PROJECT.root   # local Meld project folder always exists
    try:
        if sys.platform == "win32":
            os.startfile(str(target))   # type: ignore[attr-defined]  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
        return jsonify({"ok": True, "path": str(target)})
    except Exception as ex:  # noqa: BLE001
        # 200 (not 500) + the path so the UI can tell the user where it is to open manually.
        return jsonify({"ok": False, "error": str(ex), "path": str(target)}), 200


@app.route("/api/export/run", methods=["POST"])
def api_export_run():
    """Manually run (or RESUME) the compression export on the current world with the saved
    settings. Resumable: the manifest skips already-compressed regions. Useful after a kill
    or to compress an already-generated world without re-generating. Body {force:true}
    overrides the disk preflight."""
    force = bool((request.json or {}).get("force"))
    return jsonify(_start_export_job("compress", force=force))


@app.route("/api/export/convert", methods=["POST"])
def api_export_convert():
    """Convert the current world's region/*.linear back to vanilla .mca (server world →
    single-player). Round-trip verified per region; .linear kept unless keep_both is off.
    Body {force:true} overrides the world-open (session.lock) guard."""
    force = bool((request.json or {}).get("force"))
    return jsonify(_start_export_job("convert", force=force))


@app.route("/api/export/reveal", methods=["POST"])
def api_export_reveal():
    """Open the folder that holds the world + its archive, selecting the archive file on
    Windows if it exists, so the user can grab the .zip/.tar.zst to share."""
    world = master_world_path(create=False)
    folder = world.parent if world.parent.exists() else PROJECT.root
    archive = None
    for ext in (".zip", ".tar.zst"):
        cand = world.parent / (world.name + ext)
        if cand.exists():
            archive = cand
            break
    try:
        if sys.platform == "win32" and archive is not None:
            subprocess.Popen(["explorer", "/select,", str(archive)])
        elif sys.platform == "win32":
            os.startfile(str(folder))   # type: ignore[attr-defined]  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(archive)] if archive else ["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
        return jsonify({"ok": True, "folder": str(folder),
                        "archive": str(archive) if archive else None})
    except Exception as ex:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(ex), "folder": str(folder),
                        "archive": str(archive) if archive else None}), 200


@app.route("/api/log")
def api_log():
    return jsonify({"log": _LOG[-400:]})


@app.route("/logs")
def logs_page():
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Meld - Log</title>"
        "<style>body{background:#13110d;color:#cdc3ad;margin:0;padding:12px;"
        "font:12px/1.5 ui-monospace,Consolas,monospace}"
        "pre{white-space:pre-wrap;word-break:break-word;margin:0}</style></head>"
        "<body><pre id='l'>loading...</pre><script>"
        "async function t(){try{const s=await fetch('/api/log').then(r=>r.json());"
        "const el=document.getElementById('l');const atBottom="
        "window.innerHeight+window.scrollY>=document.body.scrollHeight-40;"
        "el.textContent=(s.log||[]).join('\\n');"
        "if(atBottom)window.scrollTo(0,document.body.scrollHeight);}catch(e){}"
        "setTimeout(t,1500);}t();</script></body></html>"
    )


@app.route("/api/master/reset", methods=["POST"])
def api_master_reset():
    """Wipe the merged master world (region/poi/entities/level.dat). Merged cells
    revert to 'planned'."""
    mp = master_world_path(create=False)
    removed = 0
    for sub in ("region", "poi", "entities"):
        p = mp / sub
        if p.exists():
            removed += len(list(p.glob("*.mca")))
            shutil.rmtree(p, ignore_errors=True)
    dat = mp / "level.dat"
    if dat.exists():
        try:
            dat.unlink()
        except Exception:
            pass
    grid = PROJECT.load_grid()
    for k, v in list(grid.items()):
        if v == "merged":
            grid[k] = "planned"
    PROJECT.save_grid(grid)
    log(f"  [Master reset] removed {removed} region(s)")
    return jsonify({"ok": True, "removed_regions": removed})


@app.route("/api/origin", methods=["POST"])
def api_set_origin():
    d = request.json or {}
    if d.get("lat") is None or d.get("lon") is None:
        return jsonify({"ok": False, "error": "lat and lon required"}), 400
    # Snap the origin onto the global region grid (anchored at 0,0) so it lands on
    # an exact region corner and is deterministic: the same coords always snap to
    # the same origin. That makes pasting an origin from another project reproduce
    # it exactly, and keeps the cell grid predefined/stable.
    scale = float(PROJECT.settings().get("scale", 1.0) or 1.0)
    slat, slon = snap_to_region_grid(float(d["lat"]), float(d["lon"]), scale)
    res = PROJECT.set_origin(slat, slon, force=bool(d.get("force")))
    return jsonify(res), (200 if res.get("ok") else 409)


@app.route("/api/origin/unlock", methods=["POST"])
def api_unlock_origin():
    return jsonify({"ok": True, "origin": PROJECT.unlock_origin()})


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "GET":
        return jsonify(PROJECT.settings())
    patch = request.json or {}
    # Seed is stored on the elevation block, not the settings blob — persist it
    # independently so editing the Seed field actually sticks.
    if "seed" in patch:
        PROJECT.set_seed(patch.pop("seed"))
    # Guard rails: cell size 1-16 (snapped to a power of two on the UI), workers 1-64,
    # CPU budget 10-95% (95 cap leaves the OS + disk-save phase headroom).
    if patch.get("job_size_regions") is not None:
        patch["job_size_regions"] = max(1, min(64, int(patch["job_size_regions"])))
    if patch.get("max_workers") is not None:
        patch["max_workers"] = max(1, min(64, int(patch["max_workers"])))
    if patch.get("cpu_target_pct") is not None:
        patch["cpu_target_pct"] = max(10, min(95, int(patch["cpu_target_pct"])))
    if patch.get("min_threads_per_worker") is not None:
        patch["min_threads_per_worker"] = max(1, min(8, int(patch["min_threads_per_worker"])))
    if patch.get("cave_biome_amounts") is not None:
        raw = patch["cave_biome_amounts"] if isinstance(patch["cave_biome_amounts"], dict) else {}
        clean = {}
        for name in arnis_cmd.CAVE_BIOMES:
            try:
                clean[name] = max(0, min(200, int(raw.get(name, 100))))
            except (TypeError, ValueError):
                clean[name] = 100
        patch["cave_biome_amounts"] = clean
    if patch.get("tree_size_weights") is not None:
        raw = patch["tree_size_weights"] if isinstance(patch["tree_size_weights"], dict) else {}
        clean = {}
        for name, default in arnis_cmd.TREE_SIZE_TIERS:
            try:
                clean[name] = max(0, min(200, int(raw.get(name, default))))
            except (TypeError, ValueError):
                clean[name] = default
        patch["tree_size_weights"] = clean
    if patch.get("cpu_stagger_seconds") is not None:
        patch["cpu_stagger_seconds"] = max(1, min(4, int(round(float(patch["cpu_stagger_seconds"])))))
    if patch.get("cpu_stagger_enabled") is not None:
        patch["cpu_stagger_enabled"] = bool(patch["cpu_stagger_enabled"])
    if patch.get("cpu_stagger_adaptive") is not None:
        patch["cpu_stagger_adaptive"] = bool(patch["cpu_stagger_adaptive"])
    if patch.get("map_item") is not None:
        patch["map_item"] = bool(patch["map_item"])
    # Export / compression. Format is validated against the known set; level 0-22 (0=auto);
    # compression workers 0-256 (0=auto=cores-1, INDEPENDENT of max_workers by contract).
    if patch.get("export_format") is not None:
        ef = str(patch["export_format"]).strip().lower()
        patch["export_format"] = ef if ef in exportmod.VALID_FORMATS else "none"
    if patch.get("export_level") is not None:
        patch["export_level"] = max(0, min(22, int(patch["export_level"])))
    if patch.get("export_compression_workers") is not None:
        patch["export_compression_workers"] = max(0, min(256, int(patch["export_compression_workers"])))
    if patch.get("export_keep_both") is not None:
        patch["export_keep_both"] = bool(patch["export_keep_both"])
    if patch.get("export_stream_and_free") is not None:
        patch["export_stream_and_free"] = bool(patch["export_stream_and_free"])
    if patch.get("export_overlap") is not None:
        patch["export_overlap"] = bool(patch["export_overlap"])
    if patch.get("export_destination") is not None:
        ed = str(patch["export_destination"]).strip().lower()
        patch["export_destination"] = "separate" if ed == "separate" else "in_place"
    if patch.get("export_blinear_variant") is not None:
        bv = str(patch["export_blinear_variant"]).strip().lower()
        patch["export_blinear_variant"] = bv if bv in ("v2", "v3") else "v3"
    if patch.get("export_blinear_keep") is not None:
        bk = str(patch["export_blinear_keep"]).strip().lower()
        patch["export_blinear_keep"] = bk if bk in ("both", "blinear_only", "archive_mca") else "both"
    # AWS-only and regional-only elevation are mutually exclusive (the fork rejects both).
    # Enforce it on whichever one this patch turns ON, so the stored state can never hold
    # both regardless of how the UI fired — belt for the client-side exclusivity handler.
    if patch.get("aws_only_elevation") is True:
        patch["regional_elevation_only"] = False
    elif patch.get("regional_elevation_only") is True:
        patch["aws_only_elevation"] = False
    s = PROJECT.update_settings(patch)
    POOL.set_max_workers(int(s.get("max_workers") or 4))
    # Stagger off => 0s (all workers start at once).
    POOL.stagger_seconds = float(s.get("cpu_stagger_seconds", 2) or 0) if s.get("cpu_stagger_enabled", True) else 0.0
    POOL.stagger_adaptive = bool(s.get("cpu_stagger_adaptive", True))
    return jsonify({**s, "seed": PROJECT.elevation().get("seed", 1)})


@app.route("/api/survey", methods=["POST"])
def api_survey():
    d = request.json or {}
    bbox = d.get("bbox")
    if not bbox:
        return jsonify({"ok": False, "error": "bbox required"}), 400
    zoom = int(d.get("zoom", 10))
    log(f"[Survey] surveying elevation over the selection (z{zoom}, parallel)…")
    _t0 = time.time()
    res = survey_elevation(bbox, zoom=zoom)
    _el = time.time() - _t0
    if res.get("ok"):
        seed = int(PROJECT.elevation().get("seed", 1) or 1)
        PROJECT.set_elevation_lock(res["min_m"], res["max_m"], seed=seed)
        log(f"[Survey] done in {_el:.0f}s — range {res['min_m']} to {res['max_m']} m from "
            f"{res['tiles']} tile(s); elevation lock set")
    else:
        log(f"[Survey] failed in {_el:.0f}s — {res.get('reason', '?')}")
    return jsonify(res)


@app.route("/api/elevation/manual", methods=["POST"])
def api_elevation_manual():
    d = request.json or {}
    if d.get("min_m") is None or d.get("max_m") is None:
        return jsonify({"ok": False, "error": "min_m and max_m required"}), 400
    ev = PROJECT.set_elevation_lock(d["min_m"], d["max_m"], seed=d.get("seed"))
    return jsonify({"ok": True, "elevation": ev})


@app.route("/api/grid", methods=["POST"])
def api_grid():
    if _run_active():
        return jsonify({"ok": False, "error": "stop the generation before changing the plan"}), 409
    d = request.json or {}
    bbox = d.get("bbox")
    # Accept a single ring (`polygon`) or many rings (`polygons`, for multi-polygon
    # countries / island nations). Cells are kept if inside ANY ring.
    rings = d.get("polygons") or ([d.get("polygon")] if d.get("polygon") else None)
    mode = (d.get("mode") or "add")   # add | replace (same as add here) | remove
    origin = PROJECT.origin()
    if origin.get("lat") is None:
        return jsonify({"ok": False, "error": "set origin first"}), 400
    has_rings = bool(rings) and any(r and len(r) >= 3 for r in rings)
    if not bbox and not has_rings:
        return jsonify({"ok": False, "error": "bbox or polygon required"}), 400
    settings = PROJECT.settings()
    # Cell size is a free 1..64: bigger cells write larger save bursts (heavier on RAM and the save
    # disk at the end of each cell, auto stream-to-disk for 8+), but the user owns the trade-off.
    size = max(1, min(64, int(d.get("size") or settings.get("job_size_regions") or 4)))
    scale = float(settings.get("scale", 1.0))
    if has_rings:
        cells = cells_for_polygons(rings, origin, scale, size)   # follow the drawn/searched shape
    else:
        cells = cells_for_bbox(bbox, origin, scale, size)
    grid = PROJECT.load_grid()
    if mode == "remove":
        removed = []
        for c in cells:
            k = c["cell_key"]
            if grid.get(k) and grid[k] != "merged":   # never wipe merged content
                grid.pop(k, None)
                removed.append(k)
        PROJECT.save_grid(grid)
        return jsonify({"ok": True, "cells": cells, "count": len(cells), "removed": removed})
    for c in cells:
        grid.setdefault(c["cell_key"], "planned")
    PROJECT.save_grid(grid)
    # Persist the drawn area per-project so a restart redraws it (cells already persist in grid.json;
    # this restores the live selection + outline so coverage/data-pack/generate work without re-drawing).
    sel_bbox = bbox or dp.rings_bbox(rings)
    if sel_bbox:
        PROJECT.save_selection({"bbox": sel_bbox, "polygons": rings})
    return jsonify({"ok": True, "cells": cells, "count": len(cells)})


@app.route("/api/selection", methods=["POST"])
def api_selection():
    """Persist or clear the drawn selection for the active project (so a restart redraws it). Body:
    {selection:{bbox, polygons}} to save, or {selection:null}/{} to clear. Cheap; the client calls
    it whenever the area is drawn/moved/edited/cleared so project.json always has the latest."""
    PROJECT.save_selection((request.json or {}).get("selection"))
    return jsonify({"ok": True})


def _run_active() -> bool:
    """A generation (or its prefetch) is in flight. Editing the grid mid-run desyncs the
    worker pool (the queue is separate from grid.json), so cell-edit routes refuse while True."""
    return POOL.is_running() or bool(_PREFETCH.get("active"))


def _valid_cell_key(k) -> bool:
    """A cell key is exactly 'rx,rz,size' with integer parts and size in 1..16. Rejects the
    'NaN,1,4' / '1.5,2,4' junk a client paint-at-edge or float can otherwise poison grid.json with
    (which then crashes a later int() in grow / bbox / submit)."""
    if not isinstance(k, str):
        return False
    parts = k.split(",")
    if len(parts) != 3:
        return False
    try:
        rx, rz, sz = int(parts[0]), int(parts[1]), int(parts[2])
    except (TypeError, ValueError):
        return False
    return 1 <= sz <= 16


@app.route("/api/cell/toggle", methods=["POST"])
def api_cell_toggle():
    """Add or remove ONE cell from the plan (cell-by-cell editing). Empty cell ->
    planned; planned/queued/failed cell -> removed. Merged cells are left alone
    (delete those via /api/world/delete, they hold real content)."""
    if _run_active():
        return jsonify({"ok": False, "error": "stop the generation before editing cells"}), 409
    d = request.json or {}
    key = (d.get("cell_key") or "").strip()
    if not _valid_cell_key(key):
        return jsonify({"ok": False, "error": "cell_key required (rx,rz,size with integer parts)"}), 400
    grid = PROJECT.load_grid()
    cur = grid.get(key)
    if cur == "merged":
        return jsonify({"ok": True, "cell_key": key, "status": "merged", "changed": False})
    if cur is None:
        PROJECT.set_cell_status(key, "planned")
        return jsonify({"ok": True, "cell_key": key, "status": "planned", "changed": True})
    grid.pop(key, None)
    PROJECT.save_grid(grid)
    return jsonify({"ok": True, "cell_key": key, "status": None, "changed": True})


@app.route("/api/cell/toggle-bulk", methods=["POST"])
def api_cell_toggle_bulk():
    """Paint-drag: add or remove MANY cells in ONE atomic grid write (so a drag doesn't fire
    dozens of single toggles that hammer disk + race the worker pipeline). op='add' plans every
    empty key; op='remove' drops every non-merged key. Merged cells are always left alone."""
    if _run_active():
        return jsonify({"ok": False, "error": "stop the generation before editing cells"}), 409
    d = request.json or {}
    keys = [k.strip() for k in (d.get("cell_keys") or []) if _valid_cell_key((k or "").strip())]
    op = d.get("op")
    if op not in ("add", "remove") or not keys:
        return jsonify({"ok": False, "error": "op (add|remove) + valid cell_keys required"}), 400
    changed = PROJECT.bulk_set_cells(keys, op)
    return jsonify({"ok": True, "op": op, "changed": changed})


@app.route("/api/grid/grow", methods=["POST"])
def api_grid_grow():
    """Grow the plan outward by N ring(s) of cells: add every empty cell adjacent to a
    cell already in the plan. Lets you extend tiles just OUTSIDE a country/polygon
    selection (it follows the shape's outline). Neighbours are same-size."""
    if _run_active():
        return jsonify({"ok": False, "error": "stop the generation before changing the plan"}), 409
    d = request.json or {}
    rings = max(1, min(20, int(d.get("rings") or 1)))
    diagonal = bool(d.get("diagonal", True))
    origin = PROJECT.origin()
    if origin.get("lat") is None:
        return jsonify({"ok": False, "error": "nothing to grow yet"}), 400
    scale = float(PROJECT.settings().get("scale", 1.0))
    offs = [(-1, 0), (1, 0), (0, -1), (0, 1)]
    if diagonal:
        offs += [(-1, -1), (-1, 1), (1, -1), (1, 1)]
    grid = PROJECT.load_grid()
    if not grid:
        return jsonify({"ok": False, "error": "plan a selection first, then grow it"}), 400
    added: list[str] = []
    for _ in range(rings):
        frontier = set()
        for k in list(grid.keys()):
            try:
                rx, rz, sz = (int(x) for x in k.split(","))
            except ValueError:
                continue
            for dx, dz in offs:
                nk = f"{rx + dx},{rz + dz},{sz}"
                if nk not in grid and nk not in frontier:
                    frontier.add(nk)
        for nk in frontier:
            grid[nk] = "planned"
            added.append(nk)
    PROJECT.save_grid(grid)
    cells = [{"cell_key": k, "bbox": _bbox_from_cell_key(k, origin, scale)} for k in added]
    return jsonify({"ok": True, "added": added, "count": len(added), "cells": cells})


@app.route("/api/grid/clear", methods=["POST"])
def api_grid_clear():
    """Revert the grid plan. Drops planned/queued/failed cells so the selection
    can be re-split at a different cell size. Keeps 'merged' cells by default
    (their content is already in the master world)."""
    if _run_active():
        return jsonify({"ok": False, "error": "stop the generation before clearing the plan"}), 409
    d = request.json or {}
    keep_merged = d.get("keep_merged", True)
    grid = PROJECT.load_grid()
    kept = {k: v for k, v in grid.items() if keep_merged and v == "merged"}
    removed = [k for k in grid if k not in kept]
    PROJECT.save_grid(kept)
    return jsonify({"ok": True, "removed": removed, "kept": list(kept)})


def _bbox_from_cell_key(cell_key: str, origin: dict, scale: float) -> dict:
    rx, rz, size = (int(x) for x in cell_key.split(","))
    return cell_bbox(rx, rz, size, origin["lat"], origin["lon"], scale)


# ── trim open-ocean cells (no OSM features AND at/below sea level → skip generating flat water) ──
_OCEAN_SEA_LEVEL_M = 1.0
_elev_max_cache: dict = {}


def _elev_tile_stats(x: int, y: int, ez: int):
    """(min, max) terrarium height (m) of a cached elevation tile, or None if not cached/undecodable.
    Memoised so a tile shared by adjacent cells decodes once. Open sea decodes to a FLAT ~0 m
    (min≈max≈0); any real land has relief, so flatness distinguishes water from low coastal land far
    better than the OSM tiles can — those are coarser than a cell and carry maritime boundaries/ferry
    routes, so 'no OSM' never holds over the sea."""
    key = (x, y, ez)
    if key in _elev_max_cache:
        return _elev_max_cache[key]
    p = dp.aws_tile_path(x, y, ez)
    out = None
    if p.exists():
        try:
            from PIL import Image
            im = Image.open(p).convert("RGB")
            try:
                import numpy as _np
                a = _np.asarray(im, dtype=_np.float64)
                h = a[..., 0] * 256.0 + a[..., 1] + a[..., 2] / 256.0 - 32768.0
                h = h[h > -1000.0]      # drop no-data (-32768) samples
                out = (float(h.min()), float(h.max())) if h.size else None
            except Exception:           # no numpy → coarse pixel sample
                px = im.load(); w, hgt = im.size; lo = 1e9; hi = -1e9
                for j in range(0, hgt, 16):
                    for i in range(0, w, 16):
                        r, g, b = px[i, j]
                        v = r * 256.0 + g + b / 256.0 - 32768.0
                        if v > -1000.0:
                            lo = min(lo, v); hi = max(hi, v)
                out = (lo, hi) if hi > -1e9 else None
        except Exception:  # noqa: BLE001
            out = None
    _elev_max_cache[key] = out
    return out


def _cell_is_ocean(cbb: dict, ez: int) -> bool:
    """Open water = EVERY covering elevation tile is FLAT at sea level: max ≤ 1 m, min ≥ -5 m, and
    spread ≤ 1.5 m. Pure sea decodes to (0, 0); land has relief (max-min > 1.5) or rises above 1 m, so
    it's kept. Conservative: any uncached tile → not ocean. Reversible (re-plan re-adds)."""
    etiles = dp.tiles_for_bbox(cbb, ez)
    if not etiles:
        return False
    for (x, y) in etiles:
        st = _elev_tile_stats(x, y, ez)
        if st is None:
            return False
        lo, hi = st
        if hi > _OCEAN_SEA_LEVEL_M or lo < -5.0 or (hi - lo) > 1.5:
            return False
    return True


@app.route("/api/grid/trim-ocean", methods=["POST"])
def api_grid_trim_ocean():
    """Drop planned cells that are open ocean (no OSM features AND at/below sea level) so generation
    skips flat-water tiles. Only touches planned/queued cells (never merged/running). Reversible —
    re-draw or re-plan re-adds them. Needs the region's OSM + elevation cached to classify."""
    if _run_active():
        return jsonify({"ok": False, "error": "stop the generation before trimming"}), 409
    origin = PROJECT.origin()
    if not origin.get("locked"):
        return jsonify({"ok": False, "error": "lock an origin first"}), 400
    settings = PROJECT.settings()
    scale = float(settings.get("scale", 1.0) or 1.0)
    ez = effective_elev_zoom(settings, float(origin.get("lat") or 45.0))
    grid = PROJECT.load_grid()
    planned = [k for k, v in grid.items() if v in ("planned", "queued")]
    _elev_max_cache.clear()
    ocean = []
    for k in planned:
        try:
            if _cell_is_ocean(_bbox_from_cell_key(k, origin, scale), ez):
                ocean.append(k)
        except Exception:  # noqa: BLE001
            continue
    if ocean:
        PROJECT.bulk_set_cells(ocean, "remove")
    log(f"[Grid] trim ocean: removed {len(ocean)} open-water cell(s) of {len(planned)} planned "
        f"(no OSM + ≤{_OCEAN_SEA_LEVEL_M:.0f} m)")
    return jsonify({"ok": True, "removed": len(ocean), "planned": len(planned)})


def _submit_cells(cells: list[dict], osm_files: dict | None = None,
                  settings: dict | None = None, origin: dict | None = None,
                  keep_started: bool = False, reset_timing: bool = False) -> list[str]:
    """Submit a list of {cell_key, bbox} to the pool and (re)start the run clock.
    osm_files maps cell_key -> pre-fetched OSM json path (passed to Arnis as --file).
    settings/origin may be a snapshot taken before a prefetch, so the cells generate
    with the SAME scale/seam/origin the chunk bboxes were built from (otherwise a
    settings change mid-prefetch could leave a cell's bbox outside its chunk file).
    Shared by /api/queue, /api/cell/regenerate and /api/resume."""
    osm_files = osm_files or {}
    if reset_timing:
        # Only a brand-new run wipes the benchmark; resume/regenerate keep + accumulate it,
        # so the report remembers across people stopping and starting generations.
        _timing_reset()
    settings = settings if settings is not None else PROJECT.settings()
    origin = origin if origin is not None else PROJECT.origin()
    elevation = PROJECT.elevation()
    world_name = PROJECT.load().get("name", "Meld World")
    # Generate center-out (spiral): order cells by their ring distance from the selection
    # center, tie-broken by angle, so the middle fills first and the build grows outward
    # in concentric rings (nicer to watch + the dense city core lands first).
    if cells:
        rc = [tuple(int(x) for x in c["cell_key"].split(",")[:2]) for c in cells]
        cx = sum(r[0] for r in rc) / len(rc)
        cz = sum(r[1] for r in rc) / len(rc)
        def _spiral_key(c):
            rx, rz = (int(x) for x in c["cell_key"].split(",")[:2])
            dx, dz = rx - cx, rz - cz
            return (max(abs(dx), abs(dz)), math.atan2(dz, dx))   # Chebyshev ring, then angle
        cells = sorted(cells, key=_spiral_key)
    # Set the run clock (incl. total) BEFORE submitting, so a fast cell completing can't
    # see total=0 in _on_complete and mark the run ended prematurely.
    est_regions = sum(int(c["cell_key"].split(",")[2]) ** 2 for c in cells)
    with _RUN_LOCK:
        # keep_started: prefetch already started the clock, so the elapsed timer spans the
        # OSM/terrain warm-up too (the prefetch DOES cost wall time). Otherwise start now.
        started = _RUN.get("started") if (keep_started and _RUN.get("started")) else time.time()
        _RUN.update(started=started, ended=None, total=len(cells), done=0, failed=0,
                    est_regions=est_regions, est_mb=est_regions * MB_PER_REGION,
                    actual_mb=None, phase="generating")
    _reset_export_status()   # a fresh run resets the export progress + the one-pass guard
    queued = []
    for c in cells:
        ck = c["cell_key"]
        out = str(PROJECT.cells_dir / ck.replace(",", "_"))
        PROJECT.set_cell_status(ck, "queued")   # atomic; won't clobber a worker's status
        _timing_queued(ck)
        POOL.submit({
            "cell_key": ck, "bbox": c["bbox"], "settings": settings,
            "origin": origin, "elevation": elevation, "output_path": out,
            "world_name": world_name, "osm_file": osm_files.get(ck),
        })
        queued.append(ck)
    return queued


def _start_generation(cells: list[dict], reset_timing: bool = False) -> tuple[list[str], bool]:
    """Pre-fetch the selection's OSM once, then submit the cells. Returns
    (cell_keys, prefetching). When prefetch is enabled the fetch runs in a background
    thread (so the HTTP call returns at once) and the cells are submitted to the pool
    only after the OSM is cached; otherwise cells are submitted immediately.

    settings + origin are snapshotted ONCE here and used for both the prefetch (chunk
    bboxes) and the generation (cell bboxes), so the two always agree."""
    settings = PROJECT.settings()
    origin = PROJECT.origin()
    # Re-assert the configured worker count at the START of EVERY run (initial + all retry paths
    # funnel through here), so a run never inherits a stale pool size — e.g. a 2 left behind by a
    # prior low-scale AWS-burst clamp. The clamp (now legacy-AWS-only) may lower it again later,
    # but the default Mapterhorn/regional path keeps exactly what you set.
    _cfg_workers = min(WorkerPool.MAX_WORKERS_HARD_CAP, int(settings.get("max_workers") or 4))
    if POOL.max_workers != _cfg_workers:
        POOL.set_max_workers(_cfg_workers)
        log(f"[Workers] generation start: set pool to your {_cfg_workers} worker(s)")
    exe = resolve_arnis_exe()
    # NOTE: stream-to-disk is delivered via the ARNIS_STREAM_TO_DISK env var in _runner
    # (the merged Arnis dropped the CLI flag). The env var is harmless on a binary that
    # doesn't support it, so no capability gate is needed here anymore.
    if not settings.get("prefetch_enabled", True) or not exe or not cells:
        return _submit_cells(cells, settings=settings, origin=origin, reset_timing=reset_timing), False

    # Mark the cells queued now so the grid/overlay shows them during the prefetch.
    for c in cells:
        PROJECT.set_cell_status(c["cell_key"], "queued")
    # Start the run clock NOW (prefetch phase) so the elapsed counter includes the OSM +
    # terrain warm-up, and the UI can show "Prefetching…" as the live phase.
    est_regions = sum(int(c["cell_key"].split(",")[2]) ** 2 for c in cells)
    with _RUN_LOCK:
        _RUN.update(started=time.time(), ended=None, total=len(cells), done=0, failed=0,
                    est_regions=est_regions, est_mb=est_regions * MB_PER_REGION,
                    actual_mb=None, phase="prefetch")
    with _PREFETCH_LOCK:
        _PREFETCH.update(active=True, done=False, chunks=[], started=time.time(), phase="osm",
                         terrain={"done": 0, "total": 0, "ok": 0, "failed": 0},
                         note=f"prefetching OSM for {len(cells)} cell(s)…")

    def _worker():
        _t_osm0 = time.time()
        _phase_t = {}                      # phase -> seconds, for the end-of-prefetch report
        # Phase 1: OSM (Overpass) — download once, share to every cell.
        try:
            osm_files = run_prefetch(cells, origin, settings, str(exe),
                                     _osm_cache_dir(), log, _prefetch_on_chunk)
        except Exception as ex:  # noqa: BLE001
            log(f"[Prefetch] error, falling back to live fetch: {ex}")
            osm_files = {}
        _phase_t["osm"] = time.time() - _t_osm0
        _t_terr0 = time.time()

        # Phase 2: terrain — pre-warm the AWS elevation tiles serially (single process) so the
        # parallel cells hit the cache instead of bursting S3 (the 757-byte truncation -> flat
        # seams around the center). Best-effort; cells fetch live for any tile that misses.
        if settings.get("terrain", True) and settings.get("prefetch_terrain", True):
            # Skip the (serial, minutes-long) terrain warm ENTIRELY when the build's elevation tiles
            # are already cached — re-validating a complete data pack on every run is pure waste and
            # was a big slice of the per-run wait. The per-cell live fallback still covers any gap.
            _skip_warm = False
            # coverage_elevation() only measures the AWS terrarium cache. In regional-only
            # mode the cells never read those tiles — they read the IGN/USGS regional cache,
            # which this gate can't see — so a "99% AWS cached" reading must NOT skip the warm
            # (that warm is the only thing filling the regional cache; skipping it sends every
            # parallel cell live to the provider = the rate-limit burst we're avoiding).
            if not settings.get("regional_elevation_only"):
                try:
                    _bs0 = [c["bbox"] for c in cells if c.get("bbox")]
                    if _bs0:
                        _ubb0 = {"south": min(b["south"] for b in _bs0), "west": min(b["west"] for b in _bs0),
                                 "north": max(b["north"] for b in _bs0), "east": max(b["east"] for b in _bs0)}
                        _ez0 = effective_elev_zoom(settings, float(origin.get("lat") or 45.0))
                        _ec0 = dp.coverage_elevation(_ubb0, zoom=_ez0)
                        if _ec0.get("pct", 0) >= 99.0:
                            _skip_warm = True
                            log(f"[Terrain] elevation {_ec0.get('pct', 0)}% cached at z{_ez0} — skipping "
                                f"the terrain warm (no re-validation needed)")
                except Exception:  # noqa: BLE001
                    _skip_warm = False
            with _PREFETCH_LOCK:
                tiles = [] if _skip_warm else [c["bbox"] for c in _PREFETCH["chunks"]
                         if c.get("bbox") and c.get("state") in ("done", "cached")]
            if not tiles and not _skip_warm:
                # OSM prefetch produced no usable chunks (e.g. it failed) — warm terrain over the
                # whole selection anyway, so the parallel cells still hit the cache instead of
                # bursting S3. Terrain zoom clamps to 15 for any bbox size, so one sweep is fine.
                bs = [c["bbox"] for c in cells if c.get("bbox")]
                if bs:
                    tiles = [{"south": min(b["south"] for b in bs), "west": min(b["west"] for b in bs),
                              "north": max(b["north"] for b in bs), "east": max(b["east"] for b in bs)}]
            if tiles:
                with _PREFETCH_LOCK:
                    _PREFETCH.update(phase="terrain", note="warming terrain tiles…",
                                     terrain={"done": 0, "total": len(tiles), "ok": 0, "failed": 0})
                try:
                    purge_small_tiles(log=log)   # drop legacy poisoned tiles first

                    def _tp(done, total, ok, failed):
                        with _PREFETCH_LOCK:
                            _PREFETCH["terrain"].update(done=done, total=total, ok=ok, failed=failed)

                    # Warm at the SAME zoom the cells fetch (ARNIS_ELEV_ZOOM), or the warm fills the
                    # wrong zoom and every cell re-downloads live (the 64-way S3 burst).
                    _lat = float(origin.get("lat") or 45.0)
                    ez = effective_elev_zoom(settings, _lat)
                    run_terrain_prefetch(tiles, str(exe), log, _tp, elev_zoom=ez,
                                         scale=float(settings.get("scale", 1.0) or 1.0),
                                         aws_only=bool(settings.get("aws_only_elevation")),
                                         regional_only=bool(settings.get("regional_elevation_only")))
                except Exception as ex:  # noqa: BLE001
                    log(f"[Terrain] prefetch error (cells will fetch live): {ex}")

        # AWS-burst clamp, applied HERE (post-warm) so it sees ACTUAL elevation coverage, not a flag.
        # At scale<0.5 each cell fetches many AWS tiles; >2 cells live-fetching at once bursts S3 and
        # truncates terrain into flat seams. If the build's elevation tiles ARE cached (warm worked /
        # data pack at the right zoom) we keep the user's full worker count; if they're NOT, we hold
        # at 2 for this run so the cells that must live-fetch don't burst.
        try:
            uw = min(WorkerPool.MAX_WORKERS_HARD_CAP, int(settings.get("max_workers") or 4))
            sc = float(settings.get("scale", 1.0) or 1.0)
            # The AWS-burst clamp is LEGACY-AWS-ONLY. It exists because >2 cells live-fetching the
            # AWS terrarium set at scale<0.5 burst S3 and truncate terrain into flat seams. The
            # DEFAULT source is now Mapterhorn (plus the regional providers), which cells fetch
            # instead of the AWS terrarium set — so its coverage is irrelevant here, and measuring
            # it would wrongly hold the whole run at 2 workers even though you set more. Mapterhorn
            # caps its own downloads and has pyramid-parent hole-proofing (it degrades gracefully,
            # never the flat-seam truncation AWS does), and regional-only retries on error off the
            # warmed provider cache — so in both cases keep the user's full worker count. (Bake
            # Mapterhorn elevation up front if you want the run fully offline.) Only force the clamp
            # when the run is pinned to legacy AWS via --aws-only-elevation.
            if not settings.get("aws_only_elevation"):
                log(f"[Workers] using your {uw} worker(s) — Mapterhorn/regional elevation "
                    f"(no legacy-AWS S3 burst risk)")
            elif sc < 0.5 and uw > 2:
                bs = [c["bbox"] for c in cells if c.get("bbox")]
                if bs:
                    ubb = {"south": min(b["south"] for b in bs), "west": min(b["west"] for b in bs),
                           "north": max(b["north"] for b in bs), "east": max(b["east"] for b in bs)}
                    ez2 = effective_elev_zoom(settings, float(origin.get("lat") or 45.0))
                    cov = dp.coverage_elevation(ubb, zoom=ez2)
                    if cov.get("pct", 0) < 99.0:
                        POOL.set_max_workers(2)
                        log(f"[Workers] clamped to 2 — LEGACY AWS elevation only {cov.get('pct', 0)}% "
                            f"cached at z{ez2} (scale<0.5 S3-burst safety); cells would re-fetch S3 live")
                    else:
                        log(f"[Workers] using your {uw} worker(s) — AWS elevation "
                            f"{cov.get('pct', 0)}% cached at z{ez2}")
            else:
                log(f"[Workers] using your {uw} worker(s) — legacy AWS elevation")
        except Exception as ex:  # noqa: BLE001
            log(f"[Workers] coverage clamp check skipped ({ex}); keeping user worker count")

        _phase_t["terrain"] = time.time() - _t_terr0
        with _PREFETCH_LOCK:
            _PREFETCH.update(active=False, done=True, phase="generating",
                             note=f"{len(osm_files)}/{len(cells)} cells from cached OSM",
                             timings=dict(_phase_t))
        # Prefetch report: how long each pre-generation data phase took, so a slow phase is visible.
        _rep = " · ".join(f"{k} {v:.0f}s" for k, v in _phase_t.items())
        log(f"[Prefetch] done in {sum(_phase_t.values()):.0f}s ({_rep}) — "
            f"{len(osm_files)}/{len(cells)} cells share cached OSM; starting generation")
        _submit_cells(cells, osm_files, settings=settings, origin=origin, keep_started=True, reset_timing=reset_timing)

    threading.Thread(target=_worker, daemon=True).start()
    return [c["cell_key"] for c in cells], True


def _elevation_gate_ok() -> bool:
    s = PROJECT.settings()
    return s.get("elevation_mode", "global") != "global" or PROJECT.elevation().get("locked")


def _world_param_drift(settings: dict, origin: dict) -> str | None:
    """If the master world already has merged cells, refuse a run whose scale/origin differ
    from what the world was built at (mixing coordinate systems = cliffs at every join).
    Compares against the world's meld-world.json sidecar. Returns an error string or None."""
    grid = PROJECT.load_grid()
    if not any(v == "merged" for v in grid.values()):
        return None
    meta = read_world_meta(master_world_path(create=False))
    if not meta:
        return None
    ms = meta.get("settings") or {}
    mo = meta.get("origin") or {}
    cur_scale = float(settings.get("scale", 1.0) or 1.0)
    meta_scale = float(ms.get("scale", cur_scale) or cur_scale)
    if abs(cur_scale - meta_scale) > 1e-9:
        return (f"This world was built at scale {meta_scale}; the current setting is {cur_scale}. "
                f"Mixing scales creates cliffs. Start a New world, or set scale back to {meta_scale}.")
    if mo.get("lat") is not None and origin.get("lat") is not None:
        if (abs(float(mo["lat"]) - float(origin["lat"])) > 1e-6
                or abs(float(mo["lon"]) - float(origin["lon"])) > 1e-6):
            return ("This world was built at a different origin. Start a New world, or restore the "
                    "original origin (Import world settings).")
    return None


@app.route("/api/queue", methods=["POST"])
def api_queue():
    d = request.json or {}
    origin = PROJECT.origin()
    if origin.get("lat") is None:
        return jsonify({"ok": False, "error": "set origin first"}), 400

    settings = PROJECT.settings()
    if not _elevation_gate_ok():
        return jsonify({"ok": False, "error": "elevation_mode is 'global' but no "
                        "elevation lock — run the survey first or set a manual range, "
                        "or switch elevation_mode to 'local'."}), 400

    # World guard: refuse a scale/origin that differs from the existing master world.
    d = request.json or {}
    drift = _world_param_drift(settings, origin)
    if drift and not d.get("force"):
        return jsonify({"ok": False, "error": drift, "drift": True}), 409

    # Worker cap: hard-capped at WorkerPool.MAX_WORKERS_HARD_CAP (64). The user owns the
    # high range now — the UI warns above 8 about the heavy save phase (disk + RAM) and lets
    # them accept the risk — so we do NOT silently reduce their choice here. The only forced
    # clamp is the low-scale one: scale<0.5 means a huge per-region real area where >2
    # concurrent AWS-tile fetches corrupt elevation.
    workers = min(WorkerPool.MAX_WORKERS_HARD_CAP, int(settings.get("max_workers") or 4))
    scale = float(settings.get("scale", 1.0) or 1.0)
    note = ""
    # Honor the user's worker count here. The scale<0.5 AWS-burst clamp is applied LATER — after the
    # terrain warm, gated on the build's ACTUAL elevation coverage (src/server _start_generation) —
    # because only then do we know whether cells will hit the cache or live-fetch S3. Clamping on a
    # flag here was wrong: prefetch_terrain ON did not mean the right-zoom tiles were cached.
    POOL.set_max_workers(workers)

    cells = d.get("cells")
    if not cells:
        # Generate runs the SAVED plan (grid.json), skipping already-merged cells. It must NOT
        # refill the whole bounding rectangle from bbox when a custom plan exists — otherwise a
        # Generate issued after a page/server refresh (when the client's in-memory plannedCells is
        # empty and btnGen falls back to {bbox: selection}) silently expands a custom shape (e.g.
        # Romania+Moldova+1-cell border) into every cell of its bounding box. Only a project with no
        # standing plan (all cells merged, or none planned yet) falls through to a fresh bbox split.
        grid = PROJECT.load_grid()
        plan_keys = [k for k, v in grid.items() if v != "merged"]
        if plan_keys:
            cells = [{"cell_key": k, "bbox": _bbox_from_cell_key(k, origin, scale)} for k in plan_keys]
        else:
            bbox = d.get("bbox")
            if not bbox:
                return jsonify({"ok": False, "error": "cells or bbox required"}), 400
            size = int(d.get("size") or settings.get("job_size_regions") or 4)
            cells = cells_for_bbox(bbox, origin, scale, size)

    # Continue-where-left-off: skip cells already merged unless force=true.
    if not d.get("force"):
        grid = PROJECT.load_grid()
        cells = [c for c in cells if grid.get(c["cell_key"]) != "merged"]
    if not cells:
        return jsonify({"ok": True, "queued": [], "count": 0,
                        "note": "nothing to do — all cells already merged"})

    # Fail fast if the save drive is offline/unwritable — otherwise every cell generates, then dies
    # at merge with a cryptic per-cell WinError, wasting the whole run (and the prefetch).
    drive_ok, drive_why = _output_drive_ok()
    if not drive_ok:
        return jsonify({"ok": False, "error": drive_why}), 409

    # Fresh full-world queue: this is the only path that wipes the benchmark. Resume and the
    # regenerate-* routes keep the prior timings so the report accumulates across stop/start.
    queued, prefetching = _start_generation(cells, reset_timing=True)
    return jsonify({"ok": True, "queued": queued, "count": len(queued), "note": note,
                    "prefetching": prefetching})


@app.route("/api/cell/regenerate", methods=["POST"])
def api_cell_regenerate():
    """Re-queue a single cell (click-a-square to retry/regenerate)."""
    d = request.json or {}
    ck = d.get("cell_key")
    if not ck or len(ck.split(",")) != 3:
        return jsonify({"ok": False, "error": "valid cell_key required"}), 400
    origin = PROJECT.origin()
    if origin.get("lat") is None:
        return jsonify({"ok": False, "error": "no origin set"}), 400
    scale = float(PROJECT.settings().get("scale", 1.0) or 1.0)
    bbox = _bbox_from_cell_key(ck, origin, scale)
    queued, prefetching = _start_generation([{"cell_key": ck, "bbox": bbox}])
    return jsonify({"ok": True, "queued": queued, "prefetching": prefetching})


@app.route("/api/resume", methods=["POST"])
def api_resume():
    """Re-queue every NOT-merged cell — crash/overnight recovery."""
    origin = PROJECT.origin()
    if origin.get("lat") is None:
        return jsonify({"ok": False, "error": "no origin set"}), 400
    scale = float(PROJECT.settings().get("scale", 1.0) or 1.0)
    grid = PROJECT.load_grid()
    todo = [k for k, v in grid.items() if v != "merged" and len(k.split(",")) == 3]
    if not todo:
        return jsonify({"ok": True, "queued": [], "count": 0, "note": "all cells already merged"})
    cells = [{"cell_key": k, "bbox": _bbox_from_cell_key(k, origin, scale)} for k in todo]
    queued, prefetching = _start_generation(cells)
    return jsonify({"ok": True, "queued": queued, "count": len(queued), "prefetching": prefetching})


@app.route("/api/cell/regenerate-region", methods=["POST"])
def api_cell_regenerate_region():
    """Re-run only the cells whose CENTER falls inside a drawn rectangle/polygon. For fixing
    a localized artifact without redoing the whole world. Reuses the world's origin/scale/lock
    so redone cells line up; merge overwrites the cell's own regions safely."""
    d = request.json or {}
    bbox = d.get("bbox")
    raw = d.get("polygons") or d.get("polygon")
    origin = PROJECT.origin()
    if origin.get("lat") is None:
        return jsonify({"ok": False, "error": "no origin set"}), 400
    scale = float(PROJECT.settings().get("scale", 1.0) or 1.0)
    # normalize polygon input to a list of rings of (lat, lon)
    rings = None
    if raw and isinstance(raw[0], list) and raw[0] and isinstance(raw[0][0], list):
        rings = [[(float(p[0]), float(p[1])) for p in r] for r in raw]
    elif raw and isinstance(raw[0], list):
        rings = [[(float(p[0]), float(p[1])) for p in raw]]
    if not bbox and not rings:
        return jsonify({"ok": False, "error": "bbox or polygon required"}), 400
    grid = PROJECT.load_grid()
    sel = []
    for k in grid:
        if len(k.split(",")) != 3:
            continue
        b = _bbox_from_cell_key(k, origin, scale)
        clat = (b["south"] + b["north"]) / 2.0
        clon = (b["west"] + b["east"]) / 2.0
        if rings:
            inside = any(_point_in_poly(clat, clon, r) for r in rings if len(r) >= 3)
        else:
            inside = bbox["south"] <= clat <= bbox["north"] and bbox["west"] <= clon <= bbox["east"]
        if inside:
            sel.append(k)
    if not sel:
        return jsonify({"ok": True, "queued": [], "count": 0, "note": "no cells in that region"})
    cells = [{"cell_key": k, "bbox": _bbox_from_cell_key(k, origin, scale)} for k in sel]
    queued, prefetching = _start_generation(cells)
    return jsonify({"ok": True, "queued": queued, "count": len(queued), "prefetching": prefetching})


@app.route("/api/cell/regenerate-suspect", methods=["POST"])
def api_cell_regenerate_suspect():
    """Re-run only the cells flagged suspect (terrain-tile retry / ESA 404). One-click fix for
    the truncated-terrain artifacts; the redo runs the terrain prefetch so they cache cleanly."""
    origin = PROJECT.origin()
    if origin.get("lat") is None:
        return jsonify({"ok": False, "error": "no origin set"}), 400
    scale = float(PROJECT.settings().get("scale", 1.0) or 1.0)
    grid = PROJECT.load_grid()
    with _CELL_HEALTH_LOCK:
        keys = [k for k, v in _CELL_HEALTH.items() if v.get("suspect") and k in grid]
    if not keys:
        return jsonify({"ok": True, "queued": [], "count": 0, "note": "no suspect cells"})
    cells = [{"cell_key": k, "bbox": _bbox_from_cell_key(k, origin, scale)} for k in keys]
    queued, prefetching = _start_generation(cells)
    return jsonify({"ok": True, "queued": queued, "count": len(queued), "prefetching": prefetching})


@app.route("/api/cell/regenerate-cells", methods=["POST"])
def api_cell_regenerate_cells():
    """Re-run an explicit list of cell_keys (the select-clump-to-retry flow). Only keys that
    exist in the grid are re-queued; merged cells are re-run too (the user asked for them)."""
    d = request.json or {}
    origin = PROJECT.origin()
    if origin.get("lat") is None:
        return jsonify({"ok": False, "error": "no origin set"}), 400
    scale = float(PROJECT.settings().get("scale", 1.0) or 1.0)
    grid = PROJECT.load_grid()
    keys = [k.strip() for k in (d.get("cell_keys") or [])
            if isinstance(k, str) and k.strip() in grid]
    if not keys:
        return jsonify({"ok": True, "queued": [], "count": 0, "note": "no matching cells"})
    cells = [{"cell_key": k, "bbox": _bbox_from_cell_key(k, origin, scale)} for k in keys]
    queued, prefetching = _start_generation(cells)
    return jsonify({"ok": True, "queued": queued, "count": len(queued), "prefetching": prefetching})


@app.route("/api/finalcheck", methods=["POST"])
def api_finalcheck():
    """On-demand missing-region scan of the merged world (the same one that auto-runs at end of a
    run). Returns the interior holes + the owning cell keys, and stores them for /api/status so the
    map draws the black-square + ⚠️ markers."""
    if _run_active():
        return jsonify({"ok": False, "error": "a generation is running — wait for it to finish"}), 409
    missing = _scan_missing_regions()
    return jsonify({"ok": True, "missing": missing,
                    "cells": finalcheck.missing_cell_keys(missing), "count": len(missing)})


@app.route("/api/finalcheck/retry", methods=["POST"])
def api_finalcheck_retry():
    """Re-queue the cells that own the detected missing regions through the normal generation path
    (_start_generation) - the same route the failed-cell retry uses. Re-merging a cell overwrites
    its own disjoint canonical regions, so the holes fill in seamlessly."""
    if _run_active():
        return jsonify({"ok": False, "error": "stop the generation first"}), 409
    origin = PROJECT.origin()
    if origin.get("lat") is None:
        return jsonify({"ok": False, "error": "no origin set"}), 400
    scale = float(PROJECT.settings().get("scale", 1.0) or 1.0)
    grid = PROJECT.load_grid()
    with _MISSING_LOCK:
        keys = [k for k in finalcheck.missing_cell_keys(list(_MISSING)) if k in grid]
    if not keys:
        return jsonify({"ok": True, "queued": [], "count": 0, "note": "no missing cells to retry"})
    cells = [{"cell_key": k, "bbox": _bbox_from_cell_key(k, origin, scale)} for k in keys]
    queued, prefetching = _start_generation(cells)
    # the re-run will produce a fresh scan when it finishes; clear the stale markers now
    with _MISSING_LOCK:
        _MISSING.clear()
    return jsonify({"ok": True, "queued": queued, "count": len(queued), "prefetching": prefetching})


# ── project switching (multiple worlds, swap between test + big) ─────────────
def _switch_project(slug: str) -> dict:
    global PROJECT, ACTIVE_SLUG
    if POOL.is_running() or _PREFETCH.get("active"):
        return {"ok": False, "error": "a generation is running — stop it before switching projects"}
    root = PROJECTS_ROOT / slug
    p = Project(root)
    if not (root / "project.json").exists():
        p.save(p.load())   # materialize defaults so the project is listable
    PROJECT = p
    ACTIVE_SLUG = slug
    _write_active_slug(slug)
    _load_cell_health()    # suspects are per-project
    with _MISSING_LOCK:    # missing-region markers are per-project
        _MISSING.clear()
    with _PREFETCH_LOCK:
        _PREFETCH.update(active=False, done=False, chunks=[], phase="idle", note="",
                         terrain={"done": 0, "total": 0, "ok": 0, "failed": 0})
    with _RUN_LOCK:
        _RUN.update(started=None, ended=None, total=0, done=0, failed=0,
                    est_regions=0, est_mb=0, actual_mb=None, phase="idle")
    return {"ok": True, "slug": slug}


def _project_info(slug: str) -> dict:
    root = PROJECTS_ROOT / slug
    try:
        data = json.loads((root / "project.json").read_text(encoding="utf-8"))
    except Exception:
        data = {}
    grid = {}
    try:
        grid = json.loads((root / "grid.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    s = data.get("settings") or {}
    return {
        "slug": slug, "name": data.get("name", slug),
        "save_location": (s.get("master_world_dir") or "").strip(),
        "cells": len(grid), "merged": sum(1 for v in grid.values() if v == "merged"),
        "scale": s.get("scale"), "active": slug == ACTIVE_SLUG,
    }


def _next_world_name() -> str:
    names = set()
    try:
        for p in PROJECTS_ROOT.iterdir():
            jf = p / "project.json"
            if jf.exists():
                try:
                    names.add(json.loads(jf.read_text(encoding="utf-8")).get("name"))
                except Exception:
                    pass
    except Exception:
        pass
    base, name, n = "Meld World", "Meld World", 2
    while name in names:
        name = f"{base} {n}"
        n += 1
    return name


@app.route("/api/projects")
def api_projects():
    PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    slugs = sorted(p.name for p in PROJECTS_ROOT.iterdir()
                   if p.is_dir() and (p / "project.json").exists())
    if ACTIVE_SLUG not in slugs:
        slugs.insert(0, ACTIVE_SLUG)
    return jsonify({"active": ACTIVE_SLUG, "projects": [_project_info(s) for s in slugs]})


@app.route("/api/projects/switch", methods=["POST"])
def api_projects_switch():
    d = request.json or {}
    slug = _slugify(d.get("slug") or "")
    if not (PROJECTS_ROOT / slug / "project.json").exists():
        return jsonify({"ok": False, "error": "project not found"}), 404
    res = _switch_project(slug)
    return jsonify(res), (200 if res.get("ok") else 409)


@app.route("/api/projects/new", methods=["POST"])
def api_projects_new():
    """Create a NEW project (a fresh world workspace) and switch to it. The current project
    and its world stay intact, so you can swap back. Auto-named 'Meld World N' if no name."""
    d = request.json or {}
    name = (d.get("name") or "").strip() or _next_world_name()
    base = _slugify(name)
    slug, i = base, 2
    while (PROJECTS_ROOT / slug / "project.json").exists():
        slug = f"{base}-{i}"
        i += 1
    p = Project(PROJECTS_ROOT / slug)
    data = p.load()
    data["name"] = name
    # Inherit the save location so new worlds land in the same folder by default.
    cur_dir = (PROJECT.settings().get("master_world_dir") or "").strip()
    if cur_dir and d.get("inherit_save_location", True):
        data.setdefault("settings", {})["master_world_dir"] = cur_dir
    p.save(data)
    res = _switch_project(slug)
    if not res.get("ok"):
        return jsonify(res), 409
    return jsonify({"ok": True, "slug": slug, "name": name})


@app.route("/api/projects/rename", methods=["POST"])
def api_projects_rename():
    d = request.json or {}
    slug = _slugify(d.get("slug") or ACTIVE_SLUG)
    name = (d.get("name") or "").strip()
    root = PROJECTS_ROOT / slug
    if not (root / "project.json").exists() or not name:
        return jsonify({"ok": False, "error": "project + name required"}), 400
    p = Project(root)
    data = p.load()
    data["name"] = name
    p.save(data)
    return jsonify({"ok": True, "slug": slug, "name": name})


@app.route("/api/projects/delete", methods=["POST"])
def api_projects_delete():
    """Remove a project's WORKSPACE (grid/logs/osm_cache/state). The saved Minecraft world on
    disk is NOT touched. Cannot delete the active project or while a run is going."""
    d = request.json or {}
    slug = _slugify(d.get("slug") or "")
    if slug == ACTIVE_SLUG:
        return jsonify({"ok": False, "error": "cannot delete the active project — switch first"}), 409
    if POOL.is_running():
        return jsonify({"ok": False, "error": "a generation is running"}), 409
    root = PROJECTS_ROOT / slug
    if not (root / "project.json").exists():
        return jsonify({"ok": False, "error": "project not found"}), 404
    shutil.rmtree(root, ignore_errors=True)
    return jsonify({"ok": True, "removed": slug,
                    "note": "project workspace removed (the saved world on disk is kept)"})


@app.route("/api/new-world", methods=["POST"])
def api_new_world():
    """Start a fresh world (Arnis-style 'new world'). The current world stays saved
    on disk; the project resets — next free 'Meld World N' name, cleared plan, and a
    fresh origin + elevation so the next selection starts clean."""
    POOL.clear()
    parent = Path((PROJECT.settings().get("master_world_dir") or "").strip() or PROJECT.root)
    existing = set()
    try:
        existing = {p.name for p in parent.iterdir() if p.is_dir()}
    except Exception:
        pass
    base, name, n = "Meld World", "Meld World", 2
    while name in existing:
        name = f"{base} {n}"
        n += 1
    data = PROJECT.load()
    data["name"] = name
    data["origin"] = {"lat": None, "lon": None, "locked": False}
    ev = data.get("elevation") or {"seed": 1}
    ev.update(min_m=None, max_m=None, locked=False)
    data["elevation"] = ev
    PROJECT.save(data)
    PROJECT.save_grid({})
    with _CELL_HEALTH_LOCK:           # a fresh world has no suspect cells
        _CELL_HEALTH.clear()
        _save_cell_health()
    with _RUN_LOCK:
        _RUN.update(started=None, ended=None, total=0, done=0, failed=0,
                    est_regions=0, est_mb=0, actual_mb=None, phase="idle")
    log(f"[New world] reset → '{name}'")
    return jsonify({"ok": True, "name": name})


@app.route("/api/queue/clear", methods=["POST"])
def api_queue_clear():
    n = POOL.clear()
    return jsonify({"ok": True, "cleared": n})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    POOL.clear()
    n = POOL.terminate_all()
    # Finalize a PARTIAL benchmark report for a run stopped mid-way, so the work so far (and where
    # it broke) is still saveable — cells that never finished show as running/incomplete.
    finalize = False
    with _RUN_LOCK:
        if _RUN.get("started") and not _RUN.get("ended"):
            _RUN["ended"] = time.time()
            try:
                _RUN["actual_mb"] = _dir_size_mb(master_world_path(create=False))
            except Exception:
                _RUN["actual_mb"] = None
            finalize = True
    if finalize:
        write_world_meta()
        _write_run_report()
    return jsonify({"ok": True, "terminated": n})


@app.route("/api/status")
def api_status():
    with _RUN_LOCK:
        run = dict(_RUN)
    now = time.time()
    run["elapsed"] = ((run["ended"] or now) - run["started"]) if run["started"] else 0
    run["active"] = bool(run["started"] and not run["ended"])
    states = POOL.get_states()
    stats = _sys_stats()
    if run["active"]:
        n_running = sum(1 for s in states if s.get("running"))
        ram_pct = (round(stats["ram_used_gb"] / stats["ram_total_gb"] * 100)
                   if stats.get("ram_used_gb") and stats.get("ram_total_gb") else None)
        _timeline_sample(n_running, run.get("done", 0) or 0, run.get("failed", 0) or 0,
                         cpu=stats.get("cpu_pct"), ram=ram_pct)
    with _RUN_TIMING_LOCK:
        run["timeline"] = [{k: v for k, v in b.items() if not k.startswith("_")} for b in _RUN_TIMELINE]
    with _PREFETCH_LOCK:
        prefetch = {"active": _PREFETCH["active"], "done": _PREFETCH["done"],
                    "note": _PREFETCH["note"], "chunks": list(_PREFETCH["chunks"]),
                    "phase": _PREFETCH.get("phase", "idle"),
                    "terrain": dict(_PREFETCH.get("terrain", {}))}
    with _CELL_HEALTH_LOCK:
        suspects = {k: v for k, v in _CELL_HEALTH.items() if v.get("suspect")}
        cell_fail = dict(_CELL_FAIL)
    with _MISSING_LOCK:
        missing = [dict(m) for m in _MISSING]
    with _EXPORT_LOCK:
        export = dict(_EXPORT)
    with _MCSERVER_LOCK:
        mcstat = {k: v for k, v in _MCSERVER.items() if k != "plan"}
        mcstat["console"] = list(mcstat["console"])
        p = _MCSERVER_PROC.get("proc")
        mcstat["running"] = bool(p and p.alive())
    # per-project server profile so the card re-fills after a reload/restart
    _st = PROJECT.settings()
    mcstat["profile"] = {k: _st.get(k) for k in
                         ("server_version", "server_mode", "server_dir", "server_world_src",
                          "server_extras", "server_voxy", "server_auto_restart",
                          "server_ram_gb", "server_cpu_pct", "server_backup_first")}
    mcstat["machine"] = _machine_specs()
    return jsonify({
        "workers": states,
        "queue_size": POOL.queue_size(),
        "running": POOL.is_running(),
        "grid": PROJECT.load_grid(),
        "cell_fail": cell_fail,
        "run": run,
        "prefetch": prefetch,
        "cell_health": suspects,
        "missing": missing,
        "stats": stats,
        "export": export,
        "mcserver": mcstat,
        "report_ready": _report_exists(),
        "log": _LOG[-150:],
    })


@app.route("/api/report")
def api_report():
    """Serve the latest benchmark report (meld-report.html) inline, so the UI can open it in a
    new tab. Falls back to the current world's report file if the in-memory pointer is stale."""
    p = _LAST_REPORT.get("html")
    if not (p and Path(p).exists()):
        try:
            cand = master_world_path(create=False) / runreport.REPORT_HTML_NAME
            p = str(cand) if cand.exists() else None
        except Exception:
            p = None
    if not p:
        return ("No benchmark report yet. Finish a generation run first.", 404)
    pp = Path(p)
    resp = send_from_directory(str(pp.parent), pp.name)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/api/report.json")
def api_report_json():
    """Serve the latest benchmark raw data (meld-report.json), so the report's 'Open full list'
    button can show every cell without bloating the printable HTML."""
    p = _LAST_REPORT.get("json")
    if not (p and Path(p).exists()):
        try:
            cand = master_world_path(create=False) / runreport.REPORT_JSON_NAME
            p = str(cand) if cand.exists() else None
        except Exception:
            p = None
    if not p:
        return ("No benchmark report yet. Finish a generation run first.", 404)
    pp = Path(p)
    resp = send_from_directory(str(pp.parent), pp.name, mimetype="application/json")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/api/prefetch/plan")
def api_prefetch_plan():
    """Preview the OSM download footprint for the current plan: the tiles Meld pre-splits the
    selection into (each ≤ the km² budget), drawn as gray-blue dotted boxes in the UI. At run
    time each tile downloads as one Overpass request; if one is still rejected or times out it
    splits into quadrants, and the live /api/status prefetch.chunks reflect that."""
    origin = PROJECT.origin()
    settings = PROJECT.settings()
    if origin.get("lat") is None or not settings.get("prefetch_enabled", True):
        return jsonify({"enabled": bool(settings.get("prefetch_enabled", True)), "chunks": []})
    scale = float(settings.get("scale", 1.0) or 1.0)
    grid = PROJECT.load_grid()
    cells = [{"cell_key": k, "bbox": _bbox_from_cell_key(k, origin, scale)}
             for k, v in grid.items() if v != "merged" and len(k.split(",")) == 3]
    return jsonify({"enabled": True, "chunks": preview_clumps(cells, origin, settings)})


def _save_drive_dir() -> str:
    """The directory the worlds save to (custom master_world_dir, else the project root),
    used to report free space on the RIGHT disk."""
    return (PROJECT.settings().get("master_world_dir") or "").strip() or str(PROJECT.root)


def _existing_dir(path: str) -> "Path | None":
    """Nearest EXISTING directory at or above `path`. shutil.disk_usage raises on a path that
    doesn't exist yet (a custom save folder not created, a deep world subpath), so climb to
    the first real ancestor to keep the disk gauge reporting the right drive. Returns None
    only if the whole drive is offline/unreachable."""
    if not path:
        return None
    try:
        p = Path(os.path.abspath(os.path.expanduser(path)))
    except Exception:  # noqa: BLE001
        return None
    while True:
        if p.exists():
            return p
        parent = p.parent
        if parent == p:        # reached the drive root and it doesn't exist → offline
            return None
        p = parent


# Overall CPU% from a dedicated background sampler. psutil.cpu_percent(interval=1) blocks 1s for an
# ACCURATE rolling system average; calling it with interval=None from the request path measured the
# sub-second gap between two polls under the threaded server, which read ~0%. The sampler stores the
# latest value; _sys_stats reads it non-blocking, so the gauge + report match Task Manager.
_CPU_PCT = {"v": 0, "started": False}


def _ensure_cpu_sampler() -> None:
    if _CPU_PCT["started"] or psutil is None:
        return
    _CPU_PCT["started"] = True

    def _loop():
        while True:
            try:
                _CPU_PCT["v"] = round(psutil.cpu_percent(interval=1.0))
            except Exception:
                time.sleep(1.0)
    threading.Thread(target=_loop, name="cpu-sampler", daemon=True).start()


def _sys_stats() -> dict:
    """Live CPU% / RAM / save-disk usage for the left-rail System card. CPU comes from the
    background sampler (accurate rolling %); RAM is total-available (Task Manager's 'in use')."""
    _ensure_cpu_sampler()
    out = {"cpu_pct": None, "ram_used_gb": None, "ram_total_gb": None,
           "disk_free_gb": None, "disk_total_gb": None, "drive": None}
    try:
        if psutil is not None:
            out["cpu_pct"] = _CPU_PCT["v"]   # rolling 1s average, matches Task Manager
            vm = psutil.virtual_memory()
            # "in use" the way Task Manager shows it = total - available (NOT psutil's .used, which
            # on Windows excludes the modified/standby cache and reads low vs the task manager number).
            out["ram_used_gb"] = round((vm.total - vm.available) / 1e9, 1)
            out["ram_total_gb"] = round(vm.total / 1e9, 1)
            out["ram_pct"] = round(vm.percent)
        else:
            out["ram_total_gb"] = _total_ram_gb()
    except Exception:
        pass
    try:
        raw = _save_drive_dir()
        out["drive"] = os.path.splitdrive(os.path.abspath(os.path.expanduser(raw)))[0] or raw
        d = _existing_dir(raw)                          # nearest existing ancestor
        if d is not None:
            du = shutil.disk_usage(str(d))              # decimal GB to match how drives report
            out["disk_free_gb"] = round(du.free / 1e9, 1)
            out["disk_total_gb"] = round(du.total / 1e9, 1)
        else:
            out["disk_offline"] = True                  # configured save drive is unreachable
    except Exception:
        pass
    return out


_HW_CACHE: dict = {}
_DDR_TYPE = {20: "DDR", 21: "DDR2", 24: "DDR3", 26: "DDR4", 34: "DDR5", 35: "DDR5"}


def _hw_specs(drive_hint: str | None = None) -> dict:
    """Best-effort hardware detail for the benchmark report: CPU model, RAM type + speed +
    module layout, and the save drive's media type (NVMe SSD / SSD / HDD). Windows uses a one-shot
    CIM probe (cached, ~1s); other OSes fall back to platform.processor(). Never raises."""
    key = (drive_hint or "").upper()[:1]
    if key in _HW_CACHE:
        return _HW_CACHE[key]
    out = {"cpu_model": None, "ram_kind": None, "ram_speed": None, "ram_modules": None, "drive_type": None}
    try:
        import platform
        out["cpu_model"] = (platform.processor() or "").strip() or None
    except Exception:
        pass
    if sys.platform == "win32":
        letter = key if key.isalpha() else "C"
        ps = (
            "$ErrorActionPreference='SilentlyContinue';"
            "$cpu=(Get-CimInstance Win32_Processor|Select-Object -First 1).Name;"
            "$mem=Get-CimInstance Win32_PhysicalMemory|ForEach-Object{[pscustomobject]@{cap=$_.Capacity;spd=$_.Speed;typ=$_.SMBIOSMemoryType}};"
            f"$pd=Get-Partition -DriveLetter {letter} -ErrorAction SilentlyContinue|Get-Disk|Get-PhysicalDisk;"
            "$media=($pd.MediaType|Select-Object -First 1);$bus=($pd.BusType|Select-Object -First 1);"
            "[pscustomobject]@{cpu=$cpu;mem=@($mem);media=\"$media\";bus=\"$bus\"}|ConvertTo-Json -Compress -Depth 4"
        )
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                               capture_output=True, text=True, timeout=12)
            data = json.loads((r.stdout or "").strip() or "{}")
            if data.get("cpu"):
                out["cpu_model"] = str(data["cpu"]).strip()
            mem = data.get("mem") or []
            if isinstance(mem, dict):
                mem = [mem]
            speeds = [int(m["spd"]) for m in mem if m.get("spd")]
            typs = [_DDR_TYPE.get(m.get("typ")) for m in mem if _DDR_TYPE.get(m.get("typ"))]
            caps = [int(m["cap"]) for m in mem if m.get("cap")]
            if speeds:
                out["ram_speed"] = max(speeds)
            if typs:
                out["ram_kind"] = typs[0]
            if caps:
                gb = [round(c / 1e9) for c in caps]
                out["ram_modules"] = (f"{len(gb)}×{gb[0]} GB" if len(set(gb)) == 1
                                      else " + ".join(f"{g} GB" for g in gb))
            media = (data.get("media") or "").strip()
            bus = (data.get("bus") or "").strip()
            if bus.lower() == "nvme":
                out["drive_type"] = "NVMe SSD"
            elif media and media.lower() not in ("unspecified", "0", ""):
                out["drive_type"] = media   # "SSD" / "HDD"
        except Exception:
            pass
    _HW_CACHE[key] = out
    return out


# ── recommend settings wizard: probe this PC + the save disk ────────────────────
def _total_ram_gb() -> float | None:
    try:  # Windows
        import ctypes

        class _MEMSTAT(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = _MEMSTAT(); m.dwLength = ctypes.sizeof(_MEMSTAT)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            return round(m.ullTotalPhys / (1024 ** 3), 1)
    except Exception:
        pass
    try:  # POSIX
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 ** 3), 1)
    except Exception:
        return None


def _disk_write_mbps(target_dir: str) -> float | None:
    """Sustained write speed of the disk the worlds save to (~192 MB, fsync'd)."""
    p = Path(target_dir)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    f = p / ".meld_diskbench.tmp"
    chunk = b"\0" * (8 * 1024 * 1024)   # 8 MB
    n = 24                              # ~192 MB
    try:
        t0 = time.time()
        with open(f, "wb") as fh:
            for _ in range(n):
                fh.write(chunk)
            fh.flush()
            os.fsync(fh.fileno())
        dt = time.time() - t0
        return round(n * 8 / dt) if dt > 0 else None
    except Exception:
        return None
    finally:
        try:
            f.unlink(missing_ok=True)
        except Exception:
            pass


@app.route("/api/recommend")
def api_recommend():
    """Probe CPU, RAM and the save-disk write speed, then recommend cell size, worker count and
    threads-per-worker. Generation is mostly CPU bound, so the recommendation keeps
    workers x threads at or under the core count (each worker gets >= 2 threads), with RAM and
    save-disk speed as secondary caps on the worker count. The UI lets the user push higher
    manually, with a warning."""
    cores = os.cpu_count() or 4
    ram_gb = _total_ram_gb()
    save_dir = str(master_world_path(create=True).parent)
    disk_mbps = _disk_write_mbps(save_dir)
    drive = (os.path.splitdrive(save_dir)[0] or save_dir)[:24]

    ram = ram_gb or 16.0
    disk = float(disk_mbps or 800)
    by_cpu = max(2, cores // 2)        # leave room for >= 2 threads per worker (workers x threads ~ cores)
    by_ram = max(2, int(ram // 3))     # ~3 GB per concurrent heavy (baked) save
    by_disk = max(2, int(disk // 90))  # ~90 MB/s sustained per worker during save bursts
    rec_workers = max(2, min(8, by_cpu, by_ram, by_disk))
    rec_threads = max(1, min(8, cores // max(1, rec_workers)))   # fill the cores: workers x threads ~ cores
    rec_cell = 4 if (disk < 600 or ram < 16) else 6
    rec_bake = ram >= 16
    bound = min([("CPU", by_cpu), ("RAM", by_ram), ("disk", by_disk)], key=lambda x: x[1])[0]
    note = (f"{rec_workers} workers x {rec_threads} threads = {rec_workers * rec_threads} of your "
            f"{cores} logical CPUs (hardware threads). Generation is mostly CPU bound, so this fills the "
            f"machine without oversubscribing (limited by {bound}; RAM and save-disk speed are secondary "
            f"caps). You can push higher manually.")
    return jsonify({"ok": True, "cores": cores, "ram_gb": ram_gb, "disk_mbps": disk_mbps,
                    "drive": drive, "rec_cell": rec_cell, "rec_workers": rec_workers,
                    "rec_threads": rec_threads, "rec_bake": rec_bake, "note": note})


# ── Border & zones (Advanced) ────────────────────────────────────────────────
# Build concentric country/zone rings (in world block coords), preview them on the map, and export
# WorldGuard regions.yml + per-ring point files. "Trim to ring" reuses /api/grid with the hard ring.
_LOOT_DEFAULT_CACHE = None
_VALID_ITEMS_CACHE = None
_LOOT_PRESET_DIR = BASE_DIR / "assets" / "loot_presets"


def _valid_items() -> set:
    """Set of valid Minecraft item ids (bundled from the 1.21 registry). Empty
    set if the bundle is missing, in which case id validation is skipped."""
    global _VALID_ITEMS_CACHE
    if _VALID_ITEMS_CACHE is None:
        try:
            data = json.loads((BASE_DIR / "assets" / "valid_items.json").read_text(encoding="utf-8"))
            _VALID_ITEMS_CACHE = set(data)
        except Exception:
            _VALID_ITEMS_CACHE = set()
    return _VALID_ITEMS_CACHE


def _loot_default() -> dict:
    """The built-in default loot table, exactly as the arnis binary defines it.
    Generated once via `arnis --dump-loot-table` into a bundled asset and cached,
    so the UI's default + "reset to default" always match the shipped binary."""
    global _LOOT_DEFAULT_CACHE
    if _LOOT_DEFAULT_CACHE is not None:
        return _LOOT_DEFAULT_CACHE
    cache_path = BASE_DIR / "assets" / "loot_table_default.json"
    if not cache_path.exists():
        exe = resolve_arnis_exe()
        if exe:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                # --bbox is a required arg; a dummy satisfies clap, then arnis
                # dumps the table and exits before any generation happens.
                subprocess.run([str(exe), "--dump-loot-table", str(cache_path),
                                "--bbox", "0,0,0.001,0.001"],
                               timeout=30, capture_output=True)
            except Exception as ex:
                log(f"loot: could not generate default table: {ex}")
    try:
        _LOOT_DEFAULT_CACHE = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        _LOOT_DEFAULT_CACHE = {}
    return _LOOT_DEFAULT_CACHE


def _validate_loot(cfg) -> str | None:
    """Structural check mirroring the Rust validator; returns an error string or None."""
    if not isinstance(cfg, dict):
        return "loot table must be a JSON object"
    for k in ("empty_weight", "rolls_min", "rolls_max"):
        if not isinstance(cfg.get(k), int) or isinstance(cfg.get(k), bool) or cfg[k] < 0:
            return f"'{k}' must be a non-negative integer"
    if cfg["rolls_max"] < cfg["rolls_min"]:
        return "rolls_max must be >= rolls_min"
    themes = cfg.get("themes")
    if not isinstance(themes, list) or not themes:
        return "'themes' must be a non-empty list"
    for ti, t in enumerate(themes):
        if not isinstance(t, dict) or not isinstance(t.get("weight"), int) or t["weight"] < 0:
            return f"theme {ti}: 'weight' must be a non-negative integer"
        items = t.get("items")
        if not isinstance(items, list) or not items:
            return f"theme {ti}: 'items' must be a non-empty list"
        for ii, it in enumerate(items):
            if not isinstance(it, dict):
                return f"theme {ti} item {ii}: must be an object"
            iid = it.get("id")
            if not isinstance(iid, str) or ":" not in iid:
                return f"theme {ti} item {ii}: 'id' must look like 'minecraft:apple'"
            reg = _valid_items()
            if reg and iid not in reg:
                return f"theme {ti} item {ii}: unknown item id '{iid}'"
            for k in ("min", "max", "weight"):
                if not isinstance(it.get(k), int) or isinstance(it.get(k), bool):
                    return f"theme {ti} item {ii}: '{k}' must be an integer"
            if it["min"] < 0 or it["min"] > it["max"] or it["max"] > 64:
                return f"theme {ti} item {ii}: bad count range (need 0 <= min <= max <= 64)"
    return None


@app.route("/api/loot-table", methods=["GET", "POST"])
def api_loot_table():
    """GET returns the project's loot table (or the built-in default if none saved);
    POST validates + saves a custom table to <project>/loot_table.json."""
    lt = PROJECT.root / "loot_table.json"
    if request.method == "GET":
        if lt.exists():
            try:
                return jsonify({"ok": True, "is_default": False,
                                "config": json.loads(lt.read_text(encoding="utf-8"))})
            except Exception as ex:
                return jsonify({"ok": False,
                                "error": f"stored loot_table.json is unreadable: {ex}"}), 400
        return jsonify({"ok": True, "is_default": True, "config": _loot_default()})
    cfg = (request.json or {}).get("config")
    err = _validate_loot(cfg)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    lt.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return jsonify({"ok": True})


@app.route("/api/loot-table/reset", methods=["POST"])
def api_loot_table_reset():
    """Delete the project's custom loot table so generation falls back to the default."""
    lt = PROJECT.root / "loot_table.json"
    try:
        lt.unlink(missing_ok=True)
    except Exception:
        pass
    return jsonify({"ok": True, "config": _loot_default()})


@app.route("/api/loot-items")
def api_loot_items():
    """The valid Minecraft item ids (for the loot editor's searchable picker).
    Sprites for each are served from /assets/items/<id_without_namespace>.png."""
    return jsonify(sorted(_valid_items()))


@app.route("/api/loot-presets")
def api_loot_presets():
    """List the bundled vanilla-structure loot presets (trial chambers, strongholds, etc.)."""
    idx = _LOOT_PRESET_DIR / "_index.json"
    if not idx.exists():
        return jsonify([])
    try:
        return jsonify(json.loads(idx.read_text(encoding="utf-8")))
    except Exception:
        return jsonify([])


@app.route("/api/loot-preset/<path:fname>")
def api_loot_preset(fname):
    """Return one preset's loot config so the editor can load it. Path-safe."""
    safe = Path(fname).name  # strip any directory components
    p = _LOOT_PRESET_DIR / safe
    if not safe.endswith(".json") or not p.exists() or p.parent != _LOOT_PRESET_DIR:
        return jsonify({"ok": False, "error": "unknown preset"}), 404
    try:
        return jsonify({"ok": True, "config": json.loads(p.read_text(encoding="utf-8"))})
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)}), 400


@app.route("/api/border/countries")
def api_border_countries():
    try:
        return jsonify({"ok": True, "countries": border.list_countries()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _border_inputs():
    d = request.json or {}
    origin = PROJECT.origin()
    if not origin or origin.get("lat") is None:
        return None, None, None, None, "set origin first"
    scale = float(PROJECT.settings().get("scale", 1.0))
    spec = d.get("spec") or {
        "zones": d.get("zones", []),
        "shared_lines": d.get("shared_lines", []),
        "shared_points": int(d.get("shared_points", 20)),
    }
    return d, spec, origin, scale, None


@app.route("/api/border/preview", methods=["POST"])
def api_border_preview():
    _d, spec, origin, scale, err = _border_inputs()
    if err:
        return jsonify({"ok": False, "error": err}), 400
    if not spec.get("zones"):
        return jsonify({"ok": False, "error": "add at least one zone"}), 400
    try:
        res = border.build(spec, origin, scale)
        return jsonify({"ok": True, "preview": border.preview(res)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/border/export", methods=["POST"])
def api_border_export():
    d, spec, origin, scale, err = _border_inputs()
    if err:
        return jsonify({"ok": False, "error": err}), 400
    if not spec.get("zones"):
        return jsonify({"ok": False, "error": "add at least one zone"}), 400
    s = PROJECT.settings()
    min_y = int(d.get("min_y", s.get("ground_level", -64)))
    max_y = int(d.get("max_y", 2031 if s.get("disable_height_limit") else 320))
    try:
        res = border.build(spec, origin, scale)
        outdir = str(Path(PROJECT.root) / "border")
        info = border.write_exports(res, outdir, min_y, max_y, d.get("skript") or {})
        return jsonify({"ok": True, **info, "min_y": min_y, "max_y": max_y})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


def _preview_window_bbox(b: dict, scale: float, regions: int) -> dict:
    """Centered sub-bbox spanning at most `regions` x `regions` Minecraft regions (512 blocks
    each) about the center of `b`, so a preview shows LOCAL detail instead of a whole huge
    selection downsampled to nothing. Clipped to `b`, so a selection smaller than the window
    just returns the whole selection. `regions <= 0` means no windowing (full selection)."""
    if regions is None or regions <= 0:
        return dict(b)
    mpd_lat = 111_320.0
    clat = (float(b["south"]) + float(b["north"])) / 2.0
    clon = (float(b["west"]) + float(b["east"])) / 2.0
    mpd_lon = mpd_lat * math.cos(math.radians(clat))
    scale = scale if scale and scale > 0 else 1.0
    half_lat = (regions / 2.0) * 512.0 / (mpd_lat * scale)
    half_lon = (regions / 2.0) * 512.0 / (max(mpd_lon, 1e-6) * scale)
    return {
        "south": max(float(b["south"]), clat - half_lat),
        "north": min(float(b["north"]), clat + half_lat),
        "west": max(float(b["west"]), clon - half_lon),
        "east": min(float(b["east"]), clon + half_lon),
    }


def _preview_regions_arg(default: int = 3) -> int:
    """Read the `regions` window size from the request body (clamped 0..25; 0 = full)."""
    try:
        r = int((request.get_json(silent=True) or {}).get("regions", default))
    except (TypeError, ValueError):
        r = default
    return max(0, min(25, r))


# ── cave biome zone-map preview ───────────────────────────────────────────────

@app.route("/api/cavemap", methods=["POST"])
def api_cavemap():
    """Render the cave BIOME ZONE layout for the drawn selection through the arnis
    fork's --cave-zone-map mode (the exact zone picker + seed + --cave-biomes values
    generation will use), and return measured per-theme percentages + overlay bounds.
    Fast (no worldgen); runs synchronously."""
    origin = PROJECT.origin()
    if origin.get("lat") is None:
        return jsonify({"ok": False, "error": "set the origin first"}), 400
    exe = resolve_arnis_exe()
    if not exe:
        return jsonify({"ok": False, "error": "arnis binary not found"}), 400
    st = PROJECT.settings()
    sel = PROJECT.load_selection()
    if sel:
        b = sel["bbox"]
    else:
        # no drawn selection: cover the PLANNED/merged cells instead
        grid = PROJECT.load_grid()
        scale_f = float(st.get("scale", 1.0) or 1.0)
        b = None
        for key in grid:
            parts = key.split(",")
            if len(parts) != 3:
                continue
            cb = cell_bbox(int(parts[0]), int(parts[1]), int(parts[2]),
                           origin["lat"], origin["lon"], scale_f)
            if b is None:
                b = dict(cb)
            else:
                b["south"] = min(b["south"], cb["south"])
                b["west"] = min(b["west"], cb["west"])
                b["north"] = max(b["north"], cb["north"])
                b["east"] = max(b["east"], cb["east"])
        if b is None:
            return jsonify({"ok": False, "error": "draw a selection (or plan cells) first"}), 400
    seed = int((PROJECT.load().get("elevation") or {}).get("seed", 1) or 1)
    # Window to a centered NxN-region patch (default 3x3) so the preview shows local detail.
    b = _preview_window_bbox(b, float(st.get("scale", 1.0) or 1.0), _preview_regions_arg())
    out_dir = Path(PROJECT.root) / "cavemap"
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = out_dir / "zones"
    cmd = [str(exe),
           "--bbox", f"{b['south']},{b['west']},{b['north']},{b['east']}",
           "--scale", str(float(st.get("scale", 1.0) or 1.0)),
           "--master-origin-lat", str(origin["lat"]),
           "--master-origin-lng", str(origin["lon"]),
           "--tile-invariant-rendering", str(seed),
           "--cave-zone-map", str(prefix)]
    # square size in blocks (one zone sample per square, drawn as a crisp cell);
    # 0/absent = fine per-block sampling
    try:
        step = int((request.get_json(silent=True) or {}).get("step") or 0)
    except (TypeError, ValueError):
        step = 0
    if step > 0:
        cmd += ["--cave-zone-map-step", str(max(4, min(512, step)))]
    spec = arnis_cmd.cave_biomes_spec(st)
    if spec:
        cmd += ["--cave-biomes", spec]
    try:
        pr = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=180)
    except (OSError, subprocess.TimeoutExpired) as e:
        return jsonify({"ok": False, "error": f"zone-map run failed: {e}"}), 400
    stats = None
    for line in (pr.stdout or "").splitlines():
        if line.startswith("ZONEMAP "):
            try:
                stats = json.loads(line[len("ZONEMAP "):])
            except ValueError:
                pass
    if pr.returncode != 0 or stats is None:
        tail = ((pr.stderr or "") + (pr.stdout or ""))[-300:]
        return jsonify({"ok": False, "error": f"zone-map failed (rc={pr.returncode}): {tail}"}), 400
    return jsonify({"ok": True, "stats": stats,
                    "bounds": [[b["south"], b["west"]], [b["north"], b["east"]]],
                    "images": {"upper": "/api/cavemap/img/upper",
                               "deep": "/api/cavemap/img/deep"}})


@app.route("/api/cavemap/img/<tag>")
def api_cavemap_img(tag):
    if tag not in ("upper", "deep"):
        return jsonify({"ok": False, "error": "tag must be upper|deep"}), 400
    p = Path(PROJECT.root) / "cavemap" / f"zones-{tag}.png"
    if not p.is_file():
        return jsonify({"ok": False, "error": "no zone map rendered yet"}), 404
    resp = send_file(str(p), mimetype="image/png")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/climatemap", methods=["POST"])
def api_climatemap():
    """Render the Koppen CLIMATE layout for the drawn selection (or planned cells)
    through the arnis fork's --climate-map mode, which colours each sample by the same
    grouped Climate that drives biome tint + arid/polar surface blocks during real
    generation. Sampled in lat/lon over the bbox, so it is a pure function of the
    bounding box. Fast (no worldgen); runs synchronously."""
    origin = PROJECT.origin()
    if origin.get("lat") is None:
        return jsonify({"ok": False, "error": "set the origin first"}), 400
    exe = resolve_arnis_exe()
    if not exe:
        return jsonify({"ok": False, "error": "arnis binary not found"}), 400
    st = PROJECT.settings()
    sel = PROJECT.load_selection()
    if sel:
        b = sel["bbox"]
    else:
        # no drawn selection: cover the PLANNED/merged cells instead
        grid = PROJECT.load_grid()
        scale_f = float(st.get("scale", 1.0) or 1.0)
        b = None
        for key in grid:
            parts = key.split(",")
            if len(parts) != 3:
                continue
            cb = cell_bbox(int(parts[0]), int(parts[1]), int(parts[2]),
                           origin["lat"], origin["lon"], scale_f)
            if b is None:
                b = dict(cb)
            else:
                b["south"] = min(b["south"], cb["south"])
                b["west"] = min(b["west"], cb["west"])
                b["north"] = max(b["north"], cb["north"])
                b["east"] = max(b["east"], cb["east"])
        if b is None:
            return jsonify({"ok": False, "error": "draw a selection (or plan cells) first"}), 400
    # Window to a centered NxN-region patch (default 3x3) so the preview shows local detail.
    b = _preview_window_bbox(b, float(st.get("scale", 1.0) or 1.0), _preview_regions_arg())
    out_dir = Path(PROJECT.root) / "climatemap"
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = out_dir / "climate"
    cmd = [str(exe),
           "--bbox", f"{b['south']},{b['west']},{b['north']},{b['east']}",
           "--climate-map", str(prefix)]
    try:
        pr = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=180)
    except (OSError, subprocess.TimeoutExpired) as e:
        return jsonify({"ok": False, "error": f"climate-map run failed: {e}"}), 400
    stats = None
    for line in (pr.stdout or "").splitlines():
        if line.startswith("CLIMATEMAP "):
            try:
                stats = json.loads(line[len("CLIMATEMAP "):])
            except ValueError:
                pass
    if pr.returncode != 0 or stats is None:
        tail = ((pr.stderr or "") + (pr.stdout or ""))[-300:]
        return jsonify({"ok": False, "error": f"climate-map failed (rc={pr.returncode}): {tail}"}), 400
    return jsonify({"ok": True, "stats": stats,
                    "bounds": [[b["south"], b["west"]], [b["north"], b["east"]]],
                    "image": "/api/climatemap/img"})


@app.route("/api/climatemap/img")
def api_climatemap_img():
    p = Path(PROJECT.root) / "climatemap" / "climate.png"
    if not p.is_file():
        return jsonify({"ok": False, "error": "no climate map rendered yet"}), 404
    resp = send_file(str(p), mimetype="image/png")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/elevationmap", methods=["POST"])
def api_elevationmap():
    """Render the elevation heightmap for the drawn selection (or planned cells) through the arnis
    fork's --elevation-map mode, which uses the REAL provider stack generation uses (Mapterhorn /
    regional / AWS), so the preview matches the world's terrain. Returns the PNG + geographic bounds
    for a Leaflet imageOverlay + the min/max metres. Fast (no worldgen); runs synchronously."""
    origin = PROJECT.origin()
    if origin.get("lat") is None:
        return jsonify({"ok": False, "error": "set the origin first"}), 400
    exe = resolve_arnis_exe()
    if not exe:
        return jsonify({"ok": False, "error": "arnis binary not found"}), 400
    st = PROJECT.settings()
    sel = PROJECT.load_selection()
    if sel:
        b = sel["bbox"]
    else:
        grid = PROJECT.load_grid()
        scale_f = float(st.get("scale", 1.0) or 1.0)
        b = None
        for key in grid:
            parts = key.split(",")
            if len(parts) != 3:
                continue
            cb = cell_bbox(int(parts[0]), int(parts[1]), int(parts[2]),
                           origin["lat"], origin["lon"], scale_f)
            if b is None:
                b = dict(cb)
            else:
                b["south"] = min(b["south"], cb["south"])
                b["west"] = min(b["west"], cb["west"])
                b["north"] = max(b["north"], cb["north"])
                b["east"] = max(b["east"], cb["east"])
        if b is None:
            return jsonify({"ok": False, "error": "draw a selection (or plan cells) first"}), 400
    seed = int((PROJECT.load().get("elevation") or {}).get("seed", 1) or 1)
    mode = (request.get_json(silent=True) or {}).get("mode", "hillshade")
    if mode not in ("hillshade", "grayscale"):
        mode = "hillshade"
    out_dir = Path(PROJECT.root) / "elevmap"
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = out_dir / "elev"
    cmd = [str(exe),
           "--bbox", f"{b['south']},{b['west']},{b['north']},{b['east']}",
           "--scale", str(float(st.get("scale", 1.0) or 1.0)),
           "--master-origin-lat", str(origin["lat"]),
           "--master-origin-lng", str(origin["lon"]),
           "--tile-invariant-rendering", str(seed),
           "--elevation-map", str(prefix),
           "--elevation-map-mode", mode]
    # Use the SAME provider generation will: force legacy AWS only if that toggle is set.
    if st.get("aws_only_elevation"):
        cmd += ["--aws-only-elevation"]
    elif st.get("regional_elevation_only"):
        cmd += ["--regional-elevation-only"]
    try:
        pr = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        return jsonify({"ok": False, "error": f"elevation-map run failed: {e}"}), 400
    stats = None
    for line in (pr.stdout or "").splitlines():
        if line.startswith("ELEVMAP "):
            try:
                stats = json.loads(line[len("ELEVMAP "):])
            except ValueError:
                pass
    if pr.returncode != 0 or stats is None:
        tail = ((pr.stderr or "") + (pr.stdout or ""))[-300:]
        return jsonify({"ok": False, "error": f"elevation-map failed (rc={pr.returncode}): {tail}"}), 400
    bb = stats.get("bbox", [b["south"], b["west"], b["north"], b["east"]])
    return jsonify({"ok": True,
                    "bounds": [[bb[0], bb[1]], [bb[2], bb[3]]],
                    "image": "/api/elevationmap/img",
                    "min_m": stats.get("min_m"), "max_m": stats.get("max_m"),
                    "provider": stats.get("provider"), "mode": stats.get("mode", mode)})


@app.route("/api/elevationmap/img")
def api_elevationmap_img():
    p = Path(PROJECT.root) / "elevmap" / "elev.png"
    if not p.is_file():
        return jsonify({"ok": False, "error": "no elevation map rendered yet"}), 404
    resp = send_file(str(p), mimetype="image/png")
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ── one-click Leaf server setup ───────────────────────────────────────────────
# Turns the finished world into a ready-to-run Leaf server. Every step that
# downloads or executes anything checks an explicit confirm flag SERVER-SIDE;
# EULA acceptance is its own deliberate action. See src/mcserver.py.

def _mcs_server_dir() -> Path:
    eff = _mcs_effective_server()
    if eff is not None:
        return eff[0]
    with _MCSERVER_LOCK:
        d = _MCSERVER.get("server_dir")
    if d:
        return Path(d)
    return Path(PROJECT.root) / "server" / "leaf"


def _mcs_effective_server() -> tuple[Path, str] | None:
    """(server_dir, target_world) from live state, falling back to the saved per-project
    profile — so Pre-render / Backup / Start work on an already-staged server right after
    a Meld restart, without forcing a re-Stage (which would re-copy the world)."""
    with _MCSERVER_LOCK:
        sdir, tw = _MCSERVER.get("server_dir"), _MCSERVER.get("target_world")
    if sdir and tw:
        return Path(sdir), tw
    st = PROJECT.settings()
    custom = (st.get("server_dir") or "").strip()
    ver = (st.get("server_version") or "").strip()
    cand = Path(custom) if custom else (
        Path(PROJECT.root) / "server" / f"leaf-{ver}" if ver else None)
    if not cand or not cand.is_dir():
        return None
    mode = st.get("server_mode") or "main"
    # Stage feeds the display name through mcs.safe_world_name, which also maps "." to
    # "_" ("Romania Server 1.0" -> romania_server_1_0) — reproduce the exact chain or
    # the recovered name misses the staged folder.
    tw = "world" if mode == "main" else mcs.safe_world_name(
        _safe_world_name(PROJECT.load().get("name", "Meld World")).replace(" ", "_").lower())
    # Leaf migrates an imported sub-world into world/dimensions/minecraft/<name>/ on
    # first boot — after that the staged top-level folder no longer exists.
    if not ((cand / tw).is_dir()
            or (cand / "world" / "dimensions" / "minecraft" / tw).is_dir()):
        return None
    _mcs_set(server_dir=str(cand), target_world=tw)
    return cand, tw


@app.route("/api/mcserver/versions")
def api_mcs_versions():
    try:
        return jsonify({"ok": True, **mcs.list_versions()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/mcserver/plan", methods=["POST"])
def api_mcs_plan():
    """Dry run: resolve jar + plugins + world source for review. Downloads nothing."""
    d = request.get_json(silent=True) or {}
    version = str(d.get("version") or "").strip()
    mode = "subworld" if d.get("mode") == "subworld" else "main"
    if not version:
        return jsonify({"ok": False, "error": "pick a version first"}), 400
    world = master_world_path(create=False)
    if not (world / "region").is_dir():
        return jsonify({"ok": False, "error": "no finished world yet — generate first"}), 400
    st = PROJECT.settings()
    # world files + Leaf region-format are decided TOGETHER (they must agree):
    # auto follows the Export settings; mca/linear/blinear are explicit picks.
    choice = str(d.get("world_src") or "auto")
    try:
        src, fmt = mcs.pick_world_source(world, st.get("export_format", "none"),
                                         st.get("export_destination", "in_place"), choice)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    try:
        plan = mcs.build_plan(version, mode=mode, with_extras=bool(d.get("extras")),
                              with_voxy=bool(d.get("voxy")))
    except Exception as e:
        return jsonify({"ok": False, "error": f"resolve failed: {e}"}), 400
    java = mcs.find_java(mcs.required_java(version))
    _mcs_set(version=version, mode=mode, plan=plan, voxy=bool(plan.get("voxy")),
             world_choice=choice, message="plan ready")
    # server profile: remember the choices so re-opening the project re-fills the card
    PROJECT.update_settings({"server_version": version, "server_mode": mode,
                             "server_extras": bool(d.get("extras")),
                             "server_voxy": bool(d.get("voxy")),
                             "server_world_src": choice})
    return jsonify({"ok": True, "plan": plan, "world_source": str(src),
                    "region_format": fmt,
                    "java": java,
                    "java_required": mcs.required_java(version),
                    "java_note": None if java else
                    "No suitable Java found — install a matching JRE (e.g. via the Modrinth app) "
                    "and re-plan. Meld never downloads a Java runtime itself."})


@app.route("/api/mcserver/stage", methods=["POST"])
def api_mcs_stage():
    """Scaffold the server dir + copy the world in (configs, eula stub, start scripts)."""
    d = request.get_json(silent=True) or {}
    with _MCSERVER_LOCK:
        plan, version, mode = _MCSERVER.get("plan"), _MCSERVER.get("version"), _MCSERVER.get("mode")
        choice = _MCSERVER.get("world_choice") or "auto"
    if not plan:
        return jsonify({"ok": False, "error": "run Plan first"}), 400
    st = PROJECT.settings()
    world = master_world_path(create=False)
    try:
        src, fmt = mcs.pick_world_source(world, st.get("export_format", "none"),
                                         st.get("export_destination", "in_place"), choice)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    java = mcs.find_java(mcs.required_java(version)) or {"exe": "java"}
    # user-chosen server folder (Browse… in the UI); blank = inside the project.
    custom = str(d.get("dir") or "").strip()
    sdir = Path(custom) if custom else Path(PROJECT.root) / "server" / f"leaf-{version}"
    name = _safe_world_name(PROJECT.load().get("name", "Meld World")).replace(" ", "_").lower()
    port = int(d.get("port") or 25565)
    heap, cpu_n = _mcs_resources()
    try:
        info = mcs.stage_server(
            sdir, src, mode=mode, world_name=name,
            region_format=fmt,
            motd=f"Meld — {PROJECT.load().get('name', 'Meld World')} (Leaf {version})",
            port=port, jar_name=plan["jar"]["jar_name"], java_exe=java["exe"],
            with_voxy=bool(plan.get("voxy")), heap=heap, cpu_count=cpu_n)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    _mcs_set(server_dir=str(sdir), target_world=info["target_world"], port=port,
             phase="staged", message=f"staged at {sdir}")
    PROJECT.update_settings({"server_dir": custom})
    return jsonify({"ok": True, "server_dir": str(sdir), **info})


@app.route("/api/mcserver/install", methods=["POST"])
def api_mcs_install():
    """Download the reviewed jar + plugins. Requires confirm:true (checked here, server-side)."""
    d = request.get_json(silent=True) or {}
    if d.get("confirm") is not True:
        return jsonify({"ok": False, "error": "downloads require confirm:true"}), 403
    with _MCSERVER_LOCK:
        plan = _MCSERVER.get("plan")
        sdir = _MCSERVER.get("server_dir")
    if not plan or not sdir:
        return jsonify({"ok": False, "error": "run Plan + Stage first"}), 400

    def _work():
        _mcs_set(phase="downloading", message="downloading jar + plugins…")
        try:
            written = mcs.install_plan(Path(sdir), plan, on_progress=lambda m: _mcs_set(message=m))
            _mcs_set(phase="installed", message=f"downloaded {len(written)} files (hash-verified)")
        except Exception as e:  # noqa: BLE001
            _mcs_set(phase="error", message=f"download failed: {e}")

    threading.Thread(target=_work, daemon=True, name="mcserver-install").start()
    return jsonify({"ok": True})


@app.route("/api/mcserver/eula", methods=["POST"])
def api_mcs_eula():
    """Accept Mojang's EULA — separate, deliberate action; never bundled with anything else."""
    d = request.get_json(silent=True) or {}
    if d.get("accept") is not True:
        return jsonify({"ok": False, "error": "requires accept:true"}), 403
    try:
        mcs.accept_eula(_mcs_server_dir(), True)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    _mcs_set(eula=True, message="EULA accepted")
    return jsonify({"ok": True})


def _mcs_launch() -> tuple[bool, str | None]:
    """Preflight + launch from the current _MCSERVER state. Shared by the Start
    route and the crash watchdog's auto-restart (which re-runs the same checks)."""
    with _MCSERVER_LOCK:
        plan, sdir, mode, tw, port = (_MCSERVER.get("plan"), _MCSERVER.get("server_dir"),
                                      _MCSERVER.get("mode"), _MCSERVER.get("target_world"),
                                      _MCSERVER.get("port", 25565))
        version = _MCSERVER.get("version")
    p = _MCSERVER_PROC.get("proc")
    if p and p.alive():
        return False, "server already running"
    if not sdir or not tw:
        eff = _mcs_effective_server()   # already-staged server surviving a Meld restart
        if eff is None:
            return False, "run Plan + Stage + Download first"
        sdir, tw = str(eff[0]), eff[1]
    sdir_p = Path(sdir)
    # The plan only contributes the jar name at this point. After a Meld restart the
    # plan is gone but the staged server is fully intact — recover the jar from disk
    # instead of forcing the user back through Plan/Stage/Download.
    if plan:
        jar_name = plan["jar"]["jar_name"]
    else:
        jars = sorted(sdir_p.glob("leaf-*.jar")) or sorted(sdir_p.glob("*.jar"))
        if not jars:
            return False, "run Plan + Stage + Download first"
        jar_name = jars[-1].name
        if not version:
            m = re.match(r"leaf-([\w.]+?)-\d+\.jar", jar_name)
            version = m.group(1) if m else (PROJECT.settings().get("server_version") or None)
    eula = (sdir_p / "eula.txt").read_text(encoding="utf-8", errors="replace") if (sdir_p / "eula.txt").exists() else ""
    if "eula=true" not in eula:
        return False, "EULA not accepted yet"
    if not (sdir_p / jar_name).is_file():
        return False, "server jar missing — run Download first"
    if not mcs.port_free(port):
        return False, f"port {port} is already in use"
    java = mcs.find_java(mcs.required_java(version))
    if not java:
        return False, "no suitable Java runtime found"

    border_dir = Path(PROJECT.root) / "border"

    def _on_ready():
        # first-start automation, all via the server's own console (stdin) — no RCON:
        # mount the sub-world through Multiverse's supported import path, push the
        # border exports into the live plugin dirs, reload them.
        try:
            pushed = mcs.push_border_files(sdir_p, border_dir, tw or "world")
            proc = _MCSERVER_PROC.get("proc")
            if proc and proc.alive():
                for cmd in mcs.first_start_commands(mode, tw or "world", bool(pushed)):
                    proc.send(cmd)
                    time.sleep(1.0)
            _mcs_set(message="ready" + (f" · pushed: {', '.join(pushed)}" if pushed else ""))
        except Exception as e:  # noqa: BLE001
            _mcs_set(message=f"ready, but post-start automation failed: {e}")

    def _on_exit(code, user_stop):
        # crash watchdog: a user stop is reported by the stop route; anything else
        # is a crash — auto-restart (if enabled) with a 3-per-10-min brake.
        _MCSERVER_PROC["proc"] = None
        if user_stop:
            return
        with _MCSERVER_LOCK:
            auto = _MCSERVER.get("auto_restart", True)
        now = time.time()
        _MCSERVER_RESTARTS[:] = [t for t in _MCSERVER_RESTARTS if now - t < 600]
        if not auto:
            _mcs_set(phase="crashed", running=False,
                     message=f"server exited unexpectedly (code {code}) — auto-restart is off")
            return
        if len(_MCSERVER_RESTARTS) >= 3:
            _mcs_set(phase="crashed", running=False,
                     message=f"server exited unexpectedly (code {code}) — restart brake hit "
                             "(3 crashes in 10 min); check the console/logs, then Start manually")
            return
        _MCSERVER_RESTARTS.append(now)
        _mcs_set(phase="restarting", running=False, restarts=len(_MCSERVER_RESTARTS),
                 message=f"server exited unexpectedly (code {code}) — auto-restarting "
                         f"({len(_MCSERVER_RESTARTS)}/3 in 10 min)…")
        time.sleep(3.0)
        ok, err = _mcs_launch()
        if not ok:
            _mcs_set(phase="crashed", running=False, message=f"auto-restart failed: {err}")

    heap, cpu_n = _mcs_resources()
    try:
        # keep the manual start scripts in sync with the knobs used for this launch
        mcs.write_start_scripts(sdir_p, jar_name, java["exe"],
                                xms=heap, xmx=heap, cpu_count=cpu_n)
    except OSError:
        pass
    try:
        proc = mcs.ServerProc(sdir_p, java["exe"], jar_name,
                              on_line=_mcs_console, on_ready=_on_ready, on_exit=_on_exit,
                              xms=heap, xmx=heap, cpu_count=cpu_n)
    except Exception as e:  # noqa: BLE001
        return False, f"launch failed: {e}"
    _MCSERVER_PROC["proc"] = proc
    _mcs_set(phase="running", running=True,
             message=f"starting… (localhost:{port}, {heap} heap, {cpu_n} cores)")
    return True, None


@app.route("/api/mcserver/start", methods=["POST"])
def api_mcs_start():
    """Launch the staged server (requires confirm:true). Runs downloaded code —
    only after the user explicitly confirmed both the install and this start.
    The FIRST start also zips the staged world to backups/ before launching."""
    d = request.get_json(silent=True) or {}
    if d.get("confirm") is not True:
        return jsonify({"ok": False, "error": "starting the server requires confirm:true"}), 403
    with _MCSERVER_LOCK:
        sdir, tw = _MCSERVER.get("server_dir"), _MCSERVER.get("target_world")
    marker = Path(sdir) / ".meld-first-start-done" if sdir else None
    backup_first = PROJECT.settings().get("server_backup_first", True)
    if marker is not None and sdir and not marker.exists():
        # first start happened — record it even when the backup is skipped, so
        # enabling the toggle later never backs up an already-played world
        if not backup_first:
            try:
                marker.write_text("backup: skipped (server_backup_first off)\n", encoding="utf-8")
            except OSError:
                pass
    if (backup_first and marker is not None and sdir and not marker.exists()
            and (Path(sdir) / (tw or "world")).is_dir()):
        def _backup_then_launch():
            try:
                _mcs_set(phase="backing-up", message="first start — zipping a world backup…")
                dest = mcs.backup_world(Path(sdir), tw or "world",
                                        on_progress=lambda i, n: _mcs_set(
                                            message=f"first start — backing up world ({i}/{n} files)…"))
                marker.write_text("backup: " + dest.name + "\n", encoding="utf-8")
                _mcs_set(message=f"backup done ({dest.name}) — starting…")
            except Exception as e:  # noqa: BLE001
                # the master world in the project is the true source; don't block on a failed zip
                _mcs_set(message=f"backup failed ({e}) — starting anyway (master world is intact)")
            ok, err = _mcs_launch()
            if not ok:
                _mcs_set(phase="error", running=False, message=f"start failed: {err}")
        threading.Thread(target=_backup_then_launch, daemon=True, name="mcserver-start").start()
        return jsonify({"ok": True, "backup": True})
    ok, err = _mcs_launch()
    if not ok:
        code = 403 if "EULA" in (err or "") else 400
        return jsonify({"ok": False, "error": err}), code
    return jsonify({"ok": True})


@app.route("/api/mcserver/stop", methods=["POST"])
def api_mcs_stop():
    p = _MCSERVER_PROC.get("proc")
    if not p or not p.alive():
        return jsonify({"ok": False, "error": "not running"}), 400

    def _work():
        _mcs_set(phase="stopping", message="saving world + stopping…")
        code = p.stop()
        _mcs_set(phase="stopped", running=False, message=f"stopped (exit {code})")

    threading.Thread(target=_work, daemon=True, name="mcserver-stop").start()
    return jsonify({"ok": True})


@app.route("/api/mcserver/cmd", methods=["POST"])
def api_mcs_cmd():
    """Console passthrough — type a command into the running server."""
    d = request.get_json(silent=True) or {}
    cmd = str(d.get("command") or "").strip()
    p = _MCSERVER_PROC.get("proc")
    if not cmd:
        return jsonify({"ok": False, "error": "empty command"}), 400
    if not p or not p.alive():
        return jsonify({"ok": False, "error": "not running"}), 400
    try:
        p.send(cmd)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/mcserver/open", methods=["POST"])
def api_mcs_open():
    """Open the server folder in the OS file browser (climbs to the first existing
    ancestor, same convention as /api/open-folder)."""
    target = _mcs_server_dir()
    while not target.exists() and target.parent != target:
        target = target.parent
    if not target.exists():
        return jsonify({"ok": False, "error": "server folder does not exist yet — Stage first"}), 400
    try:
        if sys.platform == "win32":
            os.startfile(str(target))   # type: ignore[attr-defined]  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
        return jsonify({"ok": True, "folder": str(target)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/mcserver/backup", methods=["POST"])
def api_mcs_backup():
    """Zip the staged server world to backups/ on demand. Refused while the server
    runs (a live world zips inconsistent) — stop first, or rely on the automatic
    first-start backup."""
    eff = _mcs_effective_server()
    if eff is None:
        return jsonify({"ok": False, "error": "no staged server"}), 400
    sdir, tw = (str(eff[0]), eff[1])
    p = _MCSERVER_PROC.get("proc")
    if p and p.alive():
        return jsonify({"ok": False, "error": "stop the server first — a running world zips inconsistent"}), 400

    def _work():
        try:
            _mcs_set(message="zipping world backup…")
            dest = mcs.backup_world(Path(sdir), tw, on_progress=lambda i, n: _mcs_set(
                message=f"backup: {i}/{n} files…"))
            _mcs_set(message=f"backup written: backups/{dest.name}")
        except Exception as e:  # noqa: BLE001
            _mcs_set(message=f"backup failed: {e}")

    threading.Thread(target=_work, daemon=True, name="mcserver-backup").start()
    return jsonify({"ok": True})


@app.route("/api/mcserver/console")
def api_mcs_console_tail():
    """Light console feed for the pop-out window (avoids the full /api/status payload)."""
    with _MCSERVER_LOCK:
        console = list(_MCSERVER.get("console") or [])
        phase = _MCSERVER.get("phase")
    p = _MCSERVER_PROC.get("proc")
    return jsonify({"ok": True, "console": console, "phase": phase,
                    "running": bool(p and p.alive())})


@app.route("/console")
def console_page():
    """Pop-out live server console — same pattern as /logs, plus a command input."""
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Meld - Server console</title>"
        "<style>body{background:#13110d;color:#cdc3ad;margin:0;display:flex;"
        "flex-direction:column;height:100vh;font:12px/1.5 ui-monospace,Consolas,monospace}"
        "#head{padding:6px 12px;color:#e3a417;border-bottom:1px solid #2e2a20;flex:0 0 auto}"
        "#wrap{flex:1 1 auto;overflow-y:auto;padding:10px 12px}"
        "pre{white-space:pre-wrap;word-break:break-word;margin:0}"
        "#bar{display:flex;gap:6px;padding:8px 12px;border-top:1px solid #2e2a20;flex:0 0 auto}"
        "input{flex:1;background:#0b0a08;color:#f0e9da;border:1px solid #2e2a20;"
        "padding:6px 8px;font:inherit;outline:none}"
        "button{background:#e3a417;color:#241a02;border:0;padding:6px 14px;font:inherit;"
        "font-weight:700;cursor:pointer}</style></head>"
        "<body><div id='head'>server console - <span id='st'>...</span></div>"
        "<div id='wrap'><pre id='c'>loading...</pre></div>"
        "<div id='bar'><input id='cmd' placeholder='console command (e.g. mv list, save-all)'>"
        "<button onclick='send()'>Send</button></div><script>"
        "async function t(){try{const s=await fetch('/api/mcserver/console').then(r=>r.json());"
        "const w=document.getElementById('wrap');"
        "const stick=w.scrollTop+w.clientHeight>=w.scrollHeight-40;"
        "document.getElementById('c').textContent=(s.console||[]).join('\\n')||'(no output yet)';"
        "document.getElementById('st').textContent=(s.running?'RUNNING':'stopped')+"
        "' ['+(s.phase||'?')+']';"
        "if(stick)w.scrollTop=w.scrollHeight;}catch(e){}setTimeout(t,1200);}t();"
        "async function send(){const i=document.getElementById('cmd');const v=i.value.trim();"
        "if(!v)return;i.value='';"
        "await fetch('/api/mcserver/cmd',{method:'POST',"
        "headers:{'Content-Type':'application/json'},body:JSON.stringify({command:v})});}"
        "document.getElementById('cmd').addEventListener('keydown',"
        "e=>{if(e.key==='Enter')send();});"
        "</script></body></html>"
    )


@app.route("/api/mcserver/opts", methods=["POST"])
def api_mcs_opts():
    """Server card knobs (auto_restart, RAM, CPU%). Persisted into the project's server
    profile; RAM/CPU apply on the next Start (the JVM can't resize a live heap)."""
    d = request.get_json(silent=True) or {}
    m = _machine_specs()
    out = {}
    if "auto_restart" in d:
        v = d.get("auto_restart") is True
        _mcs_set(auto_restart=v)
        PROJECT.update_settings({"server_auto_restart": v})
        out["auto_restart"] = v
    if "backup_first" in d:
        v = d.get("backup_first") is True
        PROJECT.update_settings({"server_backup_first": v})
        out["backup_first"] = v
    # voxy/extras persist the moment the checkbox flips (not only at Plan), so the
    # choice survives reloads even when added later
    if "voxy" in d:
        v = d.get("voxy") is True
        PROJECT.update_settings({"server_voxy": v})
        out["voxy"] = v
    if "extras" in d:
        v = d.get("extras") is True
        PROJECT.update_settings({"server_extras": v})
        out["extras"] = v
    if "ram_gb" in d:
        try:
            gb = int(d.get("ram_gb") or 0)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "ram_gb must be a number (0 = auto)"}), 400
        gb = 0 if gb <= 0 else max(1, min(max(1, m["ram_gb"] - 2), gb))
        PROJECT.update_settings({"server_ram_gb": gb})
        out["ram_gb"] = gb
    if "cpu_pct" in d:
        try:
            pct = int(d.get("cpu_pct") or 100)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "cpu_pct must be a number"}), 400
        pct = max(10, min(100, pct))
        PROJECT.update_settings({"server_cpu_pct": pct})
        out["cpu_pct"] = pct
    heap, cpu_n = _mcs_resources()
    return jsonify({"ok": True, **out, "effective": {"heap": heap, "cpu_count": cpu_n,
                    "machine": m}})


if __name__ == "__main__":
    # Restart-safe continuation: KEEP the plan so a run interrupted by a restart / PC close can be
    # resumed (the whole point — losing it forced a full re-plan). A run that was mid-flight leaves
    # cells "queued"/"running" but the worker pool is gone, so just RESET those back to "planned" (a
    # clean, re-runnable state — no stale "running" cell that no worker owns). "merged" (real generated
    # content), "planned" and "failed" are left as-is. Generation is NOT auto-started on boot, so the
    # cells simply reappear on the map; hit Generate / Resume unfinished to continue. Runs only on an
    # actual server start, never on `import server`.
    try:
        _g = PROJECT.load_grid()
        _fixed = {k: ("planned" if v in ("queued", "running") else v) for k, v in _g.items()}
        if _fixed != _g:
            PROJECT.save_grid(_fixed)
            _n = sum(1 for v in _g.values() if v in ("queued", "running"))
            print(f"restart: kept the {len(_g)}-cell plan; reset {_n} interrupted cell(s) to 'planned' "
                  f"— hit Generate / Resume unfinished to continue")
        elif _g:
            print(f"restart: kept the {len(_g)}-cell plan from the last session")
    except Exception:
        pass
    _load_cell_health()   # restore suspect-cell flags so "Redo suspect" survives a restart
    port = int(os.environ.get("PORT", 5630))
    print(f"light-meld -> http://127.0.0.1:{port}")
    _exe = resolve_arnis_exe()
    if _exe:
        print(f"arnis binary: {_exe}")
    else:
        _want = "arnis.exe" if sys.platform == "win32" else "arnis"
        print(f"arnis binary: NOT FOUND — put '{_want}' next to server.py. On Linux/macOS the "
              f"file must be named 'arnis' (no .exe) and be the matching OS build.")
    app.run(host="127.0.0.1", port=port, threaded=True)
