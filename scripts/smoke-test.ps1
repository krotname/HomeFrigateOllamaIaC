param(
    [string]$HostName = "ADLER-WHITE-1W",
    [string]$HostAddress = "192.168.1.33",
    [string]$VmAddress = "192.168.1.138",
    [string]$VmUser = "krt",
    [string]$KeyPath = "$env:USERPROFILE\.ssh\win-home-codex_ed25519",
    [string]$HostCredentialPath = "$env:USERPROFILE\.codex\secrets\adler-winrm.credential.xml",
    [string]$HostConfigurationName = "PowerShell.7",
    [string]$VmName = "frigate-ubuntu",
    [string]$FrigateUrl = "https://192.168.1.138:8971",
    [string]$FrigateAuthUser = $env:FRIGATE_BASIC_USER,
    [string]$FrigateAuthPassword = $env:FRIGATE_BASIC_PASSWORD,
    [string]$OllamaUrl = "http://192.168.1.138:11434",
    [string]$OllamaModel = "huihui_ai/gpt-oss-abliterated:20b",
    [string]$ReportPath = "",
    [switch]$SkipOllamaGenerate,
    [switch]$TrustUnknownHostKeys
)

$ErrorActionPreference = "Stop"
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

function Add-Result {
    param(
        [string]$Name,
        [bool]$Pass,
        [string]$Detail = ""
    )

    [pscustomobject]@{
        name   = $Name
        pass   = $Pass
        detail = $Detail
    }
}

function Invoke-SshText {
    param(
        [string]$Target,
        [string]$Command,
        [int]$TimeoutSeconds = 30
    )

    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = "ssh"
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    $sshArgs = @(
        "-i", $KeyPath,
        "-o", "CertificateFile=none",
        "-o", "BatchMode=yes"
    )
    if ($TrustUnknownHostKeys) {
        $sshArgs += @("-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=NUL")
    } else {
        $sshArgs += @("-o", "StrictHostKeyChecking=accept-new")
    }
    $sshArgs += @(
        "-o", "ConnectTimeout=10",
        $Target,
        $Command
    )
    foreach ($arg in $sshArgs) {
        $psi.ArgumentList.Add($arg) | Out-Null
    }

    $process = [System.Diagnostics.Process]::Start($psi)
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        try {
            $process.Kill($true)
        } catch {
            try { $process.Kill() } catch { }
        }
        $process.WaitForExit()
        throw "SSH command timed out for $Target"
    }

    $stdout = $stdoutTask.GetAwaiter().GetResult()
    $stderr = $stderrTask.GetAwaiter().GetResult()
    $outputParts = @()
    if (-not [string]::IsNullOrWhiteSpace($stdout)) { $outputParts += $stdout.TrimEnd() }
    if (-not [string]::IsNullOrWhiteSpace($stderr)) { $outputParts += $stderr.TrimEnd() }
    $text = ($outputParts -join "`n").Trim()
    if ($process.ExitCode -ne 0) {
        throw "SSH command failed for $Target with exit $($process.ExitCode). Output: $text"
    }
    $text
}

function Invoke-HostPowerShellJson {
    param([string]$Script, [int]$TimeoutSeconds = 30)

    if (-not (Test-Path -LiteralPath $HostCredentialPath)) {
        throw "WinRM credential not found: $HostCredentialPath"
    }

    $credential = Import-Clixml -LiteralPath $HostCredentialPath
    $job = Start-Job -ScriptBlock {
        param($ComputerName, $ConfigurationName, $Credential, $ScriptText)
        $ErrorActionPreference = "Stop"
        Invoke-Command `
            -ComputerName $ComputerName `
            -UseSSL `
            -ConfigurationName $ConfigurationName `
            -Credential $Credential `
            -Authentication Negotiate `
            -ScriptBlock ([scriptblock]::Create($ScriptText))
    } -ArgumentList $HostName, $HostConfigurationName, $credential, $Script

    if (-not (Wait-Job $job -Timeout $TimeoutSeconds)) {
        Stop-Job $job | Out-Null
        Remove-Job $job -Force | Out-Null
        throw "WinRM command timed out for $HostName"
    }

    $raw = Receive-Job $job
    $state = $job.State
    $reason = $job.ChildJobs[0].JobStateInfo.Reason
    Remove-Job $job -Force | Out-Null
    if ($state -ne "Completed") {
        throw "WinRM command failed for $HostName. $reason"
    }

    $text = ($raw | Out-String).Trim()
    $jsonLine = ($text -split "`n" | Where-Object { $_.TrimStart().StartsWith("{") -or $_.TrimStart().StartsWith("[") } | Select-Object -First 1)
    if (-not $jsonLine) {
        throw "No JSON returned from WinRM host script. Output: $text"
    }
    $jsonLine | ConvertFrom-Json
}

function Invoke-VmBashJson {
    param([string]$Script, [int]$TimeoutSeconds = 60)

    $payload = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Script))
    $command = "printf '%s' '$payload' | base64 -d | tr -d '\r' | bash"
    $target = "$VmUser@$VmAddress"
    $text = Invoke-SshText -Target $target -Command $command -TimeoutSeconds $TimeoutSeconds
    $jsonLine = ($text -split "`n" | Where-Object { $_.TrimStart().StartsWith("{") } | Select-Object -Last 1)
    if (-not $jsonLine) {
        throw "No JSON returned from VM script. Output: $text"
    }
    $jsonLine | ConvertFrom-Json
}

function Get-FrigateCurlArgs {
    $args = @("--insecure", "--silent", "--show-error", "--fail", "--max-time", "15")
    if (-not [string]::IsNullOrWhiteSpace($FrigateAuthUser) -and -not [string]::IsNullOrWhiteSpace($FrigateAuthPassword)) {
        $args += @("--user", "$($FrigateAuthUser):$FrigateAuthPassword")
    }
    $args
}

$results = New-Object System.Collections.Generic.List[object]

try {
    if (-not (Test-Path -LiteralPath $KeyPath)) {
        throw "VM SSH key not found: $KeyPath"
    }
    if (-not (Test-Path -LiteralPath $HostCredentialPath)) {
        throw "WinRM credential not found: $HostCredentialPath"
    }

    $hostState = Invoke-HostPowerShellJson -TimeoutSeconds 120 -Script @"
`$ErrorActionPreference = 'Stop'
`$os = Get-CimInstance Win32_OperatingSystem
`$vm = Get-VM -Name '$VmName'
`$assignable = Get-VMAssignableDevice -VMName '$VmName' | Select-Object -First 1
`$adapter = Get-VMNetworkAdapter -VMName '$VmName'
[pscustomobject]@{
  Hostname = `$env:COMPUTERNAME
  LastBootUpTime = `$os.LastBootUpTime.ToString('s')
  VmName = `$vm.Name
  VmState = `$vm.State.ToString()
  VmMemoryAssigned = `$vm.MemoryAssigned
  VmProcessorCount = `$vm.ProcessorCount
  AutomaticStartAction = `$vm.AutomaticStartAction.ToString()
  AutomaticStartDelay = `$vm.AutomaticStartDelay
  AutomaticStopAction = `$vm.AutomaticStopAction.ToString()
  GpuLocationPath = `$assignable.LocationPath
  GpuInstanceID = `$assignable.InstanceID
  VmIPs = @(`$adapter.IPAddresses)
  Whoami = whoami
  PSVersion = `$PSVersionTable.PSVersion.ToString()
  IsAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
} | ConvertTo-Json -Compress
"@

    $results.Add((Add-Result "host.identity" ($hostState.Hostname -eq "ADLER-WHITE-1W") "hostname=$($hostState.Hostname)"))
    $results.Add((Add-Result "host.winrm.powershell7_admin" ($hostState.PSVersion -match "^7\." -and [bool]$hostState.IsAdmin) "whoami=$($hostState.Whoami), ps=$($hostState.PSVersion), is_admin=$($hostState.IsAdmin)"))
    $results.Add((Add-Result "hyperv.vm.running" ($hostState.VmState -eq "Running") "state=$($hostState.VmState)"))
    $results.Add((Add-Result "hyperv.vm.autostart" ($hostState.AutomaticStartAction -eq "Start" -and [int]$hostState.AutomaticStartDelay -ge 1) "action=$($hostState.AutomaticStartAction), delay=$($hostState.AutomaticStartDelay)"))
    $results.Add((Add-Result "hyperv.gpu.assigned" ($hostState.GpuInstanceID -match "VEN_10DE" -and $hostState.GpuLocationPath -eq "PCIROOT(0)#PCI(0300)#PCI(0000)") "gpu=$($hostState.GpuInstanceID)"))
    $results.Add((Add-Result "hyperv.vm.ip" (($hostState.VmIPs -contains $VmAddress)) "ips=$($hostState.VmIPs -join ',')"))

    $frigateCurlArgs = Get-FrigateCurlArgs
    $certOutput = & curl.exe @frigateCurlArgs "$FrigateUrl/api/version" 2>&1
    $results.Add((Add-Result "frigate.lan.version" ($LASTEXITCODE -eq 0 -and $certOutput -match "^\d+\.\d+") "version=$certOutput"))
    $directFrigatePortOpen = Test-NetConnection -ComputerName $VmAddress -Port 8971 -WarningAction SilentlyContinue -InformationLevel Quiet
    $results.Add((Add-Result "frigate.direct_port.open" $directFrigatePortOpen "vm=$VmAddress, port=8971, open=$directFrigatePortOpen"))

    $ollamaOutput = & curl.exe --silent --show-error --fail --max-time 15 "$OllamaUrl/api/version" 2>&1
    $results.Add((Add-Result "ollama.lan.version" ($LASTEXITCODE -eq 0 -and $ollamaOutput -match '"version"') $ollamaOutput))

    $vmScript = @'
set -euo pipefail

python3 - <<'PY'
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import yaml

def run(args, timeout=30):
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout)

def read_url(url, timeout=10, insecure=False):
    import ssl
    ctx = ssl._create_unverified_context() if insecure else None
    with urllib.request.urlopen(url, timeout=timeout, context=ctx) as r:
        return r.read().decode()

summary = {}

summary["hostname"] = run(["hostname"]).stdout.strip()
summary["docker_active"] = run(["systemctl", "is-active", "docker"]).stdout.strip()
summary["ollama_active"] = run(["systemctl", "is-active", "ollama"]).stdout.strip()
summary["ollama_enabled"] = run(["systemctl", "is-enabled", "ollama"]).stdout.strip()

df = run(["df", "-T", "/media/frigate"]).stdout.strip().splitlines()
summary["media_df"] = df[-1] if len(df) >= 2 else ""
summary["media_ext4"] = " ext4 " in f" {summary['media_df']} "

smi = run([
    "nvidia-smi",
    "--query-gpu=name,driver_version,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
    "--format=csv,noheader,nounits",
]).stdout.strip()
summary["nvidia_smi"] = smi
summary["gpu_is_p40"] = "Tesla P40" in smi

summary["frigate_version"] = read_url("https://127.0.0.1:8971/api/version", insecure=True).strip()
summary["ollama_version"] = json.loads(read_url("http://127.0.0.1:11434/api/version"))["version"]

config = json.loads(read_url("https://127.0.0.1:8971/api/config", insecure=True))
with open("/opt/frigate/config/config.yml", "r", encoding="utf-8") as f:
    raw_config = yaml.safe_load(f)
genai = config.get("genai", {})
review_genai = config.get("review", {}).get("genai", {})
objects_genai = config.get("objects", {}).get("genai", {})
model = config.get("model", {})
summary["frigate_genai_config"] = {
    "provider": genai.get("provider"),
    "base_url": genai.get("base_url"),
    "model": genai.get("model"),
    "review_enabled": bool(review_genai.get("enabled")),
    "review_alerts": bool(review_genai.get("alerts")),
    "objects_enabled": bool(objects_genai.get("enabled")),
}
summary["frigate_detector_config"] = config.get("detectors", {})
summary["frigate_raw_detector_config"] = raw_config.get("detectors", {})
summary["frigate_model_config"] = {
    "model_type": model.get("model_type"),
    "path": model.get("path"),
    "labelmap_path": model.get("labelmap_path"),
    "width": model.get("width"),
    "height": model.get("height"),
    "input_tensor": model.get("input_tensor"),
    "input_dtype": model.get("input_dtype"),
}
providers = run([
    "docker", "exec", "frigate", "python3", "-c",
    "import json, onnxruntime as ort; print(json.dumps(ort.get_available_providers()))",
], timeout=30).stdout.strip()
summary["onnxruntime_providers"] = json.loads(providers) if providers else []

stats = json.loads(read_url("https://127.0.0.1:8971/api/stats", insecure=True))
summary["cameras"] = {
    name: {
        "camera_fps": camera.get("camera_fps"),
        "process_fps": camera.get("process_fps"),
        "skipped_fps": camera.get("skipped_fps"),
        "ffmpeg_pid": camera.get("ffmpeg_pid"),
    }
    for name, camera in stats.get("cameras", {}).items()
}
summary["detectors"] = stats.get("detectors", {})

compose_ps = run(["docker", "compose", "-f", "/opt/frigate/docker-compose.yml", "ps"], timeout=30).stdout
summary["frigate_compose_ps"] = compose_ps
summary["frigate_healthy"] = "healthy" in compose_ps

ffmpeg_ps = run(["docker", "exec", "frigate", "sh", "-lc", "ps -eo args | grep ffmpeg | grep -v grep"], timeout=30).stdout
summary["ffmpeg_uses_cuda"] = "-hwaccel cuda" in ffmpeg_ps and "scale_cuda" in ffmpeg_ps

container_tags = run([
    "docker", "exec", "frigate", "python3", "-c",
    "import urllib.request; print(urllib.request.urlopen('http://host.docker.internal:11434/api/tags', timeout=10).read().decode())"
], timeout=30).stdout
summary["frigate_can_reach_ollama"] = "huihui_ai/gpt-oss-abliterated:20b" in container_tags

model_list = run(["ollama", "list"], timeout=30).stdout
summary["ollama_has_model"] = "huihui_ai/gpt-oss-abliterated:20b" in model_list

recent_records = run(["bash", "-lc", "find /media/frigate/recordings -type f -mmin -10 | head -5"], timeout=30).stdout.strip()
summary["recent_recording_files"] = recent_records.splitlines() if recent_records else []

print(json.dumps(summary, ensure_ascii=True, separators=(",", ":")))
PY
'@

    $vmState = Invoke-VmBashJson -Script $vmScript -TimeoutSeconds 120
    $results.Add((Add-Result "vm.identity" ($vmState.hostname -eq "frigate-ubuntu") "hostname=$($vmState.hostname)"))
    $results.Add((Add-Result "vm.docker.active" ($vmState.docker_active -eq "active") "state=$($vmState.docker_active)"))
    $results.Add((Add-Result "vm.media.ext4" ([bool]$vmState.media_ext4) $vmState.media_df))
    $results.Add((Add-Result "gpu.nvidia_smi.p40" ([bool]$vmState.gpu_is_p40) $vmState.nvidia_smi))
    $results.Add((Add-Result "frigate.container.healthy" ([bool]$vmState.frigate_healthy) (($vmState.frigate_compose_ps -split "`n") -join " | ")))
    $results.Add((Add-Result "frigate.api.version" ($vmState.frigate_version -match "^\d+\.\d+") "version=$($vmState.frigate_version)"))
    $genaiConfig = $vmState.frigate_genai_config
    $genaiPass = $genaiConfig.provider -eq "ollama" `
        -and $genaiConfig.base_url -eq "http://host.docker.internal:11434" `
        -and $genaiConfig.model -eq $OllamaModel `
        -and -not [bool]$genaiConfig.review_enabled `
        -and -not [bool]$genaiConfig.objects_enabled
    $genaiDetail = "provider=$($genaiConfig.provider), base_url=$($genaiConfig.base_url), model=$($genaiConfig.model), review_enabled=$($genaiConfig.review_enabled), review_alerts=$($genaiConfig.review_alerts), objects_enabled=$($genaiConfig.objects_enabled)"
    $results.Add((Add-Result "frigate.genai.ollama_config" $genaiPass $genaiDetail))
    $detectorStats = $vmState.detectors.onnx
    $detectorConfig = $vmState.frigate_raw_detector_config.onnx
    $detectorSpeed = if ($null -ne $detectorStats) { [double]$detectorStats.inference_speed } else { 0.0 }
    $onnxProviders = @($vmState.onnxruntime_providers)
    $detectorPass = $null -ne $detectorStats `
        -and $null -ne $detectorConfig `
        -and $detectorConfig.type -eq "onnx" `
        -and $detectorConfig.device -eq "GPU" `
        -and ($onnxProviders -contains "CUDAExecutionProvider") `
        -and $detectorSpeed -gt 0 `
        -and $detectorSpeed -lt 25
    $detectorDetail = "type=$($detectorConfig.type), device=$($detectorConfig.device), inference_speed_ms=$detectorSpeed, providers=$($onnxProviders -join ',')"
    $results.Add((Add-Result "frigate.detector.onnx_gpu" $detectorPass $detectorDetail))
    $modelConfig = $vmState.frigate_model_config
    $modelPass = $modelConfig.model_type -eq "yolo-generic" `
        -and $modelConfig.path -eq "/config/model_cache/yolov9-t-320.onnx" `
        -and $modelConfig.labelmap_path -eq "/config/model_cache/coco-yolo-80.txt" `
        -and [int]$modelConfig.width -eq 320 `
        -and [int]$modelConfig.height -eq 320 `
        -and $modelConfig.input_tensor -eq "nchw" `
        -and $modelConfig.input_dtype -eq "float"
    $modelDetail = "model_type=$($modelConfig.model_type), path=$($modelConfig.path), labelmap_path=$($modelConfig.labelmap_path), size=$($modelConfig.width)x$($modelConfig.height), input_tensor=$($modelConfig.input_tensor), input_dtype=$($modelConfig.input_dtype)"
    $results.Add((Add-Result "frigate.model.yolov9_onnx" $modelPass $modelDetail))
    $results.Add((Add-Result "frigate.ffmpeg.cuda" ([bool]$vmState.ffmpeg_uses_cuda) "ffmpeg has -hwaccel cuda and scale_cuda"))

    $cameraFailures = @()
    foreach ($camera in $vmState.cameras.PSObject.Properties) {
        $value = $camera.Value
        if ([double]$value.camera_fps -lt 4.0 -or [double]$value.process_fps -lt 4.0 -or [double]$value.skipped_fps -gt 0.5) {
            $cameraFailures += "$($camera.Name): camera_fps=$($value.camera_fps), process_fps=$($value.process_fps), skipped_fps=$($value.skipped_fps)"
        }
    }
    $cameraProps = @($vmState.cameras.PSObject.Properties)
    $cameraDetail = ($cameraProps | ForEach-Object {
        "$($_.Name): camera_fps=$($_.Value.camera_fps), process_fps=$($_.Value.process_fps), skipped_fps=$($_.Value.skipped_fps)"
    }) -join "; "
    $cameraPass = ($cameraFailures.Count -eq 0) -and ($cameraProps.Count -ge 2)
    $results.Add((Add-Result "frigate.cameras.fps" $cameraPass $cameraDetail))
    $results.Add((Add-Result "frigate.recordings.recent" ($vmState.recent_recording_files.Count -gt 0) (($vmState.recent_recording_files | Select-Object -First 3) -join "; ")))

    $results.Add((Add-Result "ollama.service.active" ($vmState.ollama_active -eq "active" -and $vmState.ollama_enabled -eq "enabled") "active=$($vmState.ollama_active), enabled=$($vmState.ollama_enabled)"))
    $results.Add((Add-Result "ollama.api.version" ($vmState.ollama_version -match "^\d+\.\d+") "version=$($vmState.ollama_version)"))
    $results.Add((Add-Result "ollama.model.present" ([bool]$vmState.ollama_has_model) "model=$OllamaModel"))
    $results.Add((Add-Result "frigate.to_ollama.network" ([bool]$vmState.frigate_can_reach_ollama) "Frigate container can query Ollama tags"))

    if (-not $SkipOllamaGenerate) {
        $genPrompt = "Напиши одно короткое предложение по-русски: локальная модель работает."
        $genPayload = @{
            model   = $OllamaModel
            prompt  = $genPrompt
            stream  = $false
            think   = "low"
            options = @{ num_predict = 256; num_ctx = 2048 }
        } | ConvertTo-Json -Compress
        $genB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($genPayload))
        $genScript = @"
set -euo pipefail
printf '%s' '$genB64' | base64 -d > /tmp/ollama-smoke-payload.json
python3 - <<'PY'
import base64, json, ssl, subprocess, urllib.request

def read_url(url, timeout=20, insecure=False):
    ctx = ssl._create_unverified_context() if insecure else None
    with urllib.request.urlopen(url, timeout=timeout, context=ctx) as r:
        return r.read()

payload=json.load(open('/tmp/ollama-smoke-payload.json'))
req=urllib.request.Request(
    'http://127.0.0.1:11434/api/generate',
    data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
    headers={'Content-Type':'application/json'},
)
d=json.loads(urllib.request.urlopen(req, timeout=900).read().decode())
ps=subprocess.run(['ollama','ps'], text=True, capture_output=True, timeout=30).stdout
smi=subprocess.run(['nvidia-smi','--query-gpu=name,memory.used,memory.total,utilization.gpu','--format=csv,noheader,nounits'], text=True, capture_output=True, timeout=30).stdout.strip()
print(json.dumps({'done': d.get('done'), 'response': d.get('response',''), 'ollama_ps': ps, 'nvidia_smi': smi}, ensure_ascii=True, separators=(',', ':')))
PY
"@
$genState = Invoke-VmBashJson -Script $genScript -TimeoutSeconds 900
        $hasCyrillic = $genState.response -match "[\u0400-\u04FF]"
        $textPass = [bool]$genState.done -and -not [string]::IsNullOrWhiteSpace($genState.response) -and $hasCyrillic
        $results.Add((Add-Result "ollama.text.generate" $textPass "response=$($genState.response)"))
        $results.Add((Add-Result "ollama.text.gpu" ($genState.ollama_ps -match "100% GPU") (($genState.ollama_ps -split "`n") -join " | ")))
    }
}
catch {
    $results.Add((Add-Result "test.runner.exception" $false $_.Exception.Message))
}

$failed = @($results | Where-Object { -not $_.pass })
$report = [pscustomobject]@{
    generated_at = (Get-Date).ToString("s")
    host         = $HostAddress
    vm           = $VmAddress
    failed_count = $failed.Count
    results      = $results
}

$jsonReport = $report | ConvertTo-Json -Depth 8

if ([string]::IsNullOrWhiteSpace($ReportPath)) {
    $scriptRoot = Split-Path -Parent $PSCommandPath
    $ReportPath = Join-Path $scriptRoot "logs\frigate-vm-smoke-latest.json"
}

$reportDir = Split-Path -Parent $ReportPath
if (-not [string]::IsNullOrWhiteSpace($reportDir)) {
    New-Item -ItemType Directory -Force -Path $reportDir | Out-Null
}
$jsonReport | Set-Content -LiteralPath $ReportPath -Encoding UTF8
[Console]::Error.WriteLine("Smoke report written to $ReportPath")
$jsonReport

if ($failed.Count -gt 0) {
    exit 1
}
