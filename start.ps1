param([switch]$NoBrowser, [int]$Port=18765, [string]$DataDir='')
$ErrorActionPreference = 'Stop'
$projectDir = $PSScriptRoot
if (-not $DataDir) {
    if ($env:LIVE_MONITOR_DATA) { $DataDir=$env:LIVE_MONITOR_DATA }
    else { $DataDir=Join-Path $PSScriptRoot 'data' }
}
$DataDir=[System.IO.Path]::GetFullPath($DataDir)
$address='http://127.0.0.1:'+$Port
$pythonExe=Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { $pythonExe=(Get-Command python.exe -ErrorAction Stop).Source }
New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
try {
    $health = Invoke-RestMethod -Uri "$address/api/health" -TimeoutSec 2
    if ($health.app -eq 'dahua-live-monitor') {
        $state=Invoke-RestMethod -Uri "$address/api/state" -TimeoutSec 2
        if ([System.IO.Path]::GetFullPath($state.settings.data_dir) -ne $DataDir) {
            throw '端口已被其他程序占用：另一个目录的监控台正在运行，请使用 -Port 指定其他端口。'
        }
        & (Join-Path $projectDir 'background.ps1') -DataDir $dataDir -PythonExe $pythonExe -Port $Port
        if (-not $NoBrowser) { Start-Process $address }
        Write-Output $address
        exit 0
    }
    throw '端口已被其他程序占用。'
} catch {
    if ($_.Exception.Message -like '*端口已被其他程序占用*') { throw }
}
$listener=Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listener) { throw '端口被占用或服务尚未就绪，请稍后重试或指定其他端口。' }
& $pythonExe -c 'import aiohttp, playwright' 2>$null
if ($LASTEXITCODE -ne 0) { throw '缺少运行依赖，请在此目录执行 python -m pip install -r requirements.txt' }
& (Join-Path $projectDir 'background.ps1') -DataDir $dataDir -PythonExe $pythonExe -Port $Port
$serverPath = Join-Path $projectDir 'server.py'
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    Start-Sleep -Milliseconds 250
    try {
        $health = Invoke-RestMethod -Uri "$address/api/health" -TimeoutSec 1
        if ($health.app -eq 'dahua-live-monitor') {
            if (-not $NoBrowser) { Start-Process $address }
            Write-Output $address
            exit 0
        }
    } catch { }
}
throw "监控台没有成功启动，请查看 $dataDir/server.stderr.log"
