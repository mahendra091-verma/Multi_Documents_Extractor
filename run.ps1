$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$pythonPath = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "The virtual environment is missing. Run .\install.ps1 first."
}

$envFile = Join-Path $PSScriptRoot ".env"
$hasKeyInEnvFile = (Test-Path -LiteralPath $envFile) -and
    (Select-String -LiteralPath $envFile -Pattern '^\s*GEMINI_API_KEY\s*=\s*[^\s#].*$' -Quiet)

if (-not $env:GEMINI_API_KEY -and -not $hasKeyInEnvFile) {
    $secret = Read-Host "Paste Gemini API key" -AsSecureString
    $env:GEMINI_API_KEY = [System.Net.NetworkCredential]::new("", $secret).Password
}

Write-Host "Open http://127.0.0.1:8001/docs after the server starts." -ForegroundColor Green
& $pythonPath -m uvicorn app:app --host 127.0.0.1 --port 8001
