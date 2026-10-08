@echo off
"%~dp0..\WWM-Toolkit\runtime\python\python.exe" -B "%~dp0maintenance\assets\refresh_dungeon_cards.py"
if errorlevel 1 (
  echo Dungeon card refresh failed. Previous ranking bundle retained.
  pause
  exit /b 1
)
"%~dp0..\WWM-Toolkit\runtime\python\python.exe" -B "%~dp0maintenance\assets\import_dungeon_assets.py" %*
if errorlevel 1 echo Refresh failed. Check the toolkit game-data export or pass --game-data-run with its path.
pause
