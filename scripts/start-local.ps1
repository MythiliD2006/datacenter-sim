# Start / stop helpers for local (non-Docker) demo on Windows.
# Run from repo root:  .\scripts\start-local.ps1
# Then run a load profile:  .\scripts\run-profile.ps1 normal_day
#                           .\scripts\run-profile.ps1 flash_event

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$PromDir = Join-Path $Root ".tools\prometheus"
$GrafHome = Join-Path $Root ".tools\grafana-v11.0.0"
$WinExp = Join-Path $Root ".tools\windows_exporter\windows_exporter.exe"
$Prov = Join-Path $Root ".tools\grafana-provisioning"
$GrafData = Join-Path $Root ".tools\grafana-data"

function Test-Port($port) {
  return [bool](netstat -ano | Select-String ":$port\s+.*LISTENING")
}

Write-Host "== Starting local stack ==" -ForegroundColor Cyan

# 1) FastAPI
if (Test-Port 8000) {
  Write-Host "[ok] FastAPI already on :8000"
} else {
  if (-not (Test-Path $VenvPython)) {
    throw "Missing .venv. Create it first: py -3.11 -m venv .venv ; .\.venv\Scripts\python.exe -m pip install -r app/requirements.txt"
  }
  Start-Process -FilePath $VenvPython -ArgumentList "-m","uvicorn","app.main:app","--host","0.0.0.0","--port","8000" -WorkingDirectory $Root -WindowStyle Hidden
  Start-Sleep 3
  Write-Host "[ok] FastAPI started on :8000"
}

# 2) windows_exporter (host CPU/memory)
if (Test-Port 9182) {
  Write-Host "[ok] windows_exporter already on :9182"
} else {
  if (-not (Test-Path $WinExp)) { Write-Host "[skip] windows_exporter.exe not found - host CPU/mem panels will be empty" }
  else {
    Start-Process -FilePath $WinExp -ArgumentList "--web.listen-address=:9182","--collectors.enabled=cpu,cs,logical_disk,memory,net,os,system,process" -WindowStyle Hidden
    Start-Sleep 2
    Write-Host "[ok] windows_exporter started on :9182"
  }
}

# 3) Prometheus
if (Test-Port 9090) {
  Write-Host "[ok] Prometheus already on :9090"
} else {
  Start-Process -FilePath (Join-Path $PromDir "prometheus.exe") `
    -ArgumentList "--config.file=prometheus-local.yml","--web.listen-address=0.0.0.0:9090","--storage.tsdb.path=data","--web.enable-lifecycle" `
    -WorkingDirectory $PromDir -WindowStyle Hidden
  Start-Sleep 3
  Write-Host "[ok] Prometheus started on :9090"
}

# 4) Grafana
if (Test-Port 3000) {
  Write-Host "[ok] Grafana already on :3000"
} else {
  $env:GF_SECURITY_ADMIN_USER = "admin"
  $env:GF_SECURITY_ADMIN_PASSWORD = "admin"
  $env:GF_SERVER_HTTP_PORT = "3000"
  $env:GF_PATHS_PROVISIONING = $Prov
  $env:GF_PATHS_DATA = $GrafData
  New-Item -ItemType Directory -Force -Path $GrafData | Out-Null
  Start-Process -FilePath (Join-Path $GrafHome "bin\grafana-server.exe") `
    -ArgumentList "--homepath=$GrafHome","--config=$GrafHome\conf\defaults.ini" `
    -WorkingDirectory $GrafHome -WindowStyle Hidden
  Start-Sleep 5
  Write-Host "[ok] Grafana started on :3000"
}

Write-Host ""
Write-Host "Open:" -ForegroundColor Green
Write-Host "  FastAPI   http://localhost:8000/docs"
Write-Host "  Prometheus http://localhost:9090"
Write-Host "  Grafana    http://localhost:3000  (admin / admin)"
Write-Host "  Dashboard  Datacenter Sim -> Load Test"
Write-Host ""
Write-Host "Run a load profile:"
Write-Host "  .\scripts\run-profile.ps1 normal_day"
Write-Host "  .\scripts\run-profile.ps1 flash_event"
