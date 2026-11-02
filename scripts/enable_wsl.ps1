# Run as administrator. Enables only WSL2 prerequisites, never restarts Windows.
$ErrorActionPreference = 'Stop'
$reportDirectory = Join-Path $PSScriptRoot '..\artifacts\wsl-setup'
New-Item -ItemType Directory -Path $reportDirectory -Force | Out-Null
$report = [ordered]@{ features = @(); restartNeeded = $false; error = $null }
try {
    foreach ($feature in @('Microsoft-Windows-Subsystem-Linux', 'VirtualMachinePlatform')) {
        $before = Get-WindowsOptionalFeature -Online -FeatureName $feature
        $restart = $false
        if ($before.State -eq 'Disabled') {
            $result = Enable-WindowsOptionalFeature -Online -FeatureName $feature -All -NoRestart
            $restart = [bool]$result.RestartNeeded
        }
        $after = Get-WindowsOptionalFeature -Online -FeatureName $feature
        $report.features += [ordered]@{ name = $feature; before = [string]$before.State; after = [string]$after.State; restartNeeded = $restart }
        $report.restartNeeded = $report.restartNeeded -or $restart -or ([string]$after.State -eq 'EnablePending')
    }
} catch {
    $report.error = $_.Exception.Message
} finally {
    $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $reportDirectory 'result.json') -Encoding UTF8
}
if ($report.error) { exit 1 }
if ($report.restartNeeded) { exit 3010 }
exit 0
