#!/usr/bin/env python
"""
Bucharest-centre A/B: legacy scheduler (main) vs perf/speed-to-worldgen governor.

Drives Meld's HTTP API against ONE worktree (meld-triagefix) on two branches.
Everything except the scheduler is held constant, so a wall-time delta is
attributable to the scheduler and nothing else.

  arm A (baseline)  meld @ main (1.9.7)                 + arnis 3.1.7 release
  arm B (perf)      meld @ perf/speed-to-worldgen       + arnis perf build
  both arms         native_region_format=blinear, stream_to_disk=on,
                    same bbox, same origin, same scale, warm caches

  run 1  cell size 4  (8x8 grid  = 64 cells)
  run 2  cell size 8  (4x4 grid  = 16 cells)   <- same ground area

Usage
  python ab_bucharest.py warm            # one throwaway pass to warm shared caches
  python ab_bucharest.py run A           # the two baseline runs   (checkout main first)
  python ab_bucharest.py run B           # the two perf runs       (checkout perf first)
  python ab_bucharest.py verify          # offline self-check: no server, nothing rendered
  python ab_bucharest.py check A         # plumbing smoke against a live server, no render
  python ab_bucharest.py report          # comparison table from results/

Invariants this harness enforces (it exits non-zero rather than measuring the wrong thing):
  * every run - reuse included - switches to its own project and VERIFIES it is active
    before anything is queued (H1)
  * the settings it asked for are read back off /api/settings and must match (H2)
  * the harvested report's cell_size and cell count must match what was requested, or the
    result is written with an error and excluded from the comparison table (H1)
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Arm A runs from its OWN worktree pinned at main, because meld-triagefix carries the
# perf branch's uncommitted work and must not be branch-switched underneath it.
MELD_A = Path(r"c:/tmp/meld-ab-baseline")                       # main / 1.9.7
MELD_B = Path(r"c:/Users/LEGION/Documents/Meld/meld-triagefix")  # perf/speed-to-worldgen
ARNIS_A = Path(r"D:/RomaniaSMP Server 1.0/Meld-win-x64/Meld-1.9.7/arnis.exe")  # released 3.1.7
ARNIS_B = Path(r"c:/Users/LEGION/Documents/Meld/arnis-triagefix/target/release/arnis.exe")

# Both arms share ONE data dir and ONE cache dir, so arm B cannot inherit an unfair
# warm-cache advantage from arm A (MELD_DATA_DIR / MELD_CACHE_DIR are honored by src/paths.py).
AB_ROOT = Path(r"c:/tmp/meld-ab-data")
OUT = Path(__file__).resolve().parent / "ab-results"
BASE = "http://127.0.0.1:5630"

# Bucharest centre (Piata Unirii), ~16.4 km box = 32x32 regions at 1:1.
ORIGIN = {"lat": 44.4268, "lon": 26.1025}
BBOX = {"south": 44.3532, "west": 25.9994, "north": 44.5004, "east": 26.2056}

# Held constant across every timed run. Only job_size_regions and the scheduler vary.
COMMON = {
    "scale": 1.0,
    "native_region_format": "blinear",
    "native_blinear_level": 6,
    "stream_to_disk": True,
    "prefetch_terrain": True,
    "elevation_mode": "local",
    "cpu_target_pct": 90,
    "gpu_accel": "off",
}

RUNS = [
    {"id": "cs4", "job_size_regions": 4},
    {"id": "cs8", "job_size_regions": 8},
]

# Arm A pins the workers the user actually used, so the baseline is a fair
# hand-tuned opponent rather than the stock default of 4.
ARM = {
    "A": {"label": "baseline-1.9.7", "settings": {"max_workers": 16, "governor_mode": "off"}},
    "B": {"label": "perf-governor",  "settings": {"max_workers": 20, "governor_mode": "auto"}},
    # Phase 2: the same governor, plus B1 - arnis skips writing the seam-halo region ring
    # that the merge deletes anyway. Measured per-cell at -11.5% wall / -12.3% CPU.
    "C": {"label": "perf-phase2",    "settings": {"max_workers": 20, "governor_mode": "auto",
                                                  "canonical_regions": True}},
}


# -- HTTP ----------------------------------------------------------------------
def call(path: str, payload=None, timeout=120):
    url = BASE + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"},
        method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # Meld answers a rejected request with a JSON {ok:false,error:...} body; the
        # status code alone says nothing useful, so surface the body instead.
        body = e.read().decode("utf-8", "replace")
        print(f"  HTTP {e.code} on {path}: {body[:400]}")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"raw": body[:400]}


def port_open(port=5630) -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


# -- server lifecycle ----------------------------------------------------------
def start_server(log_path: Path, meld_dir: Path):
    if port_open():
        print("  server already up on 5630 - stop it first (arms must not share a process)")
        sys.exit(4)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    AB_ROOT.mkdir(parents=True, exist_ok=True)
    fp = open(log_path, "w", encoding="utf-8")
    env = dict(os.environ,
               MELD_DATA_DIR=str(AB_ROOT / "data"),
               MELD_CACHE_DIR=str(AB_ROOT / "cache"),
               MELD_NO_TRAY="1", MELD_NO_BROWSER="1")
    proc = subprocess.Popen([sys.executable, "server.py"], cwd=str(meld_dir),
                            stdout=fp, stderr=subprocess.STDOUT, env=env)
    for _ in range(120):
        if port_open():
            time.sleep(1.0)
            print(f"  server up (pid {proc.pid})")
            return proc
        if proc.poll() is not None:
            print(f"  server DIED, see {log_path}")
            print(log_path.read_text(encoding="utf-8", errors="replace")[-2000:])
            sys.exit(1)
        time.sleep(0.5)
    print("  server did not open the port in 60s")
    sys.exit(1)


def stop_server(proc):
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
    time.sleep(1.0)


# -- one run -------------------------------------------------------------------
def project_name(arm: str, run_id: str) -> str:
    return f"ab-{ARM[arm]['label']}-{run_id}"


_SLUGS: dict[str, str] = {}   # project name -> the slug the server actually created


def slugify(name: str) -> str:
    """Fallback only. The authoritative slug comes back from /api/projects/new and is
    remembered in _SLUGS - Meld's own _slugify keeps dots, so guessing it here was wrong."""
    return _SLUGS.get(name) or name.lower()


def active_slug():
    """The slug the server says is active right now. None if the call failed."""
    return (call("/api/projects") or {}).get("active")


def resolve_slug(name: str):
    """The slug the server gave this project. _SLUGS is authoritative for projects created in
    this process; otherwise match on the project NAME in the listing. Never guess - Meld's own
    _slugify keeps dots, and a duplicate name gets a `-2` suffix."""
    if _SLUGS.get(name):
        return _SLUGS[name]
    for p in (call("/api/projects").get("projects") or []):
        if p.get("name") == name:
            _SLUGS[name] = p.get("slug")
            return p.get("slug")
    return None


def switch_project(name: str) -> str:
    """H1. do_run(reuse=True) skipped prepare_project() entirely, and prepare_project() was the
    ONLY caller of /api/projects/switch - so the warm re-run rendered into whatever project was
    active and wrote its meld-report.json there. Not hypothetical: the file at
    <data>/projects/ab-perf-governor-cs8/ab-perf-governor-cs8/meld-report.json holds cell_size 4,
    total 81, elapsed_s 160.3 and 106 cell entries - a cs4 warm run written over the cs8 report.
    The reuse path must still SWITCH; it just must not recreate."""
    slug = resolve_slug(name)
    if not slug:
        print(f"  REFUSING: project {name!r} does not exist, so there is nothing to reuse. "
              f"Run the cold pass for this arm first.")
        sys.exit(3)
    r = call("/api/projects/switch", {"slug": slug})
    if not r.get("ok"):
        print(f"  REFUSING: /api/projects/switch to {slug!r} failed: {r}")
        sys.exit(3)
    live = active_slug()
    if live != slug:
        print(f"  REFUSING: switch reported ok but the active project is {live!r}, not {slug!r}")
        sys.exit(3)
    print(f"  active project: {slug}")
    return slug


def assert_active_project(name: str, why: str) -> str:
    """The guard. Called immediately before anything that writes into a project."""
    want = resolve_slug(name)
    live = active_slug()
    if not want or live != want:
        print(f"  REFUSING ({why}): the active project is {live!r}, this run needs {want!r}. "
              f"Rendering would write its cells AND its meld-report.json into the wrong "
              f"project - the exact corruption that put a cs4 report inside "
              f"ab-perf-governor-cs8.")
        sys.exit(5)
    return want


def assert_settings_applied(patch: dict) -> dict:
    """H2, this harness's half: prove the settings the run claims are the settings the server
    has. A key the server does not know is dropped silently by update_settings, so `<absent>`
    has to read as a failure - that is how bench/matrix.json spent phase 1 asking for
    `region_format`, a setting Meld does not have."""
    live = call("/api/settings")
    drift = {}
    for k, want in patch.items():
        if k not in live:
            drift[k] = (want, "<absent>")
        elif isinstance(want, bool) or isinstance(live[k], bool):
            if bool(want) != bool(live[k]):
                drift[k] = (want, live[k])
        elif isinstance(want, (int, float)) and isinstance(live[k], (int, float)):
            if float(want) != float(live[k]):
                drift[k] = (want, live[k])
        elif want != live[k]:
            drift[k] = (want, live[k])
    if drift:
        print("  REFUSING: the settings did not apply, so this run would measure a different "
              "world than it reports:")
        for k, (w, l) in sorted(drift.items()):
            print(f"    {k}: asked {w!r}, server has {l!r}")
        print("    ('<absent>' means the key is not a Meld setting at all - check "
              "src/project.default_settings)")
        sys.exit(6)
    keys = ("native_region_format", "native_blinear_level", "stream_to_disk",
            "job_size_regions", "max_workers", "governor_mode")
    print("  settings verified live: "
          + ", ".join(f"{k}={live.get(k)!r}" for k in keys if k in live))
    return live


def prepare_project(name: str, settings: dict):
    """Fresh project every run: no merged cells to skip, no settings carry-over.
    /api/projects/new auto-switches and would suffix a duplicate slug (-2), so the
    stale one is deleted first - and delete refuses the ACTIVE project, hence the
    park on `default`."""
    slug = slugify(name)
    listing = call("/api/projects")
    slugs = {p.get("slug") for p in (listing.get("projects") or [])}
    if slug in slugs:
        if listing.get("active") == slug:
            other = next((x for x in slugs if x != slug), None)
            if other is None:
                other = call("/api/projects/new", {"name": "ab-park"}).get("slug")
            else:
                call("/api/projects/switch", {"slug": other})
        call("/api/projects/delete", {"slug": slug})
    r = call("/api/projects/new", {"name": name})
    if not r.get("ok"):
        print("  project create failed:", r)
        sys.exit(3)
    _SLUGS[name] = r["slug"]
    # /api/projects/new auto-switches, but "auto" is not "verified": if it did not, every cell
    # and the report land in the previous project.
    assert_active_project(name, "after creating the project")
    call("/api/origin", ORIGIN)
    patch = dict(COMMON)
    patch.update(settings)
    r = call("/api/settings", patch)
    if not r.get("ok", True):
        print("  settings rejected:", r)
    return assert_settings_applied(patch)


def wait_for_run(poll_s=5.0, max_s=5400, start_grace_s=1800):
    """Two-stage wait. With prefetch_terrain on, /api/queue returns immediately and a
    background thread warms terrain before a single cell is submitted, so "not running"
    right after queueing means "not started yet", not "finished". Only treat the run as
    over once it has demonstrably started."""
    t0 = time.time()
    started = False
    last = ""
    while time.time() - t0 < max_s:
        st = call("/api/status", timeout=30)
        run = st.get("run") or {}
        pf = st.get("prefetch") or {}
        active = sum(1 for w in (st.get("workers") or []) if w.get("running"))
        done = int(run.get("done") or 0)
        failed = int(run.get("failed") or 0)
        total = int(run.get("total") or 0)
        busy = bool(st.get("running")) or active > 0 or (st.get("queue_size") or 0) > 0
        warming = bool(pf.get("active")) or bool(pf.get("running"))
        if busy or done or failed:
            started = True

        line = (f"    {int(time.time()-t0):4d}s  "
                f"active={active} queue={st.get('queue_size')} "
                f"done={done}/{total} fail={failed} "
                f"cpu={(st.get('stats') or {}).get('cpu_pct')} "
                f"ram={(st.get('stats') or {}).get('ram_pct')}")
        if warming and not started:
            line += f" [warming {pf.get('phase') or ''} {pf.get('done') or ''}]"
        gov = st.get("governor")
        if gov and gov.get("state") not in (None, "OFF"):
            line += (f" | gov {gov.get('state')} w={gov.get('workers')}"
                     f"->{gov.get('target')} t={gov.get('threads')}"
                     f" bind={gov.get('binding')}")
        if line != last:
            print(line, flush=True)
            last = line

        if started and not busy:
            if total and done + failed < total:
                print(f"    ENDED EARLY: {done + failed}/{total} accounted for")
            return True
        if not started and not warming and time.time() - t0 > start_grace_s:
            print("    NEVER STARTED (no warm activity, nothing queued)")
            return False
        time.sleep(poll_s)
    print("    TIMEOUT")
    return False


SCHEMAS_OK = ("meld-run-report/3", "meld-run-report/4")
TIMER_KEYS = ("merge_s", "prune_s", "health_s", "meta_s")


def _num(v):
    """A float, or None. Every schema/4 field must survive being absent on a /3 report -
    absent is not zero."""
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _timers(obj: dict) -> dict:
    """schema/4's summary.timers{merge_s,prune_s,health_s,meta_s}. {} on a schema/3 report."""
    t = (obj or {}).get("timers")
    if not isinstance(t, dict):
        return {}
    out = {k: _num(t.get(k)) for k in TIMER_KEYS if _num(t.get(k)) is not None}
    if out:
        out["post_arnis_total_s"] = round(sum(out.values()), 3)
    return out


def report_mismatch(summary: dict, want_cell_size, expect_cells=None) -> list:
    """Does this report belong to the run that just executed? Pure, so `verify` can replay it
    against the corrupted report still on disk without a server."""
    problems = []
    got_cs = (summary or {}).get("cell_size")
    if got_cs is not None and want_cell_size is not None and int(got_cs) != int(want_cell_size):
        problems.append(f"report says cell_size {got_cs}, this run asked for {want_cell_size}")
    total = (summary or {}).get("total")
    if expect_cells and total is not None and int(total) != int(expect_cells):
        problems.append(f"report says total {total}, {expect_cells} cells were queued")
    return problems


def harvest(name: str, arm: str, run: dict, wall_s: float, since: float = 0.0,
            expect_cells=None):
    """meld-report.json is the source of truth. Reads meld-run-report/3 and /4.

    schema/4 adds summary.cells_per_min and summary.timers; where it publishes a number this
    harness would otherwise re-derive, the report wins and `cells_per_min_source` records it,
    so phase-1 (/3) results stay readable and comparable next to phase-2 (/4) ones.

    `since` guards the reuse case: a reused project already holds a report at the same
    path from the previous run, and reading it would silently report the old run's
    numbers as the new one's. The report is also written a beat AFTER the last cell
    merges, hence the poll.

    H1's post-run assertion lives at the bottom: a report whose cell_size or cell count
    disagrees with what this run asked for is the corruption signature, and it must be an
    error in the result file, not a number quietly copied into a table.
    """
    proj_root = AB_ROOT / "data" / "projects"
    root = proj_root / slugify(name)

    def fresh() -> list[Path]:
        return [f for f in sorted(root.rglob("meld-report.json"),
                                  key=lambda f: f.stat().st_mtime)
                if f.stat().st_mtime >= since]

    reports = fresh()
    for _ in range(90):
        if reports:
            break
        time.sleep(1.0)
        reports = fresh()

    rec = {"arm": arm, "arm_label": ARM[arm]["label"], "run": run["id"],
           "cell_size": run["job_size_regions"], "harness_wall_s": round(wall_s, 1)}
    if not reports:
        rec["error"] = "no fresh meld-report.json produced"
        return rec
    d = json.loads(reports[-1].read_text(encoding="utf-8"))
    s = d.get("summary", {})
    cells = [c for c in d.get("cells", []) if c.get("status") == "merged" and c.get("duration_s")]
    durs = sorted(c["duration_s"] for c in cells)
    cellsec = sum(durs)
    el = s.get("elapsed_s") or wall_s
    schema = d.get("schema")
    if schema not in SCHEMAS_OK:
        print(f"  note: report schema is {schema!r}; this harness reads "
              f"{' / '.join(SCHEMAS_OK)} and ignores anything else it carries")
    cpm_reported = _num(s.get("cells_per_min"))
    cpm_derived = round(len(cells) / el * 60, 2) if el else None
    timers = _timers(s)
    rec.update({
        "report": str(reports[-1]),
        "report_schema": schema,
        "meld_version": d.get("meld_version"),
        "elapsed_s": round(el, 1),
        "cells_total": s.get("total"),
        "cells_merged": s.get("merged"),
        "failed": s.get("failed"),
        "cells_per_min": round(cpm_reported, 2) if cpm_reported is not None else cpm_derived,
        "cells_per_min_source": "report" if cpm_reported is not None else "derived",
        "cells_per_min_derived": cpm_derived,
        "timers": timers or None,
        "post_arnis_total_s": timers.get("post_arnis_total_s"),
        "report_cell_size": s.get("cell_size"),
        "eff_parallelism": round(cellsec / el, 2) if el else None,
        "workers_setting": s.get("workers_setting"),
        "workers_peak": s.get("workers_peak"),
        "cell_median_s": s.get("cell_median_s"),
        "cell_p95_s": round(durs[int(len(durs) * 0.95) - 1], 1) if durs else None,
        "cell_slowest_s": s.get("cell_slowest_s"),
        "cpu_avg": s.get("cpu_avg"),
        "ram_peak": s.get("ram_peak"),
        "regions": s.get("regions"),
        "on_disk_mb": s.get("on_disk_mb"),
        "retries": s.get("retries"),
    })

    # H1's post-run assertion. A silent mismatch must be impossible: the cs8 report on disk
    # says cell_size 4 / total 81 because a cs4 warm run wrote over it, and nothing noticed.
    problems = report_mismatch(s, run["job_size_regions"], expect_cells)
    if problems:
        rec["error"] = ("report does not belong to this run: " + "; ".join(problems)
                        + f" (report {reports[-1]})")
        rec["harvest_ok"] = False
        print("  HARVEST MISMATCH - this result is NOT usable:")
        for pb in problems:
            print(f"    {pb}")
        print("    The report was written into the wrong project, or an older report was "
              "read. Do not quote these numbers.")
    else:
        rec["harvest_ok"] = True
    return rec


def do_run(arm: str, run: dict, *, reuse: bool = False, tag: str = ""):
    name = project_name(arm, run["id"])
    print(f"\n-- {ARM[arm]['label']} / {run['id']} (cell {run['job_size_regions']}x"
          f"{run['job_size_regions']}) -------------")
    settings = dict(ARM[arm]["settings"])
    settings["job_size_regions"] = run["job_size_regions"]
    if reuse:
        # H1: reuse means "do not RECREATE", never "do not SWITCH". Skipping the switch is what
        # made the warm cs4 pass render into - and overwrite the report of - the cs8 project.
        print("  reusing the project: governor_history should warm-start this run")
        switch_project(name)
        patch = {**COMMON, **settings}
        call("/api/settings", patch)
        assert_settings_applied(patch)
    else:
        prepare_project(name, settings)
    # Last line of defence: nothing below this point may write into another project.
    assert_active_project(name, "before queueing")
    t0 = time.time()
    q = call("/api/queue", {"bbox": BBOX, "size": run["job_size_regions"], "force": True})
    if not q.get("ok", True):
        print("  queue rejected:", q)
        return {"arm": arm, "run": run["id"], "error": str(q)[:300]}
    queued = q.get("count")
    if queued is None:
        queued = len(q.get("queued") or []) or None
    print(f"  queued {queued if queued is not None else '?'} cells")
    wait_for_run()
    wall = time.time() - t0
    assert_active_project(name, "after the run, before harvesting")
    rec = harvest(name, arm, run, wall, since=t0, expect_cells=queued)
    # A run with failed cells is not a faster run, it is a broken one: the first phase-2
    # attempt "finished" 52s early because 36 of 81 cells never rendered. Fail the row.
    if rec.get("failed"):
        rec["error"] = f"{rec['failed']} of {rec.get('cells_total')} cells FAILED"
    elif rec.get("cells_merged") != rec.get("cells_total"):
        rec["error"] = (f"only {rec.get('cells_merged')} of {rec.get('cells_total')} "
                        f"cells merged")
    rec["warm_start"] = reuse
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{arm}-{run['id']}{tag}.json").write_text(json.dumps(rec, indent=1), encoding="utf-8")
    print(f"  DONE  {rec.get('elapsed_s')}s  {rec.get('cells_per_min')} cells/min "
          f"({rec.get('cells_per_min_source')})  "
          f"ram_peak={rec.get('ram_peak')}%  cpu_avg={rec.get('cpu_avg')}%")
    t = rec.get("timers")
    if t:
        print("        post-arnis timers: "
              + " ".join(f"{k[:-2]}={t[k]}s" for k in TIMER_KEYS if k in t)
              + f" total={t.get('post_arnis_total_s')}s")
    return rec


# -- modes ---------------------------------------------------------------------
def pin_arnis(arm: str) -> Path:
    """resolve_arnis_exe() searches APP_DIR first, so dropping the arm's binary next to
    its server pins the generator deterministically. Both arms need this - a worktree
    with no arnis.exe fails every cell instantly."""
    meld_dir = MELD_A if arm == "A" else MELD_B
    src = ARNIS_A if arm == "A" else ARNIS_B
    if not src.exists():
        print(f"REFUSING: arm {arm} needs {src} and it does not exist")
        sys.exit(2)
    dst = meld_dir / "arnis.exe"
    if not dst.exists() or dst.stat().st_mtime != src.stat().st_mtime:
        shutil.copy2(src, dst)
    return src


def mode_warm():
    """Throwaway pass so every timed run hits the same warm shared cache.
    Runs on the BASELINE build: the cache it fills is shared by both arms."""
    print(f"warm: arnis={pin_arnis('A')}")
    proc = start_server(OUT / "server-warm.log", MELD_A)
    try:
        name = "ab-warmup"
        prepare_project(name, {"max_workers": 8, "job_size_regions": 8,
                               "governor_mode": "off"})
        t0 = time.time()
        call("/api/queue", {"bbox": BBOX, "size": 8, "force": True})
        wait_for_run()
        print(f"warm pass done in {int(time.time()-t0)}s - caches primed")
    finally:
        stop_server(proc)


def mode_run(arm: str):
    assert arm in ARM
    meld_dir = MELD_A if arm == "A" else MELD_B
    want = "main" if arm == "A" else "perf/speed-to-worldgen-phase2"
    branch = subprocess.run(["git", "-C", str(meld_dir), "branch", "--show-current"],
                            capture_output=True, text=True).stdout.strip()
    if branch != want:
        print(f"REFUSING: {meld_dir} is on '{branch}', arm {arm} needs '{want}'")
        sys.exit(2)
    src = pin_arnis(arm)
    print(f"arm {arm}: meld={meld_dir.name}@{branch}  arnis={src}")
    proc = start_server(OUT / f"server-{arm}.log", meld_dir)
    try:
        for run in RUNS:
            do_run(arm, run)
        if arm in ("B", "C"):
            # Runs 2+ are what a user actually experiences: the ramp is paid once.
            do_run(arm, RUNS[0], reuse=True, tag="-warm")
    finally:
        stop_server(proc)


def mode_check(arm: str = "A"):
    """Plumbing smoke: start the arm's server, build a project, set origin+settings,
    plan the grid - but never render. Proves the harness before burning machine time."""
    meld_dir = MELD_A if arm == "A" else MELD_B
    proc = start_server(OUT / f"server-check-{arm}.log", meld_dir)
    try:
        st = call("/api/status")
        print("  arnis_found:", st.get("arnis_found"), "| version:", st.get("arnis_version"))
        s = prepare_project(f"ab-check-{arm}", dict(ARM[arm]["settings"],
                                                    job_size_regions=4))
        eff = {k: s.get(k) for k in ("scale", "max_workers", "job_size_regions",
                                     "native_region_format", "stream_to_disk",
                                     "cpu_target_pct", "governor_mode")}
        print("  effective settings:", json.dumps(eff))
        g = call("/api/grid", {"bbox": BBOX, "size": 4})
        n = g.get("count") or len(g.get("cells") or [])
        print(f"  grid plan: {n} cells at cell size 4  (ok={g.get('ok', True)})")
    finally:
        stop_server(proc)


def mode_verify() -> int:
    """Offline self-check. No server, no arnis, nothing rendered - safe to run any time.

    Proves the three things that, when wrong, make a whole A/B unusable and say nothing:
    every key in COMMON is a real Meld setting of the right type; COMMON does not contradict
    bench/matrix.json; and the H1 report assertion actually trips on the corrupted report
    that is still on disk from the phase-1 run.
    """
    print("=== ab_bucharest verify (offline: no server, nothing rendered) ===")
    fails = []

    def check(name, cond, detail=""):
        print(f"  {'ok  ' if cond else 'FAIL'}  {name}{'' if cond else ' - ' + detail}")
        if not cond:
            fails.append(name)

    defaults = None
    try:
        if str(MELD_B) not in sys.path:
            sys.path.insert(0, str(MELD_B))
        from src.project import default_settings
        defaults = default_settings()
    except Exception as ex:
        print(f"  note: could not import {MELD_B}/src/project.py ({ex}) - "
              f"the setting-name check is skipped")
    if defaults:
        bogus = sorted(k for k in COMMON if k not in defaults)
        check("every COMMON key is a real Meld setting", not bogus,
              f"{bogus} are not in src/project.default_settings() - Meld drops unknown keys "
              f"silently, so these would render with the server default")
        typed = sorted(k for k, v in COMMON.items()
                       if k in defaults
                       and isinstance(defaults[k], bool) != isinstance(v, bool))
        check("every COMMON value has the right type", not typed, str(typed))
        for label, arm in ARM.items():
            bad = sorted(k for k in arm["settings"] if k not in defaults)
            check(f"arm {label} sets only real settings", not bad, str(bad))

    mpath = MELD_B / "bench" / "matrix.json"
    if mpath.exists():
        matrix = json.loads(mpath.read_text(encoding="utf-8"))
        ws = matrix.get("world_settings") or {}
        # elevation_mode is the one deliberate difference: bench_scheduler locks a global range
        # via /api/elevation/manual, this harness runs per-cell local. Everything else that both
        # files name must agree, or the two harnesses are measuring two different worlds.
        allowed_diff = {"elevation_mode"}
        shared = sorted((set(COMMON) & set(ws)) - allowed_diff)
        diff = {k: (COMMON[k], ws[k]) for k in shared if COMMON[k] != ws[k]}
        check("COMMON and bench/matrix.json agree on every shared world setting", not diff,
              "; ".join(f"{k}: harness {a!r} vs matrix {b!r}" for k, (a, b) in diff.items()))
        for k in sorted(allowed_diff & set(COMMON) & set(ws)):
            if COMMON[k] != ws[k]:
                print(f"  note: {k} differs by design (harness {COMMON[k]!r}, "
                      f"matrix {ws[k]!r}) - different elevation locking, same world shape")
        check("matrix.json declares the measured arms",
              ws.get("native_region_format") == "blinear" and ws.get("bake_lighting") is True
              and ws.get("buildings") is False and ws.get("interior") is False,
              json.dumps({k: ws.get(k) for k in ("buildings", "interior", "bake_lighting",
                                                 "native_region_format")}))
    else:
        print(f"  note: {mpath} not found - matrix parity skipped")

    # H1, replayed against the real corruption.
    check("a cs4 report harvested as cs8 is refused",
          report_mismatch({"cell_size": 4, "total": 81}, 8, 16) != [])
    check("a matching report passes",
          report_mismatch({"cell_size": 4, "total": 81}, 4, 81) == [])
    check("a queued-count mismatch is caught too",
          report_mismatch({"cell_size": 4, "total": 106}, 4, 81) != [])
    corrupt = (AB_ROOT / "data" / "projects" / "ab-perf-governor-cs8"
               / "ab-perf-governor-cs8" / "meld-report.json")
    if corrupt.exists():
        sm = json.loads(corrupt.read_text(encoding="utf-8")).get("summary") or {}
        probs = report_mismatch(sm, 8)
        check("the phase-1 cs8 report on disk is now detected as foreign", probs != [],
              f"cell_size={sm.get('cell_size')} total={sm.get('total')}")
        print(f"    ({corrupt}: cell_size={sm.get('cell_size')} total={sm.get('total')} "
              f"elapsed_s={sm.get('elapsed_s')} -> {probs})")
    else:
        print(f"  note: {corrupt} is gone - the on-disk replay is skipped")

    # schema/3 vs schema/4 reading.
    check("schema/4 timers are read", _timers({"timers": {"merge_s": 3.0, "prune_s": 1.5,
                                                          "health_s": 0.25, "meta_s": 0.25}}
                                              )["post_arnis_total_s"] == 5.0)
    check("schema/3 timers read as absent, not zero", _timers({}) == {})
    check("a null timer is dropped", _timers({"timers": {"merge_s": None}}) == {})
    check("cells_per_min is a number or None", _num("x") is None and _num(2) == 2.0)

    print("")
    print('all checks passed' if not fails else f'{len(fails)} CHECK(S) FAILED')
    return 0 if not fails else 1


def mode_report():
    rows = []
    for f in sorted(OUT.glob("[AB]-*.json")):
        rows.append(json.loads(f.read_text(encoding="utf-8")))
    if not rows:
        print("no results yet")
        return
    hdr = ["arm", "cells", "elapsed_s", "cells/min", "eff_par", "w_set", "w_peak",
           "median_s", "p95_s", "cpu%", "ram%", "disk_mb"]
    print("\n| " + " | ".join(hdr) + " |")
    print("|" + "|".join("---" for _ in hdr) + "|")
    for r in rows:
        bad = r.get("harvest_ok") is False or r.get("error")
        print("| " + " | ".join(str(x) for x in [
            f"{r.get('arm_label')} {r.get('run')}{' warm' if r.get('warm_start') else ''}"
            f"{' **UNUSABLE**' if bad else ''}",
            r.get("cells_merged"), r.get("elapsed_s"),
            r.get("cells_per_min"), r.get("eff_parallelism"), r.get("workers_setting"),
            r.get("workers_peak"), r.get("cell_median_s"), r.get("cell_p95_s"),
            r.get("cpu_avg"), r.get("ram_peak"), r.get("on_disk_mb")]) + " |")
    unusable = [r for r in rows if r.get("harvest_ok") is False or r.get("error")]
    if unusable:
        print()
        for r in unusable:
            print(f"UNUSABLE  {r.get('arm_label')} {r.get('run')}: {r.get('error')}")
        print("Those rows are excluded from the speedups below.")
    ok_rows = [r for r in rows if not (r.get("harvest_ok") is False or r.get("error"))]
    # Keyed on warm_start too. `B-cs4.json` and `B-cs4-warm.json` are two different runs of the
    # same arm, and collapsing them onto one key silently compared arm A cold against arm B warm.
    by = {(r.get("arm"), r.get("run"), bool(r.get("warm_start"))): r for r in ok_rows}
    print()
    for run in RUNS:
        a, b = by.get(("A", run["id"], False)), by.get(("B", run["id"], False))
        if a and b and a.get("elapsed_s") and b.get("elapsed_s"):
            sp = a["elapsed_s"] / b["elapsed_s"]
            print(f"{run['id']}: perf is {sp:.2f}x  "
                  f"({a['elapsed_s']}s -> {b['elapsed_s']}s), "
                  f"ram {a.get('ram_peak')}% -> {b.get('ram_peak')}%")
        w = by.get(("B", run["id"], True))
        if w and b and w.get("elapsed_s") and b.get("elapsed_s"):
            print(f"{run['id']}: perf warm vs perf cold "
                  f"{b['elapsed_s'] / w['elapsed_s']:.2f}x  "
                  f"({b['elapsed_s']}s -> {w['elapsed_s']}s)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)
    cmd = sys.argv[1]
    if cmd == "warm":
        mode_warm()
    elif cmd == "run":
        mode_run(sys.argv[2].upper())
    elif cmd == "check":
        mode_check(sys.argv[2].upper() if len(sys.argv) > 2 else "A")
    elif cmd == "verify":
        sys.exit(mode_verify())
    elif cmd == "report":
        mode_report()
    else:
        print(__doc__)
