#!/usr/bin/env python3
"""Build every icon Meld needs from one square source.

    python packaging/make_icons.py [source.png]

Source: `assets/icons/meld-source.png` - the gold Meld block, square RGBA, 1280x1280. If it is
missing, a flat placeholder is drawn instead, so a clone with no art still produces a build with
a working icon rather than failing or shipping a blank tray slot. Replace that one file and
re-run: every downstream size is derived from it and nothing else has to change.

Why a square source is required: `web/meld_icon.png` is 1024x363, a wordmark. A wordmark cannot
be an app icon; at the 16x16 a tray slot gives you it is a grey smear. The other candidate,
`web/world_icon.png`, is square but 64x64, which upscales to a mushy 256px shortcut.

Outputs
    assets/icons/meld.ico          Windows: exe, shortcut, taskbar (16..256 in one file)
    assets/icons/meld.png          512px master for Linux + the .app bundle
    assets/icons/meld-<n>.png      16/24/32/48/64/128/256/512 for hicolor + the tray
    assets/icons/meld.icns         macOS - only when run on macOS (needs iconutil)

The .icns step is skipped on other platforms rather than approximated: Pillow's ICNS writer
covers a narrower set of sizes than iconutil, and a subtly wrong .icns shows up as a blurry Dock
icon that nobody notices until release. The macOS CI job runs this script and gets the real one.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ICON_DIR = ROOT / "assets" / "icons"
SOURCE = ICON_DIR / "meld-source.png"
MASTER = 1024
SIZES = [16, 24, 32, 48, 64, 128, 256, 512]
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]

GOLD_TOP = (247, 220, 149, 255)       # lit face
GOLD_LEFT = (227, 164, 23, 255)       # the brand gold, #e3a417
GOLD_RIGHT = (169, 114, 15, 255)      # shaded face


def placeholder(size: int = MASTER) -> Image.Image:
    """A flat gold block - the shipped icon reduced to its silhouette.

    This is the fallback for a checkout with no artwork, not the real icon: `meld-source.png` is,
    and it wins whenever it exists. Kept deliberately plain, because the only job here is to be
    findable in a tray full of blue circles when the real file is missing.

    Three faces, no outline: at 16px an outline eats the shape it is meant to define.
    """
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def p(x, y):
        return (s * x, s * y)

    d.polygon([p(.50, .06), p(.94, .31), p(.50, .56), p(.06, .31)], fill=GOLD_TOP)
    d.polygon([p(.06, .31), p(.50, .56), p(.50, .94), p(.06, .69)], fill=GOLD_LEFT)
    d.polygon([p(.94, .31), p(.94, .69), p(.50, .94), p(.50, .56)], fill=GOLD_RIGHT)
    return img


def load_source() -> tuple[Image.Image, bool]:
    """(master image, is_placeholder)."""
    if len(sys.argv) > 1:
        p = Path(sys.argv[1])
    else:
        p = SOURCE
    if p.is_file():
        img = Image.open(p).convert("RGBA")
        if img.width != img.height:
            raise SystemExit(f"{p} is {img.width}x{img.height}; the icon source must be square")
        if img.width < 512:
            print(f"warning: {p} is only {img.width}px — 1024 is wanted for a crisp 256px icon")
        return img.resize((MASTER, MASTER), Image.LANCZOS), False
    return placeholder(), True


def main() -> int:
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    master, is_placeholder = load_source()
    if is_placeholder:
        master.save(SOURCE)
        print(f"no source found — wrote a placeholder to {SOURCE.relative_to(ROOT)}")
        print("   replace it with a real 1024x1024 square PNG and re-run to rebrand everything")

    for n in SIZES:
        master.resize((n, n), Image.LANCZOS).save(ICON_DIR / f"meld-{n}.png")
    master.resize((512, 512), Image.LANCZOS).save(ICON_DIR / "meld.png")
    # Pillow builds the multi-resolution .ico itself; Windows picks the size it needs per surface
    # (16 in the tray, 32 in the taskbar, 256 on the desktop).
    master.save(ICON_DIR / "meld.ico", sizes=[(n, n) for n in ICO_SIZES])
    print(f"wrote {len(SIZES)} PNG sizes + meld.ico into {ICON_DIR.relative_to(ROOT)}")

    if sys.platform == "darwin" and shutil.which("iconutil"):
        with tempfile.TemporaryDirectory() as tmp:
            iconset = Path(tmp) / "meld.iconset"
            iconset.mkdir()
            for n in (16, 32, 128, 256, 512):
                master.resize((n, n), Image.LANCZOS).save(iconset / f"icon_{n}x{n}.png")
                master.resize((n * 2, n * 2), Image.LANCZOS).save(iconset / f"icon_{n}x{n}@2x.png")
            subprocess.run(["iconutil", "-c", "icns", str(iconset),
                            "-o", str(ICON_DIR / "meld.icns")], check=True)
        print("wrote meld.icns")
    else:
        print("skipped meld.icns (built on macOS by the release job)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
