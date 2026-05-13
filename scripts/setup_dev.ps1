param(
    [string]$VenvDir = ".venv"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $root $VenvDir
$pythonExe = Join-Path $pythonExe "Scripts\python.exe"

if (-not (Test-Path $pythonExe)) {
    py -3 -m venv (Join-Path $root $VenvDir)
}

& $pythonExe -m pip install --upgrade pip
& $pythonExe -m pip install -r (Join-Path $root "requirements-dev.txt")

Write-Host "Environment ready:" $pythonExe
