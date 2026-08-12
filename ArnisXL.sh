#!/usr/bin/env bash
# ArnisXL one-click launcher (Linux). Double-click or run: ./ArnisXL.sh
# Finds Python 3, then hands off to meld_launch.py (venv + deps + arnis binary + start).
cd "$(dirname "$0")" || exit 1
PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "ArnisXL needs Python 3.9+."
  echo "Install it with your package manager, e.g.:"
  echo "  Debian/Ubuntu:  sudo apt install -y python3 python3-venv python3-pip"
  echo "  Fedora:         sudo dnf install -y python3 python3-pip"
  echo "  Arch:           sudo pacman -S python"
  read -r -p "Press Enter to close..." _
  exit 1
fi
exec "$PY" meld_launch.py
