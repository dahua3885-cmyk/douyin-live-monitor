param([string]$DataDir='', [int]$Port=18765)
$ErrorActionPreference = 'Stop'
if (-not $DataDir) {
    if (Test-Path -LiteralPath (Join-Path $PSScriptRoot '../runtime/python/python.exe')) {
        $DataDir = Join-Path $PSScriptRoot '../data'
    } elseif ($env:LIVE_MONITOR_DATA) {
        $DataDir = $env:LIVE_MONITOR_DATA
    } else {
        $DataDir = Join-Path $PSScriptRoot 'data'
    }
}
& (Join-Path $PSScriptRoot 'background.ps1') -DataDir $DataDir -Port $Port -Disable
