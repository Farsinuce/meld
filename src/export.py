"""
export.py — post-generation export/compression layer for the merged master world.

The format set was chosen from a real-world benchmark (see MELD_EXPORT_PLAN.md):

  • none   — raw .mca, vanilla single-player playable. DEFAULT. No-op here.
  • zip    — whole-world Deflate archive. Universal, extract → vanilla SP. ~1.85×.
  • tarzst — whole-world tar + zstd. Vanilla SP after extract, good for sharing. ~1.85×.
  • linear — per-region .linear (real Linear v1 format). zstd over the region's RAW NBT
             (chunk zlib stripped, sector padding gone) → ~4.85× at level 9. SERVER ONLY
             (Leaf/Folia read it natively; linear2mca converts back for vanilla SP).

Why linear is the only format that wins big: Anvil chunk payloads are ALREADY zlib-
compressed, so zip/tar/7z can only squeeze the 4 KiB sector padding (~1.85×). Linear
decompresses each chunk and re-compresses the raw NBT solid across the region → ~4.85×.

Format shapes:
  • linear   = PER-REGION. One r.X.Z.linear replaces one r.X.Z.mca. Streamable during
               generation (each merged region is final + disjoint) OR run as a post-pass.
  • zip/tarzst = WHOLE-WORLD container. Must be a post-pass (need the full world first).

SAFETY CONTRACT (never weakened):
  1. The raw .mca is written first (by the merge) and is NEVER mutated in place.
  2. Compress → temp file → fsync → atomic os.replace → fsync dir → verify the output.
  3. The raw is deleted ONLY after its compressed copy verifies, and ONLY if keep_both is
     off. keep_both is the safe default. Worst tolerable state = raw present, compressed
     missing (re-runnable).
  4. A persisted manifest (export_manifest.json) tracks per-artifact state so the whole
     export is resumable after a kill and can drive a progress view.
"""

from __future__ import annotations

import os
import queue
import struct
import sys
import tarfile
import threading
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import zstandard as zstd

# ── format identifiers ────────────────────────────────────────────────────────
FMT_NONE = "none"
FMT_ZIP = "zip"
FMT_TARZST = "tarzst"
FMT_LINEAR = "linear"
FMT_BLINEAR = "blinear"   # Buffered Linear (.b_linear), via the bundled Rust region-convert
VALID_FORMATS = (FMT_NONE, FMT_ZIP, FMT_TARZST, FMT_LINEAR, FMT_BLINEAR)

# Per-format default compression level when the UI leaves it at 0 (auto).
_DEFAULT_LEVEL = {FMT_ZIP: 6, FMT_TARZST: 9, FMT_LINEAR: 9, FMT_BLINEAR: 9, FMT_NONE: 0}
# Level clamp ranges.
_LEVEL_RANGE = {FMT_ZIP: (0, 9), FMT_TARZST: (1, 22), FMT_LINEAR: (1, 22),
                FMT_BLINEAR: (1, 22), FMT_NONE: (0, 0)}

# linear formats are "gating" (slow per byte) but parallelise across workers; archives
# are a single container. Both run after generation. Only linear streams per-region.
# blinear is a whole-world post-pass run by an external Rust binary (no overlap/stream).

# UI labels (tradeoff + purpose hint in parentheses).
FORMAT_LABELS = {
    FMT_NONE: "None / Raw .mca (vanilla playable, largest)",
    FMT_LINEAR: "Linear .linear (server only, smallest on disk — ~4.8×)",
    FMT_BLINEAR: "B_Linear .b_linear (Leaf B_LINEAR, Rust-fast ~2.5× — ~4.3×)",
    FMT_TARZST: "Portable tar.zst (vanilla playable, good for sharing — ~1.85×)",
    FMT_ZIP: "Zip (universal, vanilla playable — ~1.85×)",
}


def resolve_level(fmt: str, level) -> int:
    """0/blank → the format's sensible default; otherwise clamp into the valid band."""
    lo, hi = _LEVEL_RANGE.get(fmt, (0, 0))
    try:
        lv = int(level)
    except (TypeError, ValueError):
        lv = 0
    if lv <= 0:
        return _DEFAULT_LEVEL.get(fmt, 0)
    return max(lo, min(hi, lv))


def resolve_workers(setting, cores: int | None = None) -> int:
    """Default compression workers = logical cores − 1, reserving one thread, but only
    apply the minus-one when there are at least 4 cores. 0/blank/<=0 → that default.
    A positive setting is honoured (clamped to [1, 256]). Independent of generation
    workers by contract."""
    if cores is None:
        cores = os.cpu_count() or 4
    default = (cores - 1) if cores >= 4 else cores
    default = max(1, default)
    try:
        n = int(setting)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return default
    return max(1, min(256, n))


# ── disk estimate / preflight (safeguard A) ───────────────────────────────────
# Approx output ratios from the real benchmark (MELD_EXPORT_PLAN.md): linear ~4.8× → 0.22,
# blinear ~3-4.3× → 0.30 (conservative), zip/tar.zst ~1.85× → 0.56. Rounds the output UP.
_EST_OUT_FRAC = {FMT_LINEAR: 0.22, FMT_BLINEAR: 0.30, FMT_ZIP: 0.56,
                 FMT_TARZST: 0.56, FMT_NONE: 1.0}


def _dir_size(world_dir: Path) -> int:
    return sum(f.stat().st_size for f in Path(world_dir).rglob("*") if f.is_file())


def estimate_export(world_dir: str | Path, fmt: str, keep_both: bool = True) -> dict:
    """Estimate disk for an export. Returns bytes: raw (current world), out (compressed
    output), additional (extra free space the export will CONSUME beyond the raw already on
    disk), final (what remains after). The raw is already on disk, so `additional` is what
    the preflight must check free space against."""
    fmt = (fmt or FMT_NONE).strip().lower()
    raw = _dir_size(world_dir)
    out = int(raw * _EST_OUT_FRAC.get(fmt, 1.0))
    if fmt in (FMT_ZIP, FMT_TARZST):
        additional = out                 # one new archive beside the kept world
        final = raw + out
    elif fmt == FMT_BLINEAR:
        additional = out                 # a new sibling [BLinear] world; source kept
        final = raw + out
    elif fmt == FMT_LINEAR:
        if keep_both:
            additional = out             # .linear written alongside the kept .mca
            final = raw + out
        else:
            # delete-raw: each .linear written then its .mca freed → only a small transient bump
            additional = max(int(raw * 0.05), 64 * 1024 * 1024)
            final = out
    else:
        additional = 0
        final = raw
    return {"raw": raw, "out": out, "additional": additional, "final": final}


def free_space(path: str | Path) -> int | None:
    """Free bytes on the drive holding `path`. Climbs to the nearest EXISTING ancestor first,
    because shutil.disk_usage raises on a not-yet-created folder (a custom save location / deep
    world subpath) — without the climb the preflight would silently fail-open. None = offline."""
    import shutil
    try:
        p = Path(os.path.abspath(os.path.expanduser(str(path))))
    except Exception:  # noqa: BLE001
        return None
    while not p.exists():
        if p.parent == p:
            return None
        p = p.parent
    try:
        return shutil.disk_usage(str(p)).free
    except OSError:
        return None


def preflight_export(world_dir: str | Path, fmt: str, keep_both: bool = True,
                     safety: float = 1.15) -> dict:
    """Check free disk vs the estimated additional space (× a safety margin). Returns the
    estimate plus free/needed/enough so the caller can refuse or warn before starting."""
    est = estimate_export(world_dir, fmt, keep_both)
    free = free_space(Path(world_dir).parent)
    needed = int(est["additional"] * safety)
    enough = (free is None) or (free >= needed)
    return {**est, "free": free, "needed": needed, "enough": enough}


# ── low-level fs safety ───────────────────────────────────────────────────────
def _fsync_path(path: Path) -> None:
    """fsync a file by path (best-effort; ignored where unsupported)."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except (OSError, ValueError):
        pass


def _fsync_dir(d: Path) -> None:
    """fsync a directory so a rename is durable. No-op on Windows (can't open a dir fd)."""
    try:
        fd = os.open(str(d), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except (OSError, ValueError):
        pass  # Windows raises here; the os.replace itself is still atomic.


def atomic_write(final: Path, write_fn: Callable[[Path], None]) -> int:
    """Run write_fn(tmp) to produce <final>.tmp, fsync it, atomically replace <final>,
    fsync the directory. Returns the final file size. The temp is removed on any error
    so a half-written file never lands at the final name."""
    tmp = final.with_name(final.name + ".tmp")
    try:
        write_fn(tmp)
        _fsync_path(tmp)
        os.replace(tmp, final)          # atomic on the same filesystem
        _fsync_dir(final.parent)
        return final.stat().st_size
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# ── Linear v1 codec ───────────────────────────────────────────────────────────
# Real Linear region format (xymb / used by Leaf, Folia forks):
#   i64 SIGNATURE (0xc3ff13183cca9d9a) | i8 version(=1) | i64 newestTimestamp |
#   i8 compressionLevel | i16 chunkCount | i32 compressedLen | i64 hash(=0) |
#   <compressedLen bytes of zstd(blob)> | i64 SIGNATURE
# blob = 1024×(i32 size, i32 timestamp)  followed by  raw NBT of each present chunk
#        in MCA index order (index = (x & 31) + (z & 31) * 32).
LINEAR_SIGNATURE = 0xC3FF13183CCA9D9A
LINEAR_VERSION = 1
_SECTOR = 4096


def mca_to_linear(mca: bytes, level: int) -> bytes:
    """Encode standard Anvil region bytes into a Linear v1 region. Each chunk's zlib
    payload is inflated to raw NBT (this is where the big ratio comes from); chunks are
    re-packed with no per-chunk framing and no sector padding, then zstd-compressed."""
    sizes = [0] * 1024
    timestamps = [0] * 1024
    datas: list[bytes] = [b""] * 1024
    if len(mca) >= 2 * _SECTOR:
        for i in range(1024):
            off = struct.unpack(">I", b"\x00" + mca[i * 4 : i * 4 + 3])[0]
            cnt = mca[i * 4 + 3]
            ts = struct.unpack(">I", mca[_SECTOR + i * 4 : _SECTOR + i * 4 + 4])[0]
            if off == 0 or cnt == 0:
                continue
            start = off * _SECTOR
            if start + 5 > len(mca):
                continue
            length = struct.unpack(">I", mca[start : start + 4])[0]
            comp = mca[start + 4]
            payload = mca[start + 5 : start + 4 + length]
            try:
                raw = zlib.decompress(payload) if comp == 2 else (
                    zlib.decompress(payload, 47) if comp == 1 else payload
                )
            except zlib.error:
                raw = payload
            sizes[i] = len(raw)
            timestamps[i] = ts
            datas[i] = raw

    header = bytearray()
    for i in range(1024):
        header += struct.pack(">ii", sizes[i], timestamps[i])
    blob = bytes(header) + b"".join(d for d in datas if d)
    compressed = zstd.ZstdCompressor(level=level).compress(blob)

    chunk_count = sum(1 for s in sizes if s > 0)
    newest = max(timestamps) if timestamps else 0
    out = bytearray()
    out += struct.pack(">q", _as_i64(LINEAR_SIGNATURE))
    out += struct.pack(">b", LINEAR_VERSION)
    out += struct.pack(">q", newest)
    out += struct.pack(">b", max(-128, min(127, level)))
    out += struct.pack(">h", chunk_count if chunk_count <= 0x7FFF else 0x7FFF)
    out += struct.pack(">i", len(compressed))
    out += struct.pack(">q", 0)  # hash unused in v1
    out += compressed
    out += struct.pack(">q", _as_i64(LINEAR_SIGNATURE))
    return bytes(out)


def _as_i64(u: int) -> int:
    """Reinterpret an unsigned 64-bit value as signed for struct '>q'."""
    return u - (1 << 64) if u >= (1 << 63) else u


def verify_linear(data: bytes) -> bool:
    """Structural + integrity check: both superblocks present, version sane, the zstd
    blob decompresses, and its length equals 8192 + Σ chunk sizes. Strong enough to
    guarantee the file is a readable Linear region before any raw is deleted."""
    try:
        if len(data) < 8 + 1 + 8 + 1 + 2 + 4 + 8 + 8:
            return False
        sig = struct.unpack(">q", data[0:8])[0]
        if sig != _as_i64(LINEAR_SIGNATURE):
            return False
        ver = struct.unpack(">b", data[8:9])[0]
        if ver != LINEAR_VERSION:
            return False
        clen = struct.unpack(">i", data[20:24])[0]
        body_start = 32
        if body_start + clen + 8 > len(data):
            return False
        if struct.unpack(">q", data[body_start + clen : body_start + clen + 8])[0] != _as_i64(LINEAR_SIGNATURE):
            return False
        blob = zstd.ZstdDecompressor().decompress(
            data[body_start : body_start + clen], max_output_size=512 * 1024 * 1024
        )
        if len(blob) < 8192:
            return False
        total = 0
        for i in range(1024):
            total += struct.unpack(">i", blob[i * 8 : i * 8 + 4])[0]
        return len(blob) == 8192 + total
    except (struct.error, zstd.ZstdError):
        return False


def linear_decode(data: bytes) -> tuple[list[bytes], list[int]]:
    """Decode a Linear v1 region → (datas, timestamps), each length-1024, indexed by
    (x & 31) + (z & 31) * 32. datas[i] is the chunk's raw NBT (b'' if absent). The inverse
    of mca_to_linear's payload step. Raises on a malformed file."""
    if struct.unpack(">q", data[0:8])[0] != _as_i64(LINEAR_SIGNATURE):
        raise ValueError("linear: bad leading signature")
    ver = struct.unpack(">b", data[8:9])[0]
    if ver != LINEAR_VERSION:
        # Leaf's LINEAR_V2 implementation rewrites regions as version 3 once the server has
        # run the world; this codec reads only the classic v1 layout it writes itself.
        raise ValueError(
            f"linear: unsupported version {ver} (server-written v2/v3 file — convert it "
            "with the bundled region_converter / meldconvert.py, which reads all versions)")
    clen = struct.unpack(">i", data[20:24])[0]
    if struct.unpack(">q", data[32 + clen : 32 + clen + 8])[0] != _as_i64(LINEAR_SIGNATURE):
        raise ValueError("linear: bad trailing signature")
    blob = zstd.ZstdDecompressor().decompress(
        data[32 : 32 + clen], max_output_size=512 * 1024 * 1024)
    sizes = [struct.unpack(">i", blob[i * 8 : i * 8 + 4])[0] for i in range(1024)]
    timestamps = [struct.unpack(">i", blob[i * 8 + 4 : i * 8 + 8])[0] for i in range(1024)]
    datas: list[bytes] = [b""] * 1024
    pos = 8192
    for i in range(1024):
        if sizes[i] > 0:
            datas[i] = blob[pos : pos + sizes[i]]
            pos += sizes[i]
    return datas, timestamps


def linear_to_mca(data: bytes, zlib_level: int = 6) -> bytes:
    """Rebuild a standard Anvil .mca from a Linear v1 region (the reverse of
    mca_to_linear). Each chunk's raw NBT is re-deflated with zlib (Anvil compression
    type 2), framed, and packed into 4 KiB sectors with a fresh location + timestamp
    table. Lossless at the NBT level → vanilla single-player reads the result. A chunk
    too large for the 255-sector field is kept inline at its real size (caller verifies)."""
    datas, timestamps = linear_decode(data)
    loc = bytearray(_SECTOR)        # 1024 × (3-byte sector offset + 1-byte sector count)
    ts = bytearray(_SECTOR)         # 1024 × i32 timestamp
    body = bytearray()
    sector_cursor = 2               # sectors 0,1 are the two tables
    for i in range(1024):
        raw = datas[i]
        struct.pack_into(">i", ts, i * 4, timestamps[i] if raw else 0)
        if not raw:
            continue
        comp = zlib.compress(raw, zlib_level)
        record = struct.pack(">I", len(comp) + 1) + bytes([2]) + comp  # len, type=2(zlib), data
        pad = (-len(record)) % _SECTOR
        record += b"\x00" * pad
        n_sectors = len(record) // _SECTOR
        # Safeguard C: a chunk > 255 sectors (~1 MiB compressed) can't be addressed by the
        # 1-byte sector-count field — vanilla stores those in an external .mcc file. Rather than
        # silently truncate the count (→ a corrupt region), fail this region loudly so the
        # caller's verify catches it and the .linear is kept.
        if n_sectors > 255:
            raise ValueError(
                f"chunk index {i}: {n_sectors} sectors (>255) — oversized chunk needs an "
                f"external .mcc file, not supported by this converter")
        if sector_cursor > 0xFFFFFF:
            raise ValueError("region exceeds the 3-byte sector-offset limit (file too large)")
        # location entry: 3-byte big-endian offset, 1-byte sector count.
        off_bytes = struct.pack(">I", sector_cursor)[1:4]
        loc[i * 4 : i * 4 + 3] = off_bytes
        loc[i * 4 + 3] = n_sectors
        body += record
        sector_cursor += n_sectors
    return bytes(loc) + bytes(ts) + bytes(body)


def verify_mca_against_linear(mca: bytes, datas: list[bytes]) -> bool:
    """Confirm a rebuilt .mca inflates back to exactly the Linear region's raw NBT —
    the strong round-trip check before any .linear is deleted on convert."""
    if len(mca) < 2 * _SECTOR:
        return False
    for i in range(1024):
        off = struct.unpack(">I", b"\x00" + mca[i * 4 : i * 4 + 3])[0]
        cnt = mca[i * 4 + 3]
        if not datas[i]:
            if off != 0 or cnt != 0:
                return False
            continue
        if off == 0:
            return False
        start = off * _SECTOR
        length = struct.unpack(">I", mca[start : start + 4])[0]
        comp = mca[start + 4]
        payload = mca[start + 5 : start + 4 + length]
        try:
            raw = zlib.decompress(payload) if comp == 2 else payload
        except zlib.error:
            return False
        if raw != datas[i]:
            return False
    return True


# ── manifest ──────────────────────────────────────────────────────────────────
# Per-artifact states.
ST_PENDING = "pending"
ST_COMPRESSING = "compressing"
ST_COMPRESSED = "compressed"
ST_FREED = "freed"
ST_FAILED = "failed"


class Manifest:
    """Resumable per-artifact state, persisted atomically to export_manifest.json in the
    world dir. Keyed by artifact id (region name for linear; '__archive__' for archives)."""

    def __init__(self, world_dir: Path, meta: dict | None = None,
                 name: str = "export_manifest.json"):
        self.path = Path(world_dir) / name
        self._lock = threading.Lock()
        self.data: dict = {"meta": meta or {}, "artifacts": {}}
        if self.path.exists():
            try:
                import json
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
                self.data.setdefault("artifacts", {})
                if meta:
                    self.data["meta"] = {**self.data.get("meta", {}), **meta}
            except Exception:
                pass

    def _flush_locked(self) -> None:
        import json
        atomic_write(self.path, lambda tmp: tmp.write_text(
            json.dumps(self.data, indent=0), encoding="utf-8"))

    def set(self, art_id: str, **fields) -> None:
        with self._lock:
            a = self.data["artifacts"].setdefault(art_id, {})
            a.update(fields)
            self._flush_locked()

    def state(self, art_id: str) -> str | None:
        return (self.data["artifacts"].get(art_id) or {}).get("state")

    def is_done(self, art_id: str) -> bool:
        return self.state(art_id) in (ST_COMPRESSED, ST_FREED)

    def counts(self) -> dict:
        c: dict = {}
        for a in self.data["artifacts"].values():
            s = a.get("state", ST_PENDING)
            c[s] = c.get(s, 0) + 1
        return c


# ── progress callback payload ─────────────────────────────────────────────────
@dataclass
class ExportProgress:
    format: str
    total: int = 0
    done: int = 0
    failed: int = 0
    phase: str = "idle"          # idle | compressing | archiving | done | error
    raw_bytes: int = 0
    out_bytes: int = 0
    message: str = ""
    # live-run stats (blinear converter): regions/min over a sliding window, seconds
    # remaining at that rate (-1 = unknown), and wall seconds since the run started.
    rate_per_min: float = 0.0
    eta_s: int = -1
    elapsed_s: int = 0

    @property
    def ratio(self) -> float:
        return (self.raw_bytes / self.out_bytes) if self.out_bytes else 0.0


# ── per-region linear worker (streaming OR post-pass core) ────────────────────
def _compress_region_linear(
    mca_path: Path, level: int, keep_both: bool, free_raw: bool, manifest: Manifest,
    out_path: Path | None = None,
) -> tuple[int, int]:
    """Compress ONE r.X.Z.mca to r.X.Z.linear with the full safety contract. Returns
    (raw_size, out_size). Idempotent + resumable: a region already 'compressed' is
    skipped. The raw is removed only after the .linear verifies and only if allowed.

    out_path lets the caller write the .linear into a DIFFERENT folder (separate-folder
    export). When it is set, the source .mca lives in another directory and is NEVER
    freed — the originals stay an untouched vanilla world by construction."""
    art = mca_path.stem  # r.X.Z
    target = out_path if out_path is not None else mca_path.with_suffix(".linear")
    if manifest.is_done(art) and target.exists():
        return (0, target.stat().st_size)

    raw = mca_path.read_bytes()
    raw_size = len(raw)
    manifest.set(art, state=ST_COMPRESSING, raw=mca_path.name, raw_size=raw_size)

    encoded = mca_to_linear(raw, level)
    out_size = atomic_write(target, lambda tmp: tmp.write_bytes(encoded))

    # VERIFY before anything destructive.
    if not verify_linear(target.read_bytes()):
        manifest.set(art, state=ST_FAILED, out=target.name)
        raise ValueError(f"linear verify failed for {mca_path.name}")

    manifest.set(art, state=ST_COMPRESSED, out=target.name, out_size=out_size)

    # Only ever free the source when writing the .linear IN PLACE (out_path is None).
    if not keep_both and free_raw and out_path is None:
        try:
            mca_path.unlink()
            manifest.set(art, state=ST_FREED)
        except OSError:
            pass  # raw lingering is the safe failure; leave it.
    return (raw_size, out_size)


def _iter_region_mcas(world_dir: Path) -> list[Path]:
    region = Path(world_dir) / "region"
    if not region.is_dir():
        return []
    return sorted(region.glob("r.*.mca"))


class RegionExportPool:
    """Bounded-queue + N-thread pool for streaming per-region linear compression DURING
    generation. The merge enqueues each finalized (disjoint) region; consumers drain it
    so disk/RAM stay flat (backpressure via the bounded queue). finish() blocks until the
    queue empties. Used only for FMT_LINEAR overlap; archives use run_world_export."""

    def __init__(self, level: int, workers: int, keep_both: bool,
                 manifest: Manifest, on_progress: Callable[[ExportProgress], None] | None = None):
        # keep_both alone encodes the free policy: keep_both=False => free each raw inline after
        # its .linear verifies (the low-disk "stream & free" mode); keep_both=True keeps raws.
        self.level = level
        self.keep_both = keep_both
        self.manifest = manifest
        self.on_progress = on_progress
        self.prog = ExportProgress(format=FMT_LINEAR, phase="compressing")
        self._q: queue.Queue[Path | None] = queue.Queue(maxsize=max(2, workers * 2))
        self._lock = threading.Lock()
        self._threads = [threading.Thread(target=self._worker, daemon=True, name=f"linexp-{i}")
                         for i in range(max(1, workers))]
        self._started = False

    def start(self) -> None:
        if not self._started:
            self._started = True
            for t in self._threads:
                t.start()

    def submit(self, mca_path: Path) -> None:
        """Enqueue a finalized region (blocks if the bounded queue is full = backpressure)."""
        self.start()
        with self._lock:
            self.prog.total += 1
        self._q.put(mca_path)

    def _worker(self) -> None:
        while True:
            item = self._q.get()
            try:
                if item is None:
                    return
                try:
                    rs, os_ = _compress_region_linear(
                        item, self.level, self.keep_both,
                        free_raw=(not self.keep_both),  # stream_and_free => free inline
                        manifest=self.manifest)
                    with self._lock:
                        self.prog.done += 1
                        self.prog.raw_bytes += rs
                        self.prog.out_bytes += os_
                except Exception as e:
                    with self._lock:
                        self.prog.failed += 1
                        self.prog.message = str(e)
                if self.on_progress:
                    self.on_progress(self.prog)
            finally:
                self._q.task_done()

    def finish(self) -> ExportProgress:
        """Block until all enqueued regions are processed, then stop the threads."""
        if not self._started:
            return self.prog
        self._q.join()
        for _ in self._threads:
            self._q.put(None)
        for t in self._threads:
            t.join(timeout=30)
        self.prog.phase = "done"
        if self.on_progress:
            self.on_progress(self.prog)
        return self.prog


def _free_world_raws(world: Path) -> int:
    """Delete the raw region/poi/entities .mca of a world (the heavy bulk). Used by the
    ARCHIVE delete-raw path: called ONLY after the .zip/.tar.zst is closed + verified, so the
    archive already holds every byte. Leaves level.dat + the archive (= keep only compressed).
    Returns the number of files removed."""
    n = 0
    for sub in ("region", "poi", "entities"):
        d = Path(world) / sub
        if not d.is_dir():
            continue
        for f in d.glob("*.mca"):
            try:
                f.unlink()
                n += 1
            except OSError:
                pass
    return n


class ArchiveStreamWriter:
    """Single-writer streaming for zip / tar.zst DURING generation: as each region
    finalizes it is appended to ONE open container, so the archive build overlaps
    generation. Same start/submit/finish interface as RegionExportPool. Because a
    container is only valid once closed + verified, raws are NEVER freed here (keep-both
    is forced) — a crash leaves a discarded temp + intact raws (re-runnable). At finish()
    the rest of the world (level.dat, poi/, entities/, any region not yet streamed) is
    added, then the temp is fsync'd, atomically renamed, and verified."""

    def __init__(self, world_dir: Path, fmt: str, level: int, threads: int,
                 manifest: Manifest, on_progress: Callable[[ExportProgress], None] | None = None,
                 free_raw_at_end: bool = False):
        self.world = Path(world_dir)
        self.fmt = fmt
        self.level = level
        self.threads = threads
        self.manifest = manifest
        self.on_progress = on_progress
        # If the user chose delete-raw: raws are kept through the whole stream and removed
        # ONLY at the end, after the archive is closed + verified (never mid-stream).
        self.free_raw_at_end = free_raw_at_end
        ext = ".zip" if fmt == FMT_ZIP else ".tar.zst"
        self.dst = self.world.parent / (self.world.name + ext)
        self.tmp = self.dst.with_name(self.dst.name + ".tmp")
        self.arc_root = self.world.name
        self.prog = ExportProgress(format=fmt, phase="archiving")
        self._q: queue.Queue = queue.Queue(maxsize=64)
        self._added: set[str] = set()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True, name="arc-stream")
        self._zf = None
        self._zstream = None
        self._fh = None
        self._tf = None
        self._started = False
        self._error: Exception | None = None

    def _arcname(self, path: Path) -> str:
        return os.path.join(self.arc_root, str(Path(path).relative_to(self.world)))

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.manifest.set("__archive__", state=ST_COMPRESSING, out=self.dst.name)
        if self.fmt == FMT_ZIP:
            self._zf = zipfile.ZipFile(self.tmp, "w", compression=zipfile.ZIP_DEFLATED,
                                       compresslevel=self.level, allowZip64=True)
        else:
            self._fh = open(self.tmp, "wb")
            self._zstream = zstd.ZstdCompressor(level=self.level,
                                                threads=max(0, self.threads)).stream_writer(self._fh)
            self._tf = tarfile.open(fileobj=self._zstream, mode="w|")
        self._thread.start()

    def _add(self, path: Path) -> None:
        arc = self._arcname(path)
        if arc in self._added or not path.is_file():
            return
        if self.fmt == FMT_ZIP:
            self._zf.write(path, arcname=arc)
        else:
            self._tf.add(path, arcname=arc)
        self._added.add(arc)

    def submit(self, mca_path: Path) -> None:
        self.start()
        with self._lock:
            self.prog.total += 1
        self._q.put(mca_path)

    def _run(self) -> None:
        while True:
            item = self._q.get()
            try:
                if item is None:
                    return
                try:
                    self._add(Path(item))
                    with self._lock:
                        self.prog.done += 1
                except Exception as e:        # noqa: BLE001
                    self._error = e
                    with self._lock:
                        self.prog.failed += 1
                        self.prog.message = str(e)
                if self.on_progress:
                    self.on_progress(self.prog)
            finally:
                self._q.task_done()

    def finish(self) -> ExportProgress:
        if not self._started:
            self.prog.phase = "done"
            return self.prog
        self._q.join()
        self._q.put(None)
        self._thread.join(timeout=30)
        try:
            # add the rest of the world (level.dat, poi/, entities/, any region not streamed)
            for f in _world_files(self.world):
                self._add(f)
            if self.fmt == FMT_ZIP:
                self._zf.close()
            else:
                self._tf.close()
                self._zstream.close()
                self._fh.close()
            _fsync_path(self.tmp)
            os.replace(self.tmp, self.dst)
            _fsync_dir(self.dst.parent)
            ok = _verify_zip(self.dst) if self.fmt == FMT_ZIP else _verify_tarzst(self.dst)
            if not ok:
                raise ValueError("archive verify failed")
            size = self.dst.stat().st_size
            self.prog.out_bytes = size
            self.prog.raw_bytes = sum(f.stat().st_size for f in _world_files(self.world))
            self.manifest.set("__archive__", state=ST_COMPRESSED, out_size=size)
            # delete-raw for an archive: only NOW, after the container is closed + verified.
            if self.free_raw_at_end:
                freed = _free_world_raws(self.world)
                self.manifest.set("__archive__", state=ST_FREED, freed=freed)
            self.prog.phase = "done"
        except Exception as e:                # noqa: BLE001
            self.prog.phase = "error"
            self.prog.message = str(e)
            self.manifest.set("__archive__", state=ST_FAILED, error=str(e))
            try:
                self.tmp.unlink(missing_ok=True)
            except OSError:
                pass
        if self.on_progress:
            self.on_progress(self.prog)
        return self.prog


# ── archive builders (post-pass) ──────────────────────────────────────────────
def _world_files(world_dir: Path) -> list[Path]:
    """Every file under the world dir EXCEPT our own manifest/temp/archive outputs."""
    out = []
    skip_suffix = (".tmp",)
    skip_names = {"export_manifest.json"}
    for p in Path(world_dir).rglob("*"):
        if p.is_file() and p.suffix not in skip_suffix and p.name not in skip_names:
            out.append(p)
    return out


def _build_zip(world_dir: Path, dst: Path, level: int,
               on_file: Callable[[int, int], None] | None = None) -> int:
    files = _world_files(world_dir)
    base = Path(world_dir)
    arc_root = base.name

    def _w(tmp: Path) -> None:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=level, allowZip64=True) as zf:
            for i, f in enumerate(files):
                zf.write(f, arcname=os.path.join(arc_root, str(f.relative_to(base))))
                if on_file:
                    on_file(i + 1, len(files))

    return atomic_write(dst, _w)


def _build_tarzst(world_dir: Path, dst: Path, level: int, threads: int,
                  on_file: Callable[[int, int], None] | None = None) -> int:
    files = _world_files(world_dir)
    base = Path(world_dir)
    arc_root = base.name

    def _w(tmp: Path) -> None:
        cctx = zstd.ZstdCompressor(level=level, threads=max(0, threads))
        with open(tmp, "wb") as fh:
            with cctx.stream_writer(fh) as zw:
                with tarfile.open(fileobj=zw, mode="w|") as tf:
                    for i, f in enumerate(files):
                        tf.add(f, arcname=os.path.join(arc_root, str(f.relative_to(base))))
                        if on_file:
                            on_file(i + 1, len(files))

    return atomic_write(dst, _w)


def _verify_zip(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path, "r") as zf:
            return zf.testzip() is None and len(zf.namelist()) > 0
    except (zipfile.BadZipFile, OSError):
        return False


def _verify_tarzst(path: Path) -> bool:
    try:
        dctx = zstd.ZstdDecompressor()
        with open(path, "rb") as fh:
            with dctx.stream_reader(fh) as zr:
                with tarfile.open(fileobj=zr, mode="r|") as tf:
                    n = 0
                    for _ in tf:
                        n += 1
                    return n > 0
    except (tarfile.TarError, zstd.ZstdError, OSError):
        return False


# ── separate-folder export (originals stay an untouched vanilla world) ─────────
# Short, human-readable tag shown in the sibling folder name per format.
_FOLDER_TAG = {FMT_LINEAR: "Linear", FMT_BLINEAR: "BLinear", FMT_ZIP: "Zip", FMT_TARZST: "tar.zst"}


def format_folder_name(world_name: str, fmt: str) -> str:
    """Sibling folder name for a separate-folder export, e.g. 'Romania' + linear →
    'Romania [Linear]'. Same world name, format spelled out, so a player can keep the
    vanilla .mca world AND a compressed copy side by side without confusion."""
    tag = _FOLDER_TAG.get((fmt or "").strip().lower(), (fmt or "").strip().lower() or "Export")
    return f"{world_name} [{tag}]"


def _copy_world_skeleton(src: Path, dest: Path) -> None:
    """Copy every world file from src→dest EXCEPT region/*.mca (those become .linear) and
    our own manifests/temps/archives. So level.dat, poi/, entities/, data/, datapacks come
    across verbatim and the destination opens as a complete world."""
    import shutil
    src = Path(src)
    dest = Path(dest)
    for p in src.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(src)
        top = rel.parts[0] if rel.parts else ""
        if top == "region" and p.suffix in (".mca", ".linear"):
            continue                       # region payload is rebuilt, not copied
        if p.suffix in (".tmp", ".zip", ".zst") or p.name in (
                "export_manifest.json", "convert_manifest.json"):
            continue
        d = dest / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, d)


def export_linear_to_folder(
    src_world: Path, dest_world: Path, level: int, nworkers: int, manifest: Manifest,
    on_progress, should_stop, prog: ExportProgress,
) -> ExportProgress:
    """Build a parallel <name> [Linear]/ world: copy the skeleton, then convert each
    src region/*.mca into dest region/*.linear. The SOURCE world is never touched (the
    safest mode — keep-both is implicit). Resumable via the dest-folder manifest."""
    src = Path(src_world)
    dest = Path(dest_world)
    (dest / "region").mkdir(parents=True, exist_ok=True)
    prog.phase = "copying"
    if on_progress:
        on_progress(prog)
    _copy_world_skeleton(src, dest)

    mcas = _iter_region_mcas(src)
    prog.total = len(mcas)
    prog.phase = "compressing"
    if not mcas:
        prog.phase = "done"
        if on_progress:
            on_progress(prog)
        return prog

    work_q: queue.Queue[Path | None] = queue.Queue()
    for p in mcas:
        work_q.put(p)
    lock = threading.Lock()
    dest_region = dest / "region"

    def worker():
        while True:
            if should_stop and should_stop():
                return
            try:
                p = work_q.get_nowait()
            except queue.Empty:
                return
            try:
                if p is None:
                    return
                out = dest_region / (p.stem + ".linear")
                rs, os_ = _compress_region_linear(
                    p, level, keep_both=True, free_raw=False, manifest=manifest, out_path=out)
                with lock:
                    prog.done += 1
                    prog.raw_bytes += rs
                    prog.out_bytes += os_
            except Exception as e:        # noqa: BLE001
                with lock:
                    prog.failed += 1
                    prog.message = str(e)
            finally:
                work_q.task_done()
                if on_progress:
                    on_progress(prog)

    threads = [threading.Thread(target=worker, daemon=True, name=f"linfold-{i}")
               for i in range(max(1, nworkers))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    prog.phase = "done" if prog.failed == 0 else "error"
    if on_progress:
        on_progress(prog)
    return prog


# ── blinear (.b_linear) via the bundled Rust region-convert binary ────────────
def resolve_region_converter() -> Path | None:
    """Locate the region-convert binary for THIS OS/arch. Searches, in order:
    region-convert/bin/ (the bundled prebuilt, platform-tagged), region-convert/target/release/
    (a local `cargo build`), the light-meld root (legacy), then PATH. Cross-platform: .exe on
    Windows, bare on Linux/macOS; arch picks x86_64 vs arm64. Returns None if nothing is found."""
    import platform
    import shutil as _sh
    from .paths import exe_dir, resource_dir
    # Frozen, the converter ships next to Meld.exe (exe_dir); from source both of these are
    # the repo root, so the search order below is unchanged for a normal checkout.
    roots = list(dict.fromkeys([exe_dir(), resource_dir()]))
    sysname = sys.platform
    mach = (platform.machine() or "").lower()
    arm = mach in ("arm64", "aarch64")
    if sysname == "win32":
        names = ["region_converter-windows-arm64.exe", "region_converter.exe"] if arm \
            else ["region_converter.exe"]
        built = "region_converter.exe"
    elif sysname == "darwin":
        a = "arm64" if arm else "x86_64"
        names = [f"region_converter-macos-{a}", "region_converter-macos", "region_converter"]
        built = "region_converter"
    else:  # linux / other unix
        a = "arm64" if arm else "x86_64"
        names = [f"region_converter-linux-{a}", "region_converter-linux", "region_converter"]
        built = "region_converter"
    search = []
    for root in roots:
        r = root / "region-convert"
        search += [(r / "bin" / n) for n in names]
        search.append(r / "target" / "release" / built)
        search.append(root / built)                      # legacy: binary at the repo root
    for p in search:
        if p.is_file():
            _ensure_executable(p)
            return p
    found = _sh.which("region_converter") or _sh.which("region_converter.exe")
    return Path(found) if found else None


def _ensure_executable(p: Path) -> None:
    """Add the execute bit on Unix if it is missing.

    The bundled Linux converters were committed 100644. Git stores the execute bit, so every
    checkout and every PyInstaller build inherited a file that cannot be exec'd, and a B_Linear
    export died with `PermissionError: [Errno 13]` pointing at a binary that is plainly present -
    which reads like a corrupt download rather than a file mode. The repo modes are fixed; this
    stays as the belt-and-braces half, because it also covers a converter the user dropped in by
    hand and an archive that lost the bit in transit.
    """
    if sys.platform == "win32":
        return
    try:
        if not os.access(p, os.X_OK):
            p.chmod(p.stat().st_mode | 0o111)
    except Exception:
        pass                          # a read-only install still runs if the bit is already set


def _find_world_with_regions(root: Path, ext: str) -> Path | None:
    """Find the dir (under root) that holds region/<r.*.ext> — region-convert nests its output
    as <output>/<name>__<hash>/region/. Returns that world dir, or None."""
    for region in Path(root).rglob("region"):
        if region.is_dir() and any(region.glob(f"r.*{ext}")):
            return region.parent
    return None


def run_blinear_export(
    src_world: Path, dest_world: Path, variant: str = "v3", level: int = 9, workers: int = 0,
    on_progress: Callable[[ExportProgress], None] | None = None,
) -> ExportProgress:
    """Build a sibling <name> [BLinear]/ world by running the Rust region-convert binary
    (mca → blinear-vN), then relocating its nested output into a clean world: the source
    level.dat/poi/entities are copied verbatim and the produced region/*.b_linear are moved in.
    The SOURCE is never touched. Requires the bundled binary (resolve_region_converter)."""
    import subprocess
    src = Path(src_world)
    dest = Path(dest_world)
    prog = ExportProgress(format=FMT_BLINEAR, phase="converting")
    exe = resolve_region_converter()
    if exe is None:
        prog.phase = "error"
        prog.message = ("region-convert binary not found (region-convert/bin or build it). "
                        "blinear needs the Rust converter.")
        if on_progress:
            on_progress(prog)
        return prog

    mcas = _iter_region_mcas(src)
    prog.total = len(mcas)
    prog.raw_bytes = sum(p.stat().st_size for p in mcas)
    if on_progress:
        on_progress(prog)

    work = dest.with_name(dest.name + ".rcwork")
    import shutil as _sh
    import time as _time
    _sh.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    to_fmt = f"blinear-{variant}" if variant in ("v2", "v3") else "blinear-v3"
    cmd = [str(exe), "--to", to_fmt, "--output", str(work), str(src),
           "--compression-level", str(max(1, min(22, level)))]
    if workers and workers > 0:
        cmd += ["--threads", str(workers)]
    # Popen (not run): the converter takes an hour+ on big worlds, so poll the work dir
    # while it runs — each finished region appears as an atomically-renamed .b_linear —
    # and feed done / regions-per-min / ETA to the UI. Output goes to log files, not
    # pipes, so the child can never block on a full pipe with nobody reading it.
    out_log = work / "rc-stdout.log"
    err_log = work / "rc-stderr.log"
    try:
        with open(out_log, "wb") as so, open(err_log, "wb") as se:
            p = subprocess.Popen(cmd, stdout=so, stderr=se)
    except OSError as e:
        _sh.rmtree(work, ignore_errors=True)
        prog.phase = "error"
        prog.message = f"failed to launch region-convert: {e}"
        if on_progress:
            on_progress(prog)
        return prog

    t0 = _time.monotonic()
    samples: list[tuple[float, int]] = [(t0, 0)]   # (t, regions done) for the rate window
    _RATE_WINDOW_S = 120.0
    while p.poll() is None:
        _time.sleep(2.0)
        now = _time.monotonic()
        # only region/ output counts toward total (len of region/*.mca); the converter
        # may also emit entity/poi files which would skew the percentage
        try:
            done = sum(1 for _ in work.rglob("region/r.*.b_linear"))
        except OSError:
            done = prog.done   # work dir shuffled/removed under us — keep the last count
        samples.append((now, done))
        while len(samples) > 2 and now - samples[0][0] > _RATE_WINDOW_S:
            samples.pop(0)
        dt = now - samples[0][0]
        dr = max(0, done - samples[0][1])
        rate_s = (dr / dt) if dt > 1.0 else 0.0
        prog.done = done
        prog.rate_per_min = round(rate_s * 60.0, 1)
        prog.elapsed_s = int(now - t0)
        prog.eta_s = int((prog.total - done) / rate_s) if (rate_s > 0 and prog.total > done) else -1
        if on_progress:
            on_progress(prog)
    prog.elapsed_s = int(_time.monotonic() - t0)
    prog.eta_s = -1
    if p.returncode != 0:
        try:
            _err_tail = err_log.read_bytes()[-300:].decode("utf-8", errors="replace")
        except OSError:
            _err_tail = ""
        _sh.rmtree(work, ignore_errors=True)
        prog.phase = "error"
        prog.message = f"region-convert rc={p.returncode}: {_err_tail}"
        if on_progress:
            on_progress(prog)
        return prog

    produced = _find_world_with_regions(work, ".b_linear")
    if produced is None:
        _sh.rmtree(work, ignore_errors=True)
        prog.phase = "error"
        prog.message = "region-convert produced no .b_linear output"
        if on_progress:
            on_progress(prog)
        return prog

    # Assemble a clean dest world: source skeleton + the produced .b_linear regions.
    (dest / "region").mkdir(parents=True, exist_ok=True)
    _copy_world_skeleton(src, dest)
    moved = 0
    out_bytes = 0
    for f in sorted((produced / "region").glob("r.*.b_linear")):
        target = dest / "region" / f.name
        _sh.move(str(f), str(target))
        out_bytes += target.stat().st_size
        moved += 1
        prog.done = moved
        prog.out_bytes = out_bytes
        if on_progress:
            on_progress(prog)
    _sh.rmtree(work, ignore_errors=True)

    prog.phase = "done" if moved > 0 else "error"
    if moved == 0:
        prog.message = "no .b_linear regions were produced"
    if on_progress:
        on_progress(prog)
    return prog


# ── top-level entry: post-pass export of a finished world ─────────────────────
def run_world_export(
    world_dir: str | Path,
    fmt: str,
    *,
    level=0,
    workers=0,
    keep_both: bool = True,
    stream_and_free: bool = False,
    dest_world: str | Path | None = None,
    blinear_variant: str = "v3",
    on_progress: Callable[[ExportProgress], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> ExportProgress:
    """Export a COMPLETED master world. linear → per-region pool; zip/tarzst → one
    container; blinear → Rust region-convert into a sibling world. Honours the safety
    contract + resumable manifest. FMT_NONE is a no-op.
    `world_dir` is the folder that directly contains region/ , level.dat, etc.

    dest_world (LINEAR only): write the .linear world into this SEPARATE folder, leaving
    the source an untouched vanilla .mca world. For archives the output is already a
    standalone sibling file, so dest_world is ignored there."""
    world = Path(world_dir)
    fmt = (fmt or FMT_NONE).strip().lower()
    if fmt not in VALID_FORMATS:
        fmt = FMT_NONE
    prog = ExportProgress(format=fmt)
    if fmt == FMT_NONE:
        prog.phase = "done"
        if on_progress:
            on_progress(prog)
        return prog

    level = resolve_level(fmt, level)
    nworkers = resolve_workers(workers)

    if fmt == FMT_BLINEAR:
        # blinear is always a separate sibling world (the Rust tool only writes to --output).
        dest = Path(dest_world) if dest_world is not None else \
            world.parent / format_folder_name(world.name, FMT_BLINEAR)
        return run_blinear_export(world, dest, variant=blinear_variant, level=level,
                                  workers=nworkers, on_progress=on_progress)

    if fmt == FMT_LINEAR and dest_world is not None:
        dmanifest = Manifest(Path(dest_world), meta={
            "format": fmt, "level": level, "workers": nworkers,
            "separate_folder": True, "source": str(world),
        })
        return export_linear_to_folder(world, Path(dest_world), level, nworkers,
                                       dmanifest, on_progress, should_stop, prog)

    manifest = Manifest(world, meta={
        "format": fmt, "level": level, "workers": nworkers,
        "keep_both": keep_both, "stream_and_free": stream_and_free,
    })

    if fmt == FMT_LINEAR:
        return _run_linear_postpass(world, level, nworkers, keep_both,
                                    manifest, on_progress, should_stop, prog)
    return _run_archive_postpass(world, fmt, level, nworkers, keep_both,
                                 manifest, on_progress, should_stop, prog)


def _run_linear_postpass(world, level, nworkers, keep_both, manifest,
                         on_progress, should_stop, prog) -> ExportProgress:
    mcas = [p for p in _iter_region_mcas(world)]
    prog.total = len(mcas)
    prog.phase = "compressing"
    if not mcas:
        prog.phase = "done"
        if on_progress:
            on_progress(prog)
        return prog

    work_q: queue.Queue[Path | None] = queue.Queue()
    for p in mcas:
        work_q.put(p)
    lock = threading.Lock()

    def worker():
        while True:
            if should_stop and should_stop():
                return
            try:
                p = work_q.get_nowait()
            except queue.Empty:
                return
            try:
                if p is None:
                    return
                rs, os_ = _compress_region_linear(
                    p, level, keep_both, free_raw=(not keep_both), manifest=manifest)
                with lock:
                    prog.done += 1
                    prog.raw_bytes += rs
                    prog.out_bytes += os_
            except Exception as e:
                with lock:
                    prog.failed += 1
                    prog.message = str(e)
            finally:
                work_q.task_done()
                if on_progress:
                    on_progress(prog)

    threads = [threading.Thread(target=worker, daemon=True, name=f"linexp-{i}")
               for i in range(max(1, nworkers))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    prog.phase = "done" if prog.failed == 0 else "error"
    if on_progress:
        on_progress(prog)
    return prog


def _run_archive_postpass(world, fmt, level, nworkers, keep_both, manifest,
                          on_progress, should_stop, prog) -> ExportProgress:
    prog.phase = "archiving"
    files = _world_files(world)
    prog.total = len(files)
    art = "__archive__"
    if manifest.is_done(art):
        prog.phase = "done"
        if on_progress:
            on_progress(prog)
        return prog

    ext = ".zip" if fmt == FMT_ZIP else ".tar.zst"
    dst = world.parent / (world.name + ext)
    manifest.set(art, state=ST_COMPRESSING, out=dst.name)
    raw_total = sum(f.stat().st_size for f in files)
    prog.raw_bytes = raw_total

    def on_file(done, total):
        with threading.Lock():
            prog.done = done
        if on_progress:
            on_progress(prog)

    try:
        if fmt == FMT_ZIP:
            size = _build_zip(world, dst, level, on_file)
            ok = _verify_zip(dst)
        else:
            size = _build_tarzst(world, dst, level, nworkers, on_file)
            ok = _verify_tarzst(dst)
    except Exception as e:
        manifest.set(art, state=ST_FAILED, error=str(e))
        prog.phase = "error"
        prog.message = str(e)
        if on_progress:
            on_progress(prog)
        return prog

    if not ok:
        manifest.set(art, state=ST_FAILED)
        prog.phase = "error"
        prog.message = "archive verify failed"
        if on_progress:
            on_progress(prog)
        return prog

    prog.out_bytes = size
    manifest.set(art, state=ST_COMPRESSED, out_size=size, raw_size=raw_total)
    # delete-raw for an archive: only at the END, after the container verified.
    if not keep_both:
        freed = _free_world_raws(world)
        manifest.set(art, state=ST_FREED, freed=freed)
    prog.phase = "done"
    if on_progress:
        on_progress(prog)
    return prog


# ── linear → .mca conversion (server world → vanilla single-player) ───────────
def _convert_one_linear(lin_path: Path, keep_both: bool, manifest: Manifest) -> tuple[int, int]:
    """Convert ONE r.X.Z.linear → r.X.Z.mca with the safety contract + round-trip verify.
    Returns (linear_size, mca_size). Idempotent/resumable via the manifest."""
    art = lin_path.stem
    mca_path = lin_path.with_suffix(".mca")
    if manifest.is_done(art) and mca_path.exists():
        return (0, mca_path.stat().st_size)

    data = lin_path.read_bytes()
    lin_size = len(data)
    manifest.set(art, state=ST_COMPRESSING, src=lin_path.name, src_size=lin_size)
    datas, _ts = linear_decode(data)
    rebuilt = linear_to_mca(data)

    out_size = atomic_write(mca_path, lambda tmp: tmp.write_bytes(rebuilt))
    # Strong round-trip verify BEFORE deleting the .linear.
    if not verify_mca_against_linear(mca_path.read_bytes(), datas):
        manifest.set(art, state=ST_FAILED, out=mca_path.name)
        raise ValueError(f"mca round-trip verify failed for {lin_path.name}")
    manifest.set(art, state=ST_COMPRESSED, out=mca_path.name, out_size=out_size)

    if not keep_both:
        try:
            lin_path.unlink()
            manifest.set(art, state=ST_FREED)
        except OSError:
            pass
    return (lin_size, out_size)


def convert_linear_world(
    world_dir: str | Path,
    *,
    keep_both: bool = True,
    workers=0,
    on_progress: Callable[[ExportProgress], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> ExportProgress:
    """Convert every region/*.linear in a world back to standard Anvil .mca (so a
    server/Linear world opens in vanilla single-player). Parallel across workers, safety
    contract + round-trip verify per region, resumable via convert_manifest.json. With
    keep_both=False each .linear is removed only after its .mca verifies."""
    world = Path(world_dir)
    region = world / "region"
    prog = ExportProgress(format="linear2mca", phase="converting")
    lins = sorted(region.glob("r.*.linear")) if region.is_dir() else []
    prog.total = len(lins)
    if not lins:
        prog.phase = "done"
        if on_progress:
            on_progress(prog)
        return prog

    nworkers = resolve_workers(workers)
    manifest = Manifest(world, meta={"op": "linear2mca", "keep_both": keep_both},
                        name="convert_manifest.json")
    work_q: queue.Queue[Path] = queue.Queue()
    for p in lins:
        work_q.put(p)
    lock = threading.Lock()

    def worker():
        while True:
            if should_stop and should_stop():
                return
            try:
                p = work_q.get_nowait()
            except queue.Empty:
                return
            try:
                src, out = _convert_one_linear(p, keep_both, manifest)
                with lock:
                    prog.done += 1
                    prog.raw_bytes += src
                    prog.out_bytes += out
            except Exception as e:
                with lock:
                    prog.failed += 1
                    prog.message = str(e)
            finally:
                work_q.task_done()
                if on_progress:
                    on_progress(prog)

    threads = [threading.Thread(target=worker, daemon=True, name=f"lin2mca-{i}")
               for i in range(max(1, nworkers))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    prog.phase = "done" if prog.failed == 0 else "error"
    if on_progress:
        on_progress(prog)
    return prog
