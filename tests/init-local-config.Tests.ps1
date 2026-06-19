BeforeAll {
    $SourceRoot = Split-Path -Parent $PSScriptRoot
    $TestRoot = Join-Path ([System.IO.Path]::GetTempPath()) "HomeFrigateOllamaIaC-Pester-$([guid]::NewGuid())"
    $ScriptPath = Join-Path $TestRoot "scripts/init-local-config.ps1"
    $InventoryPath = Join-Path $TestRoot "ansible/inventory.yml"
    $VarsPath = Join-Path $TestRoot "ansible/group_vars/all.yml"

    New-Item -ItemType Directory -Path (Join-Path $TestRoot "scripts") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $TestRoot "ansible/group_vars") -Force | Out-Null

    Copy-Item -LiteralPath (Join-Path $SourceRoot "scripts/init-local-config.ps1") -Destination $ScriptPath
    Copy-Item -LiteralPath (Join-Path $SourceRoot "ansible/inventory.example.yml") -Destination (Join-Path $TestRoot "ansible/inventory.example.yml")
    Copy-Item -LiteralPath (Join-Path $SourceRoot "ansible/group_vars/all.example.yml") -Destination (Join-Path $TestRoot "ansible/group_vars/all.example.yml")

    function New-TestSecureString {
        param([string]$Text)
        $secure = [securestring]::new()
        foreach ($Character in $Text.ToCharArray()) {
            $secure.AppendChar($Character)
        }
        $secure.MakeReadOnly()
        $secure
    }

    function Remove-GeneratedConfig {
        foreach ($Path in @($InventoryPath, $VarsPath)) {
            if (Test-Path -LiteralPath $Path) {
                Remove-Item -LiteralPath $Path -Force
            }
        }
    }
}

Describe "init-local-config.ps1" {
    BeforeEach {
        Remove-GeneratedConfig
    }

    AfterEach {
        Remove-GeneratedConfig
    }

    It "creates ignored local config from the public templates" {
        & $ScriptPath `
            -VmAddress "192.168.77.20" `
            -VmUser "vm'admin" `
            -SshKeyPath "C:\Keys\home key's\id_ed25519" `
            -RtspUser "viewer'user" `
            -RtspPassword (New-TestSecureString "pa:ss #demo") `
            -Camera1Host "192.168.77.31" `
            -Camera2Host "192.168.77.32"

        Test-Path -LiteralPath $InventoryPath | Should -BeTrue
        Test-Path -LiteralPath $VarsPath | Should -BeTrue

        $inventory = Get-Content -Raw -LiteralPath $InventoryPath
        $vars = Get-Content -Raw -LiteralPath $VarsPath

        $inventory | Should -BeLike "*ansible_host: '192.168.77.20'*"
        $inventory | Should -BeLike "*ansible_user: 'vm''admin'*"
        $inventory | Should -BeLike "*ansible_ssh_private_key_file: 'C:\Keys\home key''s\id_ed25519'*"

        $vars | Should -BeLike "*frigate_vm_ip: '192.168.77.20'*"
        $vars | Should -BeLike "*frigate_rtsp_user: 'viewer''user'*"
        $vars | Should -BeLike "*frigate_rtsp_password: 'pa:ss #demo'*"
        $vars | Should -BeLike "*host: 192.168.77.31*"
        $vars | Should -BeLike "*host: 192.168.77.32*"
    }

    It "does not overwrite existing local config without Force" {
        & $ScriptPath `
            -RtspUser "viewer" `
            -RtspPassword (New-TestSecureString "first-pass")

        { & $ScriptPath -RtspUser "viewer" -RtspPassword (New-TestSecureString "second-pass") } |
            Should -Throw -ExpectedMessage "*already exists*"
    }

    It "rejects an empty RTSP password" {
        $emptyPassword = [securestring]::new()

        { & $ScriptPath -RtspUser "viewer" -RtspPassword $emptyPassword -Force } |
            Should -Throw -ExpectedMessage "*RTSP password must not be empty*"
    }
}

AfterAll {
    $resolvedTestRoot = [System.IO.Path]::GetFullPath($TestRoot)
    $resolvedTempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
    if (-not $resolvedTestRoot.StartsWith($resolvedTempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove test path outside temp root: $resolvedTestRoot"
    }
    if ((Split-Path -Leaf $resolvedTestRoot) -notlike "HomeFrigateOllamaIaC-Pester-*") {
        throw "Refusing to remove unexpected test path: $resolvedTestRoot"
    }
    if (Test-Path -LiteralPath $resolvedTestRoot) {
        $removeArgs = @{ Recurse = $true; Force = $true }
        Remove-Item -LiteralPath $resolvedTestRoot @removeArgs
    }
}
