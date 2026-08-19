@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
setlocal
cd /d "%~dp0"
if exist "source\bridgecore\daemon.py" cd source
title Bridge

echo.
echo   Bridge
echo   ------
echo.
echo   Folder: %CD%
echo.

if not exist "bridgecore\daemon.py" goto nolayout

set "PY="
where py >nul 2>&1 && set "PY=py"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY where python3 >nul 2>&1 && set "PY=python3"
if not defined PY goto nopython

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"
if errorlevel 1 goto oldpython

:: Finish the 2026-08-19 folder rebuild if it is still half done. Silent
:: when there is nothing to move, which is every start after the first.
:: Here, and not in a file somebody has to remember to run: at this point
:: the daemon is not running, so the files are free.
%PY% -m bridgecore.relayout

echo   Python: %PY%
echo   Starting. Your browser will open when it is ready.
echo   Keep this window open. Close it, or press Ctrl+C, to stop.
echo.

%PY% -m bridgecore.daemon %*

echo.
echo   The bridge stopped. If that was not on purpose, the reason is above.
pause
exit /b 0

:nolayout
echo   The folder layout is wrong.
echo.
echo   Next to this file there must be a folder called  bridge  containing
echo   daemon.py, hook.py, store.py, telegram.py, statusline.py,
echo   discover.py, install.py and panel.html.
echo.
echo   If you unpacked the zip, open the folder it made and run the
echo   bridge.bat that sits next to that subfolder.
echo.
pause
exit /b 1

:nopython
echo   Python was not found on this computer.
echo.
echo   Install it from https://www.python.org/downloads/ and tick
echo   "Add python.exe to PATH" during setup, then run this again.
echo.
pause
exit /b 1

:oldpython
echo   The Python found here is older than 3.9.
echo   Install a current version from https://www.python.org/downloads/
echo.
pause
exit /b 1
