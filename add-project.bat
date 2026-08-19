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
if not defined PY echo Python was not found. & call :wait & exit /b 1
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
call :wait
exit /b 0

:wait
rem Hold the window open only when somebody double-clicked this, because
rem that window closes the instant the script ends and the result would
rem never be read. An unattended caller has nobody to press a key, and an
rem unconditional pause then waits for one until something times out.
rem
rem Whether it actually blocks depends on what stdin is: given a console
rem with nobody at it, pause waits for ever; given a null device or a
rem closed pipe it reads EOF and returns at once. So the same launcher can
rem hang a scheduled task and sail through a test harness, which is the
rem worst combination to debug - it works everywhere you try it and stops
rem the one place you do not.
rem
rem Double-clicked, Windows runs: cmd /c ""<full path to this file>" "
rem so the script's own name appears in CMDCMDLINE. Started from a console
rem that is already open, it does not.
rem
rem The test is cmd's own string substitution and NOT `find`, deliberately:
rem on a machine with Git Bash on PATH - which is this one - `find` resolves
rem to the Unix find, which does not take /i, errors, and silently sends the
rem test down whichever branch the error picks. A launcher must not depend
rem on which of two programs called `find` comes first in somebody's PATH.
rem The quotes come out of the value before it is compared. At a double
rem click CMDCMDLINE is  cmd /c ""<path>" "  - quotes and all - and an
rem `if "%A%"=="%B%"` around a value that already contains quotes is not a
rem comparison, it is a syntax error that takes whichever branch it lands
rem on. Stripping them first is what makes the test mean what it says.
if defined BRIDGE_NO_PAUSE goto :eof
set "CCL=%CMDCMDLINE%"
set "CCL=%CCL:"=%"
if "%CCL%"=="%CCL:add-project=%" goto :eof
pause
goto :eof
