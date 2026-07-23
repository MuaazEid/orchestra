# Orchestra — one-time setup.
# Run this ONCE. It clones the repo to C:\dev\orchestra, creates a
# virtualenv there, installs everything, and prints the two commands
# you'll use every day after.
#
# Usage (in PowerShell, any location):
#   irm https://raw.githubusercontent.com/MuaazEid/orchestra/main/scripts/setup.ps1 | iex
# Or, if you have this file locally, just:
#   powershell -ExecutionPolicy Bypass -File setup.ps1

$ErrorActionPreference = "Stop"
$root = "C:\dev\orchestra"

if (Test-Path $root) {
    Write-Host "[!] $root already exists — leaving it alone." -ForegroundColor Yellow
    Write-Host "    Delete it manually if you want a fresh clone." -ForegroundColor Yellow
    exit 0
}

Write-Host "==> Cloning to $root" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path "C:\dev" | Out-Null
git clone https://github.com/MuaazEid/orchestra.git $root
Set-Location $root

Write-Host "==> Creating virtualenv" -ForegroundColor Cyan
python -m venv .venv

Write-Host "==> Installing dependencies" -ForegroundColor Cyan
& "$root\.venv\Scripts\python.exe" -m pip install --upgrade pip
& "$root\.venv\Scripts\python.exe" -m pip install -r requirements.txt

Write-Host ""
Write-Host "==> Done. From now on, your daily loop is:" -ForegroundColor Green
Write-Host ""
Write-Host "    cd C:\dev\orchestra"
Write-Host "    .\dev.ps1                 # pull latest + start hot-reload server"
Write-Host ""
Write-Host "    Server:  http://localhost:8765"
Write-Host "    Ctrl+C to stop."
Write-Host ""
