# Orchestra — daily dev loop.
# Usage (from C:\dev\orchestra):
#   .\dev.ps1
#
# What it does, in order:
#   1. git pull                     — latest changes from GitHub
#   2. pip install (if changed)     — only if requirements.txt moved
#   3. uvicorn --reload             — server with HOT RELOAD
#
# Hot reload means: edit any .py file, save, and the server restarts
# by itself. Edit any .html or .css, refresh the browser tab. No manual
# restart. Ctrl+C to stop.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "==> Pulling latest from GitHub" -ForegroundColor Cyan
git pull --ff-only

# Reinstall deps only if requirements.txt changed in this pull
$reqStamp = ".venv\.reqs.hash"
$currentHash = (Get-FileHash requirements.txt).Hash
$lastHash = if (Test-Path $reqStamp) { Get-Content $reqStamp } else { "" }
if ($currentHash -ne $lastHash) {
    Write-Host "==> requirements.txt changed - installing" -ForegroundColor Cyan
    & ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
    Set-Content $reqStamp $currentHash
}

Write-Host ""
Write-Host "==> Starting server with hot reload" -ForegroundColor Green
Write-Host "    http://localhost:8765     (Ctrl+C to stop)" -ForegroundColor Green
Write-Host ""

& ".\.venv\Scripts\python.exe" -m uvicorn orchestra.web.server:app `
    --host 127.0.0.1 --port 8765 --reload `
    --reload-dir orchestra
