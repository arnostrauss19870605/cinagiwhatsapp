# Sets up the app on Windows against a local PostgreSQL database.
# Run from the project folder:   .\scripts\dev_setup.ps1

$ErrorActionPreference = "Stop"

function Fail($message) {
    Write-Host ""
    Write-Host "STOPPED: $message" -ForegroundColor Red
    exit 1
}

# A stale PYTHONHOME or PYTHONPATH is the usual cause of
# "Could not find platform independent libraries <prefix>" and of pip
# resolving against the wrong interpreter. Clear them for this process only.
foreach ($name in @("PYTHONHOME", "PYTHONPATH")) {
    if (Test-Path "env:$name") {
        Write-Host "Ignoring $name=$((Get-Item "env:$name").Value) for this run" -ForegroundColor Yellow
        Remove-Item "env:$name"
    }
}

Write-Host "Checking Python..." -ForegroundColor Cyan
$py = $null
foreach ($candidate in @("py -3.13", "py -3.12", "python")) {
    try {
        $parts = $candidate.Split(" ")
        $version = & $parts[0] $parts[1..($parts.Length-1)] --version 2>$null
        if ($version -match "Python 3\.(1[2-9]|[2-9][0-9])") { $py = $candidate; break }
    } catch { }
}
if (-not $py) { Fail "Django 6 needs Python 3.12 or newer. Install it from python.org and run this again." }
Write-Host "Using $py" -ForegroundColor Green

if (-not (Test-Path ".venv")) {
    Write-Host "Creating the virtual environment..." -ForegroundColor Cyan
    Invoke-Expression "$py -m venv .venv"
    if ($LASTEXITCODE -ne 0) { Fail "Could not create the virtual environment." }
}

$venvPython = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) { Fail "The virtual environment looks incomplete - delete the .venv folder and run this again." }

# Confirm the venv's own interpreter is the version we think it is. If this
# disagrees with the check above, the environment is confused and pip will
# resolve against the wrong Python.
$venvVersion = & $venvPython -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
if ($LASTEXITCODE -ne 0) { Fail "The virtual environment's Python does not run. Delete .venv and try again." }
Write-Host "Virtual environment Python: $venvVersion" -ForegroundColor Green
if ($venvVersion -notmatch "^3\.(1[2-9]|[2-9][0-9])") {
    Fail "The virtual environment is on Python $venvVersion, but Django 6 needs 3.12 or newer. Delete .venv and run this again."
}

& $venvPython -m pip install --upgrade pip --quiet
if ($LASTEXITCODE -ne 0) { Fail "Could not upgrade pip." }

Write-Host "Installing dependencies (this takes a minute)..." -ForegroundColor Cyan
& $venvPython -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "Dependency installation failed. The full error above matters - look for the" -ForegroundColor Yellow
    Write-Host "block that starts 'The conflict is caused by:' and send me those lines." -ForegroundColor Yellow
    Fail "Dependencies not installed."
}

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Fail "Created .env - open it, set DB_PASSWORD, then run this again."
}

Write-Host "Setting up the database..." -ForegroundColor Cyan
& $venvPython manage.py migrate
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "If that was an authentication or connection error, check DB_PASSWORD, DB_USER" -ForegroundColor Yellow
    Write-Host "and DB_HOST in .env, and that PostgreSQL is running." -ForegroundColor Yellow
    Fail "Database setup did not complete."
}

Write-Host ""
Write-Host "Done. Two things left:" -ForegroundColor Green
Write-Host "  1.  .\.venv\Scripts\python.exe manage.py createsuperuser"
Write-Host "  2.  .\.venv\Scripts\python.exe manage.py runserver"
Write-Host ""
Write-Host "Then open http://localhost:8000" -ForegroundColor Green
