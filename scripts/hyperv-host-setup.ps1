param(
    [string]$VmName = "frigate-ubuntu",
    [int]$ProcessorCount = 8,
    [UInt64]$StartupMemoryBytes = 8GB,
    [int]$AutomaticStartDelay = 60,
    [string]$GpuLocationPath = "PCIROOT(0)#PCI(0300)#PCI(0000)",
    [switch]$AssignGpu
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command Get-VM -ErrorAction SilentlyContinue)) {
    throw "Hyper-V PowerShell module is not available. Run this on the Windows Server host."
}
if ([string]::IsNullOrWhiteSpace($VmName)) {
    throw "VmName must not be empty."
}
if ($ProcessorCount -lt 1 -or $ProcessorCount -gt 256) {
    throw "ProcessorCount must be in range 1..256."
}
if ($StartupMemoryBytes -lt 2GB -or $StartupMemoryBytes -gt 1TB) {
    throw "StartupMemoryBytes must be in range 2GB..1TB."
}
if ($AutomaticStartDelay -lt 0 -or $AutomaticStartDelay -gt 3600) {
    throw "AutomaticStartDelay must be in range 0..3600 seconds."
}
if ($AssignGpu -and $GpuLocationPath -notmatch '^PCIROOT\([0-9A-Fa-f]{1,8}\)(?:#PCI\([0-9A-Fa-f]{4}\))+$') {
    throw "GpuLocationPath is not a PCI location path."
}

$vm = Get-VM -Name $VmName
if ($AssignGpu -and $vm.State -ne "Off") {
    throw "VM $VmName must be off before assigning a DDA device. Current state: $($vm.State)."
}
Write-Host "Configuring VM $($vm.Name) on $env:COMPUTERNAME"

Set-VMProcessor -VMName $VmName -Count $ProcessorCount
Set-VMMemory -VMName $VmName -StartupBytes $StartupMemoryBytes
Set-VM -Name $VmName `
    -AutomaticStartAction Start `
    -AutomaticStartDelay $AutomaticStartDelay `
    -AutomaticStopAction ShutDown `
    -GuestControlledCacheTypes $true `
    -LowMemoryMappedIoSpace 3GB `
    -HighMemoryMappedIoSpace 64GB

if ($AssignGpu) {
    $assigned = Get-VMAssignableDevice -VMName $VmName -ErrorAction SilentlyContinue |
        Where-Object { $_.LocationPath -eq $GpuLocationPath }

    if (-not $assigned) {
        Write-Host "Assigning GPU DDA device $GpuLocationPath"
        $deviceDismounted = $false
        try {
            Dismount-VMHostAssignableDevice -LocationPath $GpuLocationPath -Force
            $deviceDismounted = $true
            Add-VMAssignableDevice -VMName $VmName -LocationPath $GpuLocationPath
            $deviceDismounted = $false
        }
        catch {
            if ($deviceDismounted) {
                try {
                    Mount-VMHostAssignableDevice -LocationPath $GpuLocationPath
                }
                catch {
                    Write-Warning "GPU assignment failed and the host device could not be remounted: $($_.Exception.Message)"
                }
            }
            throw
        }
    }
}

$state = Get-VM -Name $VmName | Select-Object Name,State,ProcessorCount,MemoryStartup,AutomaticStartAction,AutomaticStartDelay,AutomaticStopAction
$gpu = Get-VMAssignableDevice -VMName $VmName -ErrorAction SilentlyContinue |
    Select-Object LocationPath,InstanceID

[pscustomobject]@{
    VM  = $state
    GPU = $gpu
} | ConvertTo-Json -Depth 4
