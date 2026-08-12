#!/usr/bin/env python3
"""Build every icon ArnisXL needs from one square source.

    python packaging/make_icons.py [source.png]

Source: `assets/icons/arnisxl-source.png`, 1024x1024 RGBA. If it is missing, a placeholder is
drawn and used, so a clone with no art still produces a build with a working icon instead of
failing or shipping a blank tray slot. Drop a real square PNG in at that path and re-run - every
downstream size is derived, nothing else has to change.

Why a square source is required: the existing `web/meld_icon.png` is 1024x363, a wordmark. A
wordmark cannot be an app icon; at the 16x16 a tray slot gives you it is a grey smear. The other
candidate, `web/world_icon.png`, is square but 64x64, which upscales to a mushy 256px shortcut.

Outputs
    assets/icons/arnisxl.ico          Windows: exe, shortcut, taskbar (16..256 in one file)
    assets/icons/arnisxl.png          512px master for Linux + the .app bundle
    assets/icons/arnisxl-<n>.png      16/24/32/48/64/128/256/512 for hicolor + the tray
    assets/icons/arnisxl.icns         macOS - only when run on macOS (needs iconutil)

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
SOURCE = ICON_DIR / "arnisxl-source.png"
MASTER = 1024
SIZES = [16, 24, 32, 48, 64, 128, 256, 512]
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]

BG = (24, 27, 33, 255)
EDGE = (46, 52, 64, 255)
GREEN = (110, 231, 168, 255)
GREEN_DIM = (63, 150, 108, 255)


def placeholder(size: int = MASTER) -> Image.Image:
    """A terrain-ridge mark on a dark rounded square.

    Drawn to survive being shrunk to 16px: one silhouette, one accent, no thin strokes and no
    text. Real-world terrain turning into a world is what the app does, so a ridge line is a
    reasonable stand-in until the brand pass replaces it.
    """
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = s * 0.06
    d.rounded_rectangle([pad, pad, s - pad, s - pad], radius=s * 0.22, fill=BG,
                        outline=EDGE, width=max(1, int(s * 0.012)))

    # Back ridge (dimmer, sits behind) then the front ridge, both closed to the baseline so they
    # read as solid mass rather than as lines when the icon is tiny.
    base = s * 0.74
    back = [(s * 0.14, base), (s * 0.36, s * 0.40), (s * 0.52, s * 0.60),
            (s * 0.66, s * 0.44), (s * 0.88, base)]
    front = [(s * 0.14, base), (s * 0.33, s * 0.52), (s * 0.46, s * 0.66),
             (s * 0.62, s * 0.50), (s * 0.74, s * 0.62), (s * 0.88, base)]
    d.polygon(back, fill=GREEN_DIM)
    d.polygon(front, fill=GREEN)

    # A voxel notch: one square bitten out of the ridge, the only nod to Minecraft that stays
    # legible at 16px.
    q = s * 0.085
    d.rectangle([s * 0.455, s * 0.545, s * 0.455 + q, s * 0.545 + q], fill=BG)
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
        master.resize((n, n), Image.LANCZOS).save(ICON_DIR / f"arnisxl-{n}.png")
    master.resize((512, 512), Image.LANCZOS).save(ICON_DIR / "arnisxl.png")
    # Pillow builds the multi-resolution .ico itself; Windows picks the size it needs per surface
    # (16 in the tray, 32 in the taskbar, 256 on the desktop).
    master.save(ICON_DIR / "arnisxl.ico", sizes=[(n, n) for n in ICO_SIZES])
    print(f"wrote {len(SIZES)} PNG sizes + arnisxl.ico into {ICON_DIR.relative_to(ROOT)}")

    if sys.platform == "darwin" and shutil.which("iconutil"):
        with tempfile.TemporaryDirectory() as tmp:
            iconset = Path(tmp) / "arnisxl.iconset"
            iconset.mkdir()
            for n in (16, 32, 128, 256, 512):
                master.resize((n, n), Image.LANCZOS).save(iconset / f"icon_{n}x{n}.png")
                master.resize((n * 2, n * 2), Image.LANCZOS).save(iconset / f"icon_{n}x{n}@2x.png")
            subprocess.run(["iconutil", "-c", "icns", str(iconset),
                            "-o", str(ICON_DIR / "arnisxl.icns")], check=True)
        print("wrote arnisxl.icns")
    else:
        print("skipped arnisxl.icns (built on macOS by the release job)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
