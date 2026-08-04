[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Destination
)

$ErrorActionPreference = "Stop"
$bundleRoot = Split-Path -Parent $PSScriptRoot
$projectRoot = Split-Path -Parent $bundleRoot
$source = [IO.Path]::GetFullPath($projectRoot)
$target = [IO.Path]::GetFullPath($Destination)

if ($target -eq $source -or $target.StartsWith($source + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "分发目录不能位于当前项目内部：$target"
}
if (Test-Path -LiteralPath $target) {
    throw "为避免覆盖数据，目标必须是尚不存在的新目录：$target"
}

New-Item -ItemType Directory -Path $target | Out-Null
$excludeDirectories = @(
    (Join-Path $source ".venv"),
    (Join-Path $source "data"),
    (Join-Path $source ".git"),
    (Join-Path $source ".pytest_cache"),
    (Join-Path $source "__pycache__"),
    (Join-Path $source "requests-packages\logs"),
    ".git", ".pytest_cache", "__pycache__"
)
$arguments = @(
    $source, $target, "/E", "/COPY:DAT", "/DCOPY:T", "/R:1", "/W:1", "/NFL", "/NDL", "/NP",
    "/XD"
) + $excludeDirectories + @(
    "/XF", "model_api.local.json", "gui_defaults.txt", "*.pdf", "*.pyc", "*.sqlite3", "*.log"
)

& robocopy.exe @arguments | Out-Host
if ($LASTEXITCODE -ge 8) {
    throw "robocopy 创建脱敏分发副本失败，退出码 $LASTEXITCODE。"
}

$forbidden = @(
    (Join-Path $target "config\model_api.local.json"),
    (Join-Path $target "config\gui_defaults.txt"),
    (Join-Path $target "data"),
    (Join-Path $target ".venv")
)
foreach ($path in $forbidden) {
    if (Test-Path -LiteralPath $path) { throw "脱敏检查失败，仍存在：$path" }
}
$pdfs = @(Get-ChildItem -LiteralPath $target -Recurse -Filter *.pdf -File -ErrorAction SilentlyContinue)
if ($pdfs.Count -gt 0) { throw "脱敏检查失败，分发副本仍包含 PDF。" }

Write-Host "脱敏分发副本已创建：$target" -ForegroundColor Green
Write-Host "已排除 API/MySQL 本机配置、简历、data、.venv、缓存和日志；请人工复核后再分发。" -ForegroundColor Yellow
