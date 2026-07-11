param(
    [string]$BackupRoot = "F:\Files\Backups\win-home-configs",
    [int]$RetentionDays = 60,
    [string]$VmAddress = "192.168.1.138",
    [string]$VmUser = "krt",
    [string]$VmSshKeyPath = "C:\Users\KRT\.ssh\win-home-codex_ed25519",
    [string]$VmName = "frigate-ubuntu",
    [string]$LogRoot = "C:\ProgramData\KRT\ConfigBackup\logs",
    [switch]$TrustUnknownVmHostKey
)

$ErrorActionPreference = "Stop"
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
$transcriptPath = $null
$backupDir = $null
$staging = $null
$backupComplete = $false

if ($RetentionDays -lt 1 -or $RetentionDays -gt 3650) {
    throw "RetentionDays must be in range 1..3650."
}
if ($VmUser -notmatch '^[A-Za-z_][A-Za-z0-9_.-]{0,63}$') {
    throw "VmUser contains unsupported characters."
}
if ([string]::IsNullOrWhiteSpace($VmAddress) -or $VmAddress.Length -gt 253 -or
    $VmAddress -match '[\x00-\x20\x7F]' -or $VmAddress.StartsWith('-') -or
    [Uri]::CheckHostName($VmAddress) -notin @([UriHostNameType]::Dns, [UriHostNameType]::IPv4)) {
    throw "VmAddress must be a valid IPv4 address or DNS name."
}
if ($VmAddress -match '^[0-9.]+$') {
    $parsedVmAddress = $null
    if ($VmAddress -notmatch '^[0-9]{1,3}(\.[0-9]{1,3}){3}$' -or
        -not [Net.IPAddress]::TryParse($VmAddress, [ref]$parsedVmAddress) -or
        $parsedVmAddress.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) {
        throw "VmAddress must be a valid IPv4 address or DNS name."
    }
}
foreach ($pathToValidate in @($BackupRoot, $LogRoot)) {
    $fullPath = [System.IO.Path]::GetFullPath($pathToValidate)
    $pathRoot = [System.IO.Path]::GetPathRoot($fullPath)
    if ($fullPath.TrimEnd('\', '/') -eq $pathRoot.TrimEnd('\', '/')) {
        throw "Refusing to use a filesystem root as a managed backup path: $fullPath"
    }
}

function New-RestrictedDirectory {
    param([string]$Path)

    New-Item -ItemType Directory -Force -Path $Path | Out-Null

    $acl = New-Object System.Security.AccessControl.DirectorySecurity
    $inherit = [System.Security.AccessControl.InheritanceFlags]"ContainerInherit, ObjectInherit"
    $propagate = [System.Security.AccessControl.PropagationFlags]::None
    $rules = @(
        [pscustomobject]@{ Sid = "S-1-5-18"; Rights = [System.Security.AccessControl.FileSystemRights]::FullControl },
        [pscustomobject]@{ Sid = "S-1-5-32-544"; Rights = [System.Security.AccessControl.FileSystemRights]::FullControl }
    )

    try {
        $krtSid = ([System.Security.Principal.NTAccount]"$env:COMPUTERNAME\KRT").Translate([System.Security.Principal.SecurityIdentifier]).Value
        $rules += [pscustomobject]@{ Sid = $krtSid; Rights = [System.Security.AccessControl.FileSystemRights]::Modify }
    }
    catch {
        Write-Warning "Could not resolve local KRT account for backup ACL: $($_.Exception.Message)"
    }

    foreach ($rule in $rules) {
        $sid = [System.Security.Principal.SecurityIdentifier]::new($rule.Sid)
        $accessRule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            $sid,
            $rule.Rights,
            $inherit,
            $propagate,
            [System.Security.AccessControl.AccessControlType]::Allow
        )
        $acl.AddAccessRule($accessRule)
    }

    $acl.SetAccessRuleProtection($true, $false)
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Save-Json {
    param(
        [string]$Path,
        [object]$Value,
        [int]$Depth = 12
    )
    $tempPath = "$Path.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        $Value | ConvertTo-Json -Depth $Depth | Set-Content -LiteralPath $tempPath -Encoding UTF8
        [System.IO.File]::Move($tempPath, $Path, $true)
    }
    finally {
        if (Test-Path -LiteralPath $tempPath) {
            Remove-Item -LiteralPath $tempPath -Force
        }
    }
}

function ConvertTo-SafePathPart {
    param([string]$Value)
    (($Value -replace "^[A-Za-z]:", "") -replace "^[\\/]+", "" -replace "[:*?`"<>|]", "_" -replace "[\\/]+", "\")
}

function Test-ConfigFileName {
    param([System.IO.FileInfo]$File)

    $allowedExtensions = @(
        ".bat", ".cmd", ".conf", ".config", ".crl", ".crt", ".css", ".env",
        ".html", ".ini", ".js", ".json", ".mjs", ".ps1", ".psd1", ".psm1",
        ".service", ".timer", ".toml", ".txt", ".xml", ".yaml", ".yml"
    )
    $blockedNames = @(
        "*.bak", "*.db", "*.db-shm", "*.db-wal", "*.key", "*.log", "*.mp4",
        "*.onnx", "*.pfx", "*.pt", "*.sqlite", "*.sqlite3", "*.tmp",
        "ca-key.pem", "id_ed25519", "privkey.pem", "ssh_host_*_key"
    )

    foreach ($pattern in $blockedNames) {
        if ($File.Name -like $pattern) {
            return $false
        }
    }

    if ($File.Extension -eq ".pem" -and $File.Name -match "(?i)(key|priv)") {
        return $false
    }

    $allowedExtensions -contains $File.Extension.ToLowerInvariant()
}

function Test-ConfigRelativePath {
    param([string]$RelativePath)

    $blockedDirectoryNames = @(
        ".git", ".idea", ".playwright-cli", "__pycache__", "bin", "build",
        "cache", "caches", "dist", "log", "logs", "node_modules", "obj",
        "out", "target", "temp", "tmp", "www"
    )
    $parts = $RelativePath -split "[\\/]+"
    foreach ($part in $parts) {
        if ($blockedDirectoryNames -contains $part.ToLowerInvariant()) {
            return $false
        }
    }
    $true
}

function Copy-ConfigSource {
    param(
        [string]$Path,
        [string]$DestinationRoot,
        [string]$Label
    )

    $result = [ordered]@{
        path = $Path
        label = $Label
        exists = $false
        copied_files = 0
        skipped_files = 0
    }

    if (-not (Test-Path -LiteralPath $Path)) {
        return [pscustomobject]$result
    }

    $result.exists = $true
    $targetBase = Join-Path $DestinationRoot $Label
    New-Item -ItemType Directory -Force -Path $targetBase | Out-Null
    $item = Get-Item -LiteralPath $Path -Force

    if ($item.PSIsContainer) {
        $files = Get-ChildItem -LiteralPath $Path -File -Recurse -Force -ErrorAction Stop
        foreach ($file in $files) {
            $relative = $file.FullName.Substring($item.FullName.Length).TrimStart("\", "/")
            if (-not (Test-ConfigRelativePath -RelativePath $relative) -or -not (Test-ConfigFileName -File $file)) {
                $result.skipped_files++
                continue
            }
            $target = Join-Path $targetBase $relative
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
            Copy-Item -LiteralPath $file.FullName -Destination $target -Force
            $result.copied_files++
        }
    }
    elseif (Test-ConfigFileName -File $item) {
        Copy-Item -LiteralPath $item.FullName -Destination (Join-Path $targetBase $item.Name) -Force
        $result.copied_files++
    }
    else {
        $result.skipped_files++
    }

    [pscustomobject]$result
}

function Invoke-NativeChecked {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [int[]]$AllowedExitCodes = @(0)
    )

    $output = & $FilePath @Arguments 2>&1
    $exitCode = $LASTEXITCODE
    if ($AllowedExitCodes -notcontains $exitCode) {
        throw "$FilePath failed with exit code $exitCode. Output: $($output -join "`n")"
    }
    $output
}

New-RestrictedDirectory -Path $BackupRoot
New-RestrictedDirectory -Path $LogRoot
$transcriptPath = Join-Path $LogRoot "config-backup-$timestamp.log"
Start-Transcript -Path $transcriptPath -Force | Out-Null

try {
    $backupDir = Join-Path $BackupRoot "config-backup-$timestamp"
    $staging = Join-Path $backupDir "staging"
    $windowsStage = Join-Path $staging "windows"
    New-RestrictedDirectory -Path $backupDir
    New-Item -ItemType Directory -Force -Path $windowsStage | Out-Null

    $identity = [pscustomobject]@{
        generated_at = (Get-Date).ToString("s")
        hostname = hostname
        whoami = whoami
        ps_version = $PSVersionTable.PSVersion.ToString()
        backup_root = $BackupRoot
        backup_dir = $backupDir
        retention_days = $RetentionDays
        scope = "configuration-only"
    }
    Save-Json -Path (Join-Path $windowsStage "host-identity.json") -Value $identity

    Save-Json -Path (Join-Path $windowsStage "network.json") -Value ([pscustomobject]@{
        ip_configuration = @(Get-NetIPConfiguration | ForEach-Object {
            [pscustomobject]@{
                interface_alias = $_.InterfaceAlias
                interface_description = $_.InterfaceDescription
                ipv4 = @($_.IPv4Address | ForEach-Object IPAddress)
                gateway = @($_.IPv4DefaultGateway | ForEach-Object NextHop)
                dns = @($_.DNSServer.ServerAddresses)
            }
        })
        adapters = @(Get-NetAdapter | Select-Object Name, InterfaceDescription, Status, MacAddress, LinkSpeed)
        netbios = @(Get-CimInstance Win32_NetworkAdapterConfiguration -Filter "IPEnabled=True" | Select-Object Description, Index, TcpipNetbiosOptions, IPAddress, DefaultIPGateway, DNSHostName)
        bindings = @(Get-NetAdapterBinding | Select-Object Name, DisplayName, ComponentID, Enabled)
    })

    Save-Json -Path (Join-Path $windowsStage "security-local.json") -Value ([pscustomobject]@{
        password_policy = @(net accounts)
        local_users = @(Get-LocalUser | Select-Object Name, Enabled, LastLogon, PasswordRequired, PasswordLastSet, PasswordExpires, UserMayChangePassword)
        local_groups = @(Get-LocalGroup | Select-Object Name, Description, PrincipalSource, SID)
        administrators = @(Get-LocalGroupMember -Group (Get-LocalGroup -SID "S-1-5-32-544").Name | Select-Object Name, ObjectClass, PrincipalSource, SID)
    })

    Save-Json -Path (Join-Path $windowsStage "smb.json") -Value ([pscustomobject]@{
        shares = @(Get-SmbShare | Select-Object Name, Path, Description, ShareState, Special, EncryptData, FolderEnumerationMode)
        share_access = @(Get-SmbShare | ForEach-Object {
            $shareName = $_.Name
            Get-SmbShareAccess -Name $shareName | Select-Object @{n="ShareName";e={$shareName}}, AccountName, AccessControlType, AccessRight
        })
        server_configuration = Get-SmbServerConfiguration | Select-Object EnableSMB1Protocol, EnableSMB2Protocol, EncryptData, RejectUnencryptedAccess, EnableSecuritySignature, RequireSecuritySignature, AuditSmb1Access
    })

    Save-Json -Path (Join-Path $windowsStage "services.json") -Value ([pscustomobject]@{
        running_non_microsoft = @(Get-CimInstance Win32_Service | Where-Object { $_.State -eq "Running" -and ($_.PathName -notmatch "\\Windows\\System32|svchost.exe") } | Select-Object Name, DisplayName, State, StartMode, StartName, PathName)
        auto_stopped = @(Get-CimInstance Win32_Service | Where-Object { $_.StartMode -eq "Auto" -and $_.State -ne "Running" } | Select-Object Name, DisplayName, State, StartMode, StartName)
    })

    $tasksRoot = Join-Path $windowsStage "scheduled-tasks"
    New-Item -ItemType Directory -Force -Path $tasksRoot | Out-Null
    $taskRows = @()
    foreach ($task in (Get-ScheduledTask | Where-Object { $_.TaskPath -notlike "\Microsoft\*" })) {
        $taskFileName = (($task.TaskPath.Trim("\") + "_" + $task.TaskName) -replace "[\\/:*?`"<>|]", "_")
        if ([string]::IsNullOrWhiteSpace($taskFileName)) {
            $taskFileName = $task.TaskName -replace "[\\/:*?`"<>|]", "_"
        }
        $taskPath = Join-Path $tasksRoot "$taskFileName.xml"
        Export-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath | Set-Content -LiteralPath $taskPath -Encoding UTF8
        $info = $null
        try { $info = Get-ScheduledTaskInfo -TaskName $task.TaskName -TaskPath $task.TaskPath -ErrorAction Stop } catch {}
        $taskRows += [pscustomobject]@{
            path = $task.TaskPath
            name = $task.TaskName
            state = $task.State.ToString()
            last_run_time = if ($info) { $info.LastRunTime } else { $null }
            last_task_result = if ($info) { $info.LastTaskResult } else { $null }
            xml = "scheduled-tasks/$([IO.Path]::GetFileName($taskPath))"
        }
    }
    Save-Json -Path (Join-Path $windowsStage "scheduled-tasks.json") -Value $taskRows

    try {
        netsh advfirewall export (Join-Path $windowsStage "firewall-policy.wfw") | Out-Null
    }
    catch {
        $_.Exception.Message | Set-Content -LiteralPath (Join-Path $windowsStage "firewall-policy-export-error.txt") -Encoding UTF8
    }
    Save-Json -Path (Join-Path $windowsStage "firewall-rules.json") -Value ([pscustomobject]@{
        profiles = @(Get-NetFirewallProfile | Select-Object Name, Enabled, DefaultInboundAction, DefaultOutboundAction, AllowInboundRules, NotifyOnListen, LogFileName, LogAllowed, LogBlocked)
        rules = @(Get-NetFirewallRule | Select-Object Name, DisplayName, Enabled, Direction, Action, Profile, Group, Program, Service)
    })

    Save-Json -Path (Join-Path $windowsStage "hyperv.json") -Value ([pscustomobject]@{
        vm_host = Get-VMHost | Select-Object LogicalProcessorCount, MemoryCapacity, VirtualHardDiskPath, VirtualMachinePath, NumaSpanningEnabled, EnableEnhancedSessionMode
        vms = @(Get-VM | ForEach-Object {
            $vm = $_
            [pscustomobject]@{
                name = $vm.Name
                state = $vm.State.ToString()
                generation = $vm.Generation
                version = $vm.Version
                automatic_start_action = $vm.AutomaticStartAction.ToString()
                automatic_start_delay = $vm.AutomaticStartDelay
                automatic_stop_action = $vm.AutomaticStopAction.ToString()
                processor = Get-VMProcessor -VMName $vm.Name | Select-Object Count, CompatibilityForMigrationEnabled
                memory = Get-VMMemory -VMName $vm.Name | Select-Object Startup, DynamicMemoryEnabled, Minimum, Maximum
                network = @(Get-VMNetworkAdapter -VMName $vm.Name | Select-Object Name, SwitchName, MacAddress, IPAddresses)
                hard_disks = @(Get-VMHardDiskDrive -VMName $vm.Name | Select-Object ControllerType, ControllerNumber, ControllerLocation, Path)
                assignable_devices = @(Get-VMAssignableDevice -VMName $vm.Name | Select-Object InstanceID, LocationPath)
            }
        })
    })

    winrm get winrm/config | Set-Content -LiteralPath (Join-Path $windowsStage "winrm-config.txt") -Encoding UTF8

    $copyResults = @()
    $sources = @(
        @{ Path = "C:\ProgramData\ssh"; Label = "programdata_ssh" },
        @{ Path = "C:\ProgramData\Frigate"; Label = "programdata_frigate" },
        @{ Path = "C:\ProgramData\KRT\FrigateCA"; Label = "frigate_ca_public" },
        @{ Path = "F:\Server\Torrents\aria2.conf"; Label = "aria2" },
        @{ Path = "C:\server\aria2-web"; Label = "aria2_web" },
        @{ Path = "C:\server\fileserver-index"; Label = "fileserver_index" },
        @{ Path = "C:\server\openclaw"; Label = "openclaw" },
        @{ Path = "C:\server\CodexKrtBot2"; Label = "codexkrtbot2" },
        @{ Path = "C:\soft\v2rayN-windows-64-desktop\v2rayN-windows-64\guiConfigs"; Label = "v2rayn_guiconfigs" }
    )
    foreach ($source in $sources) {
        $copyResults += Copy-ConfigSource -Path $source.Path -DestinationRoot (Join-Path $windowsStage "files") -Label $source.Label
    }
    Save-Json -Path (Join-Path $windowsStage "copied-file-sources.json") -Value $copyResults

    $windowsZip = Join-Path $backupDir "windows-configs.zip"
    Compress-Archive -Path (Join-Path $windowsStage "*") -DestinationPath $windowsZip -CompressionLevel Optimal -Force

    $vmBackupPath = Join-Path $backupDir "vm-configs.tar.gz"
    $vmInventoryPath = Join-Path $backupDir "vm-inventory.json"
    $vmBackup = [ordered]@{
        enabled = $true
        vm_name = $VmName
        vm_address = $VmAddress
        ssh_key_path = $VmSshKeyPath
        archive = $vmBackupPath
        inventory = $vmInventoryPath
        status = "not_started"
        error = $null
    }

    if (-not (Test-Path -LiteralPath $VmSshKeyPath)) {
        throw "VM SSH key not found: $VmSshKeyPath"
    }

    $sshBaseArgs = @(
        "-i", $VmSshKeyPath,
        "-o", "CertificateFile=none",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10"
    )
    if ($TrustUnknownVmHostKey) {
        $sshBaseArgs += @("-o", "StrictHostKeyChecking=accept-new")
    }
    else {
        $sshBaseArgs += @("-o", "StrictHostKeyChecking=yes")
    }
    $target = "$VmUser@$VmAddress"
    $remoteScript = @'
set -euo pipefail
umask 077
tmp="$(mktemp /tmp/win-home-vm-configs.XXXXXXXX.tar.gz)"
inventory="$(mktemp /tmp/win-home-vm-inventory.XXXXXXXX.json)"
completed=0
cleanup_on_failure() {
  if [ "$completed" -ne 1 ]; then
    sudo rm -f -- "$tmp" "$inventory"
  fi
}
trap cleanup_on_failure EXIT
paths=(
  "/opt/frigate/config/config.yml"
  "/opt/frigate/docker-compose.yml"
  "/opt/frigate/.env"
  "/opt/frigate/certs/ca.pem"
  "/opt/frigate/certs/cert.pem"
  "/opt/frigate/certs/fullchain.pem"
  "/opt/frigate/certs/KRT-Frigate-Local-Root-CA-2026.crl"
  "/opt/asr/docker-compose.yml"
  "/opt/asr/.env"
  "/etc/nginx/sites-available"
  "/etc/nginx/sites-enabled"
  "/etc/docker/daemon.json"
  "/etc/netplan"
  "/etc/ssh/sshd_config"
  "/etc/fstab"
  "/etc/hosts"
  "/etc/systemd/system/ollama.service.d"
)
existing=()
for p in "${paths[@]}"; do
  if [ -e "$p" ]; then
    existing+=("${p#/}")
  fi
done
if [ "${#existing[@]}" -eq 0 ]; then
  echo "No VM config paths found" >&2
  exit 20
fi
sudo tar -C / --warning=no-file-changed -czf "$tmp" "${existing[@]}"
python3 - "${existing[@]}" <<'PY' > "$inventory"
import json, os, pathlib, subprocess
def run(cmd):
    p = subprocess.run(cmd, text=True, capture_output=True)
    return {"rc": p.returncode, "out": p.stdout.strip(), "err": p.stderr.strip()}
print(json.dumps({
  "hostname": run(["hostname"])["out"],
  "uname": run(["uname", "-a"])["out"],
  "os_release": pathlib.Path("/etc/os-release").read_text(),
  "docker_ps": run(["docker", "ps", "--format", "{{json .}}"])["out"].splitlines(),
  "services": {
    "docker": run(["systemctl", "is-active", "docker"])["out"],
    "ollama": run(["systemctl", "is-active", "ollama"])["out"],
    "nginx": run(["systemctl", "is-active", "nginx"])["out"],
    "ssh": run(["systemctl", "is-active", "ssh"])["out"],
  },
  "config_paths": __import__("sys").argv[1:]
}, ensure_ascii=True))
PY
completed=1
printf '%s\n%s\n' "$tmp" "$inventory"
'@

    $remoteArchive = $null
    $remoteInventory = $null
    try {
        $remoteOutput = $remoteScript | & ssh.exe @sshBaseArgs $target "bash -s" 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "VM config archive command failed with exit $LASTEXITCODE. Output: $($remoteOutput -join "`n")"
        }
        $remoteLines = @($remoteOutput | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        if ($remoteLines.Count -lt 2) {
            throw "VM config archive command did not return expected paths. Output: $($remoteOutput -join "`n")"
        }
        $remoteArchive = [string]$remoteLines[-2]
        $remoteInventory = [string]$remoteLines[-1]
        if ($remoteArchive -notmatch '^/tmp/win-home-vm-configs\.[A-Za-z0-9]+\.tar\.gz$' -or
            $remoteInventory -notmatch '^/tmp/win-home-vm-inventory\.[A-Za-z0-9]+\.json$') {
            throw "VM config archive command returned unsafe paths."
        }

        Invoke-NativeChecked -FilePath "scp.exe" -Arguments ($sshBaseArgs + @("$target`:$remoteArchive", $vmBackupPath)) | Out-Null
        Invoke-NativeChecked -FilePath "scp.exe" -Arguments ($sshBaseArgs + @("$target`:$remoteInventory", $vmInventoryPath)) | Out-Null
        $vmBackup.status = "ok"
    }
    finally {
        if ($remoteArchive -and $remoteInventory) {
            try {
                Invoke-NativeChecked -FilePath "ssh.exe" -Arguments (
                    $sshBaseArgs + @($target, "sudo rm -f -- '$remoteArchive' '$remoteInventory'")
                ) | Out-Null
            }
            catch {
                Write-Warning "Could not remove temporary VM backup files: $($_.Exception.Message)"
            }
        }
    }

    Remove-Item -LiteralPath $staging -Recurse -Force

    $windowsZipInfo = Get-Item -LiteralPath $windowsZip
    $vmTarInfo = Get-Item -LiteralPath $vmBackupPath
    $zipArchive = [System.IO.Compression.ZipFile]::OpenRead($windowsZip)
    try {
        if ($zipArchive.Entries.Count -eq 0) {
            throw "Windows configuration archive is empty."
        }
    }
    finally {
        $zipArchive.Dispose()
    }
    $vmArchiveEntries = @(Invoke-NativeChecked -FilePath "tar.exe" -Arguments @("-tzf", $vmBackupPath))
    if ($vmArchiveEntries.Count -eq 0) {
        throw "VM configuration archive is empty."
    }
    $vmInventory = Get-Content -Raw -LiteralPath $vmInventoryPath | ConvertFrom-Json
    if ([string]::IsNullOrWhiteSpace($vmInventory.hostname)) {
        throw "VM inventory is invalid or has no hostname."
    }
    $manifest = [pscustomobject]@{
        generated_at = (Get-Date).ToString("s")
        hostname = hostname
        backup_id = "win-home-config-$timestamp"
        scope = "configuration-only"
        backup_dir = $backupDir
        windows_configs_zip = [pscustomobject]@{
            path = $windowsZip
            bytes = $windowsZipInfo.Length
            sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $windowsZip).Hash
        }
        vm_configs_tar_gz = [pscustomobject]@{
            path = $vmBackupPath
            bytes = $vmTarInfo.Length
            sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $vmBackupPath).Hash
        }
        vm_inventory = [pscustomobject]@{
            path = $vmInventoryPath
            bytes = (Get-Item -LiteralPath $vmInventoryPath).Length
            sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $vmInventoryPath).Hash
        }
        verification = [pscustomobject]@{
            windows_zip_present = (Test-Path -LiteralPath $windowsZip)
            windows_zip_min_size_ok = $windowsZipInfo.Length -gt 1024
            windows_zip_readable = $true
            vm_archive_present = (Test-Path -LiteralPath $vmBackupPath)
            vm_archive_min_size_ok = $vmTarInfo.Length -gt 1024
            vm_archive_readable = $true
            vm_inventory_readable = $true
        }
        retention_days = $RetentionDays
        transcript = $transcriptPath
    }
    Save-Json -Path (Join-Path $backupDir "manifest.json") -Value $manifest
    Save-Json -Path (Join-Path $BackupRoot "latest.json") -Value $manifest
    $backupComplete = $true

    $cutoff = (Get-Date).AddDays(-1 * $RetentionDays)
    Get-ChildItem -LiteralPath $BackupRoot -Directory -Filter "config-backup-*" -ErrorAction SilentlyContinue |
        Where-Object { $_.CreationTime -lt $cutoff } |
        Remove-Item -Recurse -Force

    Write-Output "Backup complete: $backupDir"
    Write-Output "Windows config archive bytes: $($windowsZipInfo.Length)"
    Write-Output "VM config archive bytes: $($vmTarInfo.Length)"
}
finally {
    if ($staging -and (Test-Path -LiteralPath $staging)) {
        Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (-not $backupComplete -and $backupDir -and (Test-Path -LiteralPath $backupDir)) {
        $resolvedBackupRoot = [System.IO.Path]::GetFullPath($BackupRoot).TrimEnd('\', '/')
        $resolvedBackupDir = [System.IO.Path]::GetFullPath($backupDir)
        if ((Split-Path -Parent $resolvedBackupDir).TrimEnd('\', '/') -eq $resolvedBackupRoot -and
            (Split-Path -Leaf $resolvedBackupDir) -eq "config-backup-$timestamp") {
            Remove-Item -LiteralPath $resolvedBackupDir -Recurse -Force -ErrorAction SilentlyContinue
        }
        else {
            Write-Warning "Refusing to clean unexpected partial backup path: $resolvedBackupDir"
        }
    }
    if ($transcriptPath) {
        Stop-Transcript | Out-Null
    }
}
