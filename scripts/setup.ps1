param([string]$Python = '')
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
if ($env:OS -ne 'Windows_NT') { throw 'Run this installer on Windows.' }
if (-not $Python) {
    foreach ($candidate in @('py', 'python')) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($command) {
            $candidateArgs = @('-c', 'import sys; print(sys.executable) if (3,11) <= sys.version_info[:2] < (3,15) else sys.exit(2)')
            $found = & $command.Source @candidateArgs 2>$null
            if ($LASTEXITCODE -eq 0 -and $found) { $Python = ([string]$found).Trim(); break }
        }
    }
}
if (-not $Python) { throw 'Python 3.11-3.14 is required. Ask Codex to help install an approved Python from python.org.' }
& $Python -c 'import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] < (3,15) else 2)'
if ($LASTEXITCODE -ne 0) { throw 'Unsupported Python version.' }
$venvPython = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $Python -m venv (Join-Path $root '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the local virtual environment.' }
}
& $venvPython -m pip install --disable-pip-version-check -c (Join-Path $root 'constraints.txt') $root
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Keep the non-secret error for Codex; do not disable TLS checks.' }
& $venvPython -m unittest discover -s (Join-Path $root 'tests') -v
if ($LASTEXITCODE -ne 0) { throw 'Offline tests failed. Stop before entering credentials.' }
Write-Host 'Installation and offline tests passed. Next: 02-configure.cmd (user enters credentials locally).'
