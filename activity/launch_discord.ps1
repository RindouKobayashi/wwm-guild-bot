$ErrorActionPreference = 'Stop'
$botRoot = Split-Path $PSScriptRoot -Parent
$pythonPath = Join-Path $botRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $pythonPath)) { throw 'The bot Python environment is missing.' }
Push-Location $botRoot
try {
    & $pythonPath -B activity/run_stack.py --environment test --activity-only
    if ($LASTEXITCODE -ne 0) { throw 'Activity test launcher failed. See activity/logs/test.' }
} finally { Pop-Location }
