$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python is not installed or is not on PATH. Install Python 3.10 or newer, then reopen PowerShell."
}

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "Python 3.10 or newer is required."
}

python -m venv .venv
if ($LASTEXITCODE -ne 0) { throw "Could not create Python virtual environment." }

$pythonPath = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $pythonPath -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed. Check internet access and retry." }

& $pythonPath -m unittest -q
if ($LASTEXITCODE -ne 0) { throw "Installation finished, but the self-test failed." }

Write-Host "Installation complete. Run .\run.ps1 to start the API." -ForegroundColor Green
