[CmdletBinding()]
param(
    [switch]$Accept,
    [switch]$Login
)

$ErrorActionPreference = "Stop"
$bundleRoot = Split-Path -Parent $PSScriptRoot
$archive = Join-Path $bundleRoot "installers\codex-x86_64-pc-windows-msvc-0.133.0.exe.zip"
$installBase = Join-Path $env:LOCALAPPDATA "BossJobAssistant\Codex"
$installRoot = Join-Path $installBase "0.133.0"
$codexExe = Join-Path $installRoot "codex.exe"

function Add-UserPath([string]$PathToAdd) {
    $current = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @($current -split ";" | Where-Object { $_ })
    if ($parts -notcontains $PathToAdd) {
        [Environment]::SetEnvironmentVariable("Path", (@($parts) + $PathToAdd) -join ";", "User")
    }
    if (($env:Path -split ";") -notcontains $PathToAdd) {
        $env:Path = $PathToAdd + ";" + $env:Path
    }
}

function Find-Codex {
    $existing = Get-Command codex -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($existing) { return $existing.Source }
    return Get-ChildItem -LiteralPath $installBase -Recurse -Filter codex.exe -File `
        -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending |
        Select-Object -First 1 -ExpandProperty FullName
}

Write-Host "Codex CLI仅用于GUI的“Codex主导”模式；程序固定使用gpt-5.5。" -ForegroundColor Yellow
Write-Host "登录状态保存在当前Windows用户目录，不写入项目或配置模板。" -ForegroundColor Yellow

$codexCommand = Find-Codex
if ($codexCommand) {
    Write-Host "已检测到Codex：$codexCommand（$(& $codexCommand --version 2>$null)），保持现状。" -ForegroundColor Green
    Add-UserPath (Split-Path -Parent $codexCommand)
} else {
    if (-not $Accept) {
        $answer = Read-Host "安装随包Codex CLI 0.133.0吗？输入YES继续"
        if ($answer -cne "YES") {
            Write-Host "已跳过Codex安装。" -ForegroundColor Yellow
            exit 0
        }
    }
    if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) {
        throw "缺少Codex离线压缩包：$archive"
    }
    New-Item -ItemType Directory -Force -Path $installRoot | Out-Null
    $staging = Join-Path $installRoot "extracting"
    if (Test-Path -LiteralPath $staging) { [IO.Directory]::Delete($staging, $true) }
    New-Item -ItemType Directory -Path $staging | Out-Null
    Expand-Archive -LiteralPath $archive -DestinationPath $staging -Force
    $sourceExe = Join-Path $staging "codex-x86_64-pc-windows-msvc.exe"
    if (-not (Test-Path -LiteralPath $sourceExe -PathType Leaf)) {
        throw "Codex压缩包中未找到主程序。"
    }
    Move-Item -LiteralPath $sourceExe -Destination $codexExe -Force
    foreach ($helper in @("codex-command-runner.exe", "codex-windows-sandbox-setup.exe")) {
        $source = Join-Path $staging $helper
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "Codex压缩包中缺少 $helper。"
        }
        Move-Item -LiteralPath $source -Destination (Join-Path $installRoot $helper) -Force
    }
    [IO.Directory]::Delete($staging, $true)
    Add-UserPath $installRoot
    $codexCommand = $codexExe
    $version = (& $codexCommand --version 2>$null | Select-Object -First 1)
    if ($LASTEXITCODE -ne 0 -or $version -notmatch "0\.133\.0") {
        throw "Codex离线安装后的版本校验失败。"
    }
    Write-Host "Codex CLI已离线安装：$codexCommand（$version）" -ForegroundColor Green
}

if ($Login) {
    & $codexCommand login
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Codex登录未完成；之后可在PowerShell执行 codex login。" -ForegroundColor Yellow
        exit 0
    }
}
& $codexCommand login status 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Host "Codex安装与登录状态检查通过。" -ForegroundColor Green
} else {
    Write-Host "Codex已安装但尚未登录；需要时执行 codex login。" -ForegroundColor Yellow
}
