@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 goto :py

where python >nul 2>nul
if %errorlevel%==0 goto :python

where python3 >nul 2>nul
if %errorlevel%==0 goto :python3

echo Python was not found on PATH.
echo Install Python 3.11 or newer from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" during setup, then double-click this file again.
pause
exit /b 1

:py
py -3 dev.py %*
goto :done

:python
python dev.py %*
goto :done

:python3
python3 dev.py %*
goto :done

:done
if not %errorlevel%==0 pause
endlocal
