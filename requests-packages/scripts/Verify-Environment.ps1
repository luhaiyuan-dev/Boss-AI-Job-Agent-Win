[CmdletBinding()]
param()

$ErrorActionPreference = "Continue"
$bundleRoot = Split-Path -Parent $PSScriptRoot
$projectRoot = Split-Path -Parent $bundleRoot
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
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return $candidate
        }
    }
    return $null
}

function Find-Codex {
    $command = Get-Command codex -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { return $command.Source }
    $installRoot = Join-Path $env:LOCALAPPDATA "BossJobAssistant\Codex"
    return Get-ChildItem -LiteralPath $installRoot -Recurse -Filter codex.exe -File `
        -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending |
        Select-Object -First 1 -ExpandProperty FullName
}

function Find-PowerShell7 {
    $command = Get-Command pwsh.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command -and (Test-Path -LiteralPath $command.Source -PathType Leaf)) {
        return $command.Source
    }
    $installedPath = Join-Path $env:ProgramFiles "PowerShell\7\pwsh.exe"
    if (Test-Path -LiteralPath $installedPath -PathType Leaf) { return $installedPath }
    return $null
}

Write-Host "Boss求职助手 Win-Web 环境检查（只读，不启动Boss自动化）" -ForegroundColor Cyan
Write-Host "目标运行根目录：$projectRoot" -ForegroundColor DarkGray

$pwsh = Find-PowerShell7
$pwshVersion = if ($pwsh) {
    & $pwsh -NoLogo -NoProfile -Command '$PSVersionTable.PSVersion.ToString()' 2>$null |
        Select-Object -Last 1
} else {
    $null
}
Write-Check (
    [bool]$pwshVersion -and [int](($pwshVersion -split "\.")[0]) -ge 7
) "PowerShell 7（$pwshVersion）"

$edge = Find-Edge
Write-Check ([bool]$edge) "Microsoft Edge"
if ($edge) {
    Write-Host "[通过] Edge $((Get-Item -LiteralPath $edge).VersionInfo.FileVersion)" -ForegroundColor Green
}

$vcRuntime = Get-ItemProperty `
    "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" `
    -ErrorAction SilentlyContinue
Write-Check ($vcRuntime -and [int]$vcRuntime.Installed -eq 1) "Microsoft Visual C++ x64运行库"

$mysqlServices = @(Get-Service -Name "*mysql*" -ErrorAction SilentlyContinue)
$runningMySql = @($mysqlServices | Where-Object Status -eq "Running")
if ($runningMySql.Count -gt 0) {
    Write-Host "[通过] MySQL服务正在运行：$($runningMySql.Name -join ', ')" -ForegroundColor Green
} elseif ($mysqlServices.Count -gt 0) {
    $manualSteps += "已检测到MySQL服务但当前未运行：$($mysqlServices.Name -join ', ')。请启动实际使用的服务。"
} else {
    $manualSteps += "未检测到MySQL服务；如使用远程数据库，请忽略并在GUI配置中填写实际地址。"
}

$navicat = Get-ItemProperty @(
    "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*",
    "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*",
    "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*"
) -ErrorAction SilentlyContinue |
    Where-Object { ([string]$_.DisplayName) -match "Navicat" } |
    Select-Object -First 1
if ($navicat) {
    Write-Host "[通过] 已检测到 $($navicat.DisplayName)" -ForegroundColor Green
} else {
    Write-Host "[可选] 未检测到Navicat；不影响EXE运行。" -ForegroundColor DarkYellow
}

$guiConfig = Join-Path $projectRoot "config\gui_defaults.txt"
if (Test-Path -LiteralPath $guiConfig -PathType Leaf) {
    Write-Host "[通过] GUI/MySQL配置模板存在：$guiConfig" -ForegroundColor Green
    $guiText = Get-Content -LiteralPath $guiConfig -Raw -Encoding UTF8
    if ($guiText -match "MySQL用户名\s*[：:]\s*[。.]" -or $guiText -match "MySQL密码\s*[：:]\s*[。.]") {
        $manualSteps += "编辑 $guiConfig，填写当前电脑可用的MySQL用户名和密码。"
    }
} else {
    $manualSteps += "重新运行一键部署以创建 $guiConfig。"
}

$apiConfig = Join-Path $projectRoot "config\model_api.local.json"
if (Test-Path -LiteralPath $apiConfig -PathType Leaf) {
    try {
        $api = Get-Content -LiteralPath $apiConfig -Raw -Encoding UTF8 | ConvertFrom-Json
        $keyValue = [string]$api.api_key
        $keyEnvironmentName = [string]$api.api_key_env
        if ([string]::IsNullOrWhiteSpace($keyValue) -and -not [string]::IsNullOrWhiteSpace($keyEnvironmentName)) {
            $keyValue = [Environment]::GetEnvironmentVariable($keyEnvironmentName)
        }
        $hasKey = -not [string]::IsNullOrWhiteSpace($keyValue)
        $hasUrl = ([string]$api.base_url).StartsWith("http://") -or ([string]$api.base_url).StartsWith("https://")
        $hasModel = -not [string]::IsNullOrWhiteSpace([string]$api.model)
        if ($hasKey -and $hasUrl -and $hasModel) {
            Write-Host "[通过] 大模型API配置完整（模型：$($api.model)，未输出密钥）" -ForegroundColor Green
            $apiReady = $true
        } else {
            $manualSteps += "如使用大模型API，请填写 $apiConfig 的API Key、base_url和model。"
        }
    } catch {
        $manualSteps += "$apiConfig 不是有效JSON，请根据模板修正。"
    }
} else {
    $manualSteps += "重新运行一键部署以创建 $apiConfig。"
}

$codexCommand = Find-Codex
if ($codexCommand) {
    $codexVersion = (& $codexCommand --version 2>$null | Select-Object -First 1)
    & $codexCommand login status 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[通过] Codex已安装并登录（$codexVersion）" -ForegroundColor Green
        $codexReady = $true
    } else {
        $manualSteps += "Codex已安装（$codexVersion）但未登录；使用Codex主导前执行 codex login。"
    }
} else {
    Write-Host "[可选] 未检测到Codex；使用大模型API时可忽略。" -ForegroundColor DarkYellow
}

$assistantExe = Join-Path $projectRoot "Boss求职助手.exe"
$loginExe = Join-Path $projectRoot "Boss登录浏览器.exe"
if ((Test-Path -LiteralPath $assistantExe -PathType Leaf) -and (Test-Path -LiteralPath $loginExe -PathType Leaf)) {
    Write-Host "[通过] 两个Nuitka EXE已位于目标根目录。" -ForegroundColor Green
} else {
    $manualSteps += "环境就绪后，把Boss求职助手.exe和Boss登录浏览器.exe复制到 $projectRoot。"
}

if (-not $apiReady -and -not $codexReady) {
    $manualSteps += "开始模型审核前至少完成一种方式：API配置，或Codex安装并登录。"
}

if ($manualSteps.Count -gt 0) {
    Write-Host "`n仍需人工完成或确认：" -ForegroundColor Yellow
    foreach ($step in $manualSteps) { Write-Host "- $step" -ForegroundColor Yellow }
}

if ($hostFailed) { exit 1 }
Write-Host "`n电脑端核心环境检查完成；未打开Boss、模型接口或执行投递。" -ForegroundColor Green
exit 0
