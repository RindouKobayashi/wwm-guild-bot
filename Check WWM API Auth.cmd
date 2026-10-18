@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Missing bot .venv. Create it and install requirements-auth.txt first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m wwm_auth
set "WWM_AUTH_EXIT=%ERRORLEVEL%"
pause
exit /b %WWM_AUTH_EXIT%
