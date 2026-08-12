#!/usr/bin/env python3
"""Build the ArnisXL release folder for this machine's OS.

    python packaging/build.py                 build everything
    python packaging/build.py --no-arnis      skip fetching the generator binary
    python packaging/build.py --archive       also produce the release archive

Steps, in order:
  1. icons              packaging/make_icons.py (placeholder if there is no source yet)
  2. arnis binary       the matching prebuilt from the fork's releases, copied in beside the exe
  3. PyInstaller        packaging/arnisxl.spec -> dist/ArnisXL/
  4. archive            ArnisXL-<version>-<os>-<arch>.zip / .tar.gz

The result is a folder, not an installer: extract it anywhere and run ArnisXL. It carries its
own Python, so the machine needs nothing installed.

The arnis binary is bundled rather than downloaded on first run. It costs 45 MB in the archive
and buys an app that works offline, on a restricted network, and behind whatever is blocking
GitHub that day - the alternative is a first launch that silently cannot generate anything.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "ArnisXL"
FORK = os.environ.get("MELD_ARNIS_REPO", "Teddy563/arnis")
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"


def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def run(cmd: list[str], **kw) -> None:
    log(" ".join(str(c) for c in cmd))
    subprocess.run(cmd, check=True, cwd=str(ROOT), **kw)


def os_tag() -> tuple[str, str]:
    """(os, arch) as they appear in the archive name."""
    mach = (platform.machine() or "").lower()
    arch = "arm64" if mach in ("arm64", "aarch64") else "x64"
    if IS_WIN:
        return "win", arch
    if IS_MAC:
        return "mac", arch
    return "linux", arch


def arnis_asset() -> tuple[str | None, str]:
    """(release asset name for this OS/CPU, filename to save it as)."""
    mach = (platform.machine() or "").lower()
    arm = mach in ("arm64", "aarch64")
    if IS_WIN:
        return "arnis-windows.exe", "arnis.exe"
    if IS_MAC:
        return ("arnis-mac-arm64.tar.gz" if arm else "arnis-mac-intel.tar.gz"), "arnis"
    if sys.platform.startswith("linux"):
        return "arnis-linux.tar.gz", "arnis"
    return None, "arnis"


def fetch_arnis(dest_dir: Path) -> bool:
    """Put the matching arnis binary in dest_dir. A local copy next to the repo wins, so an
    offline build (or a locally built fork) does not go to the network."""
    asset, outname = arnis_asset()
    target = dest_dir / outname
    if target.is_file():
        log(f"arnis already present: {target}")
        return True

    for candidate in (ROOT / outname, ROOT.parent / outname):
        if candidate.is_file():
            shutil.copy2(candidate, target)
            log(f"arnis copied from {candidate}")
            if not IS_WIN:
                os.chmod(target, 0o755)
            return True

    if not asset:
        log(f"no arnis asset defined for {sys.platform}")
        return False
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{FORK}/releases/latest",
                                     headers={"User-Agent": "arnisxl-build"})
        with urllib.request.urlopen(req, timeout=30) as r:
            rel = json.loads(r.read())
        url = next((x["browser_download_url"] for x in rel.get("assets", [])
                    if x.get("name") == asset), None)
        if not url:
            log(f"release {rel.get('tag_name')} has no '{asset}'")
            return False
        log(f"downloading {asset} from {rel.get('tag_name')}")
        with urllib.request.urlopen(urllib.request.Request(
                url, headers={"User-Agent": "arnisxl-build"}), timeout=300) as r:
            blob = r.read()
    except Exception as ex:                                   # noqa: BLE001
        log(f"could not fetch arnis: {ex}")
        return False

    if asset.endswith(".tar.gz"):
        tmp = dest_dir / asset
        tmp.write_bytes(blob)
        try:
            with tarfile.open(tmp) as t:
                member = next(m for m in t.getmembers() if m.isfile())
                with t.extractfile(member) as f:              # type: ignore[union-attr]
                    target.write_bytes(f.read())
        finally:
            tmp.unlink(missing_ok=True)
    else:
        target.write_bytes(blob)
    if not IS_WIN:
        os.chmod(target, 0o755)
    log(f"arnis ready: {target}")
    return True


def version() -> str:
    try:
        for line in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8",
                                                      errors="replace").splitlines():
            if line.strip().startswith("## "):
                return line.strip()[3:].split()[0].strip("[]v")
    except Exception:
        pass
    return "dev"


def archive(folder: Path) -> Path:
    osname, arch = os_tag()
    base = ROOT / "dist" / f"ArnisXL-{version()}-{osname}-{arch}"
    if IS_WIN:
        out = base.with_suffix(".zip")
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for p in folder.rglob("*"):
                if p.is_file():
                    z.write(p, Path("ArnisXL") / p.relative_to(folder))
    else:
        out = Path(str(base) + ".tar.gz")
        # tar, not zip, on Unix: it is the only common format that preserves the executable bit,
        # and an ArnisXL that unzips without +x is a support ticket, not an app.
        with tarfile.open(out, "w:gz") as t:
            t.add(folder, arcname="ArnisXL")
    log(f"archive: {out} ({out.stat().st_size / 1e6:.0f} MB)")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-arnis", action="store_true", help="skip the generator binary")
    ap.add_argument("--no-clean", action="store_true", help="keep the previous dist/")
    ap.add_argument("--archive", action="store_true", help="also produce the release archive")
    ap.add_argument("--onefile", action="store_true",
                    help="one self-contained executable instead of a folder (slower to start)")
    ap.add_argument("--no-embed-arnis", action="store_true",
                    help="with --onefile: leave the generator as a separate file next to the exe")
    args = ap.parse_args()

    run([sys.executable, str(ROOT / "packaging" / "make_icons.py")])

    if not args.no_clean:
        for d in (ROOT / "build", ROOT / "dist"):
            shutil.rmtree(d, ignore_errors=True)

    env = dict(os.environ)
    if args.onefile:
        env["ARNISXL_ONEFILE"] = "1"
        # A lone executable has no folder for the generator to sit in, so embedding is the
        # default here - otherwise "one file you can copy anywhere" quietly is not one file.
        if not args.no_embed_arnis:
            if not fetch_arnis(ROOT):
                log("ERROR: --onefile needs the arnis binary present to embed it")
                return 1
            env["ARNISXL_EMBED_ARNIS"] = "1"

    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         str(ROOT / "packaging" / "arnisxl.spec")], env=env)

    if args.onefile:
        exe = ROOT / "dist" / ("ArnisXL.exe" if IS_WIN else "ArnisXL")
        if not exe.is_file():
            log(f"expected {exe} - PyInstaller layout changed?")
            return 1
        if not args.no_embed_arnis:
            log(f"built single file: {exe} ({exe.stat().st_size / 1e6:.0f} MB, generator inside)")
        else:
            fetch_arnis(exe.parent)
            log(f"built single file: {exe} (generator alongside)")
        return 0

    out = DIST
    if IS_MAC and (ROOT / "dist" / "ArnisXL.app").exists():
        # The .app is what a Mac user runs; the plain folder next to it is the same payload.
        out = ROOT / "dist" / "ArnisXL.app" / "Contents" / "MacOS"

    if not out.exists():
        log(f"expected build output at {out} - PyInstaller layout changed?")
        return 1

    if not args.no_arnis and not fetch_arnis(out):
        log("WARNING: no arnis binary bundled — the app will start but cannot generate. "
            "Drop one next to the executable, or re-run with network access.")

    log(f"built: {out}")
    if args.archive:
        archive(ROOT / "dist" / ("ArnisXL.app" if (IS_MAC and (ROOT / "dist" / "ArnisXL.app").exists())
                                 else "ArnisXL"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
