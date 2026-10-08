@echo off
setlocal
pushd "%~dp0..\.."
".venv\Scripts\python.exe" -B "maintenance\research\lookup_public_guild_matches.py" --limit 40
if errorlevel 1 echo Research lookup failed. See the message above.
popd
pause
