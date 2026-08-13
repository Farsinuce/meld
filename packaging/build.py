#!/usr/bin/env python3
"""Build the Meld release folder for this machine's OS.

    python packaging/build.py                 build everything
    python packaging/build.py --no-arnis      skip fetching the generator binary
    python packaging/build.py --archive       also produce the release archive

Steps, in order:
  1. icons              packaging/make_icons.py (placeholder if there is no source yet)
  2. arnis binary       the matching prebuilt from the fork's releases, copied in beside the exe
  3. PyInstaller        packaging/meld.spec -> dist/Meld/
  4. archive            Meld-<version>-<os>-<arch>.zip / .tar.gz

The result is a folder, not an installer: extract it anywhere and run Meld. It carries its
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
DIST = ROOT / "dist" / "Meld"
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


def arnis_assets() -> tuple[list[str], str]:
    """(release asset names to try in order, filename to save it as).

    A list rather than one name because macOS has two plausible layouts and only one of them is
    real: the fork builds arnis-mac-intel/arnis-mac-arm64 as CI *artifacts*, then lipos them into
    arnis-mac-universal.tar.gz - which is the only mac file the release attaches. Asking for the
    per-arch name failed on every mac runner with "release v3.0.7 has no arnis-mac-arm64.tar.gz",
    and because the missing generator is a hard error, both mac archives were lost. The per-arch
    names stay in the list after the universal so a future release that does attach them is
    picked up without another edit.
    """
    mach = (platform.machine() or "").lower()
    arm = mach in ("arm64", "aarch64")
    if IS_WIN:
        return ["arnis-windows.exe"], "arnis.exe"
    if IS_MAC:
        return ["arnis-mac-universal.tar.gz",
                "arnis-mac-arm64.tar.gz" if arm else "arnis-mac-intel.tar.gz"], "arnis"
    if sys.platform.startswith("linux"):
        return ["arnis-linux.tar.gz"], "arnis"
    return [], "arnis"


def fetch_arnis(dest_dir: Path) -> bool:
    """Put the matching arnis binary in dest_dir. A local copy next to the repo wins, so an
    offline build (or a locally built fork) does not go to the network."""
    wanted, outname = arnis_assets()
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

    if not wanted:
        log(f"no arnis asset defined for {sys.platform}")
        return False
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{FORK}/releases/latest",
                                     headers={"User-Agent": "meld-build"})
        with urllib.request.urlopen(req, timeout=30) as r:
            rel = json.loads(r.read())
        have = {x.get("name"): x.get("browser_download_url") for x in rel.get("assets", [])}
        asset = next((n for n in wanted if have.get(n)), None)
        if not asset:
            # Name the assets that ARE there. "has no arnis-mac-arm64.tar.gz" on its own reads
            # like an empty release, and the release was in fact full - of differently named
            # files. One line of output here would have saved two rounds of runner-relabelling.
            log(f"release {rel.get('tag_name')} has none of: {', '.join(wanted)}")
            log(f"       it does have: {', '.join(sorted(have)) or '(no assets at all)'}")
            return False
        url = have[asset]
        log(f"downloading {asset} from {rel.get('tag_name')}")
        with urllib.request.urlopen(urllib.request.Request(
                url, headers={"User-Agent": "meld-build"}), timeout=300) as r:
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


def write_build_info() -> dict:
    """Stamp the build so a running Meld can say WHICH build it is.

    Without this there is no way to tell a fresh binary from last week's: the version in the
    changelog only moves on a release, the exe's timestamp is invisible once it is running, and
    "it still looks the same" is impossible to diagnose when the UI is baked into the bundle.
    The stamp is shown by --check, in the console banner and in the UI footer.
    """
    import datetime
    info = {
        "version": version(),
        "built": datetime.datetime.now(datetime.timezone.utc)
                          .strftime("%Y-%m-%d %H:%M UTC"),
        "commit": "",
    }
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT),
                           capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            info["commit"] = r.stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=str(ROOT),
                               capture_output=True, text=True, timeout=15)
        if dirty.returncode == 0 and dirty.stdout.strip():
            info["commit"] += "+"          # built from a working tree with uncommitted edits
    except Exception:
        pass
    target = ROOT / "assets" / "build-info.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(info, indent=1), encoding="utf-8")
    log(f"build stamp: {info['version']} · {info['built']}"
        + (f" · {info['commit']}" if info["commit"] else ""))
    return info


def reseal_bundle() -> bool:
    """Re-sign Meld.app after the generator has been copied inside it. macOS only.

    PyInstaller ad-hoc code-signs the finished bundle as the last step of BUNDLE.assemble, which
    writes Contents/_CodeSignature/CodeResources - a manifest of every file the bundle is allowed
    to contain. build.py then copies a 45 MB arnis into Contents/MacOS, which that manifest knows
    nothing about, so the seal no longer matches its contents and

        codesign --verify --deep --strict dist/Meld.app

    fails with a sealed-resource-added error. Nothing in the build checked, so v1.8.4 shipped
    that way. It went unnoticed because a second bug hid it: the archive stripped the .app
    extension, and Gatekeeper only assesses a directory that has one - fixing the extension is
    what makes the broken seal reachable, so the two fixes belong together.

    Ad-hoc ("-") signing, not a Developer ID: it costs nothing, and it is what PyInstaller
    already applied. The build stays unnotarised either way, so a user still has to clear
    Gatekeeper by hand - but from a valid signature that path exists, and from an invalid one it
    does not.
    """
    app = ROOT / "dist" / "Meld.app"
    generator = app / "Contents" / "MacOS" / "arnis"
    try:
        # The nested binary first: --deep will not sign what it does not yet consider sealed.
        if generator.is_file():
            subprocess.run(["/usr/bin/codesign", "--force", "--all-architectures",
                            "--timestamp=none", "--sign", "-", str(generator)], check=True)
        subprocess.run(["/usr/bin/codesign", "--force", "--all-architectures", "--deep",
                        "--timestamp=none", "--sign", "-", str(app)], check=True)
        subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
                       check=True)
    except FileNotFoundError:
        log("codesign not found - not a macOS toolchain? Refusing to ship an unsealed bundle.")
        return False
    except subprocess.CalledProcessError as ex:
        log(f"ERROR: could not re-seal Meld.app ({ex}). The bundle would be rejected on launch.")
        return False
    log("re-sealed Meld.app (ad-hoc) and verified the signature")
    return True


def archive_path() -> Path:
    """Where the release archive for this machine goes, and what it is called.

    The name is what a user downloads, and the release notes name it explicitly - so it is
    built by concatenation, never Path.with_suffix(). The stem contains dots, and with_suffix()
    replaces everything after the LAST one: "Meld-1.8.4-win-x64".with_suffix(".zip") is
    "Meld-1.8.zip", losing the patch version, the OS and the arch in one go. v1.8.4 shipped
    under that name while its own release table advertised Meld-*-win-x64.zip, and the upload
    glob dist/Meld-*.zip matched it, so nothing failed loudly.
    """
    osname, arch = os_tag()
    ext = ".zip" if IS_WIN else ".tar.gz"
    return ROOT / "dist" / f"Meld-{version()}-{osname}-{arch}{ext}"


def archive(folder: Path) -> Path:
    out = archive_path()
    if IS_WIN:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for p in folder.rglob("*"):
                if p.is_file():
                    z.write(p, Path("Meld") / p.relative_to(folder))
    else:
        # tar, not zip, on Unix: it is the only common format that preserves the executable bit,
        # and a Meld that unzips without +x is a support ticket, not an app.
        #
        # arcname is the REAL directory name, never the hardcoded "Meld". On macOS this folder is
        # Meld.app, and macOS decides something is an application purely by that .app extension:
        # v1.8.4 shipped both mac tarballs with the suffix stripped, so they extracted to a plain
        # folder that Finder drew as a folder and LaunchServices refused to launch. Linux is
        # unaffected either way - its folder really is called Meld.
        with tarfile.open(out, "w:gz") as t:
            t.add(folder, arcname=folder.name)
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
    write_build_info()

    if not args.no_clean:
        for d in (ROOT / "build", ROOT / "dist"):
            shutil.rmtree(d, ignore_errors=True)

    env = dict(os.environ)
    if args.onefile:
        env["MELD_ONEFILE"] = "1"
        # A lone executable has no folder for the generator to sit in, so embedding is the
        # default here - otherwise "one file you can copy anywhere" quietly is not one file.
        if not args.no_embed_arnis:
            if not fetch_arnis(ROOT):
                log("ERROR: --onefile needs the arnis binary present to embed it")
                return 1
            env["MELD_EMBED_ARNIS"] = "1"

    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         str(ROOT / "packaging" / "meld.spec")], env=env)

    if args.onefile:
        exe = ROOT / "dist" / ("Meld.exe" if IS_WIN else "Meld")
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
    if IS_MAC and (ROOT / "dist" / "Meld.app").exists():
        # The .app is what a Mac user runs; the plain folder next to it is the same payload.
        out = ROOT / "dist" / "Meld.app" / "Contents" / "MacOS"

    if not out.exists():
        log(f"expected build output at {out} - PyInstaller layout changed?")
        return 1

    if not args.no_arnis and not fetch_arnis(out):
        # Hard failure, not a warning. A Meld archive without a generator looks fine, installs
        # fine and cannot build a single cell; shipping one is worse than not shipping. It also
        # used to surface two steps later as `--check` exiting 1 in CI, which reads like a
        # packaging bug rather than "the fork's release has no assets".
        log("ERROR: no arnis binary to bundle. The app would install and then be unable to "
            "generate anything.")
        log("       Checked, in order: a local arnis next to the repo, then the latest release "
            f"of {FORK}.")
        log("       If that release has no assets, the fork's release workflow failed - see "
            "docs/arnis-port-handoff.md.")
        log("       Pass --no-arnis to build without one deliberately.")
        return 1

    if IS_MAC and (ROOT / "dist" / "Meld.app").exists() and not reseal_bundle():
        return 1

    log(f"built: {out}")
    if args.archive:
        archive(ROOT / "dist" / ("Meld.app" if (IS_MAC and (ROOT / "dist" / "Meld.app").exists())
                                 else "Meld"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
