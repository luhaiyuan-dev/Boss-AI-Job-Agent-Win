[CmdletBinding()]
param()

$ErrorActionPreference = "Continue"
$bundleRoot = Split-Path -Parent $PSScriptRoot
$projectRoot = Split-Path -Parent $bundleRoot
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$hostFailed = $false
$manualSteps = @()
$apiReady = $false
$codexReady = $false

function Write-Check([bool]$Ok, [string]$Message) {
    if ($Ok) {
        Write-Host "[通过] $Message" -ForegroundColor Green
    } else {
        Write-Host "[失败] $Message" -ForegroundColor Red
        $script:hostFailed = $true
    }
}

function Find-Edge {
    foreach ($candidate in @(
        (Join-Path ${env:ProgramFiles(x86)} "Microsoft\Edge\Application\msedge.exe"),
        (Join-Path $env:ProgramFiles "Microsoft\Edge\Application\msedge.exe"),
        (Join-Path $env:LOCALAPPDATA "Microsoft\Edge\Application\msedge.exe")
    )) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) { return $candidate }
    }
    return $null
}

Write-Host "Boss 求职助手 Win-Web 环境检查（只读，不启动 Boss 自动化）" -ForegroundColor Cyan

Write-Check (Test-Path -LiteralPath $venvPython -PathType Leaf) "项目虚拟环境 .venv"
if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
    & $venvPython -c "import struct,sys,tkinter; assert sys.version_info[:3] == (3,13,14); assert struct.calcsize('P') == 8; print('[通过] Python', sys.version.split()[0], 'x64 + Tcl/Tk')"
    if ($LASTEXITCODE -ne 0) { $hostFailed = $true }
    & $venvPython -c "from importlib.metadata import version as v; expected={'selenium':'4.45.0','websocket-client':'1.9.0','webdriver-manager':'4.1.2','mysql-connector-python':'9.7.0','pypdf':'6.14.2','OpenCC':'1.3.2'}; bad={k:(v(k),x) for k,x in expected.items() if v(k)!=x}; assert not bad,bad; import tkinter; print('[通过] Python 直接依赖版本与离线锁定一致')"
    if ($LASTEXITCODE -ne 0) { $hostFailed = $true }
    & $venvPython -m pip check
    if ($LASTEXITCODE -ne 0) { $hostFailed = $true }
}

$edge = Find-Edge
Write-Check ([bool]$edge) "Microsoft Edge"
if ($edge) {
    $edgeVersion = (Get-Item -LiteralPath $edge).VersionInfo.FileVersion
    Write-Host "[通过] Edge $edgeVersion：$edge" -ForegroundColor Green
    if ($edgeVersion -ne "151.0.4129.59") {
        Write-Host "[提示] 随包验证版本为 151.0.4129.59；当前版本不同但未自动降级。" -ForegroundColor Yellow
    }
}

$mysqlServices = @(Get-Service -Name "*mysql*" -ErrorAction SilentlyContinue)
$runningMySql = @($mysqlServices | Where-Object Status -eq "Running")
Write-Check ($runningMySql.Count -gt 0) "至少一个 MySQL 服务正在运行"
if ($runningMySql.Count -gt 0) {
    Write-Host "[通过] MySQL 服务：$($runningMySql.Name -join ', ')" -ForegroundColor Green
}

$guiConfig = Join-Path $projectRoot "config\gui_defaults.txt"
if (Test-Path -LiteralPath $guiConfig -PathType Leaf) {
    Write-Host "[通过] GUI/MySQL 本机配置存在：$guiConfig" -ForegroundColor Green
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        & $venvPython -c "import sys; sys.path.insert(0,sys.argv[2]); import mysql.connector as m; from boss_assistant.gui.app import load_gui_defaults; d=load_gui_defaults(sys.argv[1]); c=m.connect(host=d['MySQL主机'],port=int(d['MySQL端口']),user=d['MySQL用户名'],password=d['MySQL密码'],database=d['MySQL数据库'],connection_timeout=3); c.close(); print('[通过] 按 GUI 配置连接 MySQL 成功（未输出凭据）')" $guiConfig $projectRoot
        if ($LASTEXITCODE -ne 0) {
            $manualSteps += "编辑 $guiConfig，填写可连接且有建表/读写权限的 MySQL 凭据。"
        }
    }
} else {
    $manualSteps += "从 config\gui_defaults.example.txt 创建 $guiConfig，并填写 MySQL 凭据。"
}

$navicatRecords = Get-ItemProperty @(
    "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*",
    "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*",
    "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*"
) -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -match "Navicat Premium 17" }
if (@($navicatRecords).Count -gt 0) {
    Write-Host "[通过] 已检测到 Navicat Premium 17（可选管理工具）" -ForegroundColor Green
} else {
    Write-Host "[可选] Navicat 未安装；不影响脚本运行。" -ForegroundColor DarkYellow
}

$apiConfig = Join-Path $projectRoot "config\model_api.local.json"
try {
    $api = Get-Content -LiteralPath $apiConfig -Raw -Encoding UTF8 | ConvertFrom-Json
    $keyValue = [string]$api.api_key
    $keyEnvironmentName = [string]$api.api_key_env
    if ([string]::IsNullOrWhiteSpace($keyValue) -and -not [string]::IsNullOrWhiteSpace($keyEnvironmentName)) {
        $keyValue = [Environment]::GetEnvironmentVariable($keyEnvironmentName)
    }
    $hasKey = -not [string]::IsNullOrWhiteSpace($keyValue)
    $hasBaseUrl = ([string]$api.base_url).StartsWith("http://") -or ([string]$api.base_url).StartsWith("https://")
    $hasModel = -not [string]::IsNullOrWhiteSpace([string]$api.model)
    if ($hasKey -and $hasBaseUrl -and $hasModel) {
        Write-Host "[通过] 大模型 API 配置包含密钥、base_url 与 model（未输出内容）" -ForegroundColor Green
        $apiReady = $true
    } else {
        $manualSteps += "如使用大模型API，请填写 $apiConfig 的密钥、base_url 与 model。"
    }
} catch {
    $manualSteps += "根据 config\model_api.example.json 创建有效的 $apiConfig。"
}

$codexCommand = Get-Command codex -ErrorAction SilentlyContinue | Select-Object -First 1
if ($codexCommand) {
    $codexVersion = (& $codexCommand.Source --version 2>$null | Select-Object -First 1)
    & $codexCommand.Source login status 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[通过] Codex 已安装并登录（$codexVersion）" -ForegroundColor Green
        $codexReady = $true
    } else {
        $manualSteps += "Codex 已安装（$codexVersion）但未确认登录；需要时执行 codex login。"
    }
} else {
    $manualSteps += "如使用Codex主导，请运行 scripts\Install-Codex-Optional.ps1；使用大模型API时可忽略。"
}

$resumeCount = @(Get-ChildItem -LiteralPath (Join-Path $projectRoot "resume_inbox") -File -ErrorAction SilentlyContinue | Where-Object Extension -match "^\.pdf$").Count
if ($resumeCount -eq 1) {
    Write-Host "[通过] resume_inbox 中恰好有一份 PDF（未输出文件名）" -ForegroundColor Green
} else {
    $manualSteps += "在 $projectRoot\resume_inbox 中只放一份带文本层 PDF；当前数量：$resumeCount。"
}

if (-not $apiReady -and -not $codexReady) {
    $manualSteps += "开始模型审核前至少完成一种方式：API 配置，或 Codex 安装并登录。"
}

$unexpectedAdb = @(Get-ChildItem -LiteralPath $bundleRoot -Recurse -File -ErrorAction SilentlyContinue | Where-Object Name -match "adb|platform-tools")
Write-Check ($unexpectedAdb.Count -eq 0) "Win-Web 部署包未混入 Android ADB 组件"

if ($manualSteps.Count -gt 0) {
    Write-Host "`n仍需人工完成或确认：" -ForegroundColor Yellow
    foreach ($step in $manualSteps) { Write-Host "- $step" -ForegroundColor Yellow }
}

if ($hostFailed) { exit 1 }
Write-Host "`n电脑端核心环境检查完成；本脚本没有打开 Boss、模型接口或执行投递动作。" -ForegroundColor Green
exit 0
