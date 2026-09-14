param(
    [string]$HostAddress = "adler-white-w1.lan",
    [string]$HostCredentialPath = "$env:USERPROFILE\.codex\secrets\adler-winrm.credential.xml",
    [string]$HostConfigurationName = "PowerShell.7",
    [string]$VmName = "frigate-ubuntu",
    [string]$VmAddress = "192.168.1.138",
    [string]$VmUser = "krt",
    [string]$KeyPath = "$env:USERPROFILE\.ssh\win-home-codex_ed25519",
    [int]$ExpectedCameraCount = 3,
    [int]$ExpectedRetentionDays = 3
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $HostCredentialPath -PathType Leaf)) {
    throw "Host credential file not found: $HostCredentialPath"
}
if (-not (Test-Path -LiteralPath $KeyPath -PathType Leaf)) {
    throw "SSH key not found: $KeyPath"
}

$credential = Import-Clixml -LiteralPath $HostCredentialPath
$hostState = Invoke-Command -ComputerName $HostAddress -UseSSL `
    -ConfigurationName $HostConfigurationName -Credential $credential `
    -Authentication Negotiate -ScriptBlock {
        param($TargetVm)
        $vm = Get-VM -Name $TargetVm -ErrorAction Stop
        [pscustomobject]@{
            name = $vm.Name
            state = [string]$vm.State
            cpu = [int]$vm.ProcessorCount
            memory_bytes = [long]$vm.MemoryStartup
            automatic_start = [string]$vm.AutomaticStartAction
            dda_count = @(
                Get-VMAssignableDevice -VMName $TargetVm -ErrorAction SilentlyContinue
            ).Count
            f_free_bytes = [long](Get-PSDrive -Name F -ErrorAction Stop).Free
        }
    } -ArgumentList $VmName

$python = @'
import json
import os
import ssl
import subprocess
import time
import urllib.request
import yaml


def run(*args):
    return subprocess.run(args, check=False, text=True, capture_output=True)


def cpu_sample():
    values = [int(value) for value in open("/proc/stat", encoding="ascii").readline().split()[1:]]
    return sum(values), values[3] + values[4]


config = yaml.safe_load(open("/opt/frigate/config/config.yml", encoding="utf-8"))
context = ssl._create_unverified_context()
stats = None
for _ in range(18):
    inspect = json.loads(run("sudo", "docker", "inspect", "frigate").stdout)[0]
    if inspect["State"].get("Health", {}).get("Status") == "healthy":
        try:
            with urllib.request.urlopen(
                "https://127.0.0.1:18971/api/stats", context=context, timeout=15
            ) as response:
                stats = json.load(response)
            break
        except OSError:
            pass
    time.sleep(5)
if stats is None:
    raise RuntimeError("Frigate did not become healthy with a readable API")

recent = 0
cutoff = time.time() - 600
for root, _, files in os.walk("/media/frigate/recordings"):
    for name in files:
        try:
            recent += os.path.getmtime(os.path.join(root, name)) >= cutoff
        except FileNotFoundError:
            pass

processes = run("sudo", "docker", "exec", "frigate", "ps", "-eo", "args").stdout
cpu_total_1, cpu_idle_1 = cpu_sample()
time.sleep(3)
cpu_total_2, cpu_idle_2 = cpu_sample()
cpu_delta = cpu_total_2 - cpu_total_1
cameras = stats.get("cameras", {})
result = {
    "container_health": inspect["State"].get("Health", {}).get("Status"),
    "container_runtime": inspect["HostConfig"].get("Runtime"),
    "container_devices": inspect["HostConfig"].get("Devices"),
    "container_device_requests": inspect["HostConfig"].get("DeviceRequests"),
    "configured_cameras": sorted(config.get("cameras", {})),
    "active_cameras": sorted(
        name for name, value in cameras.items() if float(value.get("camera_fps") or 0) > 0
    ),
    "camera_fps": {
        name: value.get("camera_fps") for name, value in sorted(cameras.items())
    },
    "recent_recording_files": recent,
    "retention_days": config.get("record", {}).get("continuous", {}).get("days"),
    "detect_enabled": config.get("detect", {}).get("enabled"),
    "motion_enabled": config.get("motion", {}).get("enabled"),
    "snapshots_enabled": config.get("snapshots", {}).get("enabled"),
    "analytics_keys": sorted(
        key for key in ("detectors", "model", "face_recognition", "lpr", "genai", "semantic_search")
        if key in config
    ),
    "copy_record_processes": sum("-c copy" in line for line in processes.splitlines()),
    "guest_cpu_percent": round(
        100 * (1 - (cpu_idle_2 - cpu_idle_1) / cpu_delta), 1
    ),
    "ollama_enabled": run("systemctl", "is-enabled", "ollama.service").stdout.strip(),
    "ollama_active": run("systemctl", "is-active", "ollama.service").stdout.strip(),
    "asr_containers": run(
        "sudo", "docker", "ps", "-a", "--filter", "name=asr", "--format", "{{.Names}}"
    ).stdout.split(),
}
print(json.dumps(result, ensure_ascii=False))
'@

$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($python))
$vmJson = & ssh.exe -q -i $KeyPath -o BatchMode=yes -o ConnectTimeout=10 `
    "$VmUser@$VmAddress" "echo $encoded | base64 -d | python3"
if ($LASTEXITCODE -ne 0) {
    throw "Recorder VM smoke probe failed with exit code $LASTEXITCODE."
}
$vmState = $vmJson | ConvertFrom-Json

$checks = [ordered]@{
    "vm.running" = $hostState.state -eq "Running"
    "vm.cpu" = $hostState.cpu -eq 2
    "vm.memory" = $hostState.memory_bytes -eq 4GB
    "vm.autostart" = $hostState.automatic_start -eq "Start"
    "vm.dda_removed" = $hostState.dda_count -eq 0
    "host.f_free" = $hostState.f_free_bytes -ge 150GB
    "frigate.healthy" = $vmState.container_health -eq "healthy"
    "frigate.no_gpu_runtime" = $vmState.container_runtime -eq "runc" -and
        -not $vmState.container_devices -and -not $vmState.container_device_requests
    "frigate.cameras_configured" = $vmState.configured_cameras.Count -eq $ExpectedCameraCount
    "frigate.camera_active" = $vmState.active_cameras.Count -gt 0
    "frigate.recording" = $vmState.recent_recording_files -gt 0 -and
        $vmState.copy_record_processes -gt 0
    "vm.cpu_headroom" = $vmState.guest_cpu_percent -lt 80
    "frigate.retention" = $vmState.retention_days -eq $ExpectedRetentionDays
    "frigate.analytics_disabled" = -not $vmState.detect_enabled -and
        -not $vmState.motion_enabled -and -not $vmState.snapshots_enabled -and
        $vmState.analytics_keys.Count -eq 0
    "ollama.disabled" = $vmState.ollama_enabled -eq "disabled" -and
        $vmState.ollama_active -eq "inactive"
    "asr.absent" = $vmState.asr_containers.Count -eq 0
}

$report = [pscustomobject]@{
    timestamp = (Get-Date).ToString("o")
    passed = @($checks.Values | Where-Object { $_ }).Count
    failed = @($checks.Values | Where-Object { -not $_ }).Count
    checks = $checks
    host = $hostState
    vm = $vmState
}
$report | ConvertTo-Json -Depth 8
if ($report.failed -ne 0) {
    exit 1
}
