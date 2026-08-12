# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for ArnisXL - two executables, one shared payload.

    ArnisXL(.exe)          windowed. No console, so no taskbar button: it goes to the tray and
                           that is the whole visible surface. The shortcut points at this one.
    ArnisXL-console(.exe)  console. Same code, prints the banner and the live log, for anyone who
                           wants to watch it work or drive it from a script.

The console build is NOT called `arnisxl` - Windows filenames are case-insensitive, so
`arnisxl.exe` and `ArnisXL.exe` are one file, and COLLECT silently overwrote the windowed build
with the console one. The bug is invisible until someone notices the app opens a black window.

Both are built from the same Analysis and land in the same folder, so the ~120 MB payload is
paid for once rather than twice.

onedir, not onefile. onefile unpacks the whole bundle to a temp directory on every launch -
seconds of delay for an app people start by double-clicking, a temp dir that some corporate
policies forbid executing from, and a shape antivirus heuristics dislike. A folder also lets the
user drop their own arnis binary next to the exe, which is how the fork's builds have always
been swapped in.

Run it through packaging/build.py rather than directly: that script builds the icons and fetches
the matching arnis binary first, both of which this spec assumes are already in place.
"""
import sys
from pathlib import Path

# SPECPATH is set by PyInstaller to the folder holding this file.
ROOT = Path(SPECPATH).parent            # noqa: F821 - injected by PyInstaller
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

ICON_DIR = ROOT / "assets" / "icons"
icon_win = str(ICON_DIR / "arnisxl.ico") if (ICON_DIR / "arnisxl.ico").is_file() else None
icon_mac = str(ICON_DIR / "arnisxl.icns") if (ICON_DIR / "arnisxl.icns").is_file() else None
icon = icon_win if IS_WIN else (icon_mac if IS_MAC else None)


def tree(rel: str, dest: str | None = None):
    """(source, destination) for a folder, or nothing if it is not in this checkout."""
    p = ROOT / rel
    return [(str(p), dest or rel)] if p.exists() else []


def one(rel: str, dest: str = "."):
    p = ROOT / rel
    return [(str(p), dest)] if p.is_file() else []


datas = []
datas += tree("web")                    # index.html, mini.html, images
datas += tree("assets")                 # countries.geojson, loot presets, item registry, icons
datas += tree("tree-packs")             # region tree schematics
datas += tree("cave-pack")              # cave decoration schematics
datas += tree("region-convert/bin")     # prebuilt .b_linear converter, if vendored
datas += tree("packaging")              # shortcut scripts, shipped so the user can run them
datas += one("CHANGELOG.md")            # the version shown in the banner is read from here
datas += one("README.md")

hiddenimports = [
    "server",                # imported inside main(), after the banner
    "waitress",
    "waitress.server",
    "zstandard",
]
if IS_WIN:
    hiddenimports += ["pystray._win32"]
elif IS_MAC:
    hiddenimports += ["pystray._darwin"]
else:
    hiddenimports += ["pystray._xorg", "pystray._appindicator", "pystray._gtk"]

excludes = [
    # osmium is optional (baking a local .pbf into the offline OSM pack) and is a large
    # C++/pybind extension whose PyInstaller story is unreliable across platforms. Leaving it
    # out keeps the packaged build buildable everywhere; that one feature stays available on a
    # source install, where osmium is already an optional dependency.
    "osmium",
    "tkinter",
    "pytest",
    "IPython",
    "matplotlib",
]

a = Analysis(
    [str(ROOT / "arnisxl.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)                        # noqa: F821

exe_gui = EXE(                           # noqa: F821
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="ArnisXL",
    console=False,                       # <- no console window, no taskbar button
    icon=icon,
    debug=False,
    strip=False,
    upx=False,                           # UPX-packed exes trip antivirus heuristics
)

exe_cli = EXE(                           # noqa: F821
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="ArnisXL-console",              # see the header: NOT "arnisxl" (case-insensitive clash)
    console=True,                        # <- banner + live log
    icon=icon,
    debug=False,
    strip=False,
    upx=False,
)

coll = COLLECT(                          # noqa: F821
    exe_gui, exe_cli,
    a.binaries, a.datas,
    strip=False,
    upx=False,
    name="ArnisXL",
)

if IS_MAC:
    # LSUIElement makes it a menu-bar app: no Dock tile, no window, matching how it behaves in
    # the Windows tray. NSHighResolutionCapable stops the icon rendering at 1x on a Retina
    # display, which looks broken.
    app = BUNDLE(                        # noqa: F821
        coll,
        name="ArnisXL.app",
        icon=icon_mac,
        bundle_identifier="dev.arnisxl.app",
        info_plist={
            "LSUIElement": True,
            "NSHighResolutionCapable": True,
            "CFBundleName": "ArnisXL",
            "CFBundleDisplayName": "ArnisXL",
        },
    )
