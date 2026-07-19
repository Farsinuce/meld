@echo off
REM Meld one-click launcher (Windows). Double-click to start Meld.
REM Finds Python, then hands off to meld_launch.py (venv + deps + arnis binary + start).
cd /d "%~dp0"
where py >nul 2>nul && (
  py meld_launch.py
  goto :end
)
where python >nul 2>nul && (
  python meld_launch.py
  goto :end
)
echo Meld needs Python 3.9+.
echo Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH"),
echo or from the Microsoft Store, then double-click meld.bat again.
pause
:end
