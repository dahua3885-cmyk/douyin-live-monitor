param([Parameter(Mandatory=$true)][string]$Folder)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false)
$Folder=[IO.Path]::GetFullPath($Folder).TrimEnd('\')
if (-not (Test-Path -LiteralPath ($Folder+'\') -PathType Container)) { throw 'Folder does not exist' }
Add-Type @'
using System;
using System.Runtime.InteropServices;
public class RecordingFolderWindow {
 [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr h,int n);
 [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
 [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
 [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h,out uint p);
 [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
 [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a,uint b,bool attach);
 [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h,IntPtr after,int x,int y,int w,int height,uint flags);
 public static bool BringForward(IntPtr h) {
   uint pid; uint current=GetCurrentThreadId();
   uint foreground=GetWindowThreadProcessId(GetForegroundWindow(),out pid);
   bool attached=foreground!=0 && foreground!=current && AttachThreadInput(current,foreground,true);
   try {
     ShowWindowAsync(h,9);
     SetWindowPos(h,new IntPtr(-1),0,0,0,0,0x43);
     SetForegroundWindow(h);
     SetWindowPos(h,new IntPtr(-2),0,0,0,0,0x43);
     return GetForegroundWindow()==h;
   } finally { if(attached)AttachThreadInput(current,foreground,false); }
 }
}
'@
$shell=New-Object -ComObject Shell.Application
$shell.Open($Folder+'\')
$foreground=$false
for ($attempt=0; $attempt -lt 50; $attempt++) {
    foreach ($window in @($shell.Windows())) {
        try {
            $location=[IO.Path]::GetFullPath($window.Document.Folder.Self.Path).TrimEnd('\')
            if ($location -eq $Folder) {
                $foreground=[RecordingFolderWindow]::BringForward([IntPtr][long]$window.HWND)
                if ($foreground) { break }
            }
        } catch { }
    }
    if ($foreground) { break }
    Start-Sleep -Milliseconds 100
}
@{foreground=$foreground} | ConvertTo-Json -Compress
