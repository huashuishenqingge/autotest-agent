# Run as administrator after enabling prerequisites. Never restarts Windows.
$ErrorActionPreference = 'Stop'
$directory = Join-Path $PSScriptRoot '..\artifacts\wsl-runtime'
New-Item -ItemType Directory -Path $directory -Force | Out-Null
$report = [ordered]@{ exitCode = $null; error = $null }
try {
    & "$env:SystemRoot\System32\wsl.exe" --install --no-distribution 2>&1 | Out-File -LiteralPath (Join-Path $directory 'install.log') -Encoding UTF8
    $report.exitCode = $LASTEXITCODE
} catch {
    $report.error = $_.Exception.Message
} finally {
    $report | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $directory 'result.json') -Encoding UTF8
}
