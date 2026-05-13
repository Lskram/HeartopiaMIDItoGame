@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    ".venv\Scripts\pythonw.exe" heartopia_midi_bin_maker.py
) else (
    python heartopia_midi_bin_maker.py
)
