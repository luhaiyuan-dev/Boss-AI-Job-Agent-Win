[CmdletBinding()]
param(
    [string]$PythonPath = "",
    [switch]$KeepStaging
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

if (-not $PythonPath) {
    $PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "未找到构建解释器：$PythonPath"
}

& $PythonPath -c "import struct,sys; assert sys.version_info[:2] == (3,13); assert struct.calcsize('P') == 8"
if ($LASTEXITCODE -ne 0) { throw "Nuitka构建必须使用Python 3.13 x64。" }
& $PythonPath -c "import nuitka, PIL, ordered_set, zstandard"
if ($LASTEXITCODE -ne 0) {
    throw "缺少构建依赖，请先执行：$PythonPath -m pip install -r requirements-build.txt"
}

Write-Host "`n===== 生成正式多尺寸图标 =====" -ForegroundColor Cyan
& $PythonPath tools\make_icons.py
if ($LASTEXITCODE -ne 0) { throw "正式图标生成失败。" }

$BuildRoot = Join-Path $ProjectRoot "build\nuitka"
$StagingRoot = Join-Path $BuildRoot "staging"
$OutputRoot = Join-Path $BuildRoot "output"
$ReportRoot = Join-Path $BuildRoot "reports"
New-Item -ItemType Directory -Force -Path $BuildRoot, $OutputRoot, $ReportRoot | Out-Null

Write-Host "`n===== 创建字符串加固构建副本 =====" -ForegroundColor Cyan
& $PythonPath tools\obfuscate_strings.py --project-root $ProjectRoot --output $StagingRoot
if ($LASTEXITCODE -ne 0) { throw "字符串加固构建副本创建失败。" }
& $PythonPath -m compileall -q $StagingRoot
if ($LASTEXITCODE -ne 0) { throw "加固构建副本编译检查失败。" }

$Version = "0.2.2.0"
$OfficialIcons = Join-Path $ProjectRoot "assets\icons\official"

function Invoke-NuitkaBuild {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Entry,
        [Parameter(Mandatory)][string]$Icon,
        [switch]$MainApplication
    )
    $productName = if ($MainApplication) { "Boss Job Assistant" } else { "Boss Login Browser" }
    $arguments = @(
        "--mode=onefile",
        "--assume-yes-for-downloads",
        "--msvc=latest",
        "--lto=yes",
        "--windows-console-mode=disable",
        "--python-flag=no_docstrings",
        "--output-dir=$OutputRoot",
        "--output-filename=$Name.exe",
        "--windows-icon-from-ico=$Icon",
        "--file-version=$Version",
        "--product-version=$Version",
        "--product-name=$productName",
        "--file-description=$productName",
        "--copyright=Copyright 2026 Boss Job Assistant",
        "--report=$(Join-Path $ReportRoot "$Name.xml")"
    )
    if ($MainApplication) {
        $arguments += @(
            "--enable-plugin=tk-inter",
            "--include-package=boss_assistant",
            "--include-package=mysql.connector",
            "--include-package=pypdf",
            "--include-package-data=opencc",
            "--include-data-files=$(Join-Path $OfficialIcons 'boss_assistant.ico')=boss_assistant/_assets/boss_assistant.ico"
        )
    }
    $arguments += $Entry
    Write-Host "`n===== Nuitka构建 $Name =====" -ForegroundColor Cyan
    Push-Location $StagingRoot
    try {
        & $PythonPath -m nuitka @arguments
        $nuitkaExitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($nuitkaExitCode -ne 0) { throw "Nuitka构建失败：$Name" }
}

function Update-ExplorerIcon {
    param([Parameter(Mandatory)][string]$Path)

    if (-not ("ExplorerIconRefresh" -as [type])) {
        Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

public static class ExplorerIconRefresh
{
    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    public static extern void SHChangeNotify(
        uint eventId, uint flags, string item1, IntPtr item2
    );
}
"@
    }
    [ExplorerIconRefresh]::SHChangeNotify(
        0x00002000,  # SHCNE_UPDATEITEM
        0x0005,      # SHCNF_PATHW
        [IO.Path]::GetFullPath($Path),
        [IntPtr]::Zero
    )
}

Invoke-NuitkaBuild `
    -Name "Boss登录浏览器" `
    -Entry (Join-Path $StagingRoot "tools\open_login_edge.py") `
    -Icon (Join-Path $OfficialIcons "boss_login.ico")
Invoke-NuitkaBuild `
    -Name "Boss求职助手" `
    -Entry (Join-Path $StagingRoot "run_control_panel.py") `
    -Icon (Join-Path $OfficialIcons "boss_assistant.ico") `
    -MainApplication

foreach ($name in @("Boss登录浏览器.exe", "Boss求职助手.exe")) {
    $source = Join-Path $OutputRoot $name
    $target = Join-Path $ProjectRoot $name
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Nuitka未生成预期产物：$source"
    }
    if (Test-Path -LiteralPath $target -PathType Leaf) {
        Remove-Item -LiteralPath $target -Force
    }
    Copy-Item -LiteralPath $source -Destination $target
    Update-ExplorerIcon $target
}

try {
    $iconRefresh = Start-Process `
        -FilePath (Join-Path $env:SystemRoot "System32\ie4uinit.exe") `
        -ArgumentList "-show" `
        -WindowStyle Hidden `
        -PassThru
    $iconRefresh.WaitForExit(5000) | Out-Null
} catch {
    Write-Warning "系统图标刷新命令执行失败；EXE已构建完成，可在资源管理器中按F5。"
}

if (-not $KeepStaging -and (Test-Path -LiteralPath $StagingRoot -PathType Container)) {
    [IO.Directory]::Delete($StagingRoot, $true)
}

Write-Host "`nNuitka构建完成：" -ForegroundColor Green
Get-Item "Boss登录浏览器.exe", "Boss求职助手.exe" |
    Select-Object Name, @{N="大小MB"; E={[math]::Round($_.Length / 1MB, 1)}}, LastWriteTime
