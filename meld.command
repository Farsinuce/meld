#!/usr/bin/env bash
# Meld one-click launcher (macOS). Double-click this file in Finder to start Meld.
# (First time: right-click > Open once, to get past Gatekeeper.)
# Finds Python 3, then hands off to meld_launch.py (venv + deps + arnis binary + start).
cd "$(dirname "$0")" || exit 1
PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "Meld needs Python 3.9+."
  echo "Install it with Homebrew:  brew install python"
  echo "  (or install the Xcode Command Line Tools:  xcode-select --install)"
  read -r -p "Press Enter to close..." _
  exit 1
fi
exec "$PY" meld_launch.py
