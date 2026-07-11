$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$checks = 0

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) {
        throw $Message
    }
    $script:checks++
}

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Program failed with exit code $LASTEXITCODE"
    }
}

Push-Location $root
try {
    $parseErrors = @()
    Get-ChildItem -Path scripts, tests -Filter *.ps1 -Recurse | ForEach-Object {
        $tokens = $null
        $errors = $null
        [System.Management.Automation.Language.Parser]::ParseFile(
            $_.FullName,
            [ref]$tokens,
            [ref]$errors
        ) | Out-Null
        $parseErrors += @($errors)
    }
    Assert-True ($parseErrors.Count -eq 0) "PowerShell syntax errors: $parseErrors"

    $tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) "HomeFrigate-offline-$([guid]::NewGuid())"
    try {
        New-Item -ItemType Directory -Path (Join-Path $tempRoot "scripts") -Force | Out-Null
        New-Item -ItemType Directory -Path (Join-Path $tempRoot "ansible/group_vars") -Force | Out-Null
        Copy-Item scripts/init-local-config.ps1 (Join-Path $tempRoot "scripts/init-local-config.ps1")
        Copy-Item ansible/inventory.example.yml (Join-Path $tempRoot "ansible/inventory.example.yml")
        Copy-Item ansible/group_vars/all.example.yml (Join-Path $tempRoot "ansible/group_vars/all.example.yml")
        $cameraPassword = ConvertTo-SecureString "synthetic-camera-pass" -AsPlainText -Force
        $apiPassword = ConvertTo-SecureString "synthetic-api-password" -AsPlainText -Force
        & (Join-Path $tempRoot "scripts/init-local-config.ps1") `
            -VmAddress "192.0.2.20" `
            -VmUser "test-user" `
            -SshKeyPath "C:\Keys\test key" `
            -RtspUser "viewer" `
            -RtspPassword $cameraPassword `
            -BasicAuthUser "home.test" `
            -BasicAuthPassword $apiPassword
        $generatedVars = Get-Content -Raw (Join-Path $tempRoot "ansible/group_vars/all.yml")
        $generatedInventory = Get-Content -Raw (Join-Path $tempRoot "ansible/inventory.yml")
        Assert-True ($generatedVars -match "home_ai_basic_user: 'home.test'") "Basic-auth user was not generated"
        Assert-True ($generatedVars -match "home_ai_basic_password: 'synthetic-api-password'") "Basic-auth password was not generated"
        Assert-True ($generatedInventory -match "ansible_user: 'test-user'") "VM user was not generated"
    }
    finally {
        $resolvedTemp = [System.IO.Path]::GetFullPath($tempRoot)
        $systemTemp = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
        if ($resolvedTemp.StartsWith($systemTemp, [System.StringComparison]::OrdinalIgnoreCase) -and
            (Split-Path -Leaf $resolvedTemp) -like "HomeFrigate-offline-*") {
            Remove-Item -LiteralPath $resolvedTemp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    Invoke-Checked python @("-m", "compileall", "-q", "asr", "ollama-audio-transcription", "tests")
    $checks++
    Invoke-Checked python @("-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py")
    $checks++

    $pythonValidation = @'
from pathlib import Path
import re
import yaml

for path in Path('.github').rglob('*.yml'):
    yaml.safe_load(path.read_text(encoding='utf-8'))
for path in Path('ansible').rglob('*.yml'):
    if 'templates' not in path.parts:
        yaml.safe_load(path.read_text(encoding='utf-8'))
yaml.safe_load(Path('asr/docker-compose.yml').read_text(encoding='utf-8'))

def pinned_requirements(path):
    result = {}
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        match = re.match(r'^([A-Za-z0-9_-]+)==([^;\s]+)', line)
        if match:
            result[match.group(1).lower().replace('_', '-')] = match.group(2)
    return result

declared = pinned_requirements('requirements-dev.txt')
locked = pinned_requirements('requirements-dev.lock')
if not declared or any(locked.get(name) != version for name, version in declared.items()):
    raise SystemExit('requirements-dev.txt and requirements-dev.lock are inconsistent')
'@
    $pythonValidation | python -
    if ($LASTEXITCODE -ne 0) {
        throw "YAML parsing failed"
    }
    $checks++

    $tasks = Get-Content -Raw ansible/roles/frigate_vm/tasks/main.yml
    $nginx = Get-Content -Raw ansible/roles/frigate_vm/templates/home-ai-proxies.nginx.j2
    $compose = Get-Content -Raw ansible/roles/frigate_vm/templates/docker-compose.yml.j2
    $asr = Get-Content -Raw asr/app.py
    $transcriber = Get-Content -Raw ollama-audio-transcription/transcribe_via_ollama.py
    $backupScript = Get-Content -Raw scripts/invoke-config-backup.ps1
    $caInstaller = Get-Content -Raw scripts/install-frigate-local-ca.ps1
    $hypervSetup = Get-Content -Raw scripts/hyperv-host-setup.ps1
    $smokeTest = Get-Content -Raw scripts/smoke-test.ps1
    $modelBuilder = Get-Content -Raw ansible/roles/frigate_vm/files/build-yolov9-onnx.sh
    $inventoryExample = Get-Content -Raw ansible/inventory.example.yml
    Assert-True ($tasks -match 'home_ai_basic_password') "Basic-auth provisioning is missing"
    Assert-True ($tasks -match 'docker network inspect bridge') "Docker bridge discovery is missing"
    Assert-True ($nginx -match 'auth_basic_user_file') "Nginx basic auth is missing"
    Assert-True ($nginx -match 'listen 8971 ssl') "Frigate HTTPS proxy is missing"
    Assert-True ($nginx -match 'frigate_vm_asr_port_resolved') "ASR HTTPS proxy is missing"
    Assert-True ($compose -match '127\.0\.0\.1:') "Frigate backend is not loopback-only"
    Assert-True ($asr -match 'ASR_MAX_UPLOAD_BYTES') "ASR upload limit is missing"
    Assert-True ($asr -match 'run_in_threadpool') "Blocking ASR inference returned to the event loop"
    Assert-True ($transcriber -match 'StrictHostKeyChecking=yes') "SSH host-key verification is not strict"
    Assert-True ($transcriber -notmatch 'UserKnownHostsFile=NUL') "SSH known_hosts bypass returned"
    Assert-True ($backupScript -match 'StrictHostKeyChecking=yes') "Backup SSH host-key verification is not strict by default"
    Assert-True ($backupScript -notmatch 'UserKnownHostsFile=NUL') "Backup SSH known_hosts bypass returned"
    Assert-True ($caInstaller -match 'ExpectedSha256Thumbprint') "CA installer has no out-of-band fingerprint check"
    Assert-True ($caInstaller -match 'Provide CaCertPath') "CA installer still trusts remote certificates by default"
    Assert-True ($caInstaller -match 'AllowAutoRedirect = \$false') "CA capture follows redirects"
    Assert-True ($hypervSetup -match 'AutomaticStopAction ShutDown') "Hyper-V still uses an abrupt power-off action"
    Assert-True ($hypervSetup -match 'must be off before assigning') "DDA assignment has no VM-state guard"
    Assert-True ($hypervSetup -match 'Mount-VMHostAssignableDevice') "DDA failure has no host-device rollback"
    Assert-True ($modelBuilder -notmatch 'docker image prune') "Model builder still prunes unrelated images"
    Assert-True ($modelBuilder -match 'Refusing unsafe work directory') "Model builder has no path-deletion guard"
    Assert-True ($modelBuilder -match 'onnx\.checker\.check_model') "Exported ONNX model is not validated"
    Assert-True ($tasks -match 'Install model build dependency lock') "Model build dependency lock is not deployed"
    Assert-True ($tasks -match 'checksum: "sha256:') "Ollama archive is not checksum-pinned"
    Assert-True ($tasks -notmatch 'ollama\.com/install\.sh') "Unverified Ollama installer returned"
    Assert-True ($smokeTest -notmatch '--insecure') "LAN smoke test disables TLS verification"
    Assert-True ($inventoryExample -match 'StrictHostKeyChecking=yes') "Ansible inventory learns unknown SSH host keys"

    $gitBash = "C:\Program Files\Git\bin\bash.exe"
    if (Test-Path -LiteralPath $gitBash) {
        $tokens = $null
        $errors = $null
        $backupAst = [System.Management.Automation.Language.Parser]::ParseFile(
            (Resolve-Path "scripts/invoke-config-backup.ps1"),
            [ref]$tokens,
            [ref]$errors
        )
        $remoteScriptNode = $backupAst.FindAll({
            param($node)
            $node -is [System.Management.Automation.Language.StringConstantExpressionAst] -and
                $node.Value -match 'win-home-vm-configs\.XXXXXXXX'
        }, $true) | Select-Object -First 1
        Assert-True ($null -ne $remoteScriptNode) "VM backup shell payload was not found"
        $remoteScriptNode.Value | & $gitBash -n
        if ($LASTEXITCODE -ne 0) {
            throw "VM backup shell payload has invalid Bash syntax"
        }
        $checks++

        if ($smokeTest -notmatch '(?s)\$vmScript = @''\r?\n(?<payload>.*?)\r?\n''@') {
            throw "VM smoke-test shell payload was not found"
        }
        $Matches.payload | & $gitBash -n
        if ($LASTEXITCODE -ne 0) {
            throw "VM smoke-test shell payload has invalid Bash syntax"
        }
        $checks++

        if ($smokeTest -notmatch '(?s)\$genScript = @"\r?\n(?<payload>.*?)\r?\n"@') {
            throw "Ollama smoke-test shell payload was not found"
        }
        $ollamaSmokePayload = $Matches.payload.Replace('$genB64', 'ZGVtbw==').Replace('`$', '$')
        $ollamaSmokePayload | & $gitBash -n
        if ($LASTEXITCODE -ne 0) {
            throw "Ollama smoke-test shell payload has invalid Bash syntax"
        }
        $checks++
    }

    if (Get-Command docker -ErrorAction SilentlyContinue) {
        Invoke-Checked docker @("compose", "-f", "asr/docker-compose.yml", "config", "--quiet")
        $checks++
    }
    if (Get-Command ansible-playbook -ErrorAction SilentlyContinue) {
        $env:ANSIBLE_CONFIG = Join-Path $root "ansible.cfg"
        Invoke-Checked ansible-playbook @(
            "-i", "ansible/inventory.example.yml",
            "ansible/playbooks/site.yml",
            "--syntax-check",
            "-e", "@ansible/group_vars/all.example.yml"
        )
        $checks++
    }

    git diff --check
    if ($LASTEXITCODE -ne 0) {
        throw "git diff --check failed"
    }
    $checks++

    Write-Host "PASS: $checks offline checks"
}
finally {
    Pop-Location
}
