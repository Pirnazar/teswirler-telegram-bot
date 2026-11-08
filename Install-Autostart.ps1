param([switch]$Remove)
$ErrorActionPreference = 'Stop'
$taskName = 'TeswirlerTelegramBot'
$taskStartup = [Environment]::GetFolderPath('Startup')
$taskShortcutPath = Join-Path $taskStartup ($taskName + '.lnk')
if ($Remove) {
    if (Test-Path -LiteralPath $taskShortcutPath) { Remove-Item -LiteralPath $taskShortcutPath }
    Write-Host 'Autostart removed.'
    exit
}
$taskShell = New-Object -ComObject WScript.Shell
$taskShortcut = $taskShell.CreateShortcut($taskShortcutPath)
$taskShortcut.TargetPath = (Get-Command powershell.exe).Source
$taskShortcut.Arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + (Join-Path $PSScriptRoot 'Start-Bot.ps1') + '"'
$taskShortcut.WorkingDirectory = $PSScriptRoot
$taskShortcut.WindowStyle = 7
$taskShortcut.Description = 'Personal Teswirler notifications in Telegram'
$taskShortcut.Save()
Write-Host 'Autostart enabled for this Windows user at sign-in.'
