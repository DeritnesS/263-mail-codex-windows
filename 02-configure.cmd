@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto missing
".venv\Scripts\python.exe" -m mail263 configure
if errorlevel 1 goto end
".venv\Scripts\python.exe" -m mail263 set-credential
goto end
:missing
echo Run 01-install.cmd first.
:end
pause
