param([Parameter(Mandatory=$true)][string]$Config)
$ErrorActionPreference = 'Stop'
$runtime = Get-Content -LiteralPath $Config -Encoding UTF8 -Raw | ConvertFrom-Json
# On Windows login, a prior intentional stop ends; saved room switches remain authoritative.
$stopFile = Join-Path $runtime.data_dir 'supervisor.stop'
if (Test-Path -LiteralPath $stopFile) { Remove-Item -LiteralPath $stopFile }
while ($true) {
    & $runtime.python -B -u (Join-Path $runtime.app_dir 'supervisor.py') --config $Config
    if ($LASTEXITCODE -eq 73 -or (Test-Path -LiteralPath $stopFile)) { break }
    $entry = (Get-Date -Format o) + ' Supervisor wrapper retry; exit=' + $LASTEXITCODE
    Add-Content -LiteralPath (Join-Path $runtime.data_dir 'wrapper.log') -Encoding UTF8 -Value $entry
    Start-Sleep -Seconds 5
}
exit 0
