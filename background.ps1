param([string]$DataDir, [string]$PythonExe, [int]$Port=18765, [switch]$Disable)
$ErrorActionPreference = 'Stop'
$appDir = $PSScriptRoot
$DataDir = [System.IO.Path]::GetFullPath($DataDir)
New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
$sha = [System.Security.Cryptography.SHA256]::Create()
$tag = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($DataDir.ToLowerInvariant()+$Port)))).Replace('-','').Substring(0,12)
$taskName = 'DahuaLiveMonitor-' + $tag
$startupLink = Join-Path ([Environment]::GetFolderPath('Startup')) ($taskName+'.lnk')
if ($Disable) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $startupLink) { Remove-Item -LiteralPath $startupLink }
    $metadata = Join-Path $DataDir 'autostart.json'
    if (Test-Path -LiteralPath $metadata) { Remove-Item -LiteralPath $metadata }
    Write-Output '已关闭登录后自动启动；正在运行的监控不受影响。'
    exit 0
}
$taskEnvironment = @{}
foreach ($key in @('LIVE_TRANSCRIBE_MODEL_DIR','LIVE_MONITOR_BROWSER_EXE','PLAYWRIGHT_BROWSERS_PATH')) {
    $value = [Environment]::GetEnvironmentVariable($key)
    if ($value) { $taskEnvironment[$key] = $value }
}
$taskEnvironment['PATH'] = $env:PATH
$configPath = Join-Path $DataDir 'background-config.json'
@{python=$PythonExe; app_dir=$appDir; data_dir=$DataDir; port=$Port; environment=$taskEnvironment} | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $configPath -Encoding UTF8
$psExe = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
$arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "'+(Join-Path $appDir 'run-background.ps1')+'" -Config "'+$configPath+'"'
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $psExe -Argument $arguments -WorkingDirectory $appDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
$stopFile = Join-Path $DataDir 'supervisor.stop'
if (Test-Path -LiteralPath $stopFile) { Remove-Item -LiteralPath $stopFile }
$launchMode = 'scheduled-task'
try {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description '直播监控台：登录后恢复监控，后台异常退出后自动恢复。视频仅按已保存开关录制。' -Force -ErrorAction Stop | Out-Null
    Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
} catch {
    # Current-user Startup needs no elevation. WMI starts outside the calling
    # terminal's process tree; closing a Codex task cannot reap this process.
    $launchMode = 'user-startup'
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($startupLink)
    $shortcut.TargetPath = $psExe
    $shortcut.Arguments = $arguments
    $shortcut.WorkingDirectory = $appDir
    $shortcut.WindowStyle = 7
    $shortcut.Description = '直播监控台：登录 Windows 后在后台恢复'
    $shortcut.Save()
    $processStartup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0}
    $launched = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=('"'+$psExe+'" '+$arguments); CurrentDirectory=$appDir; ProcessStartupInformation=$processStartup} -ErrorAction Stop
    if ($launched.ReturnValue -ne 0) { throw ('后台进程启动失败，Windows 返回码：'+$launched.ReturnValue) }
}
@{task_name=$taskName; user=$identity; mode=$launchMode; startup_link=$startupLink; installed_at=(Get-Date -Format o)} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $DataDir 'autostart.json') -Encoding UTF8
Write-Output ('后台守护和登录后自动启动已启用：'+$launchMode)
