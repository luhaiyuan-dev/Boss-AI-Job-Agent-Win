[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$bundleRoot = Split-Path -Parent $PSScriptRoot
$manifestPath = Join-Path $bundleRoot "manifest.json"
$checksumPath = Join-Path $bundleRoot "SHA256SUMS.txt"

if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw "缺少安装包清单：$manifestPath"
}
if (-not (Test-Path -LiteralPath $checksumPath -PathType Leaf)) {
    throw "缺少 SHA-256 清单：$checksumPath"
}

$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$checksumLines = @(
    Get-Content -LiteralPath $checksumPath -Encoding UTF8 |
        Where-Object { $_ -and -not $_.StartsWith("#") }
)
$checksums = @{}
foreach ($line in $checksumLines) {
    if ($line -notmatch "^(?<hash>[0-9A-Fa-f]{64})\s+\*(?<path>.+)$") {
        throw "SHA256SUMS.txt 行格式错误：$line"
    }
    $checksums[$Matches.path.Replace("\", "/")] = $Matches.hash.ToUpperInvariant()
}

$failed = $false
foreach ($package in $manifest.packages) {
    $relativeSlash = ([string]$package.path).Replace("\", "/")
    $relativePath = $relativeSlash.Replace("/", [IO.Path]::DirectorySeparatorChar)
    $path = Join-Path $bundleRoot $relativePath
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        Write-Host "[缺失] $($package.name)：$relativePath" -ForegroundColor Red
        $failed = $true
        continue
    }
    $item = Get-Item -LiteralPath $path
    if ($item.Length -ne [int64]$package.bytes) {
        Write-Host "[大小不符] $($package.name)：期望 $($package.bytes)，实际 $($item.Length)" -ForegroundColor Red
        $failed = $true
        continue
    }
    $actualHash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
    $expectedHash = ([string]$package.sha256).ToUpperInvariant()
    if ($actualHash -ne $expectedHash -or $checksums[$relativeSlash] -ne $expectedHash) {
        Write-Host "[哈希不符] $($package.name)：$relativePath" -ForegroundColor Red
        $failed = $true
        continue
    }
    Write-Host "[通过] $($package.name) $($package.version)" -ForegroundColor Green
}

if ($checksums.Count -ne @($manifest.packages).Count) {
    Write-Host "[清单不一致] manifest 与 SHA256SUMS 项目数不同。" -ForegroundColor Red
    $failed = $true
}

if ($failed) {
    Write-Host "安装包校验失败，已停止部署。请重新取得完整 requests-packages 文件夹。" -ForegroundColor Red
    exit 1
}
Write-Host "全部安装资源的大小与 SHA-256 校验通过。" -ForegroundColor Green
exit 0
