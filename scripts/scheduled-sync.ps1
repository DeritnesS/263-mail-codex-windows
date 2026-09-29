param([ValidateSet('Install', 'Show', 'Remove')][string]$Action = 'Show')
$ErrorActionPreference = 'Stop'
$taskName = 'Mail263Codex-IncrementalSync'
if ($Action -eq 'Show') {
    Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue | Select-Object TaskName, State
    Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction SilentlyContinue
    exit
}
if ($Action -eq 'Remove') {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host 'Mail263Codex sync task removed. Mail and credentials were not deleted.'
    exit
}
$root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $root '.venv\Scripts\python.exe'))) { throw 'Install the project first.' }
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$script = Join-Path $PSScriptRoot 'run-sync.ps1'
$arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $script + '"'
$taskAction = New-ScheduledTaskAction -Execute $powershell -Argument $arguments -WorkingDirectory $root
$triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $identity),
    (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5))
)
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $triggers -Principal $principal -Settings $settings -Description 'Read-only 263 mail incremental cache; current logged-in Windows user only.' -Force | Select-Object TaskName, State
Write-Host 'Runs every 5 minutes while this user is logged in and the computer is awake. No wake timer, no notifications.'
