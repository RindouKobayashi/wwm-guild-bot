@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -B activity\run_stack.py --environment test
if errorlevel 1 pause
