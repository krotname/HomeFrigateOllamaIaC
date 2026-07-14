param(
    [string]$VmAddress = "192.168.50.20",
    [string]$VmUser = "frigateadmin",
    [string]$SshKeyPath = "$env:USERPROFILE\.ssh\home_frigate_vm_ed25519",
    [string]$RtspUser = "",
    [securestring]$RtspPassword,
    [string]$BasicAuthUser = "",
    [securestring]$BasicAuthPassword,
    [string]$Camera1Host = "192.168.50.31",
    [string]$Camera2Host = "192.168.50.32",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

function ConvertTo-PlainText {
    param([securestring]$Secure)
    if (-not $Secure) {
        return ""
    }
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try {
        [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    }
}

function Set-YamlScalar {
    param(
        [string]$Text,
        [string]$Key,
        [string]$Value
    )
    $escaped = [regex]::Escape($Key)
    $pattern = "^(?<indent>\s*)$escaped\s*:.*$"
    if (-not [regex]::IsMatch($Text, $pattern, "Multiline")) {
        throw "YAML template does not contain required key: $Key"
    }
    $safeValue = $Value.Replace("'", "''")
    [regex]::Replace($Text, $pattern, "`${indent}$Key`: '$safeValue'", "Multiline")
}

function Set-RequiredPattern {
    param(
        [string]$Text,
        [string]$Pattern,
        [string]$Replacement,
        [string]$Description
    )
    if (-not [regex]::IsMatch($Text, $Pattern)) {
        throw "Template does not contain required value: $Description"
    }
    [regex]::new($Pattern).Replace($Text, $Replacement, 1)
}

if ([string]::IsNullOrWhiteSpace($RtspUser)) {
    $RtspUser = Read-Host "RTSP user"
}
if (-not $RtspPassword) {
    $RtspPassword = Read-Host "RTSP password" -AsSecureString
}

function Set-PrivateFilePermissions {
    param([string]$Path)

    if ($env:OS -eq "Windows_NT") {
        $acl = [System.Security.AccessControl.FileSecurity]::new()
        $acl.SetAccessRuleProtection($true, $false)
        $fullControl = [System.Security.AccessControl.FileSystemRights]::FullControl
        $allow = [System.Security.AccessControl.AccessControlType]::Allow
        $sids = @(
            [System.Security.Principal.WindowsIdentity]::GetCurrent().User,
            [System.Security.Principal.SecurityIdentifier]::new("S-1-5-18"),
            [System.Security.Principal.SecurityIdentifier]::new("S-1-5-32-544")
        )
        foreach ($sid in $sids) {
            $acl.AddAccessRule(
                [System.Security.AccessControl.FileSystemAccessRule]::new($sid, $fullControl, $allow)
            )
        }
        Set-Acl -LiteralPath $Path -AclObject $acl
    }
    elseif (Get-Command chmod -ErrorAction SilentlyContinue) {
        & chmod 600 -- $Path
        if ($LASTEXITCODE -ne 0) {
            throw "Could not restrict permissions for $Path."
        }
    }
    else {
        throw "Cannot restrict permissions for $Path on this platform."
    }
}

function Write-PrivateUtf8File {
    param(
        [string]$Path,
        [string]$Content
    )

    $directory = Split-Path -Parent $Path
    $leaf = Split-Path -Leaf $Path
    $tempPath = Join-Path $directory ".$leaf.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        [System.IO.File]::WriteAllText(
            $tempPath,
            $Content,
            [System.Text.UTF8Encoding]::new($false)
        )
        Set-PrivateFilePermissions -Path $tempPath
        [System.IO.File]::Move($tempPath, $Path, $true)
    }
    finally {
        if (Test-Path -LiteralPath $tempPath) {
            Remove-Item -LiteralPath $tempPath -Force
        }
    }
}

function Test-SafeHostName {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value.Length -gt 253 -or
        $Value -match '[\x00-\x20\x7F]' -or $Value.StartsWith('-')) {
        return $false
    }
    if ($Value -match '^[0-9.]+$') {
        $parsedAddress = $null
        return $Value -match '^[0-9]{1,3}(\.[0-9]{1,3}){3}$' -and
            [Net.IPAddress]::TryParse($Value, [ref]$parsedAddress) -and
            $parsedAddress.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork
    }
    [Uri]::CheckHostName($Value) -in @(
        [UriHostNameType]::Dns,
        [UriHostNameType]::IPv4
    )
}

if ([string]::IsNullOrWhiteSpace($BasicAuthUser)) {
    $BasicAuthUser = Read-Host "LAN API basic-auth user"
}
if (-not $BasicAuthPassword) {
    $BasicAuthPassword = Read-Host "LAN API basic-auth password" -AsSecureString
}
$plainPassword = ConvertTo-PlainText -Secure $RtspPassword
$plainBasicAuthPassword = ConvertTo-PlainText -Secure $BasicAuthPassword
if (-not (Test-SafeHostName -Value $VmAddress)) {
    throw "VmAddress must be a valid IPv4 address or DNS name."
}
if ($VmUser -notmatch '^[A-Za-z_][A-Za-z0-9_.-]{0,63}$') {
    throw "VmUser contains unsupported characters."
}
if ([string]::IsNullOrWhiteSpace($SshKeyPath) -or $SshKeyPath -match '[\x00-\x1F\x7F]') {
    throw "SshKeyPath must be a non-empty path without control characters."
}
foreach ($cameraHost in @($Camera1Host, $Camera2Host)) {
    if (-not (Test-SafeHostName -Value $cameraHost)) {
        throw "Camera host is not a valid IPv4 address or DNS name: $cameraHost"
    }
}
if ([string]::IsNullOrWhiteSpace($RtspUser)) {
    throw "RTSP user must not be empty."
}
if ($RtspUser.Length -gt 128 -or $RtspUser -match '[\x00-\x1F\x7F]') {
    throw "RTSP user must not contain control characters and must be at most 128 characters."
}
if ([string]::IsNullOrWhiteSpace($plainPassword)) {
    throw "RTSP password must not be empty."
}
if ($plainPassword.Length -gt 1024 -or $plainPassword -match '[\x00-\x1F\x7F]') {
    throw "RTSP password must not contain control characters and must be at most 1024 characters."
}
if ($BasicAuthUser -notmatch '^[A-Za-z0-9_.-]{1,64}$') {
    throw "Basic-auth user must contain only letters, digits, dot, underscore, or hyphen."
}
if ([string]::IsNullOrWhiteSpace($plainBasicAuthPassword) -or
    $plainBasicAuthPassword.Length -lt 12 -or
    $plainBasicAuthPassword.Length -gt 1024 -or
    $plainBasicAuthPassword -match '[\x00-\x1F\x7F]') {
    throw "Basic-auth password must contain 12..1024 non-control characters."
}

$inventoryPath = Join-Path $PSScriptRoot "..\ansible\inventory.yml"
$inventoryExample = Join-Path $PSScriptRoot "..\ansible\inventory.example.yml"
$varsPath = Join-Path $PSScriptRoot "..\ansible\group_vars\all.yml"
$varsExample = Join-Path $PSScriptRoot "..\ansible\group_vars\all.example.yml"

foreach ($target in @($inventoryPath, $varsPath)) {
    if ((Test-Path -LiteralPath $target) -and -not $Force) {
        throw "$target already exists. Use -Force to overwrite."
    }
}

$inventory = Get-Content -Raw -LiteralPath $inventoryExample
$inventory = Set-YamlScalar -Text $inventory -Key "ansible_host" -Value $VmAddress
$inventory = Set-YamlScalar -Text $inventory -Key "ansible_user" -Value $VmUser
$inventory = Set-YamlScalar -Text $inventory -Key "ansible_ssh_private_key_file" -Value $SshKeyPath
Write-PrivateUtf8File -Path $inventoryPath -Content $inventory

$vars = Get-Content -Raw -LiteralPath $varsExample
$vars = Set-YamlScalar -Text $vars -Key "frigate_vm_ip" -Value $VmAddress
$vars = Set-YamlScalar -Text $vars -Key "frigate_rtsp_user" -Value $RtspUser
$vars = Set-YamlScalar -Text $vars -Key "frigate_rtsp_password" -Value $plainPassword
$vars = Set-YamlScalar -Text $vars -Key "home_ai_basic_user" -Value $BasicAuthUser
$vars = Set-YamlScalar -Text $vars -Key "home_ai_basic_password" -Value $plainBasicAuthPassword
$vars = Set-RequiredPattern -Text $vars -Pattern "host:\s*192\.168\.50\.31" `
    -Replacement "host: $Camera1Host" -Description "first camera host"
$vars = Set-RequiredPattern -Text $vars -Pattern "host:\s*192\.168\.50\.32" `
    -Replacement "host: $Camera2Host" -Description "second camera host"
Write-PrivateUtf8File -Path $varsPath -Content $vars

Write-Host "Created $inventoryPath"
Write-Host "Created $varsPath"
Write-Host "These files are ignored by git. Encrypt all.yml with ansible-vault if this checkout is shared."
Write-Host ""
Write-Host "Next:"
Write-Host "  ansible-playbook -i .\ansible\inventory.yml .\ansible\playbooks\site.yml --ask-become-pass"
