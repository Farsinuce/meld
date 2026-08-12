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
INK = (58, 38, 6, 255)                # the edge between faces

#: Where the lighter and darker flecks sit on each face, in face-local grid steps. Fixed rather
#: than random so every rebuild produces a byte-identical icon.
FLECKS_TOP = ((1, 1), (3, 0), (4, 2), (2, 3))
FLECKS_LEFT = ((1, 2), (2, 5), (0, 4), (3, 7))
FLECKS_RIGHT = ((1, 3), (3, 1), (2, 6), (0, 5))

PIXEL_GRID = 32                       # the icon is drawn at 32x32 and then blown up


def pixel_cube(size: int = MASTER, grid: int = PIXEL_GRID) -> Image.Image:
    """The Meld block: an isometric cube drawn as pixel art, then scaled up with hard edges.

    Drawn small and enlarged with NEAREST on purpose. Rendering the same shape at 1024 and
    shrinking it gives smooth antialiased diagonals, which look like a vector logo; a Minecraft
    block wants visible pixel steps on its edges, and the only way to get them is to make the
    pixels big. Every intermediate size is generated from this same master, so the steps stay
    consistent instead of resampling into mush.

    Deterministic: the flecks come from a fixed table, so rebuilding never silently changes the
    icon that is already embedded in a released executable.
    """
    g = grid
    img = Image.new("RGBA", (g, g), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # A true 2:1 isometric cube. Half-width w spans the full grid; the top rhombus is w x w/2,
    # and the side faces are that rhombus edge extruded straight down by h.
    # For it to read as a CUBE and not a squat tile, the side faces have to be as tall as the
    # top rhombus is deep: total height = w (the rhombus) + h (the extrusion), and h == w is what
    # a real cube projects to in 2:1 isometric. w is sized so w + h still fits the grid.
    cx = g / 2
    w = g * 0.44                       # half width
    h = w                              # vertical extrusion of the side faces
    top_y = (g - (w + h)) / 2          # centre the whole silhouette vertically
    mid_y = top_y + w / 2              # where the left and right corners sit

    top = [(cx, top_y), (cx + w, mid_y), (cx, mid_y + w / 2), (cx - w, mid_y)]
    left = [(cx - w, mid_y), (cx, mid_y + w / 2), (cx, mid_y + w / 2 + h), (cx - w, mid_y + h)]
    right = [(cx + w, mid_y), (cx, mid_y + w / 2), (cx, mid_y + w / 2 + h), (cx + w, mid_y + h)]

    d.polygon(top, fill=GOLD_TOP)
    d.polygon(left, fill=GOLD_LEFT)
    d.polygon(right, fill=GOLD_RIGHT)

    def shade(colour, factor):
        r, gg, b, a = colour
        return (int(r * factor), int(gg * factor), int(b * factor), a)

    # Flecks: two-pixel blocks of a lighter and darker tone per face, the ore-in-stone read that
    # makes it look like a Minecraft block rather than a flat gem.
    for flecks, base, origin, step in (
            (FLECKS_TOP, GOLD_TOP, (cx - w * 0.45, top_y + w * 0.42), (2, 1.1)),
            (FLECKS_LEFT, GOLD_LEFT, (cx - w * 0.75, mid_y + w * 0.55), (2, 1.6)),
            (FLECKS_RIGHT, GOLD_RIGHT, (cx + w * 0.2, mid_y + w * 0.55), (2, 1.6))):
        for i, (fx, fy) in enumerate(flecks):
            x = origin[0] + fx * step[0]
            y = origin[1] + fy * step[1]
            tone = shade(base, 1.14 if i % 2 == 0 else 0.82)
            d.rectangle([x, y, x + 1, y + 1], fill=tone)

    # The vertical seam where the two side faces meet, and the silhouette. One pixel each: at
    # this grid size anything thicker eats the faces.
    d.line([(cx, mid_y + w / 2), (cx, mid_y + w / 2 + h)], fill=INK)
    d.line([(cx, top_y), (cx + w, mid_y), (cx + w, mid_y + h), (cx, mid_y + w / 2 + h),
            (cx - w, mid_y + h), (cx - w, mid_y), (cx, top_y)], fill=INK)

    return img.resize((size, size), Image.NEAREST)


def placeholder(size: int = MASTER) -> Image.Image:
    """The icon used when no artwork file is present. Same cube the brand uses."""
    return pixel_cube(size)


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
