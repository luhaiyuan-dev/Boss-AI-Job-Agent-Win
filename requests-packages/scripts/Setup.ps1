[CmdletBinding()]
param(
    [switch]$SkipEdge,
    [switch]$SkipMySql,
    [switch]$SkipNavicat,
    [switch]$SkipApiConfig,
    [switch]$SkipCodex,
    [switch]$SkipAiSetup,
    [switch]$NoElevation
)

$ErrorActionPreference = "Stop"
$bundleRoot = Split-Path -Parent $PSScriptRoot
$projectRoot = Split-Path -Parent $bundleRoot
$installerRoot = Join-Path $bundleRoot "installers"
$wheelhouse = Join-Path $bundleRoot "wheelhouse"
$logRoot = Join-Path $bundleRoot "logs"
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
$logPath = Join-Path $logRoot ("setup-{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
Start-Transcript -Path $logPath -Force | Out-Null

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

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

try {
    if ($SkipAiSetup) {
        $SkipApiConfig = $true
        $SkipCodex = $true
    }

    if (-not $NoElevation -and -not (Test-Administrator)) {
        Stop-Transcript | Out-Null
        $arguments = @(
            "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", ('"{0}"' -f $PSCommandPath), "-NoElevation"
        )
        foreach ($switchName in @("SkipEdge", "SkipMySql", "SkipNavicat", "SkipApiConfig", "SkipCodex", "SkipAiSetup")) {
            if ((Get-Variable -Name $switchName -ValueOnly)) { $arguments += "-$switchName" }
        }
        $elevated = Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList $arguments -Wait -PassThru
        exit $elevated.ExitCode
    }

    Write-Host "1/8 校验全部离线安装资源" -ForegroundColor Cyan
    & (Join-Path $PSScriptRoot "Verify-Packages.ps1")
    if ($LASTEXITCODE -ne 0) { throw "离线安装包校验失败。" }

    Write-Host "2/8 安装并定位 Python 3.13.14 x64" -ForegroundColor Cyan
    $pythonInstaller = Join-Path $installerRoot "python-3.13.14-amd64.exe"
    $pythonCandidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"),
        (Join-Path $env:ProgramFiles "Python313\python.exe")
    )
    $python = $pythonCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if (-not $python -and (Get-Command py.exe -ErrorAction SilentlyContinue)) {
        $detected = (& py.exe -3.13 -c "import sys; print(sys.executable)" 2>$null | Select-Object -Last 1)
        if ($LASTEXITCODE -eq 0 -and $detected -and (Test-Path -LiteralPath $detected -PathType Leaf)) {
            $python = $detected
        }
    }
    if ($python) {
        $detectedVersion = (& $python -c "import platform; print(platform.python_version())")
        if ($detectedVersion -ne "3.13.14") {
            Write-Host "检测到 Python $detectedVersion；本部署包将并行安装已验证的 3.13.14。" -ForegroundColor Yellow
            $python = $null
        }
    }
    if (-not $python) {
        $process = Start-Process -FilePath $pythonInstaller -Wait -PassThru -ArgumentList @(
            "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_pip=1",
            "Include_launcher=1", "Include_tcltk=1", "Include_test=0", "Shortcuts=0"
        )
        if ($process.ExitCode -notin @(0, 3010)) { throw "Python 安装失败，退出码 $($process.ExitCode)。" }
        $python = $pythonCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
        if (-not $python -and (Get-Command py.exe -ErrorAction SilentlyContinue)) {
            $python = (& py.exe -3.13 -c "import sys; print(sys.executable)" | Select-Object -Last 1)
        }
    }
    if (-not $python) { throw "Python 安装结束后仍未找到 python.exe。" }
    & $python -c "import struct,sys,tkinter; assert sys.version_info[:3] == (3,13,14); assert struct.calcsize('P') == 8"
    if ($LASTEXITCODE -ne 0) { throw "Python 3.13.14 x64 或 Tcl/Tk 校验失败。" }

    Write-Host "3/8 创建项目虚拟环境并离线安装 Python 依赖" -ForegroundColor Cyan
    $venvRoot = Join-Path $projectRoot ".venv"
    $venvPython = Join-Path $venvRoot "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        if (Test-Path -LiteralPath $venvRoot) {
            throw ".venv 已存在但不是有效虚拟环境。为保护现有文件，请先人工改名后重试。"
        }
        & $python -m venv $venvRoot
        if ($LASTEXITCODE -ne 0) { throw "创建 .venv 失败。" }
    }
    & $venvPython -c "import struct,sys; assert sys.version_info[:3] == (3,13,14); assert struct.calcsize('P') == 8"
    if ($LASTEXITCODE -ne 0) {
        throw "现有 .venv 不是 Python 3.13.14 x64。请人工改名或移走后重试。"
    }
    & $venvPython -m pip install --no-index --find-links $wheelhouse pip==26.1.2 setuptools==81.0.0 wheel==0.47.0
    if ($LASTEXITCODE -ne 0) { throw "离线安装 Python 基础工具失败。" }
    & $venvPython -m pip install --no-index --find-links $wheelhouse -r (Join-Path $bundleRoot "requirements-offline.txt")
    if ($LASTEXITCODE -ne 0) { throw "离线安装项目依赖失败。" }
    & $venvPython -c "import mysql.connector, opencc, pypdf, selenium, tkinter, websocket, webdriver_manager"
    if ($LASTEXITCODE -ne 0) { throw "项目依赖导入校验失败。" }

    Write-Host "4/8 检查或安装 Microsoft Edge" -ForegroundColor Cyan
    if ($SkipEdge) {
        Write-Host "已按参数跳过 Edge 安装。" -ForegroundColor Yellow
    } else {
        $edge = Find-Edge
        if ($edge) {
            $edgeVersion = (Get-Item -LiteralPath $edge).VersionInfo.FileVersion
            Write-Host "已检测到 Edge $edgeVersion，保持现有安装：$edge" -ForegroundColor Green
            if ($edgeVersion -ne "151.0.4129.59") {
                Write-Host "随包验证版本为 151.0.4129.59；未自动降级或覆盖现有 Edge。" -ForegroundColor Yellow
            }
        } else {
            $edgeInstaller = Join-Path $installerRoot "MicrosoftEdgeEnterpriseX64-151.0.4129.59.msi"
            $edgeProcess = Start-Process -FilePath "msiexec.exe" -Wait -PassThru -ArgumentList @(
                "/i", ('"{0}"' -f $edgeInstaller), "/qn", "/norestart"
            )
            if ($edgeProcess.ExitCode -notin @(0, 3010)) { throw "Edge 安装失败，退出码 $($edgeProcess.ExitCode)。" }
            $edge = Find-Edge
            if (-not $edge) { throw "Edge 安装结束后仍未找到 msedge.exe。" }
            Write-Host "Edge 151.0.4129.59 已安装。" -ForegroundColor Green
        }
    }

    Write-Host "5/8 配置 MySQL 8.0.36" -ForegroundColor Cyan
    $newMySqlInstalled = $false
    if ($SkipMySql) {
        Write-Host "已按参数跳过 MySQL；请在 GUI 中填写现有实例凭据。" -ForegroundColor Yellow
    } else {
        $serviceName = "BossJobAssistantMySQL"
        $existingOwnService = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
        $otherMySqlServices = @(Get-Service -Name "*mysql*" -ErrorAction SilentlyContinue | Where-Object { $_.Name -ne $serviceName })
        $portInUse = @(Get-NetTCPConnection -State Listen -LocalPort 3306 -ErrorAction SilentlyContinue).Count -gt 0
        if (-not $existingOwnService -and ($otherMySqlServices.Count -gt 0 -or $portInUse)) {
            Write-Host "检测到现有 MySQL 或 3306 端口占用，未覆盖现有数据库。请在 GUI 中填写实际凭据。" -ForegroundColor Yellow
        } else {
            $vcInstaller = Join-Path $installerRoot "vc_redist.x64.exe"
            $vcProcess = Start-Process -FilePath $vcInstaller -Wait -PassThru -ArgumentList @("/install", "/quiet", "/norestart")
            if ($vcProcess.ExitCode -notin @(0, 1638, 3010)) { throw "Visual C++ 运行库安装失败，退出码 $($vcProcess.ExitCode)。" }

            $mysqlBase = Join-Path $env:ProgramData "BossJobAssistant\MySQL"
            $mysqlHome = Join-Path $mysqlBase "mysql-8.0.36-winx64"
            $mysqlData = Join-Path $mysqlBase "data"
            $mysqlIni = Join-Path $mysqlBase "my.ini"
            New-Item -ItemType Directory -Force -Path $mysqlBase | Out-Null
            if (-not (Test-Path -LiteralPath (Join-Path $mysqlHome "bin\mysqld.exe") -PathType Leaf)) {
                if ($existingOwnService) {
                    throw "已存在 $serviceName 服务，但随包 MySQL 程序目录缺失；为保护现有服务已停止。"
                }
                $staging = Join-Path $mysqlBase "extracting"
                if (Test-Path -LiteralPath $staging) { [IO.Directory]::Delete($staging, $true) }
                New-Item -ItemType Directory -Path $staging | Out-Null
                Expand-Archive -LiteralPath (Join-Path $installerRoot "mysql-8.0.36-winx64.zip") -DestinationPath $staging -Force
                Move-Item -LiteralPath (Join-Path $staging "mysql-8.0.36-winx64") -Destination $mysqlHome
                [IO.Directory]::Delete($staging, $true)
            }

            $baseForward = $mysqlHome.Replace("\", "/")
            $dataForward = $mysqlData.Replace("\", "/")
            @"
[mysqld]
basedir=$baseForward
datadir=$dataForward
port=3306
bind-address=127.0.0.1
character-set-server=utf8mb4
collation-server=utf8mb4_0900_ai_ci
mysqlx=0

[client]
port=3306
default-character-set=utf8mb4
"@ | Set-Content -LiteralPath $mysqlIni -Encoding ASCII

            $mysqld = Join-Path $mysqlHome "bin\mysqld.exe"
            $mysql = Join-Path $mysqlHome "bin\mysql.exe"
            if (-not (Test-Path -LiteralPath (Join-Path $mysqlData "mysql") -PathType Container)) {
                & $mysqld "--defaults-file=$mysqlIni" --initialize-insecure --console
                if ($LASTEXITCODE -ne 0) { throw "MySQL 数据目录初始化失败。" }
            }
            if (-not $existingOwnService) {
                & $mysqld --install $serviceName "--defaults-file=$mysqlIni"
                if ($LASTEXITCODE -ne 0) { throw "MySQL Windows 服务安装失败。" }
                $newMySqlInstalled = $true
            }
            $service = Get-Service -Name $serviceName -ErrorAction Stop
            if ($service.Status -ne "Running") { Start-Service -Name $serviceName }
            $ready = $false
            for ($attempt = 0; $attempt -lt 30; $attempt++) {
                & (Join-Path $mysqlHome "bin\mysqladmin.exe") --host=127.0.0.1 --user=root --skip-password ping 2>$null | Out-Null
                if ($LASTEXITCODE -eq 0) { $ready = $true; break }
                Start-Sleep -Seconds 1
            }
            if ($newMySqlInstalled -and -not $ready) { throw "MySQL 服务启动后 30 秒内未就绪。" }
            if ($newMySqlInstalled) {
                & $mysql --host=127.0.0.1 --user=root --skip-password --execute="ALTER USER 'root'@'localhost' IDENTIFIED BY 'root'; CREATE DATABASE IF NOT EXISTS boss_job_assistant CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"
                if ($LASTEXITCODE -ne 0) { throw "MySQL root 密码或项目数据库初始化失败。" }
            }
            Add-UserPath (Join-Path $mysqlHome "bin")
            Write-Host "MySQL 仅监听 127.0.0.1:3306；新安装实例默认凭据为 root/root。" -ForegroundColor Green
        }
    }

    Write-Host "6/8 安装可选 Navicat Premium 17.3.11" -ForegroundColor Cyan
    if ($SkipNavicat) {
        Write-Host "已按参数跳过 Navicat；它不是脚本运行依赖。" -ForegroundColor Yellow
    } else {
        $navicatRecord = Get-ItemProperty @(
            "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*",
            "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*",
            "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*"
        ) -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -match "Navicat Premium 17" } | Select-Object -First 1
        $navicatExe = $null
        if ($navicatRecord -and $navicatRecord.DisplayIcon) {
            $candidate = ([string]$navicatRecord.DisplayIcon).Trim('"')
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { $navicatExe = $candidate }
        }
        $installedVersion = if ($navicatExe) { (Get-Item -LiteralPath $navicatExe).VersionInfo.FileVersion } else { $null }
        if ($installedVersion -eq "17.3.11.0") {
            Write-Host "已安装 Navicat Premium 17.3.11，保持现状。" -ForegroundColor Green
        } else {
            $navicatInstaller = Join-Path $installerRoot "navicat-premium-17.3.11-en-x64.exe"
            $navicatProcess = Start-Process -FilePath $navicatInstaller -Wait -PassThru -ArgumentList @(
                "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"
            )
            if ($navicatProcess.ExitCode -notin @(0, 3010)) { throw "Navicat 安装失败，退出码 $($navicatProcess.ExitCode)。" }
            Write-Host "Navicat 官方安装器已完成；未复制旧电脑许可证、连接或密码。" -ForegroundColor Green
        }
    }

    Write-Host "7/8 创建本机配置文件（不写入私人密钥）" -ForegroundColor Cyan
    $guiExamplePath = Join-Path $projectRoot "config\gui_defaults.example.txt"
    $guiConfigPath = Join-Path $projectRoot "config\gui_defaults.txt"
    if (-not (Test-Path -LiteralPath $guiConfigPath -PathType Leaf)) {
        $guiText = Get-Content -LiteralPath $guiExamplePath -Raw -Encoding UTF8
        if ($newMySqlInstalled) {
            $guiText = $guiText.Replace("MySQL用户名：。", "MySQL用户名：root。").Replace("MySQL密码：。", "MySQL密码：root。")
        }
        Set-Content -LiteralPath $guiConfigPath -Value $guiText -Encoding UTF8
        Write-Host "已创建 GUI 本机配置：$guiConfigPath" -ForegroundColor Green
    } else {
        Write-Host "已存在 GUI 本机配置，未覆盖：$guiConfigPath" -ForegroundColor Green
    }

    $apiExamplePath = Join-Path $projectRoot "config\model_api.example.json"
    $apiConfigPath = Join-Path $projectRoot "config\model_api.local.json"
    if (-not (Test-Path -LiteralPath $apiConfigPath -PathType Leaf)) {
        Copy-Item -LiteralPath $apiExamplePath -Destination $apiConfigPath
        Write-Host "已从脱敏模板创建 API 配置：$apiConfigPath" -ForegroundColor Green
    } else {
        Write-Host "已存在 API 本机配置，未覆盖也未输出其中内容：$apiConfigPath" -ForegroundColor Green
    }

    Write-Host "8/8 可选配置 API Key 与 Codex 登录" -ForegroundColor Cyan
    Write-Host "API 配置文件：$apiConfigPath" -ForegroundColor Yellow
    Write-Host "GUI/MySQL 配置文件：$guiConfigPath" -ForegroundColor Yellow
    if ($SkipApiConfig) {
        Write-Host "已跳过 API Key 配置；之后编辑上面的 model_api.local.json 即可。" -ForegroundColor Yellow
    } else {
        $apiChoice = Read-Host "现在打开 API 配置文件吗？输入 Y 打开，直接回车跳过"
        if ($apiChoice -match "^[Yy]$") {
            Start-Process -FilePath "notepad.exe" -ArgumentList ('"{0}"' -f $apiConfigPath) -Wait
            & $venvPython -c "import sys; sys.path.insert(0, sys.argv[2]); from dataclasses import replace; from boss_assistant.automation.api_provider import ApiProviderConfig; c=replace(ApiProviderConfig.from_json(sys.argv[1]), enabled=True); c.validate_for_request(); print('API 配置格式与必填项校验通过（未调用接口）')" $apiConfigPath $projectRoot
            if ($LASTEXITCODE -ne 0) {
                Write-Host "API 配置尚未通过校验，可先跳过，之后继续编辑：$apiConfigPath" -ForegroundColor Yellow
            }
        } else {
            Write-Host "已跳过 API Key 配置。之后请编辑：$apiConfigPath" -ForegroundColor Yellow
        }
    }

    if ($SkipCodex) {
        Write-Host "已跳过 Codex 安装与登录；之后可运行 scripts\Install-Codex-Optional.ps1。" -ForegroundColor Yellow
    } else {
        $codexChoice = Read-Host "现在离线安装并登录 Codex CLI 吗？输入 Y 继续，直接回车跳过"
        if ($codexChoice -match "^[Yy]$") {
            try {
                & (Join-Path $PSScriptRoot "Install-Codex-Optional.ps1") -Accept -Login
            } catch {
                Write-Host "Codex 可选步骤未完成：$($_.Exception.Message)" -ForegroundColor Yellow
                Write-Host "这不会撤销电脑环境部署；之后可重新运行 Codex 可选安装脚本。" -ForegroundColor Yellow
            }
        } else {
            Write-Host "已跳过 Codex 安装/登录。登录状态不在项目内，也不会随部署包复制。" -ForegroundColor Yellow
        }
    }

    & (Join-Path $PSScriptRoot "Verify-Environment.ps1")
    Write-Host "`n电脑端部署完成。下一步请阅读 requests-packages\首次使用向导.md。" -ForegroundColor Green
    Write-Host "先双击“打开Boss登录Edge.cmd”手动登录，再双击“启动Boss助手.cmd”。" -ForegroundColor Green
    Write-Host "API 与 Codex 可以都跳过，但开始模型审核前必须至少完成一种。" -ForegroundColor Yellow
    Write-Host "本部署过程没有打开 Boss 职位、填写草稿或发送任何内容。" -ForegroundColor Yellow
    Write-Host "安装日志：$logPath"
} catch {
    Write-Host "`n部署失败：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host "安装日志：$logPath" -ForegroundColor Yellow
    exit 1
} finally {
    try { Stop-Transcript | Out-Null } catch {}
}
