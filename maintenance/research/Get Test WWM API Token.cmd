@echo off
setlocal
cd /d "%~dp0..\.."
if not exist ".venv\Scripts\python.exe" (
  echo The bot Python environment is missing: .venv\Scripts\python.exe
  pause
  exit /b 1
)
".venv\Scripts\python.exe" "maintenance\research\run_test_token_provider.py"
set "WWM_PROVIDER_EXIT=%ERRORLEVEL%"
pause
exit /b %WWM_PROVIDER_EXIT%
