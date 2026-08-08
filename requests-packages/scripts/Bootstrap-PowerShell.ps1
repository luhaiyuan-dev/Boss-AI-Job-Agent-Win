[CmdletBinding()]
param(
    [switch]$NoElevation
)

$ErrorActionPreference = "Stop"
$bundleRoot = Split-Path -Parent $PSScriptRoot
$archivePath = Join-Path $bundleRoot "installers\PowerShell-7.6.4-win-x64.zip"
$expectedHash = "80832551C52809301E6071C8BAC977BEB5A2F1EC953EB4DB9F94DEB953333793"
$setupPath = Join-Path $PSScriptRoot "Setup.ps1"

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Find-PowerShell7 {
    $command = Get-Command pwsh.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command -and (Test-Path -LiteralPath $command.Source -PathType Leaf)) {
        return $command.Source
    }
    $installedPath = Join-Path $env:ProgramFiles "PowerShell\7\pwsh.exe"
    if (Test-Path -LiteralPath $installedPath -PathType Leaf) {
        return $installedPath
    }
    return $null
}

function Test-PowerShell7([string]$Path) {
    if (-not $Path) { return $false }
    $major = & $Path -NoLogo -NoProfile -Command '$PSVersionTable.PSVersion.Major' 2>$null
    return ($LASTEXITCODE -eq 0 -and [int]($major | Select-Object -Last 1) -ge 7)
}

try {
    $pwsh = Find-PowerShell7
    if (-not (Test-PowerShell7 $pwsh)) {
        if (-not $NoElevation -and -not (Test-Administrator)) {
            $elevated = Start-Process `
                -FilePath "powershell.exe" `
                -Verb RunAs `
                -Wait `
                -PassThru `
                -ArgumentList @(
                    "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    ('"{0}"' -f $PSCommandPath), "-NoElevation"
                )
            exit $elevated.ExitCode
        }

        if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) {
            throw "PowerShell 7 offline archive is missing: $archivePath"
        }
        $actualHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash
        if ($actualHash -ne $expectedHash) {
            throw "PowerShell 7 archive SHA-256 verification failed."
        }

        Write-Host "PowerShell 7 was not found. Installing the bundled x64 LTS archive..." -ForegroundColor Cyan
        $powerShellParent = Join-Path $env:ProgramFiles "PowerShell"
        $installRoot = Join-Path $powerShellParent "7"
        $stagingRoot = Join-Path $powerShellParent "7.installing"
        if (Test-Path -LiteralPath $installRoot) {
            throw "PowerShell 7 install directory already exists but pwsh.exe is unavailable: $installRoot"
        }
        if (Test-Path -LiteralPath $stagingRoot) {
            [IO.Directory]::Delete($stagingRoot, $true)
        }
        New-Item -ItemType Directory -Path $stagingRoot -Force | Out-Null
        try {
            Expand-Archive -LiteralPath $archivePath -DestinationPath $stagingRoot -Force
            $stagedPwsh = Join-Path $stagingRoot "pwsh.exe"
            if (-not (Test-PowerShell7 $stagedPwsh)) {
                throw "Extracted PowerShell 7 could not be started."
            }
            Move-Item -LiteralPath $stagingRoot -Destination $installRoot
        } catch {
            if (Test-Path -LiteralPath $stagingRoot) {
                [IO.Directory]::Delete($stagingRoot, $true)
            }
            throw
        }

        $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
        $machineParts = @($machinePath -split ";" | Where-Object { $_ })
        if ($machineParts -notcontains $installRoot) {
            [Environment]::SetEnvironmentVariable(
                "Path", (@($machineParts) + $installRoot) -join ";", "Machine"
            )
        }
        if (($env:Path -split ";") -notcontains $installRoot) {
            $env:Path = $installRoot + ";" + $env:Path
        }
        $pwsh = Find-PowerShell7
        if (-not (Test-PowerShell7 $pwsh)) {
            throw "PowerShell 7 installation completed, but pwsh.exe could not be verified."
        }
    }

    $version = & $pwsh -NoLogo -NoProfile -Command '$PSVersionTable.PSVersion.ToString()'
    Write-Host "PowerShell $version is ready. Starting deployment..." -ForegroundColor Green
    $setupArguments = @(
        "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $setupPath
    )
    if (Test-Administrator) { $setupArguments += "-NoElevation" }
    & $pwsh @setupArguments
    exit $LASTEXITCODE
} catch {
    Write-Host "PowerShell bootstrap failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
