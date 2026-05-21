# 备份 data/ 运行时文件（本地，勿 push）
# 用法: .\scripts\backup_data.ps1
#       .\scripts\backup_data.ps1 -Dest D:\backup\autoctp

param(
    [string]$Dest = ''
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$DataDir = Join-Path $Root 'data'
if (-not (Test-Path $DataDir)) {
    Write-Error "data 目录不存在: $DataDir"
}

$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
if (-not $Dest) {
    $Dest = Join-Path $Root "data\backup-$Stamp"
}
New-Item -ItemType Directory -Force -Path $Dest | Out-Null

$patterns = @('*.csv', '*.json', '*.jsonl', '*.txt', '*.pid')
$copied = 0
foreach ($pat in $patterns) {
    Get-ChildItem -Path $DataDir -Filter $pat -File -ErrorAction SilentlyContinue | ForEach-Object {
        Copy-Item $_.FullName -Destination $Dest -Force
        $copied++
    }
}

Write-Host "已备份 $copied 个文件到 $Dest"
