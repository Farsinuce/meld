#!/usr/bin/env python3
"""bench/bench_scheduler.py — legacy scheduler vs governor, side by side, same area.

WHAT THIS PROVES
    1. SPEED     - wall time, cells/min, effective parallelism, CPU/RAM, median + p95 cell time
                   for a legacy arm (fixed `max_workers`, `governor_mode="off"`) and a governor
                   arm (`governor_mode="auto"` with a ceiling), on the SAME bbox / scale / seed.
    2. SAMENESS  - the per-cell arnis block_hash VECTOR must be byte-identical across every
                   config in the sweep. Scheduling must not change one block of output. A
                   mismatch aborts the sweep with a loud report.

HOW IT DRIVES MELD
    Only over Meld's own HTTP API (default http://127.0.0.1:5630). Either it starts its own
    server per sweep (isolation, and it can set ARNIS_BLOCK_HASH=1 in the server env so every
    child arnis emits its content hash), or it attaches to one you already have running
    (--attach). Each run gets a FRESH project => fresh grid, fresh logs, fresh world folder;
    world-shaping settings are pinned identically and only the scheduling knobs move.

    Timings are NOT re-derived here: at the end of a run Meld writes meld-report.json
    (schema meld-run-report/3, cells[] + summary{}) and serves it at /api/report.json. That
    file is the source of truth for every number in the table.

USAGE
    python bench/bench_scheduler.py --dry-run                  # validate + print the plan
    python bench/bench_scheduler.py --only smoke               # one fast config
    python bench/bench_scheduler.py                            # the whole default matrix
    python bench/bench_scheduler.py --attach http://127.0.0.1:5630 --only smoke

    See bench/README.md for the full command list and how to read the table.

Stdlib only (uses `requests` when it happens to be installed, urllib otherwise; psutil only
for the swap/abort watchdog, and it degrades gracefully when missing).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

# A Windows console defaults to the machine's ANSI code page (cp1250 here), and the table
# below prints a delta sign. Force UTF-8 with replacement so formatting can never take the
# harness down mid-sweep — same guard server.py installs.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parent
RESULTS_DIR = BENCH_DIR / "results"
DEFAULT_MATRIX = BENCH_DIR / "matrix.json"
DEFAULT_PORT = 5630

# ── what may and may not differ between arms ──────────────────────────────────
# Scheduling knobs: free to differ, that is the whole experiment.
SCHED_KEYS = {
    "max_workers", "governor_mode", "governor_max_workers", "cpu_target_pct",
    "flush_threads_cap", "ram_headroom_mb", "min_threads_per_worker",
    "worker_autoscale", "cpu_stagger_enabled", "cpu_stagger_seconds",
    "arnis_log_verbose", "stream_to_disk",
}
# World-shaping settings: MUST be identical across the sweep or the determinism gate is
# comparing two different worlds and its verdict means nothing. A run that tries to override
# one of these is refused unless --allow-world-override is passed.
WORLD_KEYS = {
    "scale", "job_size_regions", "seed", "buildings", "terrain", "interior", "roof",
    "overture", "trees", "caves", "land_cover", "fill_ground", "bake_lighting",
    "elevation_zoom", "elevation_mode", "seam_buffer_chunks", "vertical_exaggeration",
    "ground_level", "world_min_y", "world_max_y", "height_headroom", "height_underroom",
    "disable_height_limit", "mc_version", "region_format", "field_mix", "field_scale",
    "scatter_mode", "signage", "rocks", "bushes", "grass_texture", "cave_biome_amounts",
    "tree_size_weights", "gpu_accel",
}

# Rough cells/min used ONLY for the dry-run wall-time estimate. Measured on the reference
# machine (24 logical cores, 31.4 GB, NVMe) from real meld-report.json files; see the
# "Measured ground truth" table in bench/README.md. Keyed by f"{scale_bucket}/{cell_size}".
THROUGHPUT_MODEL = {
    "1:1/1": 30.0,      # smoke only, NOT measured: a 1-region cell is 1/16 the ground of a
                        #   cs=4 one, but per-cell fixed cost (spawn, fetch, merge) dominates,
                        #   so it is nowhere near 16x the throughput. A guess, and labelled one.
    "1:1/4": 21.0,      # Berlin + Bucuresti: flat ~20-23 cells/min from 8..24 workers
    "1:1/8": 6.3,       # 100 cells in 948 s at 15 workers
    "1:2..1:9/4": 14.0,  # not re-measured; interpolated
    "1:10+/4": 8.0,     # 1:20 documented at ~1.02 cores/cell, ~1.2 GB/cell: cheap in cores,
    "1:10+/8": 3.0,     #   slow per cell (a cell covers 100x the ground), knee ~16-20 workers
}
DEFAULT_THROUGHPUT = 20.0

BLOCK_HASH_RE = re.compile(r"\[BENCHMARK\]\s+block_hash=([0-9a-fA-F]+)")


def scale_bucket(scale: float) -> str:
    """The governor's own bucket naming, mirrored so the bench keys line up with its history."""
    s = float(scale)
    if s >= 0.5:
        return "1:1"
    if s >= 0.1:
        return "1:2..1:9"
    return "1:10+"


class BenchError(RuntimeError):
    """Configuration or plumbing failure — the sweep cannot continue."""


class SweepAbort(RuntimeError):
    """A watchdog / RAM / determinism condition ended the sweep early."""


# ── tiny HTTP client (requests if present, urllib otherwise) ──────────────────

class Http:
    def __init__(self, base: str, timeout: float = 60.0) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout
        try:
            import requests  # noqa: PLC0415
            self._rq = requests
        except Exception:  # noqa: BLE001
            self._rq = None

    def _url(self, path: str) -> str:
        return self.base + (path if path.startswith("/") else "/" + path)

    def get(self, path: str, timeout: float | None = None) -> tuple[int, dict]:
        return self._call("GET", path, None, timeout)

    def post(self, path: str, payload: dict | None = None,
             timeout: float | None = None) -> tuple[int, dict]:
        return self._call("POST", path, payload if payload is not None else {}, timeout)

    def _call(self, method: str, path: str, payload, timeout) -> tuple[int, dict]:
        url = self._url(path)
        to = self.timeout if timeout is None else timeout
        if self._rq is not None:
            r = (self._rq.get(url, timeout=to) if method == "GET"
                 else self._rq.post(url, json=payload, timeout=to))
            try:
                return r.status_code, (r.json() if r.content else {})
            except Exception:  # noqa: BLE001 — a non-JSON body (404 text) is still a result
                return r.status_code, {"raw": (r.text or "")[:500]}
        import urllib.error
        import urllib.request
        data = None
        headers = {}
        if method == "POST":
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:  # noqa: S310 — localhost only
                body = resp.read().decode("utf-8", "replace")
                code = resp.status
        except urllib.error.HTTPError as ex:
            body = ex.read().decode("utf-8", "replace")
            code = ex.code
        try:
            return code, (json.loads(body) if body.strip() else {})
        except Exception:  # noqa: BLE001
            return code, {"raw": body[:500]}

    def alive(self) -> bool:
        try:
            code, _ = self.get("/api/mini", timeout=3.0)
            return code == 200
        except Exception:  # noqa: BLE001
            return False


def must_ok(code: int, body: dict, what: str) -> dict:
    if code != 200 or (isinstance(body, dict) and body.get("ok") is False):
        raise BenchError(f"{what} failed (HTTP {code}): "
                         f"{(body or {}).get('error') or (body or {}).get('raw') or body}")
    return body


# ── run specs ─────────────────────────────────────────────────────────────────

@dataclass
class RunSpec:
    name: str
    arm: str                     # "legacy" | "governor"
    workers: int
    governor_mode: str           # "off" | "advise" | "auto"
    cpu_target_pct: int
    flush_threads_cap: int
    cell_size: int
    scale: float
    bbox: dict
    repeats: int = 1
    governor_max_workers: int = 0
    ram_headroom_mb: int = 2048
    group: str = ""
    baseline: bool = False
    settings: dict = field(default_factory=dict)   # extra SCHEDULING-only overrides

    @property
    def bucket(self) -> str:
        return f"{scale_bucket(self.scale)}/{self.cell_size}"

    def sched_settings(self) -> dict:
        s = {
            "max_workers": int(self.workers),
            "governor_mode": self.governor_mode,
            "governor_max_workers": int(self.governor_max_workers),
            "cpu_target_pct": int(self.cpu_target_pct),
            "flush_threads_cap": int(self.flush_threads_cap),
            "ram_headroom_mb": int(self.ram_headroom_mb),
        }
        s.update(self.settings or {})
        return s


def load_matrix(path: Path) -> dict:
    if not path.exists():
        raise BenchError(f"matrix not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as ex:
        raise BenchError(f"{path}: invalid JSON — {ex}") from ex


def _bbox_ok(b) -> bool:
    return (isinstance(b, dict)
            and all(k in b for k in ("south", "west", "north", "east"))
            and float(b["north"]) > float(b["south"])
            and float(b["east"]) > float(b["west"]))


def build_specs(matrix: dict, only: list[str] | None, repeats_override: int | None,
                allow_world_override: bool) -> list[RunSpec]:
    site = matrix.get("site") or {}
    site_bbox = site.get("bbox")
    runs = matrix.get("runs") or []
    if not runs:
        raise BenchError("matrix has no `runs`")
    world_settings = matrix.get("world_settings") or {}
    bad = sorted(set(world_settings) - WORLD_KEYS)
    if bad:
        print(f"  note: world_settings carries non-world keys {bad} — they are applied to every "
              f"run identically, which is fine, but they belong in a run's `settings` if they "
              f"are meant to differ.")

    specs: list[RunSpec] = []
    seen: set[str] = set()
    seen_groups: set[str] = set()   # every group in the FILE, so a typo can be answered usefully
    for i, r in enumerate(runs):
        name = str(r.get("name") or f"run{i}").strip()
        if not name:
            raise BenchError(f"runs[{i}] has no name")
        if name in seen:
            raise BenchError(f"duplicate run name {name!r}")
        seen.add(name)
        # --only takes a run name OR a group name: "--only smoke" is the whole smoke pair,
        # which is what you want, because a lone run in a group cannot be compared to
        # anything and the determinism gate would skip it.
        run_group = str(r.get("group") or "")
        if run_group:
            seen_groups.add(run_group)
        if only and name not in only and run_group not in only:
            continue
        bbox = r.get("bbox") or site_bbox
        if not _bbox_ok(bbox):
            raise BenchError(f"{name}: bbox missing or malformed (need south/west/north/east)")
        extra = dict(r.get("settings") or {})
        clash = sorted(set(extra) & WORLD_KEYS)
        if clash and not allow_world_override:
            raise BenchError(
                f"{name}: settings override world-shaping keys {clash}. Two arms with different "
                f"world settings are two different worlds and the determinism gate would be "
                f"meaningless. Move them to `world_settings`, or pass --allow-world-override "
                f"if you really mean to compare different worlds.")
        unknown = sorted(set(extra) - SCHED_KEYS - WORLD_KEYS)
        if unknown:
            print(f"  note: {name}: passing through unrecognised settings keys {unknown}")
        arm = str(r.get("arm") or ("governor" if (r.get("governor_mode") or "off") != "off"
                                   else "legacy"))
        spec = RunSpec(
            name=name,
            arm=arm,
            workers=int(r.get("workers", 8)),
            governor_mode=str(r.get("governor_mode", "off")),
            cpu_target_pct=int(r.get("cpu_target_pct", 90)),
            flush_threads_cap=int(r.get("flush_threads_cap", 12)),
            cell_size=int(r.get("cell_size", 4)),
            scale=float(r.get("scale", 1.0)),
            bbox=dict(bbox),
            repeats=int(repeats_override if repeats_override else r.get("repeats", 1)),
            governor_max_workers=int(r.get("governor_max_workers", 0)),
            ram_headroom_mb=int(r.get("ram_headroom_mb", 2048)),
            group=str(r.get("group") or f"{scale_bucket(float(r.get('scale', 1.0)))}"
                                        f"/cs{int(r.get('cell_size', 4))}"),
            baseline=bool(r.get("baseline", False)),
            settings=extra,
        )
        if spec.workers < 1 or spec.workers > 64:
            raise BenchError(f"{name}: workers {spec.workers} outside 1..64")
        if spec.governor_mode not in ("off", "advise", "auto"):
            raise BenchError(f"{name}: governor_mode {spec.governor_mode!r} not off|advise|auto")
        if not 10 <= spec.cpu_target_pct <= 95:
            raise BenchError(f"{name}: cpu_target_pct {spec.cpu_target_pct} outside 10..95")
        if not 1 <= spec.flush_threads_cap <= 24:
            raise BenchError(f"{name}: flush_threads_cap {spec.flush_threads_cap} outside 1..24")
        if not 512 <= spec.ram_headroom_mb <= 8192:
            raise BenchError(f"{name}: ram_headroom_mb {spec.ram_headroom_mb} outside 512..8192")
        if not 0 <= spec.governor_max_workers <= 64:
            raise BenchError(f"{name}: governor_max_workers outside 0..64")
        if spec.repeats < 1:
            raise BenchError(f"{name}: repeats must be >= 1")
        specs.append(spec)

    if only:
        missing = sorted(set(only) - seen - seen_groups)
        if missing:
            known = sorted(seen | seen_groups)
            raise BenchError(f"--only named unknown run/group(s): {missing}. "
                             f"This matrix offers: {known}")
    if not specs:
        raise BenchError("no runs selected")
    # Every run in a group must share bbox/scale/cell_size, or the group's delta column is a lie.
    for g in {s.group for s in specs}:
        gs = [s for s in specs if s.group == g]
        keys = {(s.scale, s.cell_size, json.dumps(s.bbox, sort_keys=True)) for s in gs}
        if len(keys) > 1:
            raise BenchError(f"group {g!r} mixes different bbox/scale/cell_size — "
                             f"give the odd one out its own `group`")
    return specs


def estimate_cells(spec: RunSpec, origin: dict) -> int | None:
    """Cell count for the dry-run plan, using Meld's own grid math when importable."""
    try:
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        from src.coords import snap_to_region_grid  # noqa: PLC0415
        from src.grid import count_cells_for_bbox   # noqa: PLC0415
        lat, lon = snap_to_region_grid(float(origin["lat"]), float(origin["lon"]), spec.scale)
        return int(count_cells_for_bbox(spec.bbox, {"lat": lat, "lon": lon},
                                        spec.scale, spec.cell_size))
    except Exception:  # noqa: BLE001 — estimate only; the server is the real authority
        return None


def estimate_minutes(spec: RunSpec, cells: int | None) -> float | None:
    """Wall-time estimate for the dry-run plan. Throughput is flat at and above the knee
    (measured: 8-12 workers at 1:1) and falls off linearly below it, so a 4-worker arm is
    predicted slower than a 12-worker one even though both sit on the same model number."""
    if not cells:
        return None
    per_min = THROUGHPUT_MODEL.get(spec.bucket, DEFAULT_THROUGHPUT)
    knee = 8.0 if scale_bucket(spec.scale) == "1:1" else 16.0
    ceiling = spec.governor_max_workers or spec.workers
    workers = ceiling if spec.governor_mode == "auto" else spec.workers
    per_min *= min(1.0, workers / knee)
    return cells / per_min * spec.repeats


# ── server lifecycle ──────────────────────────────────────────────────────────

class Server:
    """Meld's own server, started per sweep for isolation (or attached to)."""

    def __init__(self, *, port: int, python: str | None, data_dir: str | None,
                 cache_dir: str | None, block_hash: bool, attach_url: str | None,
                 log_path: Path) -> None:
        self.port = port
        self.python = python or sys.executable
        self.data_dir = Path(data_dir).resolve() if data_dir else REPO_ROOT
        self.cache_dir = cache_dir
        self.block_hash = block_hash
        self.attach_url = attach_url
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None
        self._log_fh = None
        self.url = attach_url or f"http://127.0.0.1:{port}"
        self.http = Http(self.url)

    # -- start/stop ---------------------------------------------------------
    def start(self) -> None:
        if self.attach_url:
            if not self.http.alive():
                raise BenchError(f"nothing answering at {self.attach_url} — start Meld first, "
                                 f"or drop --attach and let the bench start its own server")
            print(f"  attached to {self.url}")
            if self.block_hash:
                print("  NOTE: attached mode cannot set ARNIS_BLOCK_HASH in the server's "
                      "environment. Unless Meld was started with it, the determinism gate "
                      "falls back to region-file hashing (see README).")
            return
        if self.http.alive():
            raise BenchError(f"something is already serving {self.url}. Close Meld, pick another "
                             f"--port, or use --attach.")
        lock = self.data_dir / "meld.lock"
        env = dict(os.environ)
        env["PORT"] = str(self.port)
        env["MELD_DATA_DIR"] = str(self.data_dir)
        env["MELD_CACHE_DIR"] = str(Path(self.cache_dir).resolve() if self.cache_dir
                                    else REPO_ROOT / "cache")
        if self.block_hash:
            env["ARNIS_BLOCK_HASH"] = "1"   # every child arnis prints its content hash
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.pop("ARNIS_OFFLINE", None)      # the harness never forces offline mode
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_fh = open(self.log_path, "w", encoding="utf-8", errors="replace")
        creation = 0
        new_session = False
        if sys.platform == "win32":
            creation = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            # The server MUST lead its own process group, because kill()/stop() reach its
            # arnis children with os.killpg(os.getpgid(pid), ...). Without this the child
            # inherits OUR group and an abort would signal the harness itself.
            new_session = True
        print(f"  starting Meld: {self.python} server.py  (port {self.port}, "
              f"data {self.data_dir}, log {self.log_path.name})")
        self.proc = subprocess.Popen(
            [self.python, "server.py"], cwd=str(REPO_ROOT), env=env,
            stdout=self._log_fh, stderr=subprocess.STDOUT,
            creationflags=creation, start_new_session=new_session,
        )
        deadline = time.time() + 120
        while time.time() < deadline:
            if self.proc.poll() is not None:
                tail = self._log_tail()
                extra = ""
                if lock.exists() and "already running" in tail:
                    extra = ("  Another Meld holds the single-instance lock. Close it, or run "
                             "the bench with --data-dir <scratch> (the cache stays shared).")
                raise BenchError(f"server exited during startup (rc={self.proc.returncode}).\n"
                                 f"{tail}\n{extra}")
            if self.http.alive():
                print(f"  server up at {self.url}")
                return
            time.sleep(0.5)
        self.stop()
        raise BenchError(f"server did not answer within 120 s — see {self.log_path}")

    def _log_tail(self, n: int = 20) -> str:
        try:
            if self._log_fh:
                self._log_fh.flush()
            lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            return "  " + "\n  ".join(lines[-n:])
        except Exception:  # noqa: BLE001
            return ""

    def kill(self, why: str = "") -> None:
        """Kill the server PROCESS (and its arnis children). Never /api/stop — an abort must
        not depend on a server that may be the thing that is wedged."""
        if why:
            print(f"  killing the server: {why}")
        if self.proc is not None and self.proc.poll() is None:
            pid = self.proc.pid
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True, check=False)
            else:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except Exception:  # noqa: BLE001
                    self.proc.kill()
            try:
                self.proc.wait(timeout=30)
            except Exception:  # noqa: BLE001
                pass
        elif self.attach_url:
            self._kill_listener()
        self._close_log()

    def _kill_listener(self) -> None:
        """Attached mode: find whoever is listening on the port and kill it (best effort)."""
        try:
            import psutil  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            print("  cannot kill an attached server without psutil — stop Meld yourself.")
            return
        try:
            for c in psutil.net_connections(kind="inet"):
                if c.laddr and c.laddr.port == self.port and c.status == psutil.CONN_LISTEN and c.pid:
                    p = psutil.Process(c.pid)
                    for ch in p.children(recursive=True):
                        ch.kill()
                    p.kill()
                    print(f"  killed attached server pid {c.pid}")
                    return
        except Exception as ex:  # noqa: BLE001
            print(f"  could not kill the attached server: {ex}")

    def stop(self) -> None:
        if self.attach_url:
            return
        if self.proc is not None and self.proc.poll() is None:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                               capture_output=True, check=False)
            else:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
                except Exception:  # noqa: BLE001
                    self.proc.terminate()
            try:
                self.proc.wait(timeout=60)
            except Exception:  # noqa: BLE001
                self.kill("shutdown timed out")
        self._close_log()

    def _close_log(self) -> None:
        try:
            if self._log_fh:
                self._log_fh.flush()
                self._log_fh.close()
        except Exception:  # noqa: BLE001
            pass
        self._log_fh = None

    def restart(self) -> None:
        """Fresh process between runs: no carried-over governor history, no warm pool."""
        if self.attach_url:
            return
        self.stop()
        time.sleep(1.5)
        self.proc = None
        self.start()


# ── the bench itself ──────────────────────────────────────────────────────────

class Bench:
    def __init__(self, args, matrix: dict, specs: list[RunSpec]) -> None:
        self.args = args
        self.matrix = matrix
        self.specs = specs
        self.site = matrix.get("site") or {}
        self.prep_cfg = matrix.get("prep") or {}
        self.wd = matrix.get("watchdog") or {}
        self.world_settings = dict(matrix.get("world_settings") or {})
        self.seed = int(matrix.get("seed", 1))
        self.label = args.label or matrix.get("label") or "sweep"
        self.results: list[dict] = []
        self.elev_cache: dict[str, dict] = {}
        self.server: Server | None = None
        self.hash_mode = args.hash_mode
        self.data_dir = Path(args.data_dir).resolve() if args.data_dir else REPO_ROOT

    # -- plumbing -----------------------------------------------------------
    @property
    def http(self) -> Http:
        assert self.server is not None
        return self.server.http

    def origin(self) -> dict:
        o = self.site.get("origin")
        if isinstance(o, dict) and o.get("lat") is not None:
            return {"lat": float(o["lat"]), "lon": float(o["lon"])}
        bbox = self.site.get("bbox") or self.specs[0].bbox
        return {"lat": float(bbox["south"]), "lon": float(bbox["west"])}

    def project_root(self, slug: str) -> Path:
        return self.data_dir / "projects" / slug

    # -- prep ---------------------------------------------------------------
    def prep(self) -> None:
        """Warm the caches so the sweep measures compute, not the network."""
        if not self.prep_cfg.get("enabled", True):
            print("  prep disabled in the matrix — runs will include cold-cache fetches")
            return
        print("\n== prep: warming caches ==")
        slug = self.new_project("bench-prep")
        for spec in self._distinct_areas():
            self.apply_settings(spec, prep=True)
            self.set_origin()
            self.http.post("/api/selection", {"selection": {"bbox": spec.bbox}})
            key = json.dumps(spec.bbox, sort_keys=True)
            if self.prep_cfg.get("survey", True) and key not in self.elev_cache:
                self.elev_cache[key] = self.survey(spec.bbox)
            if self.prep_cfg.get("bake_elevation", True):
                self.wait_datapack(self.http.post("/api/datapack/bake-mapterhorn", {}),
                                   "elevation tiles")
            if self.prep_cfg.get("prewarm_overture", True) and self.world_settings.get("overture"):
                self.wait_overture()
        print(f"  prep project: {slug} (kept; it holds no world)")

    def _distinct_areas(self) -> list[RunSpec]:
        out, seen = [], set()
        for s in self.specs:
            k = (json.dumps(s.bbox, sort_keys=True), s.scale)
            if k not in seen:
                seen.add(k)
                out.append(s)
        return out

    def survey(self, bbox: dict) -> dict:
        """One elevation survey per area; every run then gets the SAME manual lock, so the
        elevation range (a world-shaping input) cannot drift between arms."""
        print("  survey: elevation range over the selection…")
        code, body = self.http.post("/api/survey",
                                    {"bbox": bbox, "zoom": int(self.prep_cfg.get("survey_zoom", 10))},
                                    timeout=1800)
        if code != 200 or not body.get("ok"):
            fallback = self.prep_cfg.get("elevation") or {"min_m": 0, "max_m": 200}
            print(f"    survey failed ({body.get('reason') or body.get('error')}) — "
                  f"falling back to the matrix's fixed range {fallback}")
            return {"min_m": fallback["min_m"], "max_m": fallback["max_m"]}
        print(f"    range {body['min_m']}..{body['max_m']} m from {body.get('tiles')} tile(s)")
        return {"min_m": body["min_m"], "max_m": body["max_m"]}

    def wait_datapack(self, res, what: str) -> None:
        code, body = res
        if code != 200 or not body.get("ok"):
            print(f"    {what}: skipped — {body.get('error')}")
            return
        print(f"    {what}: warming {body.get('areas')} area(s)…")
        while True:
            time.sleep(3)
            _, st = self.http.get("/api/datapack/status")
            if not st.get("active"):
                print(f"    {what}: {st.get('note')}")
                return

    def wait_overture(self) -> None:
        code, body = self.http.post("/api/overture/prewarm", {})
        if code != 200 or not body.get("ok"):
            print(f"    Overture prewarm: skipped — {body.get('error')}")
            return
        print("    Overture prewarm: running…")
        while True:
            time.sleep(3)
            _, st = self.http.get("/api/overture/status")
            if not st.get("active"):
                print(f"    Overture prewarm: {st.get('note')}")
                return

    # -- per-run plumbing ---------------------------------------------------
    def new_project(self, name: str) -> str:
        code, body = self.http.post("/api/projects/new",
                                    {"name": name, "inherit_save_location": False})
        must_ok(code, body, f"create project {name}")
        return body["slug"]

    def set_origin(self) -> None:
        o = self.origin()
        code, body = self.http.post("/api/origin", {**o, "force": True})
        must_ok(code, body, "set origin")

    def apply_settings(self, spec: RunSpec, prep: bool = False) -> dict:
        patch = dict(self.world_settings)
        patch["scale"] = spec.scale
        patch["job_size_regions"] = spec.cell_size
        patch["seed"] = self.seed
        if not prep:
            patch.update(spec.sched_settings())
        code, body = self.http.post("/api/settings", patch)
        must_ok(code, body, "apply settings")
        _, live = self.http.get("/api/settings")
        missing = {k: v for k, v in patch.items()
                   if k != "seed" and k in live and live.get(k) != v}
        absent = [k for k in patch if k not in live and k != "seed"]
        if absent:
            print(f"    warning: this server does not know settings {absent} — the governor "
                  f"keys may not be merged yet; the run still executes with the legacy path")
        if missing:
            print(f"    note: server clamped/normalised {missing}")
        return live

    def lock_elevation(self, spec: RunSpec) -> None:
        key = json.dumps(spec.bbox, sort_keys=True)
        ev = self.elev_cache.get(key) or self.prep_cfg.get("elevation") or {"min_m": 0, "max_m": 200}
        code, body = self.http.post("/api/elevation/manual",
                                    {"min_m": ev["min_m"], "max_m": ev["max_m"], "seed": self.seed})
        must_ok(code, body, "lock elevation")

    def plan_grid(self, spec: RunSpec) -> int:
        code, body = self.http.post("/api/grid",
                                    {"bbox": spec.bbox, "size": spec.cell_size, "mode": "add"},
                                    timeout=300)
        must_ok(code, body, "plan grid")
        return int(body.get("count") or 0)

    # -- one run ------------------------------------------------------------
    def run_one(self, spec: RunSpec, rep: int) -> dict:
        tag = f"{spec.name}#{rep + 1}" if spec.repeats > 1 else spec.name
        print(f"\n== run {tag} ({spec.arm}: workers={spec.workers} "
              f"governor={spec.governor_mode} cpu={spec.cpu_target_pct}% "
              f"flush<={spec.flush_threads_cap} scale={spec.scale} cs={spec.cell_size}) ==")
        if self.args.restart_between and self.server is not None:
            self.server.restart()
        slug = self.new_project(f"bench {self.label} {tag}")
        # SETTINGS BEFORE ORIGIN, always. /api/origin snaps the coordinate onto the region
        # grid *at the project's current scale* (src/coords.snap_to_region_grid), and a fresh
        # project starts at the DEFAULTS scale (0.1). Setting the origin first would snap every
        # run — the 1:1 arms included — on the 1:10 grid, whose quantum is 5120 blocks: the
        # rendered area would sit up to ~2.5 km away from the bbox that was asked for.
        self.apply_settings(spec)
        self.set_origin()
        self.lock_elevation(spec)
        self.http.post("/api/selection", {"selection": {"bbox": spec.bbox}})
        n_cells = self.plan_grid(spec)
        print(f"  project {slug}: {n_cells} cell(s) planned")
        if n_cells == 0:
            raise BenchError(f"{tag}: the bbox planned 0 cells — widen it or shrink cell_size")

        t0 = time.time()
        code, body = self.http.post("/api/queue", {"force": True}, timeout=600)
        must_ok(code, body, "queue")
        outcome = self.poll_run(tag, n_cells, t0)
        wall_s = time.time() - t0

        report = self.fetch_report()
        rec = {
            "run": spec.name, "tag": tag, "rep": rep + 1, "arm": spec.arm, "group": spec.group,
            "slug": slug, "bucket": spec.bucket, "spec": asdict(spec),
            "cells_planned": n_cells, "harness_wall_s": round(wall_s, 1),
            "outcome": outcome,
            "metrics": metrics_from_report(report, wall_s),
            "governor": self.governor_snapshot(),
            "report_summary": (report or {}).get("summary") or {},
        }
        rec["hashes"], rec["hash_source"] = self.collect_hashes(slug)
        self.save_report_copy(report, tag)
        m = rec["metrics"]
        print(f"  -> {fmt(m.get('wall_s'))}s wall · {fmt(m.get('cells_per_min'))} cells/min · "
              f"par {fmt(m.get('effective_parallelism'))}x · cpu {fmt(m.get('cpu_avg'))}% · "
              f"ram {fmt(m.get('ram_peak'))}% · median {fmt(m.get('cell_median_s'))}s · "
              f"p95 {fmt(m.get('cell_p95_s'))}s · peak {m.get('workers_peak')}w · "
              f"{len(rec['hashes'])} hash(es) via {rec['hash_source']}")
        return rec

    def poll_run(self, tag: str, n_cells: int, t0: float) -> str:
        """Poll /api/mini until the run ends, enforcing the abort criteria."""
        run_timeout = float(self.wd.get("run_timeout_s", 7200))
        stall_timeout = float(self.wd.get("stall_timeout_s", 1800))
        ram_abort = float(self.wd.get("ram_abort_pct", 95))
        swap_growth_mb = float(self.wd.get("swap_growth_mb", 4096))
        swap0 = swap_used_mb()
        started = False
        last_finished, last_change = -1, time.time()
        ram_hot = 0
        last_print = 0.0
        while True:
            time.sleep(2.0)
            try:
                _, mini = self.http.get("/api/mini", timeout=30)
            except Exception as ex:  # noqa: BLE001 — a dead server is an abort, not a crash
                raise SweepAbort(f"{tag}: the server stopped answering ({ex})") from ex
            total = int(mini.get("total") or 0)
            done = int(mini.get("done") or 0)
            failed = int(mini.get("failed") or 0)
            active = bool(mini.get("active"))
            if active:
                started = True
            elif started:
                return "ok" if failed == 0 else f"finished with {failed} failed cell(s)"
            elapsed = time.time() - t0
            if not started and elapsed > 300:
                raise SweepAbort(f"{tag}: the run never started (5 min); "
                                 f"last task: {(mini.get('task') or {}).get('title')}")
            fin = done + failed
            if fin != last_finished:
                last_finished, last_change = fin, time.time()
            stats = mini.get("stats") or {}
            ram = stats.get("ram_pct")
            ram_hot = ram_hot + 1 if (ram is not None and float(ram) > ram_abort) else 0
            if ram_hot >= 3:
                raise SweepAbort(f"{tag}: RAM above {ram_abort}% for three samples "
                                 f"(last {ram}%) — the machine is about to swap")
            swap_now = swap_used_mb()
            if swap0 is not None and swap_now is not None and (swap_now - swap0) > swap_growth_mb:
                raise SweepAbort(f"{tag}: swap grew {swap_now - swap0:.0f} MB "
                                 f"(> {swap_growth_mb:.0f} MB) — the timings are worthless")
            if elapsed > run_timeout:
                raise SweepAbort(f"{tag}: watchdog — {elapsed / 60:.0f} min "
                                 f"(> {run_timeout / 60:.0f} min budget)")
            if started and (time.time() - last_change) > stall_timeout:
                raise SweepAbort(f"{tag}: stalled — no cell finished for "
                                 f"{stall_timeout / 60:.0f} min at {fin}/{total}")
            if time.time() - last_print > 30:
                last_print = time.time()
                gov = mini.get("gov") or {}
                gtxt = (f" gov={gov.get('state')} {gov.get('w')}->{gov.get('target')}w"
                        if gov else "")
                print(f"    {fin}/{total or n_cells} cells · {int(elapsed)}s · "
                      f"{mini.get('workers_busy')}/{mini.get('workers_max')} busy · "
                      f"cpu {stats.get('cpu_pct')}% ram {ram}%{gtxt}")

    def governor_snapshot(self) -> dict:
        try:
            code, body = self.http.get("/api/governor", timeout=15)
            if code == 200 and isinstance(body, dict):
                body.pop("history", None)      # the whole history is noise in a per-run record
                return body
        except Exception:  # noqa: BLE001
            pass
        return {}

    def fetch_report(self) -> dict:
        """meld-report.json is the source of truth for timings — never re-derive them here."""
        for _ in range(20):
            code, body = self.http.get("/api/report.json", timeout=120)
            if code == 200 and isinstance(body, dict) and body.get("summary"):
                if body.get("schema") != "meld-run-report/3":
                    print(f"    note: report schema is {body.get('schema')!r}, "
                          f"this harness was written for meld-run-report/3")
                return body
            time.sleep(1.5)
        raise BenchError("the run finished but /api/report.json never produced a report")

    def save_report_copy(self, report: dict, tag: str) -> None:
        try:
            d = RESULTS_DIR / self.label / "reports"
            d.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", tag)
            (d / f"{safe}.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
        except Exception as ex:  # noqa: BLE001
            print(f"    note: could not copy the report ({ex})")

    # -- determinism --------------------------------------------------------
    def collect_hashes(self, slug: str) -> tuple[dict, str]:
        """The per-cell block_hash vector, or the region-file fallback.

        arnis prints `[BENCHMARK] block_hash=<hex>` when ARNIS_BLOCK_HASH is set in its
        environment, and Meld tees every arnis line into <project>/logs/cell-<key>.log.
        That is the strong gate: the hash covers the block content of every region the cell
        wrote. If it is not there (attached to a server started without the env var), we hash
        the region FILES instead — see the caveat in bench/README.md.
        """
        if self.hash_mode == "off":
            return {}, "off"
        if self.hash_mode in ("auto", "block"):
            hashes = {}
            logs = self.project_root(slug) / "logs"
            for f in sorted(logs.glob("cell-*.log")) if logs.is_dir() else []:
                try:
                    txt = f.read_text(encoding="utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    continue
                found = BLOCK_HASH_RE.findall(txt)
                if found:
                    cell = f.stem[len("cell-"):].replace("_", ",")
                    hashes[cell] = found[-1].lower()   # last = the attempt that succeeded
            if hashes:
                return hashes, "block_hash"
            if self.hash_mode == "block":
                raise BenchError("no block_hash lines in the cell logs — the server was not "
                                 "started with ARNIS_BLOCK_HASH=1 (use --hash-mode region "
                                 "or drop --attach)")
        return self.region_hashes(), "region_files"

    def region_hashes(self) -> dict:
        """Fallback: sha256 of each region file with the 4 KiB timestamp table zeroed.

        WEAKER THAN block_hash. .mca bytes carry per-chunk timestamps (zeroed here) but also
        depend on chunk write ORDER and zlib output, both of which a different worker/flush
        layout can legitimately change without changing a single block. Treat a mismatch here
        as "look closer", not as proof.
        """
        code, st = self.http.get("/api/state", timeout=30)
        world = Path((st or {}).get("master_world") or "")
        region = world / "region"
        out = {}
        if not region.is_dir():
            return out
        for f in sorted(region.glob("*.mca")):
            try:
                data = bytearray(f.read_bytes())
            except Exception:  # noqa: BLE001
                continue
            if len(data) >= 8192:
                data[4096:8192] = b"\x00" * 4096      # chunk mtimes: not content
            out[f.name] = hashlib.sha256(bytes(data)).hexdigest()[:16]
        return out

    def determinism_gate(self) -> dict:
        """Per GROUP (same bbox + scale + cell_size), every run's hash vector must match.

        Comparison is deliberately NOT across groups: a 1:1 world and a 1:10 world are
        different worlds and are supposed to hash differently. Inside a group only the
        scheduling knobs moved, so any difference is a scheduler bug.
        """
        groups: dict[str, dict] = {}
        order: list[str] = []
        for r in self.results:
            if r["group"] not in order:
                order.append(r["group"])
        for g in order:
            recs = [r for r in self.results if r["group"] == g and r.get("hashes")]
            if len(recs) < 2:
                groups[g] = {"verdict": "skipped",
                             "reason": "fewer than two runs in this group produced hashes",
                             "source": recs[0]["hash_source"] if recs else "none",
                             "strength": "none"}
                continue
            source = recs[0]["hash_source"]
            if any(r["hash_source"] != source for r in recs):
                groups[g] = {"verdict": "skipped", "source": "mixed", "strength": "none",
                             "reason": "runs used different hash sources; compare like with like"}
                continue
            base = recs[0]
            mismatches = []
            for r in recs[1:]:
                a, b = base["hashes"], r["hashes"]
                if set(a) != set(b):
                    mismatches.append({
                        "run": r["tag"], "kind": "key-set",
                        "only_in_baseline": sorted(set(a) - set(b))[:12],
                        "only_in_run": sorted(set(b) - set(a))[:12],
                    })
                    continue
                diff = [k for k in sorted(a) if a[k] != b[k]]
                if diff:
                    mismatches.append({
                        "run": r["tag"], "kind": "value",
                        "cells": [{"cell": k, "baseline": a[k], "run": b[k]} for k in diff[:12]],
                        "n_differing": len(diff), "n_total": len(a),
                    })
            groups[g] = {"verdict": "identical" if not mismatches else "MISMATCH",
                         "source": source, "baseline": base["tag"],
                         "vector_len": len(base["hashes"]), "compared": len(recs),
                         "mismatches": mismatches,
                         "strength": "strong" if source == "block_hash" else "weak"}
        verdicts = {g["verdict"] for g in groups.values()}
        overall = ("MISMATCH" if "MISMATCH" in verdicts
                   else "identical" if "identical" in verdicts else "skipped")
        strength = ("strong" if any(g.get("strength") == "strong" and g["verdict"] != "skipped"
                                    for g in groups.values()) else "weak")
        if overall == "MISMATCH":
            # The gate is only as strong as the groups that actually failed.
            strength = ("strong" if any(g["verdict"] == "MISMATCH" and g.get("strength") == "strong"
                                        for g in groups.values()) else "weak")
        return {"verdict": overall, "strength": strength, "groups": groups}

    # -- cleanup ------------------------------------------------------------
    def cleanup(self) -> None:
        if not self.args.cleanup:
            return
        print("\n== cleanup: removing bench project workspaces ==")
        try:
            _, body = self.http.get("/api/projects")
            active = body.get("active")
            for p in body.get("projects") or []:
                slug = p.get("slug") or p.get("name")
                if not slug or not str(slug).startswith("bench") or slug == active:
                    continue
                self.http.post("/api/projects/delete", {"slug": slug})
                print(f"  removed {slug}")
        except Exception as ex:  # noqa: BLE001
            print(f"  cleanup skipped: {ex}")


def swap_used_mb() -> float | None:
    try:
        import psutil  # noqa: PLC0415
        return psutil.swap_memory().used / (1024 * 1024)
    except Exception:  # noqa: BLE001
        return None


# ── metrics ───────────────────────────────────────────────────────────────────

def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    if len(v) == 1:
        return v[0]
    pos = q * (len(v) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


def metrics_from_report(report: dict, harness_wall_s: float) -> dict:
    """Everything in the table, read out of meld-report.json (schema meld-run-report/3)."""
    sm = (report or {}).get("summary") or {}
    cells = [c for c in ((report or {}).get("cells") or [])
             if c.get("duration_s") and c.get("status") == "merged"]
    durs = [float(c["duration_s"]) for c in cells]
    wall = float(sm.get("elapsed_s") or harness_wall_s or 0.0)
    merged = int(sm.get("merged") or len(durs))
    return {
        "wall_s": round(wall, 1),
        "cells_merged": merged,
        "cells_failed": int(sm.get("failed") or 0),
        "cells_per_min": round(merged / wall * 60.0, 2) if wall > 0 else None,
        "effective_parallelism": round(sum(durs) / wall, 2) if wall > 0 and durs else None,
        "cpu_avg": sm.get("cpu_avg"),
        "cpu_peak": sm.get("cpu_peak"),
        "ram_peak": sm.get("ram_peak"),
        "cell_median_s": round(statistics.median(durs), 1) if durs else None,
        "cell_p95_s": round(percentile(durs, 0.95), 1) if durs else None,
        "cell_avg_s": round(sum(durs) / len(durs), 1) if durs else None,
        "cell_slowest_s": round(max(durs), 1) if durs else None,
        "workers_peak": sm.get("workers_peak"),
        "workers_setting": sm.get("workers_setting"),
        "on_disk_mb": sm.get("on_disk_mb"),
        "retries": sm.get("retries"),
    }


def fmt(v, nd: int = 1) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def delta_pct(base, new, lower_is_better: bool) -> str:
    """Signed change vs the baseline, always written so + means BETTER."""
    try:
        b, n = float(base), float(new)
    except (TypeError, ValueError):
        return "-"
    if b == 0:
        return "-"
    d = (n - b) / b * 100.0
    if lower_is_better:
        d = -d
    return f"{d:+.1f}%"


def render_table(results: list[dict]) -> str:
    """The markdown comparison table: one section per group, deltas vs that group's baseline."""
    lines: list[str] = []
    groups: list[str] = []
    for r in results:
        if r["group"] not in groups:
            groups.append(r["group"])
    for g in groups:
        rows = [r for r in results if r["group"] == g]
        base = next((r for r in rows if r["spec"].get("baseline")), None) \
            or next((r for r in rows if r["arm"] == "legacy"), rows[0])
        bm = base["metrics"]
        lines.append(f"\n### {g}  (baseline: `{base['tag']}`)\n")
        lines.append("| config | arm | w set | wall s | Δ wall | cells/min | Δ thru | eff.par | "
                     "cpu avg | ram peak | median s | p95 s | w peak | outcome |")
        lines.append("|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|")
        for r in rows:
            m = r["metrics"]
            gov = r["spec"]["governor_mode"]
            wset = (f"{r['spec']['workers']}" if gov == "off"
                    else f"auto<={r['spec']['governor_max_workers'] or r['spec']['workers']}")
            lines.append(
                f"| `{r['tag']}` | {r['arm']} | {wset} | {fmt(m.get('wall_s'))} | "
                f"{'-' if r is base else delta_pct(bm.get('wall_s'), m.get('wall_s'), True)} | "
                f"{fmt(m.get('cells_per_min'), 2)} | "
                f"{'-' if r is base else delta_pct(bm.get('cells_per_min'), m.get('cells_per_min'), False)} | "
                f"{fmt(m.get('effective_parallelism'), 2)} | {fmt(m.get('cpu_avg'), 0)} | "
                f"{fmt(m.get('ram_peak'), 0)} | {fmt(m.get('cell_median_s'))} | "
                f"{fmt(m.get('cell_p95_s'))} | {fmt(m.get('workers_peak'), 0)} | "
                f"{r['outcome']} |")
    return "\n".join(lines)


def render_determinism(gate: dict) -> str:
    out = [f"\n### Determinism gate: {'IDENTICAL' if gate.get('verdict') == 'identical' else gate.get('verdict', '?').upper()}"
           f"  ({gate.get('strength')} evidence)\n"]
    for g, res in (gate.get("groups") or {}).items():
        v = res.get("verdict")
        if v == "skipped":
            out.append(f"- **{g}**: skipped — {res.get('reason')}")
            continue
        src = ("block_hash — strong: arnis's own content hash of every region a cell wrote"
               if res.get("source") == "block_hash"
               else "region files — WEAK, see bench/README.md")
        if v == "identical":
            out.append(f"- **{g}**: identical across {res['compared']} run(s), "
                       f"{res['vector_len']} hash(es) each, baseline `{res['baseline']}` "
                       f"({src})")
            continue
        out.append(f"- **{g}**: **MISMATCH** vs baseline `{res['baseline']}` "
                   f"({res['vector_len']} hashes, {src}). Scheduling changed the OUTPUT.")
        for m in res.get("mismatches", []):
            if m["kind"] == "key-set":
                out.append(f"    - `{m['run']}`: different cell set. "
                           f"only in baseline: {m['only_in_baseline']}; "
                           f"only in run: {m['only_in_run']}")
            else:
                out.append(f"    - `{m['run']}`: {m['n_differing']}/{m['n_total']} cells differ")
                for c in m["cells"]:
                    out.append(f"        - `{c['cell']}`: baseline `{c['baseline']}` "
                               f"vs run `{c['run']}`")
    if not (gate.get("groups") or {}):
        out.append(f"- nothing to compare — {gate.get('reason', 'no runs completed')}")
    return "\n".join(out) + "\n"


# ── dry run ───────────────────────────────────────────────────────────────────

def print_plan(bench: Bench) -> None:
    o = bench.origin()
    print("\n=== PLAN ===")
    print(f"label            {bench.label}")
    print(f"matrix           {bench.args.matrix}")
    print(f"server           {'attach ' + bench.args.attach if bench.args.attach else 'spawned'}"
          f"  port {bench.args.port}  data_dir {bench.data_dir}")
    print(f"site             {bench.site.get('name', '(unnamed)')}  "
          f"origin {o['lat']:.5f},{o['lon']:.5f}  seed {bench.seed}")
    if bench.hash_mode == "off":
        print("hash mode        off  (NO determinism gate — timings only)")
    elif bench.args.attach:
        print(f"hash mode        {bench.hash_mode}  (attached: block_hash only if THAT server "
              f"was started with ARNIS_BLOCK_HASH=1, else region-file fallback)")
    else:
        print(f"hash mode        {bench.hash_mode}  "
              f"(ARNIS_BLOCK_HASH=1 goes into the spawned server's env)")
    ws = ", ".join(f"{k}={v}" for k, v in sorted(bench.world_settings.items()))
    print(f"world settings   {ws or '(server defaults)'}")
    prep = bench.prep_cfg
    print(f"prep             survey={prep.get('survey', True)} "
          f"bake_elevation={prep.get('bake_elevation', True)} "
          f"prewarm_overture={prep.get('prewarm_overture', True)}")
    wd = bench.wd
    print(f"abort            ram>{wd.get('ram_abort_pct', 95)}%  "
          f"swap_growth>{wd.get('swap_growth_mb', 4096)}MB  "
          f"run>{wd.get('run_timeout_s', 7200)}s  stall>{wd.get('stall_timeout_s', 1800)}s")
    print(f"\n{'run':<22} {'arm':<9} {'workers':<12} {'gov':<7} {'cpu%':<5} {'flush':<6} "
          f"{'scale':<7} {'cs':<3} {'rep':<4} {'cells':<7} {'est min':<8}")
    print("-" * 104)
    total_min = 0.0
    for s in bench.specs:
        cells = estimate_cells(s, o)
        mins = estimate_minutes(s, cells)
        if mins:
            total_min += mins
        wtxt = (str(s.workers) if s.governor_mode == "off"
                else f"auto<={s.governor_max_workers or s.workers}")
        print(f"{s.name:<22} {s.arm:<9} {wtxt:<12} {s.governor_mode:<7} "
              f"{s.cpu_target_pct:<5} {s.flush_threads_cap:<6} {s.scale:<7} {s.cell_size:<3} "
              f"{s.repeats:<4} {str(cells or '?'):<7} {fmt(mins, 1):<8}")
    print("-" * 104)
    n_runs = sum(s.repeats for s in bench.specs)
    print(f"{n_runs} run(s).  Estimated render time ~{total_min:.0f} min "
          f"({total_min / 60:.1f} h), plus prep (survey + elevation bake + Overture prewarm, "
          f"usually 2-20 min on a cold cache and seconds on a warm one).")
    print("Estimates come from bench/bench_scheduler.py THROUGHPUT_MODEL - measured on the "
          "reference machine, not a promise.")
    print("\nSameness: inside each group, every run's per-cell hash vector must match the "
          "group's first run. Groups are never compared to each other.")
    print("Nothing was rendered (--dry-run).")


# ── self test (pure functions only; no server, no rendering) ──────────────────

def selftest() -> int:
    """`--selftest`: exercise the parts that decide whether a sweep is valid or void.

    Deliberately server-free so it runs anywhere, including in CI on a machine with no
    arnis binary. It checks the three things a wrong answer would silently corrupt: the
    world/scheduling settings split, the metric arithmetic, and the determinism gate.
    """
    fails: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(f"  {'ok  ' if cond else 'FAIL'}  {name}{'' if cond else ' — ' + detail}")
        if not cond:
            fails.append(name)

    bbox = {"south": 44.36, "west": 25.96, "north": 44.51, "east": 26.17}
    base_matrix = {
        "label": "t", "site": {"bbox": bbox},
        "runs": [
            {"name": "a", "arm": "legacy", "workers": 4, "governor_mode": "off",
             "cell_size": 4, "scale": 1.0, "baseline": True},
            {"name": "b", "arm": "governor", "workers": 8, "governor_mode": "auto",
             "governor_max_workers": 20, "cell_size": 4, "scale": 1.0},
        ],
    }
    specs = build_specs(base_matrix, None, None, False)
    check("matrix parses into two specs", len(specs) == 2)
    check("groups derive from scale + cell size", specs[0].group == specs[1].group,
          f"{specs[0].group} vs {specs[1].group}")
    check("scheduling patch carries the governor keys",
          set(specs[1].sched_settings()) == {"max_workers", "governor_mode",
                                             "governor_max_workers", "cpu_target_pct",
                                             "flush_threads_cap", "ram_headroom_mb"})

    bad = json.loads(json.dumps(base_matrix))
    bad["runs"][1]["settings"] = {"scale": 0.5}
    try:
        build_specs(bad, None, None, False)
        check("a world-shaping override is refused", False, "it was accepted")
    except BenchError:
        check("a world-shaping override is refused", True)
    check("--allow-world-override opens that door",
          len(build_specs(bad, None, None, True)) == 2)

    mixed = json.loads(json.dumps(base_matrix))
    for r in mixed["runs"]:                  # one explicit group, two different worlds
        r["group"] = "g"
    mixed["runs"][1]["scale"] = 0.1
    try:
        build_specs(mixed, None, None, False)
        check("a group mixing scales is refused", False, "it was accepted")
    except BenchError:
        check("a group mixing scales is refused", True)

    try:
        build_specs(base_matrix, ["nope"], None, False)
        check("--only with an unknown name is refused", False, "it was accepted")
    except BenchError:
        check("--only with an unknown name is refused", True)

    grouped = json.loads(json.dumps(base_matrix))
    for r in grouped["runs"]:
        r["group"] = "pair"
    check("--only takes a group name, not just a run name",
          len(build_specs(grouped, ["pair"], None, False)) == 2)
    check("--only still takes a single run name",
          len(build_specs(grouped, ["a"], None, False)) == 1)

    report = {
        "schema": "meld-run-report/3",
        "summary": {"elapsed_s": 120.0, "merged": 4, "failed": 0, "cpu_avg": 79,
                    "ram_peak": 81, "workers_peak": 8, "workers_setting": 8},
        "cells": [{"cell": f"0,{i},4", "status": "merged", "duration_s": d}
                  for i, d in enumerate([10.0, 20.0, 30.0, 40.0])],
    }
    m = metrics_from_report(report, 130.0)
    check("wall comes from the report, not the harness clock", m["wall_s"] == 120.0)
    check("cells/min = merged / wall", m["cells_per_min"] == 2.0, str(m["cells_per_min"]))
    check("effective parallelism = sum(cell) / wall",
          m["effective_parallelism"] == round(100.0 / 120.0, 2), str(m["effective_parallelism"]))
    check("median cell time", m["cell_median_s"] == 25.0, str(m["cell_median_s"]))
    check("p95 cell time interpolates", m["cell_p95_s"] == 38.5, str(m["cell_p95_s"]))
    check("percentile of one sample is that sample", percentile([7.0], 0.95) == 7.0)

    check("faster wall reads as a positive delta", delta_pct(100, 80, True) == "+20.0%")
    check("higher throughput reads as a positive delta", delta_pct(10, 12, False) == "+20.0%")
    check("a slower wall reads as negative", delta_pct(100, 125, True) == "-25.0%")

    class _B(Bench):
        def __init__(self):     # noqa: D107 — gate-only stub, no server, no args
            self.results = []

    def rec(tag, group, hashes, source="block_hash", spec=None):
        return {"tag": tag, "run": tag, "group": group, "arm": "legacy",
                "hashes": hashes, "hash_source": source,
                "spec": spec or {"baseline": False, "workers": 8, "governor_mode": "off",
                                 "governor_max_workers": 0},
                "metrics": metrics_from_report(report, 120.0), "outcome": "ok"}

    b = _B()
    b.results = [rec("a", "g1", {"0,0,4": "aa", "0,1,4": "bb"}),
                 rec("b", "g1", {"0,0,4": "aa", "0,1,4": "bb"}),
                 rec("c", "g2", {"0,0,4": "zz"})]
    g = b.determinism_gate()
    check("matching vectors pass the gate", g["verdict"] == "identical", json.dumps(g))
    check("a lone run in a group is skipped, not passed",
          g["groups"]["g2"]["verdict"] == "skipped")
    check("gate strength is reported", g["strength"] == "strong")

    b.results[1]["hashes"]["0,1,4"] = "cc"
    g = b.determinism_gate()
    check("one differing cell fails the gate", g["verdict"] == "MISMATCH")
    check("the failing cell is named",
          g["groups"]["g1"]["mismatches"][0]["cells"][0]["cell"] == "0,1,4")

    b.results[1]["hashes"] = {"0,0,4": "aa"}
    g = b.determinism_gate()
    check("a different cell SET fails the gate too",
          g["groups"]["g1"]["mismatches"][0]["kind"] == "key-set")

    b.results = [rec("a", "g1", {"r.0.0.mca": "aa"}, source="region_files"),
                 rec("b", "g1", {"r.0.0.mca": "bb"}, source="region_files")]
    g = b.determinism_gate()
    check("a region-file mismatch is flagged weak",
          g["verdict"] == "MISMATCH" and g["strength"] == "weak")

    b.results = [rec("a", "g1", {"r.0.0.mca": "aa"}, source="region_files"),
                 rec("b", "g1", {"0,0,4": "aa"})]
    g = b.determinism_gate()
    check("mixed hash sources are not compared",
          g["groups"]["g1"]["verdict"] == "skipped")

    b.results = [rec("a", "g1", {"0,0,4": "aa"},
                     spec={"baseline": True, "workers": 4, "governor_mode": "off",
                           "governor_max_workers": 0}),
                 rec("b", "g1", {"0,0,4": "aa"},
                     spec={"baseline": False, "workers": 8, "governor_mode": "auto",
                           "governor_max_workers": 20})]
    b.results[1]["arm"] = "governor"
    table = render_table(b.results)
    check("the table renders both arms", "`a`" in table and "`b`" in table)
    check("the governor row shows its ceiling", "auto<=20" in table, table)
    check("the determinism block renders", "Determinism gate" in
          render_determinism(b.determinism_gate()))

    print(f"\n{'all checks passed' if not fails else str(len(fails)) + ' CHECK(S) FAILED'}")
    return 0 if not fails else 1


# ── main ──────────────────────────────────────────────────────────────────────

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="bench_scheduler",
        description="Legacy scheduler vs governor, same area, same settings: speed AND "
                    "identical output.")
    p.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX,
                   help=f"config matrix (default {DEFAULT_MATRIX.name})")
    p.add_argument("--only", default="", help="comma-separated run names to execute")
    p.add_argument("--label", default="", help="results label (default: matrix `label`)")
    p.add_argument("--repeats", type=int, default=0, help="override every run's `repeats`")
    p.add_argument("--dry-run", action="store_true",
                   help="validate the matrix and print the plan; render nothing")
    p.add_argument("--selftest", action="store_true",
                   help="check the harness's own logic (config split, metrics, gate); "
                        "no server, no rendering")
    p.add_argument("--attach", nargs="?", const=f"http://127.0.0.1:{DEFAULT_PORT}", default="",
                   help="attach to a running Meld instead of starting one "
                        f"(bare --attach means {'http://127.0.0.1:%d' % DEFAULT_PORT})")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="port for the spawned server")
    p.add_argument("--python", default="", help="interpreter used to start server.py")
    p.add_argument("--data-dir", default="",
                   help="MELD_DATA_DIR for the spawned server (its own projects + "
                        "single-instance lock, so it can run beside your Meld). The cache stays "
                        "shared via --cache-dir.")
    p.add_argument("--cache-dir", default="",
                   help="MELD_CACHE_DIR for the spawned server (default <repo>/cache — keep it "
                        "shared so the caches stay warm)")
    p.add_argument("--hash-mode", choices=("auto", "block", "region", "off"), default="auto",
                   help="determinism source: auto = block_hash if present else region files")
    p.add_argument("--strict-fallback", action="store_true",
                   help="treat a region-file mismatch as fatal too (default: loud warning)")
    p.add_argument("--restart-between", action="store_true", default=True,
                   help="restart the server between runs (default on; isolates governor state)")
    p.add_argument("--no-restart-between", dest="restart_between", action="store_false")
    p.add_argument("--allow-world-override", action="store_true",
                   help="permit per-run overrides of world-shaping settings (breaks the gate)")
    p.add_argument("--cleanup", action="store_true",
                   help="delete the bench project workspaces at the end (worlds are kept)")
    args = p.parse_args(argv)
    args.only = [s.strip() for s in args.only.split(",") if s.strip()]
    if args.attach:
        # A server we did not start is a server we cannot restart: its lifetime is the
        # operator's business, and killing it is reserved for an abort.
        args.restart_between = False
        try:
            args.port = int(args.attach.rsplit(":", 1)[1].split("/")[0])
        except (IndexError, ValueError):
            pass
    return args


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.selftest:
        print("=== bench_scheduler selftest ===")
        return selftest()
    try:
        matrix = load_matrix(args.matrix)
        specs = build_specs(matrix, args.only, args.repeats or None, args.allow_world_override)
    except BenchError as ex:
        print(f"config error: {ex}")
        return 2

    bench = Bench(args, matrix, specs)
    if args.dry_run:
        print_plan(bench)
        return 0

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    print_plan(bench)
    print("\n=== EXECUTING ===")
    server = Server(port=args.port, python=args.python or None,
                    data_dir=args.data_dir or None, cache_dir=args.cache_dir or None,
                    block_hash=(args.hash_mode in ("auto", "block")),
                    attach_url=args.attach or None,
                    log_path=RESULTS_DIR / bench.label / "server.log")
    bench.server = server
    aborted = None
    t_sweep = time.time()
    try:
        server.start()
        bench.prep()
        for spec in specs:
            for rep in range(spec.repeats):
                bench.results.append(bench.run_one(spec, rep))
    except SweepAbort as ex:
        aborted = str(ex)
        print(f"\n!! ABORT: {ex}")
        server.kill("abort criteria hit")
    except BenchError as ex:
        aborted = str(ex)
        print(f"\n!! ERROR: {ex}")
    except KeyboardInterrupt:
        aborted = "interrupted by the operator"
        print("\n!! interrupted — killing the server")
        server.kill("operator interrupt")
    finally:
        gate = bench.determinism_gate() if bench.results else {"verdict": "skipped",
                                                               "reason": "no runs completed"}
        out = {
            "label": bench.label,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "matrix": str(args.matrix),
            "sweep_wall_s": round(time.time() - t_sweep, 1),
            "aborted": aborted,
            "determinism": gate,
            "runs": bench.results,
        }
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        jpath = RESULTS_DIR / f"{bench.label}.json"
        jpath.write_text(json.dumps(out, indent=1), encoding="utf-8")
        table = ""
        if bench.results:
            table = render_table(bench.results)
        md = (f"# Scheduler bench — {bench.label}\n\n"
              f"{out['generated_at']} · matrix `{args.matrix}` · "
              f"sweep {out['sweep_wall_s'] / 60:.1f} min"
              + (f"\n\n**ABORTED:** {aborted}\n" if aborted else "\n")
              + table + "\n" + render_determinism(gate))
        (RESULTS_DIR / f"{bench.label}.md").write_text(md, encoding="utf-8")
        print(table)
        print(render_determinism(gate))
        print(f"results: {jpath}")
        print(f"         {RESULTS_DIR / (bench.label + '.md')}")
        try:
            bench.cleanup()
        finally:
            server.stop()

    if gate.get("verdict") == "MISMATCH":
        strong = gate.get("strength") == "strong"
        if strong or args.strict_fallback:
            print("\nDETERMINISM GATE FAILED — the sweep is void. Fix the scheduler before "
                  "reading any timing above.")
            return 3
        print("\nRegion-file hashes differ. That is the WEAK gate (chunk order and zlib output "
              "are not content); re-run with a server started under ARNIS_BLOCK_HASH=1 before "
              "calling it a bug.")
    return 1 if aborted else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
