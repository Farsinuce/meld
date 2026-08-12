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

YELLOW = (245, 197, 66, 255)          # the brand colour: a warm amber, not a highlighter yellow
YELLOW_HI = (255, 224, 130, 255)      # lit top facet
YELLOW_LO = (196, 148, 32, 255)       # shaded bottom facet
INK = (26, 21, 8, 255)                # the seam and the outline


def placeholder(size: int = MASTER) -> Image.Image:
    """A yellow rhombus.

    Shape first, detail second: a diamond is one of the few silhouettes still identifiable at
    16x16, and in a tray of blue and grey circles a warm yellow diamond is findable without
    reading anything. It is drawn edge to edge with no plate behind it, because a rounded-square
    background would eat most of those 16 pixels and leave the mark too small to tell apart.

    The two facets are what stop it looking like a flat lozenge at large sizes; at 16px they
    merge into a single yellow shape, which is the intended reading.
    """
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # A slightly tall diamond, inset just enough that the outline is never clipped.
    cx, cy = s * 0.5, s * 0.5
    hw, hh = s * 0.40, s * 0.455
    top, right = (cx, cy - hh), (cx + hw, cy)
    bottom, left = (cx, cy + hh), (cx - hw, cy)
    outline_w = max(1, int(s * 0.028))

    d.polygon([top, right, bottom, left], fill=YELLOW, outline=INK, width=outline_w)
    # Upper facet catches the light, lower facet falls away - the classic gem read.
    d.polygon([top, right, (cx, cy), left], fill=YELLOW_HI)
    d.polygon([left, (cx, cy), right, bottom], fill=YELLOW_LO)
    # Re-draw the silhouette so the facet fills cannot spill over the outline.
    d.polygon([top, right, bottom, left], outline=INK, width=outline_w)
    # The waist seam, drawn short of the edges so it does not muddy the outline when shrunk.
    d.line([(cx - hw * 0.82, cy), (cx + hw * 0.82, cy)], fill=INK, width=max(1, int(s * 0.02)))
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
