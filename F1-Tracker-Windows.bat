@echo off
rem Double-click to start F1 Live Track Positions.
rem The first run sets everything up automatically (about a minute, needs internet).
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 run_f1_tracker.py %*
) else (
    where python >nul 2>nul
    if errorlevel 1 goto nopython
    python run_f1_tracker.py %*
)
if errorlevel 1 pause
goto :eof

:nopython
echo.
echo   Python isn't installed (or isn't on PATH).
echo   Install Python 3.12 or newer from https://www.python.org/downloads/
echo   and tick "Add python.exe to PATH" in the installer. Then run this again.
echo.
pause
