@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -m mail263 doctor
pause
