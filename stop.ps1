param([int]$Port=18765, [string]$DataDir='')
$ErrorActionPreference = 'Stop'
if (-not $DataDir) {
    if ($env:LIVE_MONITOR_DATA) { $DataDir=$env:LIVE_MONITOR_DATA }
    else { $DataDir=Join-Path $PSScriptRoot 'data' }
}
$DataDir=[System.IO.Path]::GetFullPath($DataDir)
New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
$pidPath = Join-Path $dataDir 'server.pid'
if (-not (Test-Path -LiteralPath $pidPath)) { New-Item -ItemType File -Path (Join-Path $dataDir 'supervisor.stop') -Force | Out-Null; Write-Output '监控台当前未运行'; exit 0 }
$workerId = [int](Get-Content -LiteralPath $pidPath -Encoding ASCII -Raw)
$worker = Get-CimInstance Win32_Process -Filter "ProcessId = $workerId" -ErrorAction SilentlyContinue
$serverPath = Join-Path $PSScriptRoot 'server.py'
if ($worker -and $worker.Name -match '^python(w)?\.exe$' -and $worker.CommandLine.Contains($serverPath)) {
    try {
        $address='http://127.0.0.1:'+$Port
        $state=Invoke-RestMethod -Uri "$address/api/state" -TimeoutSec 2
        if ([System.IO.Path]::GetFullPath($state.settings.data_dir) -eq $DataDir) {
            Invoke-RestMethod -Uri "$address/api/shutdown?pause=1" -Method Post -ContentType 'application/json' -Body '{}' -TimeoutSec 5 | Out-Null
        }
    } catch { }
    New-Item -ItemType File -Path (Join-Path $dataDir 'supervisor.stop') -Force | Out-Null
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        if (-not (Get-Process -Id $workerId -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 250
    }
    if (Get-Process -Id $workerId -ErrorAction SilentlyContinue) {
        throw '监控台还在清理录制或浏览器，请稍后再运行停止。'
    }
    Write-Output '监控台已停止，历史数据保留'
} else {
    New-Item -ItemType File -Path (Join-Path $dataDir 'supervisor.stop') -Force | Out-Null
    Write-Output '没有找到对应的监控台进程'
}
