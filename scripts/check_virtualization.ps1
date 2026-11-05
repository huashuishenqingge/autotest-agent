# Read-only administrator diagnostics. Does not change boot configuration.
$ErrorActionPreference = 'Stop'
$directory = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\artifacts\wsl-runtime'))
$system = Get-CimInstance Win32_ComputerSystem
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$bootLines = & "$env:SystemRoot\System32\bcdedit.exe" /enum '{current}'
$bootExit = $LASTEXITCODE
$report = [ordered]@{
    hypervisorPresent = $system.HypervisorPresent
    firmwareVirtualization = $cpu.VirtualizationFirmwareEnabled
    bootExitCode = $bootExit
    hypervisorBootSetting = @($bootLines | Select-String 'hypervisorlaunchtype' | ForEach-Object { $_.Line.Trim() })
    features = @()
}
foreach ($name in @('Microsoft-Windows-Subsystem-Linux', 'VirtualMachinePlatform')) {
    $feature = Get-WindowsOptionalFeature -Online -FeatureName $name
    $report.features += @{name=$name;state=[string]$feature.State}
}
$report | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $directory 'virtualization.json') -Encoding UTF8
