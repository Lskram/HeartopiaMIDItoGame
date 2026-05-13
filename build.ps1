$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
$venvPyInstaller = Join-Path $root ".venv\Scripts\pyinstaller.exe"
$python = if (Test-Path $venvPython) { $venvPython } else { "python" }
$pyinstaller = if (Test-Path $venvPyInstaller) { $venvPyInstaller } else { "pyinstaller" }

& $python -m pip install -r (Join-Path $root "requirements-dev.txt")
& $pyinstaller --noconfirm --clean --distpath (Join-Path $root "dist") --workpath (Join-Path $root "build") (Join-Path $root "HeartopiaMidiBinMaker.spec")

Write-Host "Built: dist\\HeartopiaMidiBinMaker.exe"
