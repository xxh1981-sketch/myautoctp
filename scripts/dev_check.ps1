# 提交前本地检查（与 CI lint + pytest-unit 对齐）
# 用法: .\scripts\dev_check.ps1

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$env:AUTOCTP_ALLOW_MISSING_DEPS = '1'

Write-Host '== unit manifest ==' -ForegroundColor Cyan
py -3 scripts/check_ci_readiness.py --check-unit-manifest

Write-Host '== ruff ==' -ForegroundColor Cyan
py -3 -m ruff check .

Write-Host '== sensitive files ==' -ForegroundColor Cyan
py -3 scripts/check_sensitive_files.py

Write-Host '== unit tests ==' -ForegroundColor Cyan
py -3 scripts/run_unit_tests.py

Write-Host 'OK: dev_check passed' -ForegroundColor Green
