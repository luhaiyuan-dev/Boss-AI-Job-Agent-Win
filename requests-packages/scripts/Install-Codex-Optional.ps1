[CmdletBinding()]
param(
    [switch]$Accept,
    [switch]$Login
)

$ErrorActionPreference = "Stop"
$bundleRoot = Split-Path -Parent $PSScriptRoot
$archive = Join-Path $bundleRoot "installers\codex-x86_64-pc-windows-msvc-0.133.0.exe.zip"
$installRoot = Join-Path $env:LOCALAPPDATA "BossJobAssistant\Codex\0.133.0"
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

Write-Host "Codex CLI 仅用于 GUI 的“Codex主导”审核方式；使用“大模型API”时可以跳过。" -ForegroundColor Yellow
Write-Host "登录状态由 Codex 保存在当前 Windows 用户目录，不写入项目文件。" -ForegroundColor Yellow

if (-not $Accept) {
    $answer = Read-Host "安装随包 Codex CLI 0.133.0 吗？输入 YES 继续，直接回车跳过"
    if ($answer -ne "YES") {
        Write-Host "已跳过 Codex 安装/登录。之后可重新运行本脚本。" -ForegroundColor Yellow
        exit 0
    }
}

$existing = Get-Command codex -ErrorAction SilentlyContinue | Select-Object -First 1
if ($existing) {
    $existingVersion = (& $existing.Source --version 2>$null | Select-Object -First 1)
    Write-Host "已检测到 Codex：$($existing.Source)（$existingVersion），保持现有安装。" -ForegroundColor Green
    $codexCommand = $existing.Source
} else {
    if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) {
        throw "缺少 Codex 离线压缩包：$archive"
    }
    New-Item -ItemType Directory -Force -Path $installRoot | Out-Null
    $staging = Join-Path $installRoot "extracting"
    if (Test-Path -LiteralPath $staging) {
        [IO.Directory]::Delete($staging, $true)
    }
    New-Item -ItemType Directory -Path $staging | Out-Null
    Expand-Archive -LiteralPath $archive -DestinationPath $staging -Force
    $sourceExe = Join-Path $staging "codex-x86_64-pc-windows-msvc.exe"
    if (-not (Test-Path -LiteralPath $sourceExe -PathType Leaf)) {
        throw "Codex 压缩包中未找到主程序。"
    }
    Move-Item -LiteralPath $sourceExe -Destination $codexExe -Force
    foreach ($helper in @("codex-command-runner.exe", "codex-windows-sandbox-setup.exe")) {
        $source = Join-Path $staging $helper
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "Codex 压缩包中缺少 $helper。"
        }
        Move-Item -LiteralPath $source -Destination (Join-Path $installRoot $helper) -Force
    }
    [IO.Directory]::Delete($staging, $true)
    Add-UserPath $installRoot
    $codexCommand = $codexExe
    $version = (& $codexCommand --version 2>$null | Select-Object -First 1)
    if ($LASTEXITCODE -ne 0 -or $version -notmatch "0\.133\.0") {
        throw "Codex 离线安装后的版本校验失败。"
    }
    Write-Host "Codex CLI 已离线安装：$codexCommand（$version）" -ForegroundColor Green
}

if ($Login) {
    & $codexCommand login
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Codex 登录未完成；之后可在 PowerShell 执行 codex login。" -ForegroundColor Yellow
        exit 0
    }
    & $codexCommand login status
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Codex 登录状态检查通过。" -ForegroundColor Green
    } else {
        Write-Host "Codex 已安装，但登录状态仍需用户确认。" -ForegroundColor Yellow
    }
} else {
    Write-Host "Codex 安装完成，登录步骤已跳过；需要时执行 codex login。" -ForegroundColor Green
}
