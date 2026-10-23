@echo off
setlocal
cd /d "%~dp0"
where dotnet >nul 2>nul
if errorlevel 1 (
  echo Install the .NET 8 SDK first. No desktop Steam installation is needed.
  pause
  exit /b 1
)
dotnet restore wwm_auth\headless_steam\HeadlessSteam.csproj --locked-mode --nologo
if errorlevel 1 goto end
dotnet build wwm_auth\headless_steam\HeadlessSteam.csproj -c Release --no-restore --nologo
if errorlevel 1 goto end
dotnet wwm_auth\headless_steam\bin\Release\net8.0\HeadlessSteam.dll setup "%cd%\.wwm-auth\context.json"
:end
pause
