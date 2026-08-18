@echo off
setlocal
cd /d "%~dp0"
echo Use the panel instead - it lists your projects and adds them in one click.
echo This file is only here for the command line.
echo.
set "PY="
where py >nul 2>&1 && set "PY=py"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY where python3 >nul 2>&1 && set "PY=python3"
if not defined PY echo Python was not found. & pause & exit /b 1
set "FOLDER=%~1"
if "%FOLDER%"=="" set /p FOLDER=Full path to the project folder: 
%PY% -m bridge.install "%FOLDER%" --role %~2
echo.
pause
