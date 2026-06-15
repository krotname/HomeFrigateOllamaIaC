param(
    [string]$VmAddress = "192.168.1.138",
    [string]$HostAddress = "192.168.1.33",
    [string]$VmUser = "krt",
    [string]$SshKeyPath = "$env:USERPROFILE\.ssh\win-home-codex_ed25519",
    [string]$RtspUser = "",
    [securestring]$RtspPassword,
    [string]$Camera1Host = "192.168.1.12",
    [string]$Camera2Host = "192.168.1.65",
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
    $safeValue = $Value.Replace("\", "\\").Replace("`"", "\`"")
    [regex]::Replace($Text, "^(?<indent>\s*)$escaped\s*:.*$", "`${indent}$Key`: $safeValue", "Multiline")
}

if ([string]::IsNullOrWhiteSpace($RtspUser)) {
    $RtspUser = Read-Host "RTSP user"
}
if (-not $RtspPassword) {
    $RtspPassword = Read-Host "RTSP password" -AsSecureString
}
$plainPassword = ConvertTo-PlainText -Secure $RtspPassword

$inventoryPath = Join-Path $PSScriptRoot "..\ansible\inventory.yml"
$inventoryExample = Join-Path $PSScriptRoot "..\ansible\inventory.example.yml"
$varsPath = Join-Path $PSScriptRoot "..\ansible\group_vars\all.yml"
$varsExample = Join-Path $PSScriptRoot "..\ansible\group_vars\all.example.yml"

foreach ($target in @($inventoryPath, $varsPath)) {
    if ((Test-Path -LiteralPath $target) -and -not $Force) {
        throw "$target already exists. Use -Force to overwrite."
    }
}

Copy-Item -LiteralPath $inventoryExample -Destination $inventoryPath -Force
Copy-Item -LiteralPath $varsExample -Destination $varsPath -Force

$inventory = Get-Content -Raw -LiteralPath $inventoryPath
$inventory = $inventory -replace "ansible_host:\s*\S+", "ansible_host: $VmAddress"
$inventory = $inventory -replace "ansible_user:\s*\S+", "ansible_user: $VmUser"
$inventory = $inventory -replace "ansible_ssh_private_key_file:\s*.*", "ansible_ssh_private_key_file: $SshKeyPath"
Set-Content -LiteralPath $inventoryPath -Value $inventory -Encoding UTF8

$vars = Get-Content -Raw -LiteralPath $varsPath
$vars = Set-YamlScalar -Text $vars -Key "frigate_vm_ip" -Value $VmAddress
$vars = Set-YamlScalar -Text $vars -Key "frigate_rtsp_user" -Value $RtspUser
$vars = Set-YamlScalar -Text $vars -Key "frigate_rtsp_password" -Value $plainPassword
$vars = $vars -replace "host:\s*192\.168\.1\.12", "host: $Camera1Host"
$vars = $vars -replace "host:\s*192\.168\.1\.65", "host: $Camera2Host"
Set-Content -LiteralPath $varsPath -Value $vars -Encoding UTF8

Write-Host "Created $inventoryPath"
Write-Host "Created $varsPath"
Write-Host "These files are ignored by git. Encrypt all.yml with ansible-vault if this checkout is shared."
Write-Host ""
Write-Host "Next:"
Write-Host "  ansible-playbook -i .\ansible\inventory.yml .\ansible\playbooks\site.yml --ask-become-pass"
