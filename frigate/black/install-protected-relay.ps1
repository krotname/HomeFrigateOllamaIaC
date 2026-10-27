[CmdletBinding()]
param(
    [string]$Directory = 'C:\ProgramData\FrigateProtected',
    [string]$Helper = 'C:\AI\repo\krotname\CodexSkills\skin-mne\scripts\tg_bot.py'
)
$ErrorActionPreference = 'Stop'
if ($env:COMPUTERNAME -ne 'ADLER-WHITE-W1') { throw 'Relay belongs on ADLER-WHITE-W1' }
$pythonPath = 'C:\Python314\python.exe'
$sshPath = 'C:\Program Files\OpenSSH\ssh.exe'
foreach ($requiredPath in @($pythonPath,$sshPath,$Helper,(Join-Path $Directory 'ocr\frigate_protected_relay.py'))) {
    if (-not (Test-Path -LiteralPath $requiredPath)) { throw "Missing: $requiredPath" }
}
if (-not [Environment]::GetEnvironmentVariable('TELEGRAM_CODEX_BOT_TOKEN','User')) { throw 'Missing existing user bot token' }
$configPath = Join-Path $Directory 'relay.json'
$targetsPath = Join-Path (Split-Path (Split-Path $Helper)) 'config\telegram_targets.json'
$targets = Get-Content -Raw -LiteralPath $targetsPath | ConvertFrom-Json
if (-not (Test-Path -LiteralPath $configPath)) {
    @{python=$pythonPath;ssh=$sshPath;helper=$Helper;owner_chat=$targets.owner.userId;enabled_at=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()} |
        ConvertTo-Json | Set-Content -LiteralPath $configPath -Encoding utf8NoBOM
}
$taskName = 'Frigate protected trailer Telegram'
$action = New-ScheduledTaskAction -Execute 'C:\Program Files\PowerShell\7\pwsh.exe' -WorkingDirectory $Directory -Argument (
    '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -Command "& ''{0}'' -X utf8 -m ocr.frigate_protected_relay --directory ''{1}''; exit $LASTEXITCODE"' -f $pythonPath,$Directory)
$principal = New-ScheduledTaskPrincipal -UserId 'ADLER-WHITE-W1\KRT' -LogonType Interactive -RunLevel Limited
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User 'ADLER-WHITE-W1\KRT'
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Trigger @($trigger,$logonTrigger) -Settings $settings -Force | Select-Object TaskName,State
