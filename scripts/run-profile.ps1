# Run a Locust profile against local FastAPI.
# Usage:  .\scripts\run-profile.ps1 normal_day
#         .\scripts\run-profile.ps1 flash_event

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$ProfileName = $args[0]
if (-not $ProfileName) {
  Write-Host "Usage: .\scripts\run-profile.ps1 <normal_day|flash_event>"
  exit 1
}

$ProfilePath = Join-Path $Root "profiles\$ProfileName.yaml"
if (-not (Test-Path $ProfilePath)) {
  Write-Host "Profile not found: $ProfilePath"
  exit 1
}

# Ensure FastAPI is up
try {
  $null = Invoke-WebRequest -Uri "http://127.0.0.1:8000/health" -UseBasicParsing -TimeoutSec 3
} catch {
  Write-Host "FastAPI is not running on :8000. Start it first: .\scripts\start-local.ps1"
  exit 1
}

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$VenvLocust = Join-Path $Root ".venv\Scripts\locust.exe"

# Install locust deps into venv if missing
if (-not (Test-Path $VenvLocust)) {
  Write-Host "Installing Locust into .venv ..."
  & $VenvPython -m pip install -r (Join-Path $Root "locust\requirements.txt")
}

Write-Host "Running profile: $ProfileName ($ProfilePath)" -ForegroundColor Cyan
Write-Host "Watch Grafana: http://localhost:3000  ->  Datacenter Sim / Load Test"
Write-Host ""

$env:LOCUST_PROFILE = $ProfilePath
& $VenvLocust `
  -f "locustfile.py,loadshapes.py" `
  --host "http://127.0.0.1:8000" `
  --headless `
  --csv "results_$ProfileName"
