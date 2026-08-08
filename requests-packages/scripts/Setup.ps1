[CmdletBinding()]
param(
    [switch]$SkipEdge,
    [switch]$SkipMySql,
    [switch]$SkipNavicat,
    [switch]$NoElevation
)

$ErrorActionPreference = "Stop"
$bundleRoot = Split-Path -Parent $PSScriptRoot
$projectRoot = Split-Path -Parent $bundleRoot
$installerRoot = Join-Path $bundleRoot "installers"
$templateRoot = Join-Path $bundleRoot "templates"
$logRoot = Join-Path $bundleRoot "logs"
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
$logPath = Join-Path $logRoot ("setup-{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
if ($PSVersionTable.PSVersion.Major -lt 7) {
    Write-Host "正式部署必须由PowerShell 7执行；请双击requests-packages\一键部署.cmd。" -ForegroundColor Red
    exit 1
}
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
        [Environment]::SetEnvironmentVariable(
            "Path",
            (@($parts) + $PathToAdd) -join ";",
            "User"
        )
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

function Get-InstalledProduct([string]$Pattern) {
    return Get-ItemProperty @(
        "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*",
        "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*",
        "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*"
    ) -ErrorAction SilentlyContinue |
        Where-Object { ([string]$_.DisplayName) -match $Pattern } |
        Select-Object -First 1
}

function Test-VcRuntimeX64 {
    $runtime = Get-ItemProperty `
        "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" `
        -ErrorAction SilentlyContinue
    return ($runtime -and [int]$runtime.Installed -eq 1)
}

function ConvertTo-PlainText([Security.SecureString]$SecureValue) {
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureValue)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

function Read-ConfirmedMySqlPassword {
    while ($true) {
        $firstSecure = Read-Host "请设置新MySQL root密码（至少8个字符，输入不回显）" -AsSecureString
        $secondSecure = Read-Host "请再次输入MySQL root密码" -AsSecureString
        $first = ConvertTo-PlainText $firstSecure
        $second = ConvertTo-PlainText $secondSecure
        if ($first.Length -lt 8) {
            Write-Host "密码长度不足8个字符，请重新输入。" -ForegroundColor Yellow
        } elseif ($first -cne $second) {
            Write-Host "两次密码不一致，请重新输入。" -ForegroundColor Yellow
        } else {
            return $first
        }
        $first = $null
        $second = $null
    }
}

function ConvertTo-MySqlStringLiteral([string]$Value) {
    $escaped = $Value.Replace("\", "\\").Replace("'", "''")
    return "'$escaped'"
}

try {
    if (-not $NoElevation -and -not (Test-Administrator)) {
        Stop-Transcript | Out-Null
        $arguments = @(
            "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", ('"{0}"' -f $PSCommandPath), "-NoElevation"
        )
        foreach ($switchName in @("SkipEdge", "SkipMySql", "SkipNavicat")) {
            if ((Get-Variable -Name $switchName -ValueOnly)) {
                $arguments += "-$switchName"
            }
        }
        $pwshPath = Join-Path $PSHOME "pwsh.exe"
        if (-not (Test-Path -LiteralPath $pwshPath -PathType Leaf)) {
            throw "当前未找到PowerShell 7主程序，请重新运行一键部署.cmd。"
        }
        $elevated = Start-Process `
            -FilePath $pwshPath `
            -Verb RunAs `
            -ArgumentList $arguments `
            -Wait `
            -PassThru
        exit $elevated.ExitCode
    }

    Write-Host "1/6 校验离线安装资源" -ForegroundColor Cyan
    & (Join-Path $PSScriptRoot "Verify-Packages.ps1")
    if ($LASTEXITCODE -ne 0) { throw "离线安装包校验失败。" }

    Write-Host "2/6 检查或安装 Microsoft Edge" -ForegroundColor Cyan
    if ($SkipEdge) {
        Write-Host "已按参数跳过 Edge 安装。" -ForegroundColor Yellow
    } else {
        $edge = Find-Edge
        if ($edge) {
            $edgeVersion = (Get-Item -LiteralPath $edge).VersionInfo.FileVersion
            Write-Host "已检测到 Edge $edgeVersion，保持现有安装。" -ForegroundColor Green
        } else {
            $edgeInstaller = Join-Path $installerRoot "MicrosoftEdgeEnterpriseX64-151.0.4129.59.msi"
            $edgeProcess = Start-Process `
                -FilePath "msiexec.exe" `
                -Wait `
                -PassThru `
                -ArgumentList @("/i", ('"{0}"' -f $edgeInstaller), "/qn", "/norestart")
            if ($edgeProcess.ExitCode -notin @(0, 3010)) {
                throw "Edge 安装失败，退出码 $($edgeProcess.ExitCode)。"
            }
            $edge = Find-Edge
            if (-not $edge) { throw "Edge 安装结束后仍未找到 msedge.exe。" }
            Write-Host "Microsoft Edge 已安装。" -ForegroundColor Green
        }
    }

    Write-Host "3/6 检查或安装 Microsoft Visual C++ x64 运行库" -ForegroundColor Cyan
    if (Test-VcRuntimeX64) {
        Write-Host "已检测到 VC++ x64 运行库，保持现有安装。" -ForegroundColor Green
    } else {
        $vcInstaller = Join-Path $installerRoot "vc_redist.x64.exe"
        $vcProcess = Start-Process `
            -FilePath $vcInstaller `
            -Wait `
            -PassThru `
            -ArgumentList @("/install", "/quiet", "/norestart")
        if ($vcProcess.ExitCode -notin @(0, 1638, 3010)) {
            throw "Visual C++ 运行库安装失败，退出码 $($vcProcess.ExitCode)。"
        }
        Write-Host "VC++ x64 运行库已安装。" -ForegroundColor Green
    }

    Write-Host "4/6 检查或安装 MySQL 8.0.36" -ForegroundColor Cyan
    if ($SkipMySql) {
        Write-Host "已按参数跳过 MySQL；之后请在配置中填写现有实例凭据。" -ForegroundColor Yellow
    } else {
        $serviceName = "BossJobAssistantMySQL"
        $mysqlBase = Join-Path $env:ProgramData "BossJobAssistant\MySQL"
        $mysqlHome = Join-Path $mysqlBase "mysql-8.0.36-winx64"
        $mysqlData = Join-Path $mysqlBase "data"
        $mysqlIni = Join-Path $mysqlBase "my.ini"
        $mysqlServices = @(Get-Service -Name "*mysql*" -ErrorAction SilentlyContinue)
        $portInUse = @(
            Get-NetTCPConnection -State Listen -LocalPort 3306 -ErrorAction SilentlyContinue
        ).Count -gt 0
        $mysqlFilesExist = Test-Path -LiteralPath (Join-Path $mysqlHome "bin\mysqld.exe") -PathType Leaf

        if ($mysqlServices.Count -gt 0 -or $portInUse -or $mysqlFilesExist) {
            $serviceNames = @($mysqlServices | ForEach-Object Name) -join ", "
            Write-Host (
                "已检测到MySQL环境或3306占用，保持现状且不修改数据库。" +
                $(if ($serviceNames) { " 服务：$serviceNames" } else { "" })
            ) -ForegroundColor Yellow
        } else {
            $mysqlPassword = Read-ConfirmedMySqlPassword
            New-Item -ItemType Directory -Force -Path $mysqlBase | Out-Null
            $staging = Join-Path $mysqlBase "extracting"
            if (Test-Path -LiteralPath $staging) {
                [IO.Directory]::Delete($staging, $true)
            }
            New-Item -ItemType Directory -Path $staging | Out-Null
            Expand-Archive `
                -LiteralPath (Join-Path $installerRoot "mysql-8.0.36-winx64.zip") `
                -DestinationPath $staging `
                -Force
            Move-Item `
                -LiteralPath (Join-Path $staging "mysql-8.0.36-winx64") `
                -Destination $mysqlHome
            [IO.Directory]::Delete($staging, $true)

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
            & $mysqld "--defaults-file=$mysqlIni" --initialize-insecure --console
            if ($LASTEXITCODE -ne 0) { throw "MySQL 数据目录初始化失败。" }
            & $mysqld --install $serviceName "--defaults-file=$mysqlIni"
            if ($LASTEXITCODE -ne 0) { throw "MySQL Windows 服务安装失败。" }
            Start-Service -Name $serviceName

            $ready = $false
            for ($attempt = 0; $attempt -lt 30; $attempt++) {
                $mysqlAdminExitCode = -1
                $savedErrorActionPreference = $ErrorActionPreference
                try {
                    # MySQL may emit a harmless security warning on stderr. The
                    # native exit code remains the authoritative readiness result.
                    $ErrorActionPreference = "Continue"
                    & (Join-Path $mysqlHome "bin\mysqladmin.exe") `
                        --host=127.0.0.1 --user=root --skip-password ping 2>$null | Out-Null
                    $mysqlAdminExitCode = $LASTEXITCODE
                } finally {
                    $ErrorActionPreference = $savedErrorActionPreference
                }
                if ($mysqlAdminExitCode -eq 0) { $ready = $true; break }
                Start-Sleep -Seconds 1
            }
            if (-not $ready) { throw "MySQL 服务启动后30秒内未就绪。" }

            $passwordLiteral = ConvertTo-MySqlStringLiteral $mysqlPassword
            $sql = (
                "ALTER USER 'root'@'localhost' IDENTIFIED BY $passwordLiteral; " +
                "CREATE DATABASE IF NOT EXISTS boss_job_assistant " +
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"
            )
            $mysqlBootstrapExitCode = -1
            $savedErrorActionPreference = $ErrorActionPreference
            try {
                $ErrorActionPreference = "Continue"
                $sql | & $mysql --host=127.0.0.1 --user=root --skip-password 2>$null
                $mysqlBootstrapExitCode = $LASTEXITCODE
            } finally {
                $ErrorActionPreference = $savedErrorActionPreference
            }
            if ($mysqlBootstrapExitCode -ne 0) {
                throw "MySQL root密码或项目数据库初始化失败。"
            }
            $sql = $null
            $passwordLiteral = $null
            $mysqlPassword = $null
            Add-UserPath (Join-Path $mysqlHome "bin")
            Write-Host "MySQL已安装并仅监听127.0.0.1:3306。" -ForegroundColor Green
            Write-Host "请稍后把刚才设置的凭据填写到config\gui_defaults.txt。" -ForegroundColor Yellow
        }
    }

    Write-Host "5/6 检查或安装 Navicat Premium" -ForegroundColor Cyan
    if ($SkipNavicat) {
        Write-Host "已按参数跳过 Navicat。" -ForegroundColor Yellow
    } else {
        $navicat = Get-InstalledProduct "Navicat"
        if ($navicat) {
            Write-Host "已检测到 $($navicat.DisplayName)，保持现有安装。" -ForegroundColor Green
        } else {
            $navicatInstaller = Join-Path $installerRoot "navicat-premium-17.3.11-en-x64.exe"
            $navicatProcess = Start-Process `
                -FilePath $navicatInstaller `
                -Wait `
                -PassThru `
                -ArgumentList @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-")
            if ($navicatProcess.ExitCode -notin @(0, 3010)) {
                throw "Navicat 安装失败，退出码 $($navicatProcess.ExitCode)。"
            }
            Write-Host "Navicat官方安装器已完成；未复制许可证或连接信息。" -ForegroundColor Green
        }
    }

    Write-Host "6/6 在目标目录创建外置配置模板" -ForegroundColor Cyan
    $configRoot = Join-Path $projectRoot "config"
    New-Item -ItemType Directory -Force -Path $configRoot | Out-Null
    $templateMappings = @(
        @("model_api.local.json", "model_api.local.json"),
        @("gui_defaults.txt", "gui_defaults.txt")
    )
    foreach ($mapping in $templateMappings) {
        $source = Join-Path $templateRoot $mapping[0]
        $target = Join-Path $configRoot $mapping[1]
        if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
            Copy-Item -LiteralPath $source -Destination $target
            Write-Host "已创建：$target" -ForegroundColor Green
        } else {
            Write-Host "已存在且未覆盖：$target" -ForegroundColor Green
        }
    }
    New-Item -ItemType Directory -Force -Path (Join-Path $projectRoot "resume_inbox") | Out-Null

    & (Join-Path $PSScriptRoot "Verify-Environment.ps1")
    if ($LASTEXITCODE -ne 0) { throw "电脑端核心环境验证未通过。" }

    Write-Host "`n环境部署完成。" -ForegroundColor Green
    Write-Host "请填写父目录config中的API/MySQL配置，再把两个EXE复制到父目录。" -ForegroundColor Yellow
    Write-Host "如需Codex主导模式，请另行双击“安装Codex（可选）.cmd”。" -ForegroundColor Yellow
    Write-Host "本部署过程不会打开Boss、调用模型、填写草稿或发送消息。" -ForegroundColor Yellow
    Write-Host "安装日志：$logPath"
} catch {
    Write-Host "`n部署失败：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host "安装日志：$logPath" -ForegroundColor Yellow
    exit 1
} finally {
    try { Stop-Transcript | Out-Null } catch {}
}
