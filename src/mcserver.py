"""One-click Leaf server setup from a generated Meld world.

Everything needed to turn the project's finished world into a ready-to-run Leaf
(Paper fork) server: version catalog, format-aware config generation, directory
staging, hash-verified jar/plugin downloads, process lifecycle, and first-start
console automation (Multiverse import + border-file push) — all driven through
the server's own stdin, no RCON anywhere.

Verified facts this module is built on (2026-07-02):
  - Leaf publishes a PaperMC-style downloads API at api.leafmc.one/v2 with
    versions 1.21.4 .. 26.2, per-version builds, and sha256 for every jar.
  - Every plugin below is on Modrinth's v2 API with paper-compatible builds and
    sha512-hashed CDN files (classic Vault is NOT — VaultUnlocked replaces it).
  - Leaf's config/leaf-global.yml region-format accepts MCA | LINEAR_V2 | B_LINEAR
    (read from a working Leaf 26.1.2 install), mapping 1:1 onto Meld's export
    formats (mca/zip/tarzst -> MCA, linear -> LINEAR_V2, blinear -> B_LINEAR).
  - Multiverse-Core's supported path for adopting an existing world folder is
    `mv import <name> <env>` — it persists worlds.yml itself.

SAFETY: nothing here downloads or executes anything unless the caller passes the
explicit confirmation flags checked in server.py's routes. EULA acceptance is a
separate, deliberate action that writes eula=true only when asked.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path

UA = {"User-Agent": "Meld/1.0 (world builder; server-setup)"}
LEAF_API = "https://api.leafmc.one/v2/projects/leaf"
MODRINTH_API = "https://api.modrinth.com/v2"

# Live-verified fallback (the API's own list on 2026-07-02) — used when offline.
FALLBACK_VERSIONS = ["1.21.4", "1.21.5", "1.21.6", "1.21.7", "1.21.8", "1.21.11", "26.1.2", "26.2"]

# Plugins border.sk needs (border.py's own REQUIRES list), all verified on Modrinth.
BORDER_PLUGINS = ["worldguard", "worldedit", "skript", "skworldguard", "skbee"]
# Sub-world mounting needs Multiverse; extras are optional quality-of-life.
SUBWORLD_PLUGINS = ["multiverse-core"]
EXTRA_PLUGINS = ["vaultunlocked", "essentialsx"]
# Voxy Server Side streams LOD terrain to Voxy clients straight from the world's
# region files (no server-side pre-bake exists — verified from its docs + a live
# install whose plugin folder holds ONLY a config). It requires the client and
# server MC versions to MATCH (no ViaVersion), so it is resolved exact-only.
VOXY_PLUGIN = "voxy-server-side"

_CACHE: dict = {}


def _get_json(url: str, timeout: int = 20):
    key = ("json", url)
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    _CACHE[key] = (time.time(), data)
    return data


# ── Stage 1: version catalog ─────────────────────────────────────────────────

def list_versions() -> dict:
    """All Leaf versions (1.21.4 .. latest), newest last, plus where the list came from."""
    try:
        data = _get_json(LEAF_API)
        vers = [v for v in data.get("versions", []) if v]
        if vers:
            return {"versions": vers, "source": "api.leafmc.one"}
    except Exception as e:  # noqa: BLE001 — offline is a normal condition
        return {"versions": FALLBACK_VERSIONS, "source": f"bundled fallback ({e.__class__.__name__})"}
    return {"versions": FALLBACK_VERSIONS, "source": "bundled fallback (empty api reply)"}


def latest_build(version: str) -> dict:
    """Newest build for a version + its primary jar name and sha256."""
    builds = _get_json(f"{LEAF_API}/versions/{urllib.parse.quote(version)}/builds")["builds"]
    b = builds[-1]
    detail = b if isinstance(b, dict) else _get_json(
        f"{LEAF_API}/versions/{urllib.parse.quote(version)}/builds/{b}")
    dl = detail["downloads"]["primary"] if isinstance(detail, dict) and "downloads" in detail else None
    if not dl:
        raise RuntimeError(f"no primary download for leaf {version} build {b}")
    build_no = detail.get("build", b) if isinstance(detail, dict) else b
    return {
        "version": version,
        "build": build_no,
        "jar_name": dl["name"],
        "sha256": dl.get("sha256"),
        "url": f"{LEAF_API}/versions/{urllib.parse.quote(version)}/builds/{build_no}/downloads/{dl['name']}",
    }


def _mc_family(version: str) -> str:
    """'26.1.2' -> '26.1', '1.21.8' -> '1.21' — used for plugin-version fallback."""
    parts = version.split(".")
    return ".".join(parts[:2])


def resolve_plugin(slug: str, mc_version: str, exact_only: bool = False) -> dict:
    """Pick the newest Modrinth version of `slug` compatible with `mc_version`
    (exact match preferred, same-family fallback), returning the primary file.
    exact_only skips the family fallback — for plugins like Voxy Server Side
    that hard-require the exact MC version."""
    loaders = urllib.parse.quote(json.dumps(["paper", "bukkit", "spigot", "folia"]))
    exact = urllib.parse.quote(json.dumps([mc_version]))
    versions = _get_json(f"{MODRINTH_API}/project/{slug}/version?game_versions={exact}&loaders={loaders}")
    # newest-first from the API; prefer a stable release over beta/alpha/pre when one exists
    stable = [v for v in versions if v.get("version_type") == "release"]
    picked, match = ((stable or versions)[0], "exact") if versions else (None, None)
    if picked is None and exact_only:
        raise RuntimeError(f"{slug}: no build for exactly MC {mc_version} "
                           f"(it requires matching client/server versions)")
    if picked is None:
        fam = _mc_family(mc_version)
        fallback = [v for v in _get_json(f"{MODRINTH_API}/project/{slug}/version?loaders={loaders}")
                    if any(g == fam or g.startswith(fam + ".") for g in v.get("game_versions", []))]
        fb_stable = [v for v in fallback if v.get("version_type") == "release"]
        if fallback:
            picked, match = (fb_stable or fallback)[0], f"family {fam}"
    if picked is None:
        raise RuntimeError(f"{slug}: no build compatible with MC {mc_version}")
    f = next((x for x in picked["files"] if x.get("primary")), picked["files"][0])
    return {
        "slug": slug,
        "plugin_version": picked["version_number"],
        "match": match,
        "file": f["filename"],
        "url": f["url"],
        "sha512": (f.get("hashes") or {}).get("sha512"),
    }


# ── Stage 2: format-aware config generation ──────────────────────────────────

REGION_FORMAT_BY_EXPORT = {
    "none": "MCA", "zip": "MCA", "tarzst": "MCA",
    "linear": "LINEAR_V2", "blinear": "B_LINEAR",
}

AIKAR_FLAGS = (
    "-XX:+UseG1GC -XX:+ParallelRefProcEnabled -XX:MaxGCPauseMillis=200 "
    "-XX:+UnlockExperimentalVMOptions -XX:+DisableExplicitGC -XX:G1HeapRegionSize=8M "
    "-XX:G1NewSizePercent=30 -XX:G1MaxNewSizePercent=40 -XX:G1ReservePercent=20 "
    "-XX:InitiatingHeapOccupancyPercent=15 "
    "-Dusing.aikars.flags=https://mcflags.emc.gs -Daikars.new.flags=true"
)


def write_server_properties(dest: Path, *, motd: str, port: int = 25565, world_name: str = "world",
                            gamemode: str = "survival", difficulty: str = "normal",
                            view_distance: int = 10, max_players: int = 20) -> None:
    """Survival-friendly defaults; binds 127.0.0.1:25565 + offline mode — Meld only
    ever sets up a LOCAL server. Any change beyond that (exposure, whitelist, ports)
    is the owner's own edit in this file."""
    txt = "\n".join([
        "# Generated by Meld — local server profile (127.0.0.1:25565, offline mode).",
        "# Meld never changes these. Anything beyond local play (opening the port,",
        "# online-mode, whitelist) is your own edit here.",
        f"motd={motd}",
        f"level-name={world_name}",
        "server-ip=127.0.0.1",
        f"server-port={port}",
        f"gamemode={gamemode}",
        f"difficulty={difficulty}",
        "online-mode=false",
        "spawn-monsters=true",
        "generate-structures=false",
        f"view-distance={view_distance}",
        "simulation-distance=6",
        f"max-players={max_players}",
        "spawn-protection=0",
        "allow-flight=true",
        "allow-nether=false",
        "enable-command-block=true",
        "white-list=false",
        "enable-rcon=false",
    ]) + "\n"
    dest.write_text(txt, encoding="utf-8")


def write_leaf_global(dest: Path, fmt: str) -> str:
    """Minimal leaf-global.yml carrying ONLY the region-format block (Leaf's config
    updater merges in every other default on first boot — Paper-config convention).
    `fmt` is the final region-format name (MCA / LINEAR_V2 / B_LINEAR), already
    matched to the world files being staged."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        "# Generated by Meld — only the region-format block is pinned; Leaf fills in\n"
        "# the rest of its defaults on first boot.\n"
        "config-version: '3.0'\n"
        "misc:\n"
        "  region-format:\n"
        "    # Available region format names: MCA, B_LINEAR, LINEAR_V2\n"
        f"    format-name: {fmt}\n"
        "    compress-level: 6\n",
        encoding="utf-8",
    )
    return fmt


def write_eula_stub(dest: Path) -> None:
    dest.write_text(
        "# Generated by Meld. Running the server requires agreeing to Mojang's EULA:\n"
        "# https://aka.ms/MinecraftEULA\n"
        "# Meld will only set this to true after you explicitly accept it in the UI.\n"
        "eula=false\n",
        encoding="utf-8",
    )


def accept_eula(server_dir: Path, confirmed: bool) -> None:
    """Flips eula.txt to true — ONLY with an explicit confirmation from the user."""
    if not confirmed:
        raise PermissionError("EULA acceptance requires explicit confirmation")
    (server_dir / "eula.txt").write_text(
        "# Accepted via Meld on explicit user confirmation. https://aka.ms/MinecraftEULA\n"
        "eula=true\n",
        encoding="utf-8",
    )


def write_vss_config(server_dir: Path, lod_distance_chunks: int = 256) -> None:
    """Voxy Server Side config (plugins/VoxyServerSide/vss-server-config.json) —
    values copied from a working install, with chunk generation ON so ungenerated
    edges fill in when streamed/pre-rendered."""
    dest = server_dir / "plugins" / "VoxyServerSide" / "vss-server-config.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps({
        "enabled": True,
        "lodDistanceChunks": lod_distance_chunks,
        "bytesPerSecondLimitPerPlayer": 20971520,
        "diskReaderThreads": 5,
        "sendQueueLimitPerPlayer": 4000,
        "bytesPerSecondLimitGlobal": 104857600,
        "enableChunkGeneration": True,
        "generationConcurrencyLimitGlobal": 32,
        "generationTimeoutSeconds": 60,
        "syncOnLoadRateLimitPerPlayer": 800,
        "syncOnLoadConcurrencyLimitPerPlayer": 200,
        "generationRateLimitPerPlayer": 80,
        "generationConcurrencyLimitPerPlayer": 16,
        "perDimensionTimestampCacheSizeMB": 32,
        "dirtyBroadcastIntervalSeconds": 10,
    }, indent=2), encoding="utf-8")


def launch_resources(ram_gb: int, cpu_pct: int, total_ram_gb: int, cores: int) -> tuple[str, int]:
    """(heap size string, JVM processor count) from the user's knobs, clamped to the
    machine. ram_gb 0 = auto (a quarter of RAM, 2..8 GB); heap is always capped so at
    least 2 GB stays for the OS + Meld. cpu_pct maps to -XX:ActiveProcessorCount, the
    lever the JVM actually honours (it sizes GC + worker thread pools)."""
    cap = max(1, int(total_ram_gb) - 2)
    gb = int(ram_gb) if ram_gb else min(8, max(2, round(total_ram_gb / 4)))
    gb = max(1, min(cap, gb))
    n = max(1, min(int(cores), round(int(cores) * max(10, min(100, int(cpu_pct))) / 100)))
    return f"{gb}G", n


def _cpu_flag(cpu_count: int | None) -> str:
    return f"-XX:ActiveProcessorCount={cpu_count} " if cpu_count else ""


def write_start_scripts(server_dir: Path, jar_name: str, java_exe: str, xms: str = "2G",
                        xmx: str = "4G", cpu_count: int | None = None) -> None:
    cpu = _cpu_flag(cpu_count)
    (server_dir / "start.bat").write_text(
        "@echo off\r\ncd /d \"%~dp0\"\r\n"
        f"set \"JAVA={java_exe}\"\r\n"
        "if not exist \"%JAVA%\" set \"JAVA=java\"\r\n"
        f"\"%JAVA%\" -Xms{xms} -Xmx{xmx} {cpu}{AIKAR_FLAGS} -jar {jar_name} --nogui\r\npause\r\n",
        encoding="utf-8",
    )
    (server_dir / "start.sh").write_text(
        "#!/bin/sh\ncd \"$(dirname \"$0\")\"\n"
        f"java -Xms{xms} -Xmx{xmx} {cpu}{AIKAR_FLAGS} -jar {jar_name} --nogui\n",
        encoding="utf-8",
    )


# ── Stage 3: staging ─────────────────────────────────────────────────────────

def _dir_size(p: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def resolve_world_source(master_world: Path, export_format: str, export_destination: str) -> Path:
    """The folder that actually holds the server-ready world for the chosen format
    (the '[Linear]'/'[BLinear]' sibling when the export built one)."""
    if export_format == "linear" and export_destination == "separate":
        cand = master_world.parent / f"{master_world.name} [Linear]"
        if cand.is_dir():
            return cand
    if export_format == "blinear":
        cand = master_world.parent / f"{master_world.name} [BLinear]"
        if cand.is_dir():
            return cand
    return master_world


def pick_world_source(master_world: Path, export_format: str, export_destination: str,
                      choice: str = "auto") -> tuple[Path, str]:
    """(world folder, Leaf region-format) for the server — the two must always agree,
    so they are decided TOGETHER here. choice: auto (follow the Export settings) |
    mca | linear | blinear (explicit picks, validated against the files on disk).
    Voxy note: vss reads chunks through the server's own storage API (verified:
    its PaperChunkDiskReader goes ServerLevel -> ChunkMap, no direct .mca parsing),
    so every region format Leaf supports works with it."""
    def _has(p: Path, ext: str) -> bool:
        return p.is_dir() and any((p / "region").glob(f"r.*.*{ext}"))
    lin = master_world.parent / f"{master_world.name} [Linear]"
    blin = master_world.parent / f"{master_world.name} [BLinear]"
    if choice == "mca":
        if not _has(master_world, ".mca"):
            raise RuntimeError("no .mca region files in the master world")
        return master_world, "MCA"
    if choice == "linear":
        if _has(lin, ".linear"):
            return lin, "LINEAR_V2"
        if _has(master_world, ".linear"):
            return master_world, "LINEAR_V2"
        raise RuntimeError("no Linear world found — run Export with format = linear first")
    if choice == "blinear":
        if _has(blin, ".b_linear"):
            return blin, "B_LINEAR"
        raise RuntimeError("no [BLinear] world found — run Export with format = blinear first")
    src = resolve_world_source(master_world, export_format, export_destination)
    return src, REGION_FORMAT_BY_EXPORT.get(export_format, "MCA")


def safe_world_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]+", "_", name).strip("_") or "meld_world"


def stage_server(server_dir: Path, world_src: Path, *, mode: str, world_name: str,
                 region_format: str, motd: str, port: int, jar_name: str, java_exe: str,
                 with_voxy: bool = False, heap: str = "4G", cpu_count: int | None = None) -> dict:
    """Create the server directory: configs + the world copy. `mode` = 'main'
    (world becomes level-name=world) or 'subworld' (copied under its own name,
    imported via Multiverse on first start)."""
    server_dir.mkdir(parents=True, exist_ok=True)
    (server_dir / "plugins").mkdir(exist_ok=True)

    # disk preflight: need world size + slack (mirrors export.preflight_export's intent)
    need = _dir_size(world_src) + 512 * 1024 * 1024
    free = shutil.disk_usage(server_dir).free
    if free < need:
        raise RuntimeError(f"not enough disk: need ~{need // 2**20} MB, have {free // 2**20} MB free")

    target_name = "world" if mode == "main" else safe_world_name(world_name)
    dest = server_dir / target_name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(world_src, dest)
    # a staged copy must never carry a session lock from a client that had it open
    for lock in dest.glob("session.lock"):
        try:
            lock.unlink()
        except OSError:
            pass

    write_server_properties(server_dir / "server.properties", motd=motd, port=port,
                            world_name="world")
    fmt = write_leaf_global(server_dir / "config" / "leaf-global.yml", region_format)
    if not (server_dir / "eula.txt").exists():
        write_eula_stub(server_dir / "eula.txt")
    write_start_scripts(server_dir, jar_name, java_exe, xms=heap, xmx=heap, cpu_count=cpu_count)
    if with_voxy:
        write_vss_config(server_dir)
    return {"world_dir": str(dest), "region_format": fmt, "mode": mode, "target_world": target_name}


# ── Stage 4: downloads (hash-verified) ───────────────────────────────────────

def download_file(url: str, dest: Path, *, sha256: str | None = None, sha512: str | None = None,
                  on_progress=None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers=UA)
    h256, h512 = hashlib.sha256(), hashlib.sha512()
    done = 0
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            h256.update(chunk)
            h512.update(chunk)
            done += len(chunk)
            if on_progress:
                on_progress(done, total)
    if sha256 and h256.hexdigest() != sha256:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"sha256 mismatch for {dest.name} — download discarded")
    if sha512 and h512.hexdigest() != sha512:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"sha512 mismatch for {dest.name} — download discarded")
    tmp.replace(dest)


def build_plan(version: str, *, mode: str, with_extras: bool, with_voxy: bool = False) -> dict:
    """Dry run: resolve the exact jar + plugin set (names, URLs, hashes) for review.
    Downloads NOTHING."""
    jar = latest_build(version)
    slugs = list(BORDER_PLUGINS)
    if mode == "subworld":
        slugs += SUBWORLD_PLUGINS
    if with_extras:
        slugs += EXTRA_PLUGINS
    plugins, problems = [], []
    for s in slugs:
        try:
            plugins.append(resolve_plugin(s, version))
        except Exception as e:  # noqa: BLE001 — collect, show the user, let them decide
            problems.append({"slug": s, "error": str(e)})
    if with_voxy:
        try:
            plugins.append(resolve_plugin(VOXY_PLUGIN, version, exact_only=True))
        except Exception as e:  # noqa: BLE001
            problems.append({"slug": VOXY_PLUGIN, "error": str(e)})
    return {"jar": jar, "plugins": plugins, "problems": problems,
            "voxy": with_voxy and any(p["slug"] == VOXY_PLUGIN for p in plugins)}


def install_plan(server_dir: Path, plan: dict, on_progress=None) -> list[str]:
    """Execute a previously-reviewed plan. Caller MUST have checked the user's confirm flag."""
    written = []
    jar = plan["jar"]
    dl = server_dir / jar["jar_name"]
    if on_progress:
        on_progress(f"downloading {jar['jar_name']}")
    download_file(jar["url"], dl, sha256=jar.get("sha256"))
    written.append(jar["jar_name"])
    for p in plan["plugins"]:
        if on_progress:
            on_progress(f"downloading {p['file']}")
        download_file(p["url"], server_dir / "plugins" / p["file"], sha512=p.get("sha512"))
        written.append(p["file"])
    return written


# ── Stage 5: java detection + process lifecycle ──────────────────────────────

def required_java(version: str) -> int:
    return 25 if not version.startswith("1.") else 21


def _java_major(exe: str) -> int | None:
    try:
        out = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=15)
        m = re.search(r'version "(\d+)', (out.stderr or "") + (out.stdout or ""))
        return int(m.group(1)) if m else None
    except Exception:  # noqa: BLE001
        return None


def find_java(min_major: int) -> dict | None:
    """Probe known JRE locations + PATH; return the first one that satisfies min_major.
    NEVER downloads a runtime — if none is found the caller shows instructions instead."""
    candidates: list[str] = []
    meta = Path(os.environ.get("APPDATA", "")) / "ModrinthApp" / "meta" / "java_versions"
    if meta.is_dir():
        candidates += [str(p) for p in sorted(meta.glob("*/bin/java.exe"), reverse=True)]
    candidates.append("java")
    for c in candidates:
        major = _java_major(c)
        if major and major >= min_major:
            return {"exe": c, "major": major}
    return None


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


class ServerProc:
    """A launched Leaf server: console ring buffer, stdin command channel, clean stop."""

    def __init__(self, server_dir: Path, java_exe: str, jar_name: str,
                 on_line=None, on_ready=None, on_exit=None, xms: str = "2G", xmx: str = "4G",
                 cpu_count: int | None = None):
        cmd = [java_exe, f"-Xms{xms}", f"-Xmx{xmx}",
               *([f"-XX:ActiveProcessorCount={cpu_count}"] if cpu_count else []),
               *AIKAR_FLAGS.split(), "-jar", jar_name, "--nogui"]
        self.console: deque[str] = deque(maxlen=500)
        self.ready = False
        self.user_stop = False   # set by stop() so the crash watchdog can tell stop from crash
        self._on_ready_fired = False
        self.proc = subprocess.Popen(
            cmd, cwd=str(server_dir), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1,
        )

        def _reader():
            for line in self.proc.stdout:  # type: ignore[union-attr]
                line = line.rstrip("\n")
                self.console.append(line)
                if on_line:
                    on_line(line)
                # vanilla/paper ready marker: `Done (12.345s)! For help, type "help"`
                if not self._on_ready_fired and "Done (" in line:
                    self.ready = True
                    self._on_ready_fired = True
                    if on_ready:
                        threading.Thread(target=on_ready, daemon=True).start()
            # stdout closed = process is exiting; report how, once it's fully gone
            code = self.proc.wait()
            if on_exit:
                on_exit(code, self.user_stop)

        threading.Thread(target=_reader, daemon=True, name="mcserver-console").start()

    def send(self, command: str) -> None:
        if self.proc.poll() is not None:
            raise RuntimeError("server not running")
        assert self.proc.stdin is not None
        self.proc.stdin.write(command.strip() + "\n")
        self.proc.stdin.flush()

    def stop(self, timeout: int = 45) -> int | None:
        """`stop` on the console = clean world save; terminate only as a last resort."""
        self.user_stop = True
        if self.proc.poll() is None:
            try:
                self.send("stop")
                self.proc.wait(timeout=timeout)
            except Exception:  # noqa: BLE001
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=10)
                except Exception:  # noqa: BLE001
                    self.proc.kill()
        return self.proc.poll()

    def alive(self) -> bool:
        return self.proc.poll() is None


# ── Stage 6: first-start automation ──────────────────────────────────────────

def push_border_files(server_dir: Path, border_dir: Path, target_world: str) -> list[str]:
    """Copy the already-generated border exports into the live plugin folders:
    regions.yml -> plugins/WorldGuard/worlds/<world>/, border.sk -> plugins/Skript/scripts/."""
    pushed = []
    regions = border_dir / "regions.yml"
    if regions.is_file():
        dest = server_dir / "plugins" / "WorldGuard" / "worlds" / target_world
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(regions, dest / "regions.yml")
        pushed.append(f"regions.yml -> WorldGuard/worlds/{target_world}/")
    sk = border_dir / "border.sk"
    if sk.is_file():
        dest = server_dir / "plugins" / "Skript" / "scripts"
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sk, dest / "border.sk")
        pushed.append("border.sk -> Skript/scripts/")
    return pushed


def first_start_commands(mode: str, target_world: str, pushed_border: bool) -> list[str]:
    """Console commands to run once the server logs its Done line."""
    cmds = []
    if mode == "subworld":
        cmds.append(f"mv import {target_world} normal")
    if pushed_border:
        cmds.append("rg reload")
        cmds.append("sk reload border")
    return cmds


# ── world backup (zip, fast) ─────────────────────────────────────────────────

def backup_world(server_dir: Path, target_world: str, on_progress=None) -> Path:
    """Zip the staged server's world folder to backups/<world>-<n>.zip. Level 1:
    region data is already zlib/zstd-compressed, so heavier levels only cost time."""
    import zipfile
    src = server_dir / target_world
    if not src.is_dir():
        raise RuntimeError(f"world folder missing: {src}")
    bdir = server_dir / "backups"
    bdir.mkdir(exist_ok=True)
    n = 1
    while (bdir / f"{target_world}-{n}.zip").exists():
        n += 1
    dest = bdir / f"{target_world}-{n}.zip"
    files = [p for p in src.rglob("*") if p.is_file()]
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for i, p in enumerate(files):
            zf.write(p, str(p.relative_to(server_dir)))
            if on_progress and i % 50 == 0:
                on_progress(i + 1, len(files))
    return dest


