# Gemini-API-Server startup script (Windows)
# ASCII-only on purpose: Windows PowerShell 5.1 reads .ps1 as ANSI, so any
# non-ASCII byte (e.g. Chinese comments) can break tokenization on CN Windows.
# Usage: right-click "Run with PowerShell", or run: .\start.ps1
# Prereq: pip install -r requirements.txt  (see requirements.txt / INSTALL.md)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = $null
foreach ($cand in @(".venv\Scripts\python.exe", "python")) {
    try { $v = & $cand --version 2>$null; if ($v) { $py = $cand; break } } catch {}
}
if (-not $py) { Write-Host "[ERROR] Python not found. Install Python 3.11+ and run: pip install -r requirements.txt" -ForegroundColor Red; exit 1 }

if (Test-Path ".env") {
    $envFile = Get-Content ".env" -Raw
    if ([regex]::IsMatch($envFile, 'your_psid_value_here|your_psidts_value_here')) {
        Write-Host "[WARN] .env cookies are still placeholders! Fill SECURE_1PSID / SECURE_1PSIDTS or use the fingerprint browser launcher. Starting anyway in 5s..." -ForegroundColor Yellow
        Start-Sleep -Seconds 5
    }
} else {
    Copy-Item ".env.example" ".env"
    Write-Host "[INFO] .env created from .env.example - fill in your cookies first." -ForegroundColor Yellow
}

Write-Host "Chat API:    http://127.0.0.1:4444/v1" -ForegroundColor Green
Write-Host "Dashboard:   http://127.0.0.1:4445" -ForegroundColor Green
Write-Host "Browser API: http://127.0.0.1:4446" -ForegroundColor Green
& $py "gemini_core.py"
