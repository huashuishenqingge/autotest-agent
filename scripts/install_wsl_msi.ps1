# Administrator-only offline installation from the reviewed Microsoft release.
# No Windows restart is performed. Installer itself is ignored by Git.
$ErrorActionPreference = 'Stop'
$directory = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\artifacts\wsl-runtime'))
$package = Join-Path $directory 'wsl.3.0.3.0.x64.msi'
$report = [ordered]@{ version = '3.0.3'; exitCode = $null; error = $null; restartNeeded = $false }
try {
    $expected = 'ae977286974255bd26bf925f00f40f5692c6b2084155cbc0563c09aea091f211'
    if ((Get-FileHash -LiteralPath $package -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) {
        throw 'Official release SHA-256 mismatch'
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $package
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') {
        throw 'Valid Microsoft signature required'
    }
    $log = Join-Path $directory 'msi-install.log'
    $arguments = '/i "' + $package + '" /qn /norestart /L*v "' + $log + '"'
    $installer = Start-Process -FilePath "$env:SystemRoot\System32\msiexec.exe" -ArgumentList $arguments -WindowStyle Hidden -Wait -PassThru
    $report.exitCode = $installer.ExitCode
    $report.restartNeeded = $installer.ExitCode -eq 3010
    if ($installer.ExitCode -notin @(0, 3010)) { throw ('MSI installation failed: ' + $installer.ExitCode) }
} catch {
    $report.error = $_.Exception.Message
} finally {
    $report | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $directory 'msi-result.json') -Encoding UTF8
}
