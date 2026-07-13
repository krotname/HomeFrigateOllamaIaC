param(
  [string]$VmName = 'frigate-ubuntu',
  [string]$GuestAddress = '192.168.1.138',
  [int]$FailureThreshold = 3,
  [int]$CooldownMinutes = 30,
  [int]$StabilizationMinutes = 10,
  [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$watchdogRoot = 'C:\ProgramData\KRT\Watchdogs\FrigateVm'
$logPath = Join-Path $watchdogRoot 'watchdog.jsonl'
$statePath = Join-Path $watchdogRoot 'state.json'
$sshKey = 'C:\ProgramData\KRT\ConfigBackup\ssh\win-home-codex_ed25519'
$knownHosts = 'C:\Users\KRT\.ssh\known_hosts'
$heartbeatId = '84EAAE65-2F2E-45F5-9BB5-0E857DC8EB47'

function Rotate-Log {
  if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -gt 5MB) {
    Remove-Item -LiteralPath "$logPath.1" -Force -ErrorAction SilentlyContinue
    Move-Item -LiteralPath $logPath -Destination "$logPath.1" -Force
  }
}

function Write-WatchdogLog {
  param([string]$Level, [string]$Event, [string]$Detail)
  New-Item -ItemType Directory -Path $watchdogRoot -Force | Out-Null
  Rotate-Log
  [ordered]@{
    timestamp = (Get-Date).ToString('o')
    level = $Level
    event = $Event
    detail = $Detail
    vm = $VmName
    dryRun = $DryRun.IsPresent
  } | ConvertTo-Json -Compress | Add-Content -LiteralPath $logPath -Encoding utf8
}

function Get-State {
  if (Test-Path -LiteralPath $statePath) {
    try { return Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json } catch { }
  }
  return [pscustomobject]@{ failureStreak = 0; lastRecovery = [datetimeoffset]::MinValue.ToString('o') }
}

function Save-State {
  param($State)
  $State | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding utf8NoBOM
}

function Test-Heartbeat {
  $heartbeat = Get-VMIntegrationService -VMName $VmName |
    Where-Object { $_.Id -like "*$heartbeatId" } |
    Select-Object -First 1
  return $heartbeat -and $heartbeat.Enabled -and $heartbeat.PrimaryStatusDescription -in @('OK', 'ОК')
}

function Test-GuestGpu {
  $output = & ssh.exe -i $sshKey -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$knownHosts" "krt@$GuestAddress" 'nvidia-smi -L' 2>$null
  $exitCode = $LASTEXITCODE
  return [pscustomobject]@{
    Ssh = $exitCode -eq 0
    Gpu = $exitCode -eq 0 -and (($output -join "`n") -match 'GPU [0-9]+:')
    Detail = ($output -join '; ')
  }
}

function Wait-VmState {
  param([string]$State, [int]$TimeoutSeconds)
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  do {
    if ((Get-VM -Name $VmName).State.ToString() -eq $State) { return $true }
    Start-Sleep -Seconds 5
  } while ((Get-Date) -lt $deadline)
  return $false
}

function Recover-Vm {
  param([switch]$StartOnly)
  if ($DryRun) {
    Write-WatchdogLog 'warning' 'dry-run-recovery' "would recover VM; startOnly=$($StartOnly.IsPresent)"
    return
  }
  if (-not $StartOnly) {
    Write-WatchdogLog 'warning' 'graceful-shutdown' 'requesting guest shutdown through Hyper-V integration services'
    try { Stop-VM -Name $VmName -Shutdown -ErrorAction Stop } catch { Write-WatchdogLog 'warning' 'graceful-shutdown-error' $_.Exception.Message }
    if (-not (Wait-VmState -State 'Off' -TimeoutSeconds 120)) {
      Write-WatchdogLog 'warning' 'forced-poweroff' 'graceful shutdown timed out; turning VM off once'
      Stop-VM -Name $VmName -TurnOff -Force
      if (-not (Wait-VmState -State 'Off' -TimeoutSeconds 30)) { throw 'VM did not turn off' }
    }
  }
  Start-VM -Name $VmName | Out-Null
  if (-not (Wait-VmState -State 'Running' -TimeoutSeconds 120)) { throw 'VM did not reach Running state' }
  $deadline = (Get-Date).AddMinutes(5)
  do {
    if (Test-Heartbeat) {
      Write-WatchdogLog 'info' 'vm-recovered' 'VM is running and Hyper-V heartbeat is healthy'
      return
    }
    Start-Sleep -Seconds 10
  } while ((Get-Date) -lt $deadline)
  throw 'VM heartbeat did not recover'
}

if (-not (Test-Path -LiteralPath $sshKey) -or -not (Test-Path -LiteralPath $knownHosts)) { exit 2 }
New-Item -ItemType Directory -Path $watchdogRoot -Force | Out-Null
$mutex = [System.Threading.Mutex]::new($false, 'Global\FrigateVmWatchdog')
$hasLock = $false
try {
  $hasLock = $mutex.WaitOne(0)
  if (-not $hasLock) { exit 0 }
  $state = Get-State
  $vm = Get-VM -Name $VmName -ErrorAction Stop

  if ($vm.State -eq 'Off') {
    $last = [datetimeoffset]::Parse([string]$state.lastRecovery)
    if (((Get-Date) - $last.LocalDateTime).TotalMinutes -lt $CooldownMinutes) {
      Write-WatchdogLog 'warning' 'cooldown' 'VM is off but recovery is in cooldown'
      exit 1
    }
    $state.lastRecovery = (Get-Date).ToString('o')
    Recover-Vm -StartOnly
    $state.failureStreak = 0
    Save-State $state
    exit 0
  }

  if ($vm.State -eq 'Running' -and $vm.Uptime.TotalMinutes -lt $StabilizationMinutes) {
    $state.failureStreak = 0
    Save-State $state
    Write-WatchdogLog 'info' 'stabilizing' "uptime=$([math]::Round($vm.Uptime.TotalMinutes, 1)) minutes"
    exit 0
  }

  $heartbeatOk = $vm.State -eq 'Running' -and (Test-Heartbeat)
  $guest = if ($vm.State -eq 'Running') { Test-GuestGpu } else { [pscustomobject]@{ Ssh = $false; Gpu = $false; Detail = 'VM is not running' } }
  if ($heartbeatOk -and -not $guest.Ssh) {
    $state.failureStreak = 0
    Save-State $state
    Write-WatchdogLog 'warning' 'ssh-only-failure' 'heartbeat is healthy; VM restart suppressed'
    exit 1
  }
  if ($heartbeatOk -and $guest.Gpu) {
    $state.failureStreak = 0
    Save-State $state
    Write-WatchdogLog 'info' 'healthy' $guest.Detail
    exit 0
  }

  $state.failureStreak = [int]$state.failureStreak + 1
  Write-WatchdogLog 'warning' 'persistent-failure' "streak=$($state.failureStreak)/$FailureThreshold state=$($vm.State) heartbeat=$heartbeatOk ssh=$($guest.Ssh) gpu=$($guest.Gpu)"
  if ($state.failureStreak -lt $FailureThreshold) {
    Save-State $state
    exit 1
  }

  $lastRecovery = [datetimeoffset]::Parse([string]$state.lastRecovery)
  if (((Get-Date) - $lastRecovery.LocalDateTime).TotalMinutes -lt $CooldownMinutes) {
    Save-State $state
    Write-WatchdogLog 'warning' 'cooldown' 'persistent failure reached threshold but recovery is in cooldown'
    exit 1
  }
  $state.lastRecovery = (Get-Date).ToString('o')
  Recover-Vm
  $state.failureStreak = 0
  Save-State $state
  exit 0
} catch {
  Write-WatchdogLog 'error' 'fatal' $_.Exception.Message
  exit 2
} finally {
  if ($hasLock) { $mutex.ReleaseMutex() }
  $mutex.Dispose()
}
