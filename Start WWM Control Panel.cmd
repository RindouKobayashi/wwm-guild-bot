@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Create the bot Python environment first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -B -m control_panel --environment test %*
if errorlevel 1 pause
