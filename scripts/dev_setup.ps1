# Sets up the app on Windows against a local PostgreSQL database.
# Run from the project folder:   .\scripts\dev_setup.ps1

$ErrorActionPreference = "Stop"

Write-Host "Checking Python..." -ForegroundColor Cyan
$py = $null
foreach ($candidate in @("py -3.13", "py -3.12", "python")) {
    try {
        $parts = $candidate.Split(" ")
        $version = & $parts[0] $parts[1..($parts.Length-1)] --version 2>$null
        if ($version -match "Python 3\.(1[2-9]|[2-9][0-9])") { $py = $candidate; break }
    } catch { }
}
if (-not $py) {
    Write-Host "Django 6 needs Python 3.12 or newer. Install it from python.org, then run this again." -ForegroundColor Red
    exit 1
}
Write-Host "Using $py" -ForegroundColor Green

if (-not (Test-Path ".venv")) {
    Write-Host "Creating the virtual environment..." -ForegroundColor Cyan
    Invoke-Expression "$py -m venv .venv"
}

& .\.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
Write-Host "Installing dependencies (this takes a minute)..." -ForegroundColor Cyan
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt --quiet

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env - open it and set DB_PASSWORD before continuing." -ForegroundColor Yellow
    exit 1
}

Write-Host "Setting up the database..." -ForegroundColor Cyan
& .\.venv\Scripts\python.exe manage.py migrate

Write-Host ""
Write-Host "Done. Two things left:" -ForegroundColor Green
Write-Host "  1.  .\.venv\Scripts\python.exe manage.py createsuperuser"
Write-Host "  2.  .\.venv\Scripts\python.exe manage.py runserver"
Write-Host ""
Write-Host "Then open http://localhost:8000" -ForegroundColor Green
