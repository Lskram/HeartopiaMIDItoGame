$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $pythonExe)) {
    throw "Virtual environment not found. Run scripts\setup_dev.ps1 first."
}

& $pythonExe -m py_compile (Join-Path $root "heartopia_midi_bin_maker.py")

Write-Host "py_compile passed"
