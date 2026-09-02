# ============================================================
# Moldflow Mobile - Local Backend Startup
# ============================================================
# Uses the dedicated normal Python virtual environment.
# Autodesk's embedded/free-threaded Python is NOT used.
# ============================================================

$ErrorActionPreference = "Stop"

$BackendDir = "C:\MF\MoldflowSynergyPlugin\mobile_backend"
$PythonExe = "C:\MF\MoldflowSynergyPlugin\mobile_backend\.cloud_venv\Scripts\python.exe"
$FirebaseCredentials = "C:\MF\MoldflowSynergyPlugin\secrets\moldflow-mobile-firebase-adminsdk-fbsvc-fb332a383b.json"

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " Moldflow Mobile Backend Startup" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

if (-not (Test-Path -LiteralPath $BackendDir -PathType Container)) {
    throw "Backend directory not found: $BackendDir"
}

if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Cloud backend Python not found: $PythonExe"
}

if (-not (Test-Path -LiteralPath $FirebaseCredentials -PathType Leaf)) {
    throw "Firebase credentials file not found: $FirebaseCredentials"
}

# Configure Firebase credentials for this backend process.
$env:GOOGLE_APPLICATION_CREDENTIALS = $FirebaseCredentials

# DATABASE_URL is intentionally preserved when supplied.
# No DATABASE_URL => current SQLite mode in app.py.
$DatabaseMode = if ($env:DATABASE_URL) { "POSTGRESQL" } else { "SQLITE" }

Set-Location -LiteralPath $BackendDir

Write-Host "Backend directory : $BackendDir" -ForegroundColor Green
Write-Host "Python executable : $PythonExe" -ForegroundColor Green
Write-Host "Firebase file     : $FirebaseCredentials" -ForegroundColor Green
Write-Host "Database mode     : $DatabaseMode" -ForegroundColor Green
Write-Host ""

Write-Host "Checking backend dependencies..." -ForegroundColor Yellow
& $PythonExe -c "import fastapi, uvicorn, jwt, pwdlib, psycopg; print('Backend dependencies OK')"

if ($LASTEXITCODE -ne 0) {
    throw "Backend dependency check failed."
}

Write-Host ""
Write-Host "Firebase credentials configured." -ForegroundColor Green
Write-Host "Starting FastAPI on http://0.0.0.0:8000 ..." -ForegroundColor Yellow
Write-Host "Press Ctrl+C to stop the backend." -ForegroundColor DarkGray
Write-Host ""

& $PythonExe -m uvicorn app:app --host 0.0.0.0 --port 8000
