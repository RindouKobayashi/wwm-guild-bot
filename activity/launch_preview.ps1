$ErrorActionPreference = 'Stop'
$activityRoot = $PSScriptRoot
$botRoot = Split-Path $activityRoot -Parent
$pythonPath = Join-Path $botRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $pythonPath)) { throw 'The bot Python environment is missing.' }
if (!(Test-Path -LiteralPath (Join-Path $activityRoot 'public\snapshot.json'))) { throw 'Preview snapshots are missing. Run activity/build_preview_data.py first.' }
$previewUrl = 'http://127.0.0.1:8766'
$ready = $false
try { $state = Invoke-RestMethod "$previewUrl/api/config" -TimeoutSec 2; $ready = $state.service -eq 'wwm-activity' -and $state.mode -eq 'preview' } catch {}
if (!$ready) {
    $logRoot = Join-Path $activityRoot 'logs'
    New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
    Start-Process -FilePath $pythonPath -ArgumentList '-B activity/server.py' -WorkingDirectory $botRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logRoot 'preview.log') -RedirectStandardError (Join-Path $logRoot 'preview-error.log') | Out-Null
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 300
        try { $state = Invoke-RestMethod "$previewUrl/api/config" -TimeoutSec 1; $ready = $state.service -eq 'wwm-activity' -and $state.mode -eq 'preview' } catch {}
        if ($ready) { break }
    }
}
if (!$ready) { throw 'Preview could not start on port 8766. Check activity/logs or whether the port is occupied.' }
Start-Process $previewUrl
