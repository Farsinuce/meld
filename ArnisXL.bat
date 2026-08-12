@echo off
REM ArnisXL one-click launcher (Windows). Double-click to start it.
REM
REM Hands off to meld_launch.py, which sets up the virtual environment, fetches the arnis
REM binary for this machine, and then starts arnisxl.py (tray icon + server).
REM
REM This window closes on its own once the app is up: `start /b` runs the launcher without a
REM second console, and pythonw takes over from there with no window at all. To watch it work
REM instead, run `python arnisxl.py` in a terminal - same app, with the banner and live log.
cd /d "%~dp0"
where pyw >nul 2>nul && (
  start "" pyw meld_launch.py
  goto :end
)
where pythonw >nul 2>nul && (
  start "" pythonw meld_launch.py
  goto :end
)
where py >nul 2>nul && (
  py meld_launch.py
  goto :end
)
where python >nul 2>nul && (
  python meld_launch.py
  goto :end
)
echo ArnisXL needs Python 3.9+.
echo Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH"),
echo or from the Microsoft Store, then double-click ArnisXL.bat again.
echo.
echo (The packaged release needs no Python at all - it is a folder with ArnisXL.exe in it.)
pause
:end
