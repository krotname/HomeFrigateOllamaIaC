param(
    [string]$VmName = "frigate-ubuntu",
    [int]$ProcessorCount = 8,
    [UInt64]$StartupMemoryBytes = 4GB,
    [int]$AutomaticStartDelay = 60,
    [string]$GpuLocationPath = "PCIROOT(0)#PCI(0300)#PCI(0000)",
    [switch]$AssignGpu
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command Get-VM -ErrorAction SilentlyContinue)) {
    throw "Hyper-V PowerShell module is not available. Run this on the Windows Server host."
}

$vm = Get-VM -Name $VmName
Write-Host "Configuring VM $($vm.Name) on $env:COMPUTERNAME"

Set-VMProcessor -VMName $VmName -Count $ProcessorCount
Set-VMMemory -VMName $VmName -StartupBytes $StartupMemoryBytes
Set-VM -Name $VmName `
    -AutomaticStartAction Start `
    -AutomaticStartDelay $AutomaticStartDelay `
    -AutomaticStopAction TurnOff `
    -GuestControlledCacheTypes $true `
    -LowMemoryMappedIoSpace 3GB `
    -HighMemoryMappedIoSpace 64GB

if ($AssignGpu) {
    $assigned = Get-VMAssignableDevice -VMName $VmName -ErrorAction SilentlyContinue |
        Where-Object { $_.LocationPath -eq $GpuLocationPath }

    if (-not $assigned) {
        Write-Host "Assigning GPU DDA device $GpuLocationPath"
        Dismount-VMHostAssignableDevice -LocationPath $GpuLocationPath -Force
        Add-VMAssignableDevice -VMName $VmName -LocationPath $GpuLocationPath
    }
}

$state = Get-VM -Name $VmName | Select-Object Name,State,ProcessorCount,MemoryStartup,AutomaticStartAction,AutomaticStartDelay,AutomaticStopAction
$gpu = Get-VMAssignableDevice -VMName $VmName -ErrorAction SilentlyContinue |
    Select-Object LocationPath,InstanceID

[pscustomobject]@{
    VM  = $state
    GPU = $gpu
} | ConvertTo-Json -Depth 4
