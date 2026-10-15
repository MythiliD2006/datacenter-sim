@echo off
rem datacenter-sim launcher for Windows. Double-click it, or run from a terminal:
rem   start.bat                 set up on first run, then open the control panel
rem   start.bat info            show this computer's capacity
rem   start.bat run normal_day  run one profile without the panel
rem If the folder is moved or copied, the environment is rebuilt automatically.
setlocal
cd /d "%~dp0"
set "HERE=%CD%"

set "PY="
where py >nul 2>nul && py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo [dcsim] Python 3.10 or newer is needed.
  echo [dcsim] Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH", then run start.bat again.
  pause
  exit /b 1
)

rem rebuild an environment that was created in another folder
if exist ".venv" (
  set "OLD="
  if exist ".venv\.installed" set /p OLD=<".venv\.installed"
  call :checkvenv
)

if not exist ".venv\Scripts\python.exe" (
  echo [dcsim] First run: creating a private Python environment...
  %PY% -m venv .venv || goto :venvfail
)

if not exist ".venv\.installed" (
  echo [dcsim] Installing datacenter-sim and its dependencies ^(one time, needs internet^)...
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -e . || goto :pipfail
  > ".venv\.installed" echo %HERE%
)

".venv\Scripts\python.exe" -m dcsim %*
if errorlevel 1 pause
exit /b %errorlevel%

:checkvenv
if /i not "%OLD%"=="%HERE%" (
  echo [dcsim] This folder was moved or the setup is incomplete - rebuilding the Python environment...
  rmdir /s /q ".venv"
  goto :eof
)
".venv\Scripts\python.exe" -c "import sys" >nul 2>nul || (
  echo [dcsim] The Python environment is broken - rebuilding it...
  rmdir /s /q ".venv"
)
goto :eof

:venvfail
echo [dcsim] Could not create the Python environment.
pause
exit /b 1

:pipfail
echo [dcsim] Installing dependencies failed. Check your internet connection and run start.bat again.
pause
exit /b 1