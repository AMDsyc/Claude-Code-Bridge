@echo off
setlocal
cd /d "%~dp0"
if exist "source\bridgecore\daemon.py" cd source
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
rem The role is optional. It used to be passed straight through as %~2, so
rem calling this with a path alone produced "--role" with nothing after it
rem and argparse refused with exit 2 - the documented one-argument form was
rem the one form that could not work.
set "ROLE=%~2"
if "%ROLE%"=="" set "ROLE=executor"
%PY% -m bridgecore.install "%FOLDER%" --role %ROLE%
echo.
pause
