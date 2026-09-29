@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup.ps1"
if errorlevel 1 echo Installation failed. Ask Codex to inspect the non-secret error.
pause
