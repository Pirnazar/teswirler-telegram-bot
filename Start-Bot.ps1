param([switch]$Foreground)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.secrets\telegram.json'))) {
    throw 'Connect Telegram first: python bot.py pair'
}
$taskPython = $null
foreach ($taskRelativePython in @('.venv\Scripts\python.exe', '.venv-public\Scripts\python.exe')) {
    $taskCandidatePython = Join-Path $PSScriptRoot $taskRelativePython
    if (Test-Path -LiteralPath $taskCandidatePython) { $taskPython = $taskCandidatePython; break }
}
if (-not $taskPython) {
    $taskPython = (& python -c 'import sys; print(sys.executable)').Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.10 or newer is required' }
}
$taskScript = Join-Path $PSScriptRoot 'public_bot.py'
if ($Foreground) {
    & $taskPython $taskScript
    exit $LASTEXITCODE
}
New-Item -ItemType Directory -Path (Join-Path $PSScriptRoot 'logs') -Force | Out-Null
$taskProcess = Start-Process -FilePath $taskPython -ArgumentList ('"' + $taskScript + '"') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $PSScriptRoot 'logs\stdout.log') -RedirectStandardError (Join-Path $PSScriptRoot 'logs\stderr.log')
Write-Host "Started bot process $($taskProcess.Id). Log: $PSScriptRoot\logs\public.log"
