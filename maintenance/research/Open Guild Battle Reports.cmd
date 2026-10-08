@echo off
setlocal
pushd "%~dp0..\.."
".venv\Scripts\python.exe" -B "maintenance\research\build_guild_report_viewer.py"
if errorlevel 1 goto failed
start "" "maintenance\research\output\guild-battle-viewer\index.html"
popd
exit /b
:failed
echo Could not build the viewer. Check the message above.
popd
pause
