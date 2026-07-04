#!/usr/bin/env python3
"""
Meld region converter — a standalone CLI to convert Minecraft Java region saves between
formats, using Meld's own (verified, byte-lossless) codec. No web UI, no server.

  meldconvert.py linear  <path> [--out DIR] [--level 9] [--workers N] [--delete-raw]
  meldconvert.py mca     <path> [--out DIR] [--workers N] [--delete-raw]
  meldconvert.py blinear <path> [--out DIR] [--variant v3] [--level N] [--workers N]
  meldconvert.py info    <path>

<path> may be any of:
  • a WORLD folder        (contains region/ , level.dat …)
  • a region/ FOLDER      (contains r.X.Z.mca and/or r.X.Z.linear directly)
  • a single region FILE  (r.X.Z.mca or r.X.Z.linear or r.X.Z.b_linear)

Targets (convert FROM any of the three, TO any of the three — both ways):
  linear   → Meld's Linear v1 (.linear). Read natively by Leaf/Folia (format LINEAR_V2).
  mca      → standard vanilla Anvil (.mca). Opens in single-player.
  blinear  → Luminol Buffered Linear (.b_linear). Requires region_converter(.exe) next to
             this script (https://github.com/LuminolMC/region_converter). v2 or v3.
  (mca↔linear use Meld's built-in codec; anything touching blinear shells out to
   region_converter, which Meld auto-detects the source format for.)

Output rules:
  • --out DIR  → write the converted copy there; the SOURCE is left untouched (safe).
  • no --out   → convert IN PLACE next to the source.
  • --delete-raw → IN-PLACE ONLY: after each converted file VERIFIES, delete its source
                   (peak-disk saver). With --out the source is always kept (a warning is
                   printed if you pass both), so you never lose the original on another drive.

Safety: every write is temp → fsync → atomic replace → verify; linear↔mca is round-trip
checked per region before any source deletion. Close Minecraft first (an open world locks
its files). Run from the light-meld folder.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# Windows consoles default to a legacy codepage (cp1252) that can't encode non-ASCII; make
# our own output UTF-8 so progress glyphs never crash the tool. Best-effort (redirected pipes
# may not support reconfigure; printed output also avoids non-ASCII to be safe).
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

from src import export as ex  # noqa: E402

REGION_EXTS = (".mca", ".linear", ".b_linear")
FMT_EXT = {"mca": ".mca", "linear": ".linear", "blinear": ".b_linear"}
EXT_FMT = {".mca": "mca", ".linear": "linear", ".b_linear": "blinear"}


# ── input discovery ───────────────────────────────────────────────────────────
def detect_kind(p: Path) -> str:
    """Classify the input: 'world' (has region/), 'region_dir' (holds r.*.<ext> directly),
    'file' (a single region file), or 'unknown'."""
    p = Path(p)
    if p.is_file() and p.suffix.lower() in REGION_EXTS:
        return "file"
    if p.is_dir():
        if (p / "region").is_dir():
            return "world"
        if any(p.glob("r.*.mca")) or any(p.glob("r.*.linear")) or any(p.glob("r.*.b_linear")):
            return "region_dir"
    return "unknown"


def region_dir_for(path: Path, kind: str) -> Path:
    return (path / "region") if kind == "world" else path


def source_formats(region: Path) -> set[str]:
    """The set of region formats present directly in a region folder."""
    out: set[str] = set()
    for f in region.glob("r.*"):
        fm = EXT_FMT.get(f.suffix.lower())
        if fm:
            out.add(fm)
    return out


def _linear_version(p: Path) -> int | None:
    """Version byte of a .linear file (right after the 8-byte superblock); None if the file
    is unreadable or does not carry the linear signature."""
    import struct
    try:
        with open(p, "rb") as fh:
            head = fh.read(9)
    except OSError:
        return None
    if len(head) < 9 or head[:8] != struct.pack(">q", ex._as_i64(ex.LINEAR_SIGNATURE)):
        return None
    return head[8]


def has_modern_linear(path: Path, kind: str) -> bool:
    """True if any .linear input is version 2/3. Meld's native codec writes and reads the
    classic v1 layout only; a Leaf server configured for LINEAR_V2 REWRITES regions as v3
    once it runs (verified against leaf-26.1.2 bytecode: it reads v1/2/3 but writes 3).
    Those files must be converted by region_converter, which reads all three."""
    if kind == "file":
        return path.suffix.lower() == ".linear" and _linear_version(path) not in (None, 1)
    region = region_dir_for(path, kind)
    return any(_linear_version(f) not in (None, 1) for f in region.glob("r.*.linear"))


def _fmt_mb(n: int) -> str:
    return f"{n / 1048576:.1f} MB"


def _print_progress(p) -> None:
    pct = (p.done / p.total * 100) if p.total else 0
    sys.stdout.write(
        f"\r  {p.phase:<11} {p.done}/{p.total} ({pct:4.0f}%)  "
        f"{_fmt_mb(p.raw_bytes)} -> {_fmt_mb(p.out_bytes)}  "
        f"{p.ratio:4.2f}x  failed={p.failed}   ")
    sys.stdout.flush()
    if p.phase in ("done", "error"):
        sys.stdout.write("\n")


# ── native mca<->linear region-dir converter (parallel, honours --workers) ─────
def convert_region_dir(region: Path, out_region: Path, target: str, level: int,
                       workers: int, delete_raw: bool) -> ex.ExportProgress:
    """Convert every non-target-format region file in `region` to `target` (mca or linear),
    in parallel. When out_region != region, pre-existing TARGET-format files are copied across
    so the destination is a complete superset (no dropped chunks). delete_raw deletes a source
    only in-place, only after its converted copy verifies."""
    in_place = (out_region.resolve() == region.resolve())
    out_region.mkdir(parents=True, exist_ok=True)
    target_ext = FMT_EXT[target]
    # Files to convert = those NOT already in the target format (skip stray .b_linear here;
    # any blinear involvement is routed to region_converter before reaching this function).
    src = [f for f in sorted(region.glob("r.*"))
           if f.suffix.lower() in (".mca", ".linear") and f.suffix.lower() != target_ext]
    keep = [f for f in sorted(region.glob(f"r.*{target_ext}"))]

    prog = ex.ExportProgress(format=target, phase="converting", total=len(src))
    manifest = ex.Manifest(out_region, meta={"op": f"to-{target}", "in_place": in_place},
                           name="convert_manifest.json")
    lock = threading.Lock()

    def _one(p: Path):
        out = out_region / (p.stem + target_ext)
        if target == "linear":
            rs, osz = ex._compress_region_linear(
                p, level, keep_both=True, free_raw=False, manifest=manifest,
                out_path=None if in_place else out)
        else:  # mca  (source is .linear)
            data = p.read_bytes()
            datas, _ts = ex.linear_decode(data)
            rebuilt = ex.linear_to_mca(data)
            osz = ex.atomic_write(out, lambda tmp, b=rebuilt: tmp.write_bytes(b))
            if not ex.verify_mca_against_linear(out.read_bytes(), datas):
                raise ValueError(f"round-trip verify failed for {p.name}")
            rs = len(data)
        # Source removal: in-place only, AFTER the copy verified above.
        if delete_raw and in_place and out.resolve() != p.resolve():
            p.unlink(missing_ok=True)
        return rs, osz

    def _run(p: Path):
        try:
            rs, osz = _one(p)
            with lock:
                prog.done += 1
                prog.raw_bytes += rs
                prog.out_bytes += osz
        except Exception as e:  # noqa: BLE001
            with lock:
                prog.failed += 1
                prog.message = str(e)
        _print_progress(prog)

    if src:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            list(pool.map(_run, src))
    # Carry across already-target-format regions so the dest is complete.
    if not in_place:
        for f in keep:
            dst = out_region / f.name
            if not dst.exists():
                shutil.copy2(f, dst)
    prog.phase = "done" if prog.failed == 0 else "error"
    _print_progress(prog)
    return prog


# ── single-file conversion ─────────────────────────────────────────────────────
def _resolve_file_out(out: Path | None, src: Path, ext: str) -> Path:
    """Resolve the destination for single-file conversion. No --out → sibling with the new
    extension. --out with a region extension → that exact file. --out anything else → a
    DIRECTORY (created), with <stem><ext> inside. Removes the file-vs-dir ambiguity when the
    --out path does not exist yet."""
    if out is None:
        return src.with_suffix(ext)
    if out.is_dir() or out.suffix.lower() not in REGION_EXTS:
        out.mkdir(parents=True, exist_ok=True)
        return out / (src.stem + ext)
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def convert_file(src: Path, target: str, out: Path | None, level: int,
                 delete_raw: bool) -> ex.ExportProgress:
    ext = FMT_EXT[target]
    dst = _resolve_file_out(out, src, ext)
    prog = ex.ExportProgress(format=target, phase="converting", total=1)
    try:
        if target == "linear":
            enc = ex.mca_to_linear(src.read_bytes(), level)
            osz = ex.atomic_write(dst, lambda tmp: tmp.write_bytes(enc))
            if not ex.verify_linear(dst.read_bytes()):
                raise ValueError("linear verify failed")
            prog.raw_bytes = src.stat().st_size
        else:  # mca
            data = src.read_bytes()
            datas, _ts = ex.linear_decode(data)
            rebuilt = ex.linear_to_mca(data)
            osz = ex.atomic_write(dst, lambda tmp: tmp.write_bytes(rebuilt))
            if not ex.verify_mca_against_linear(dst.read_bytes(), datas):
                raise ValueError("round-trip verify failed")
            prog.raw_bytes = len(data)
        prog.done = 1
        prog.out_bytes = osz
        if delete_raw and dst.resolve() != src.resolve():
            src.unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001
        prog.failed = 1
        prog.message = str(e)
    prog.phase = "done" if prog.failed == 0 else "error"
    _print_progress(prog)
    return prog


# ── blinear via Luminol's region_converter (handles ALL directions) ────────────
def resolve_region_converter() -> Path | None:
    """Single source of truth lives in src/export.py (cross-platform, searches
    region-convert/bin, target/release, light-meld root, then PATH)."""
    return ex.resolve_region_converter()


def run_region_converter(src: Path, out: Path | None, to_fmt: str, level: int,
                         workers: int) -> int:
    """Shell out to region_converter for any conversion involving blinear (either direction).
    to_fmt is one of mca | linear | blinear-v2 | blinear-v3. region_converter auto-detects the
    source format. Returns the process exit code (3 if the binary is missing/unlaunchable)."""
    exe = resolve_region_converter()
    if exe is None:
        print(
            "\nblinear needs Luminol's region_converter, not found next to this script.\n"
            "  1. Download from https://github.com/LuminolMC/region_converter/releases\n"
            f"  2. Put region_converter.exe in: {HERE}\n"
            "  3. Re-run. (Meld shells out to it for B_LINEAR both ways.)", file=sys.stderr)
        return 3
    dst = out or (src.parent / f"{src.name}.{to_fmt}")
    cmd = [str(exe), "--to", to_fmt, "--output", str(dst), str(src),
           "--compression-level", str(level)]
    if workers > 0:
        cmd += ["--threads", str(workers)]
    print(f"  -> {' '.join(cmd)}")
    try:
        return subprocess.run(cmd).returncode
    except OSError as e:
        print(f"failed to launch region_converter: {e}", file=sys.stderr)
        return 3


# ── info ──────────────────────────────────────────────────────────────────────
def cmd_info(path: Path) -> int:
    kind = detect_kind(path)
    if kind == "unknown":
        print(f"not a world/region/file: {path}", file=sys.stderr)
        return 2
    if kind == "file":
        print(f"{path.name}: {EXT_FMT.get(path.suffix.lower(), '?')}, {_fmt_mb(path.stat().st_size)}")
        return 0
    region = region_dir_for(path, kind)
    counts: dict[str, list[int]] = {}
    for f in sorted(region.iterdir()):
        if f.suffix.lower() in REGION_EXTS:
            c = counts.setdefault(f.suffix.lower().lstrip("."), [0, 0])
            c[0] += 1
            c[1] += f.stat().st_size
    print(f"{path}  ({kind})")
    for fmt, (cnt, sz) in sorted(counts.items()):
        print(f"  {fmt:8} {cnt:5} files   {_fmt_mb(sz)}")
    if not counts:
        print("  (no region files)")
    return 0


# ── main convert dispatch ─────────────────────────────────────────────────────
def cmd_convert(target: str, path: Path, out: Path | None, level: int,
                workers: int, delete_raw: bool, variant: str) -> int:
    if not path.exists():
        print(f"path not found: {path}", file=sys.stderr)
        return 2
    kind = detect_kind(path)
    if kind == "unknown":
        print(f"not a world/region/file: {path}", file=sys.stderr)
        return 2

    level = ex.resolve_level("linear", level) if target in ("linear", "blinear") else (level or 6)
    workers = ex.resolve_workers(workers)

    # --out that resolves to the source = in place. --delete-raw with a real --out keeps the
    # source (warn): peak-disk is the in-place job, and --out's whole point is a safe copy.
    if out is not None and out.resolve() == path.resolve():
        out = None
    # For native mca/linear, --delete-raw means in-place (a separate --out keeps the source).
    # blinear is the exception: the Rust tool ALWAYS writes a separate world, so blinear honours
    # --delete-raw WITH --out (delete the source only after the new world verifies).
    if out is not None and delete_raw and target != "blinear":
        print("note: --delete-raw is ignored with --out (the source is kept). "
              "Convert in place (omit --out) to reclaim space.", file=sys.stderr)
        delete_raw = False
    # For dir/world targets, --out must be a directory (or not exist yet), never a file.
    if out is not None and kind in ("world", "region_dir") and out.exists() and not out.is_dir():
        print(f"--out must be a folder for a {kind} input: {out}", file=sys.stderr)
        return 2

    # Determine the source format(s) so we can (a) no-op on same-format, (b) route blinear.
    if kind == "file":
        srcset = {EXT_FMT.get(path.suffix.lower())}
    else:
        srcset = source_formats(region_dir_for(path, kind))
    srcset.discard(None)
    if not srcset:
        print(f"no region files to convert in: {path}", file=sys.stderr)
        return 2

    t0 = time.time()

    # Any conversion that touches blinear (source OR target) goes through region_converter,
    # which auto-detects the source format and converts all input kinds in one call. The same
    # routing applies to server-written .linear v2/v3 sources (see has_modern_linear) — the
    # native codec below reads classic v1 only.
    modern_linear = ("linear" in srcset and target != "linear"
                     and has_modern_linear(path, kind))
    if modern_linear:
        print("note: found server-written linear v2/v3 region files — converting via "
              "region_converter (the native codec reads classic v1 only).")
    if target == "blinear" or "blinear" in srcset or modern_linear:
        to_fmt = f"blinear-{variant}" if target == "blinear" else target
        rc = run_region_converter(path, out, to_fmt, level, workers)
        # --delete-raw: reclaim the SOURCE only after a clean convert AND a verified output, and
        # only with an explicit --out (the converter always writes a separate world).
        if delete_raw:
            if out is None:
                print("note: --delete-raw needs --out for blinear (the converter writes a "
                      "separate world); source kept.", file=sys.stderr)
            elif rc == 0 and any(Path(out).rglob(
                    {"mca": "*.mca", "linear": "*.linear"}.get(to_fmt, "*.b_linear"))):
                if kind == "file":
                    path.unlink(missing_ok=True)
                    print("deleted source file (--delete-raw, after verify)")
                else:
                    region = region_dir_for(path, kind)
                    n = 0
                    for f in list(region.glob("r.*")):
                        if f.suffix.lower() in REGION_EXTS:
                            f.unlink(missing_ok=True)
                            n += 1
                    print(f"deleted {n} source region file(s) (--delete-raw, after verify)")
            else:
                print("--delete-raw skipped: convert failed or produced no output; source kept.",
                      file=sys.stderr)
        print(f"\ndone in {time.time()-t0:.1f}s (region_converter rc={rc})")
        return rc

    # Native mca<->linear from here on. No-op guard: nothing of a convertible source format.
    convertible = {f for f in srcset if f != target}
    if not convertible:
        print(f"already in {target} format — nothing to convert: {path}", file=sys.stderr)
        return 0

    if kind == "world":
        if target == "linear":
            dest = out if out else None
            prog = ex.run_world_export(path, ex.FMT_LINEAR, level=level, workers=workers,
                                       keep_both=(not delete_raw), dest_world=dest,
                                       on_progress=_print_progress)
        else:  # mca
            if out:
                ex._copy_world_skeleton(path, out)            # level.dat/poi/entities → out
                prog = convert_region_dir(path / "region", out / "region", "mca",
                                          level, workers, delete_raw=False)
            else:
                prog = ex.convert_linear_world(path, keep_both=(not delete_raw),
                                               workers=workers, on_progress=_print_progress)
    elif kind == "region_dir":
        out_region = out if out else path
        prog = convert_region_dir(path, out_region, target, level, workers, delete_raw)
    else:  # file
        prog = convert_file(path, target, out, level, delete_raw)

    dt = time.time() - t0
    ok = prog.failed == 0
    print(f"done in {dt:.1f}s · {prog.done} ok, {prog.failed} failed · "
          f"{_fmt_mb(prog.raw_bytes)} -> {_fmt_mb(prog.out_bytes)} ({prog.ratio:.2f}x)")
    if not ok and prog.message:
        print(f"last error: {prog.message}", file=sys.stderr)
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="meldconvert", description="Convert Minecraft region saves between mca / linear / blinear.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    for tgt in ("linear", "mca", "blinear"):
        sp = sub.add_parser(tgt, help=f"convert to {tgt}")
        sp.add_argument("path", type=Path, help="world folder, region/ folder, or a single region file")
        sp.add_argument("--out", type=Path, default=None,
                        help="output folder/file (source left untouched). Omit = convert in place.")
        sp.add_argument("--level", type=int, default=0, help="zstd level (0=auto: linear/blinear 9)")
        sp.add_argument("--workers", type=int, default=0, help="parallel workers (0=auto: cores-1)")
        sp.add_argument("--delete-raw", action="store_true",
                        help="in-place only: delete each source after its converted copy verifies")
        if tgt == "blinear":
            sp.add_argument("--variant", choices=["v2", "v3"], default="v3",
                            help="Buffered Linear version (default v3)")

    ip = sub.add_parser("info", help="report region-format breakdown + sizes")
    ip.add_argument("path", type=Path)

    args = ap.parse_args()
    if args.cmd == "info":
        return cmd_info(args.path)
    return cmd_convert(args.cmd, args.path, args.out, args.level, args.workers,
                       args.delete_raw, getattr(args, "variant", "v3"))


if __name__ == "__main__":
    raise SystemExit(main())
