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

$executables = @("Boss求职助手.exe", "Boss登录浏览器.exe")
foreach ($name in $executables) {
    $path = Join-Path $source $name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "缺少Nuitka构建产物：$path"
    }
}

New-Item -ItemType Directory -Path $target | Out-Null
foreach ($name in $executables) {
    Copy-Item -LiteralPath (Join-Path $source $name) -Destination (Join-Path $target $name)
}

$targetBundle = Join-Path $target "requests-packages"
$arguments = @(
    $bundleRoot, $targetBundle, "/E", "/COPY:DAT", "/DCOPY:T", "/R:1", "/W:1",
    "/NFL", "/NDL", "/NP", "/XD", (Join-Path $bundleRoot "logs")
)
& robocopy.exe @arguments | Out-Host
if ($LASTEXITCODE -ge 8) {
    throw "复制requests-packages失败，robocopy退出码 $LASTEXITCODE。"
}

$forbiddenDirectories = @(".git", ".venv", "wheelhouse", "data", "config", "__pycache__")
foreach ($name in $forbiddenDirectories) {
    $found = @(Get-ChildItem -LiteralPath $target -Recurse -Directory -Force -ErrorAction SilentlyContinue | Where-Object Name -eq $name)
    if ($found.Count -gt 0) { throw "脱敏检查失败，分发目录包含：$name" }
}
$forbiddenFiles = @(
    Get-ChildItem -LiteralPath $target -Recurse -File -Force -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Extension -in @(".py", ".pyc", ".pyo", ".pdb", ".c", ".h", ".pdf", ".sqlite3") -or
            $_.Name -eq "python-3.13.14-amd64.exe"
        }
)
if ($forbiddenFiles.Count -gt 0) {
    throw "脱敏检查失败，发现不应分发的文件：$($forbiddenFiles[0].FullName)"
}

Write-Host "无源码分发副本已创建：$target" -ForegroundColor Green
Write-Host "根目录只含两个EXE和requests-packages；config由新电脑的一键部署创建。" -ForegroundColor Yellow
