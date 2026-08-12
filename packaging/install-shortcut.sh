#!/usr/bin/env bash
# Register ArnisXL with the desktop on Linux (and drop it in /Applications on macOS).
#
# ArnisXL ships as a folder you extract anywhere, so nothing has told the desktop it exists.
# This adds the launcher entry and the icon theme files pointing at wherever you put it. Re-run
# after moving the folder - the .desktop stores an absolute path.
#
#   ./packaging/install-shortcut.sh              install
#   ./packaging/install-shortcut.sh --autostart  also start at login
#   ./packaging/install-shortcut.sh --remove     undo
set -euo pipefail

APP="ArnisXL"
ID="arnisxl"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

AUTOSTART=0
REMOVE=0
for a in "$@"; do
  case "$a" in
    --autostart) AUTOSTART=1 ;;
    --remove) REMOVE=1 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

# ── macOS ─────────────────────────────────────────────────────────────────────
if [[ "$(uname -s)" == "Darwin" ]]; then
  BUNDLE="$ROOT/$APP.app"
  LINK="/Applications/$APP.app"
  if [[ $REMOVE -eq 1 ]]; then
    [[ -L "$LINK" ]] && rm "$LINK" && echo "removed $LINK"
    exit 0
  fi
  if [[ ! -d "$BUNDLE" ]]; then
    echo "No $APP.app here. On macOS, run the app bundle from the release archive," >&2
    echo "or from a source checkout just run: python3 arnisxl.py" >&2
    exit 1
  fi
  # A symlink, not a copy: the app folder holds the data dir and the arnis binary, and a copied
  # bundle would quietly leave those behind.
  ln -sfn "$BUNDLE" "$LINK"
  echo "linked $LINK -> $BUNDLE"
  echo "$APP runs as a menu-bar app (no Dock icon) - look next to the clock."
  exit 0
fi

# ── Linux ─────────────────────────────────────────────────────────────────────
APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"
AUTOSTART_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
DESKTOP="$APPS_DIR/$ID.desktop"

if [[ $REMOVE -eq 1 ]]; then
  rm -f "$DESKTOP" "$AUTOSTART_DIR/$ID.desktop"
  for n in 16 24 32 48 64 128 256 512; do
    rm -f "$ICON_ROOT/${n}x${n}/apps/$ID.png"
  done
  command -v update-desktop-database >/dev/null && update-desktop-database "$APPS_DIR" || true
  echo "removed the $APP desktop entry and icons"
  exit 0
fi

if [[ -x "$ROOT/$APP" ]]; then
  EXEC="$ROOT/$APP"
elif [[ -f "$ROOT/arnisxl.py" ]]; then
  EXEC="$(command -v python3 || command -v python) $ROOT/arnisxl.py"
else
  echo "Could not find the $APP binary or arnisxl.py under $ROOT" >&2
  exit 1
fi

mkdir -p "$APPS_DIR"
for n in 16 24 32 48 64 128 256 512; do
  src="$ROOT/assets/icons/arnisxl-$n.png"
  [[ -f "$src" ]] || continue
  mkdir -p "$ICON_ROOT/${n}x${n}/apps"
  cp -f "$src" "$ICON_ROOT/${n}x${n}/apps/$ID.png"
done

cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=$APP
Comment=Real-world maps into Minecraft worlds
Exec=$EXEC
Icon=$ID
Terminal=false
Categories=Game;Utility;
StartupNotify=false
EOF
chmod +x "$DESKTOP"

if [[ $AUTOSTART -eq 1 ]]; then
  mkdir -p "$AUTOSTART_DIR"
  cp -f "$DESKTOP" "$AUTOSTART_DIR/$ID.desktop"
  echo "will start at login"
fi

command -v update-desktop-database >/dev/null && update-desktop-database "$APPS_DIR" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -f -t "$ICON_ROOT" 2>/dev/null || true

echo "installed $DESKTOP"
echo
echo "Note: GNOME removed the system tray. If no $APP icon appears next to the clock, install"
echo "the 'AppIndicator and KStatusNotifierItem Support' extension, or run '$APP --no-tray'"
echo "and use the browser UI - the render itself does not need the tray."
