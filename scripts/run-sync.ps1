$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$localData = Join-Path $env:LOCALAPPDATA 'Mail263Codex'
if (-not (Test-Path -LiteralPath $python)) { exit 10 }
if (-not (Test-Path -LiteralPath (Join-Path $localData 'config.json'))) { exit 11 }
# Only sanitized status metadata is emitted by refresh, never email bodies.
$result = & $python -m mail263 refresh
$exitCode = $LASTEXITCODE
$result | Set-Content -LiteralPath (Join-Path $localData 'last-scheduled-result.json') -Encoding UTF8
exit $exitCode
