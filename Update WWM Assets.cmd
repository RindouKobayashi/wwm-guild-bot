@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Missing bot .venv. Install the project dependencies first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -B "maintenance\assets\update_game_assets.py" --open-report %*
set "WWM_ASSET_EXIT=%ERRORLEVEL%"
pause
exit /b %WWM_ASSET_EXIT%
