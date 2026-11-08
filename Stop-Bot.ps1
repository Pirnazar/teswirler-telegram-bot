$ErrorActionPreference = 'Stop'
$taskScript = Join-Path $PSScriptRoot 'public_bot.py'
$taskProcesses = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -and
    $_.CommandLine.Contains($taskScript)
}
foreach ($taskProcess in $taskProcesses) {
    Stop-Process -Id $taskProcess.ProcessId
    Write-Host "Stopped bot process $($taskProcess.ProcessId)"
}
if (-not $taskProcesses) { Write-Host 'Bot is not running.' }
