@echo off
if exist "%~dp0..\..\.venv\Scripts\python.exe" (
  "%~dp0..\..\.venv\Scripts\python.exe" -B "%~dp0check_wwm_offline.py"
) else (
  py -3.11 -B "%~dp0check_wwm_offline.py"
)
pause
