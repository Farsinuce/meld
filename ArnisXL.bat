@echo off
REM ArnisXL one-click launcher (Windows). Double-click to start it.
REM
REM This window is the SETUP window: it shows the virtual environment being made, the
REM dependencies installing and the arnis binary being fetched, all of which take a few minutes
REM the first time and are worth watching. Once ArnisXL is up it hands off to a windowless
REM process and this window closes on its own - from then on the app lives in the tray, and the
REM only way to quit it is the tray icon's Quit.
REM
REM To watch it work instead, run `python arnisxl.py` in a terminal - same app, banner and all.
cd /d "%~dp0"
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
