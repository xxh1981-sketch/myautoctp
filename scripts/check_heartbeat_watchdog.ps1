# 外部心跳 watchdog — Windows 计划任务用
# 建议每 1–5 分钟运行一次：
#   Program: powershell.exe
#   Arguments: -NoProfile -ExecutionPolicy Bypass -File "D:\autoctp\scripts\check_heartbeat_watchdog.ps1"
#
# 用法:
#   .\scripts\check_heartbeat_watchdog.ps1
#   .\scripts\check_heartbeat_watchdog.ps1 -DryRun

param(
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) {
    $Python = 'python'
}

$Script = Join-Path $Root 'scripts\check_heartbeat_watchdog.py'
$Args = @($Script)
if ($DryRun) {
    $Args += '--dry-run'
}

& $Python @Args
exit $LASTEXITCODE
