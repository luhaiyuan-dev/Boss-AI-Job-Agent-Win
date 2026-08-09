#requires -Version 7.0

[CmdletBinding()]
param(
    [string]$PackageRoot = "",
    [switch]$NoCompletionPopup
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
if ($env:BOSS_DELETE_NO_POPUP -eq "1") {
    $NoCompletionPopup = $true
}

function Show-Result {
    param(
        [Parameter(Mandatory = $true)][string]$Message,
        [int]$Icon = 64
    )

    Write-Host $Message
    if ($NoCompletionPopup) { return }
    try {
        $shell = New-Object -ComObject WScript.Shell
        $null = $shell.Popup($Message, 0, "Boss 求职助手部署包清理", $Icon)
    } catch {
        # 控制台输出已经保留结果；弹窗不可用不影响删除结论。
    }
}

function Get-PackageEntries {
    param([Parameter(Mandatory = $true)][string]$Root)

    $files = [Collections.Generic.List[string]]::new()
    $directories = [Collections.Generic.List[string]]::new()
    $reparsePoints = [Collections.Generic.List[string]]::new()
    $pending = [Collections.Generic.Stack[string]]::new()
    $pending.Push($Root)

    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        foreach ($entry in [IO.Directory]::EnumerateFileSystemEntries($directory)) {
            $attributes = [IO.File]::GetAttributes($entry)
            if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                $reparsePoints.Add($entry)
                continue
            }
            if (($attributes -band [IO.FileAttributes]::Directory) -ne 0) {
                $directories.Add($entry)
                $pending.Push($entry)
            } else {
                $files.Add($entry)
            }
        }
    }

    [PSCustomObject]@{
        Files = $files
        Directories = $directories
        ReparsePoints = $reparsePoints
    }
}

function Remove-FileWithOverwrite {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]
        [Security.Cryptography.RandomNumberGenerator]$Random
    )

    $item = Get-Item -LiteralPath $Path -Force
    $length = $item.Length
    [IO.File]::SetAttributes($Path, [IO.FileAttributes]::Normal)

    if ($length -gt 0) {
        $bufferSize = 1MB
        $buffer = [byte[]]::new([Math]::Min($bufferSize, $length))
        $stream = [IO.FileStream]::new(
            $Path,
            [IO.FileMode]::Open,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None,
            $bufferSize,
            [IO.FileOptions]::WriteThrough
        )
        try {
            $remaining = $length
            while ($remaining -gt 0) {
                $count = [int][Math]::Min($buffer.Length, $remaining)
                $Random.GetBytes($buffer, 0, $count)
                $stream.Write($buffer, 0, $count)
                $remaining -= $count
            }
            $stream.Flush($true)
        } finally {
            $stream.Dispose()
        }
    }

    $temporaryName = ".wipe-$([Guid]::NewGuid().ToString('N'))"
    $temporaryPath = Join-Path ([IO.Path]::GetDirectoryName($Path)) $temporaryName
    Move-Item -LiteralPath $Path -Destination $temporaryPath -Force
    Remove-Item -LiteralPath $temporaryPath -Force
}

try {
    if (-not $PackageRoot) {
        $PackageRoot = Split-Path -Parent $PSScriptRoot
    }
    $root = [IO.Path]::GetFullPath($PackageRoot).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    $driveRoot = [IO.Path]::GetPathRoot($root).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    if ($root -eq $driveRoot -or [IO.Path]::GetFileName($root) -ne "requests-packages") {
        throw "安全检查失败：只允许删除名为 requests-packages 的非磁盘根目录。"
    }
    if (-not (Test-Path -LiteralPath $root -PathType Container)) {
        throw "部署包目录不存在。"
    }

    $parent = [IO.Directory]::GetParent($root).FullName
    $sourceMarkers = @(
        (Join-Path $parent ".git"),
        (Join-Path $parent "boss_assistant"),
        (Join-Path $parent "run_control_panel.py")
    )
    if ($sourceMarkers | Where-Object { Test-Path -LiteralPath $_ }) {
        throw "检测到源代码开发目录，已拒绝删除。请只在新电脑的无源码分发目录中使用此文件。"
    }

    $requiredMarkers = @(
        (Join-Path $root "一键部署.cmd"),
        (Join-Path $root "manifest.json"),
        (Join-Path $root "scripts\Setup.ps1"),
        (Join-Path $root "scripts\Remove-DeploymentPackage.ps1")
    )
    foreach ($marker in $requiredMarkers) {
        if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) {
            throw "安全检查失败：目录不是完整的 Boss 求职助手部署包。"
        }
    }

    $entries = Get-PackageEntries -Root $root
    if ($entries.ReparsePoints.Count -gt 0) {
        throw "安全检查失败：部署包内存在链接或重解析点，已拒绝跟随或删除。"
    }

    $scriptPath = [IO.Path]::GetFullPath($PSCommandPath)
    $launcherPath = [IO.Path]::GetFullPath((Join-Path $root "delete.cmd"))
    $controlPaths = @($launcherPath, $scriptPath)
    $payloadFiles = @(
        $entries.Files | Where-Object {
            $candidate = [IO.Path]::GetFullPath($_)
            -not ($controlPaths | Where-Object {
                $_.Equals($candidate, [StringComparison]::OrdinalIgnoreCase)
            })
        }
    )
    $totalBytes = ($entries.Files | ForEach-Object {
        (Get-Item -LiteralPath $_ -Force).Length
    } | Measure-Object -Sum).Sum

    Set-Location ([IO.Path]::GetTempPath())
    Start-Sleep -Milliseconds 1200
    Write-Host "正在永久删除 requests-packages：$($entries.Files.Count) 个文件，$totalBytes 字节。" -ForegroundColor Yellow
    Write-Host "此操作绕过回收站且无法撤销，请勿关闭窗口或关机。" -ForegroundColor Yellow

    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $index = 0
        foreach ($file in $payloadFiles) {
            $index += 1
            $percent = if ($payloadFiles.Count) {
                [Math]::Floor($index * 100 / $payloadFiles.Count)
            } else { 100 }
            Write-Progress -Activity "覆盖并删除部署包" -Status "$index / $($payloadFiles.Count)" -PercentComplete $percent
            Remove-FileWithOverwrite -Path $file -Random $random
        }
        Write-Progress -Activity "覆盖并删除部署包" -Completed

        # 启动入口与当前脚本最后处理。此时若前面的任意文件失败，控制文件仍在，
        # 用户可以修正占用问题后重新双击，而不会得到虚假的成功提示。
        foreach ($controlFile in $controlPaths) {
            if (Test-Path -LiteralPath $controlFile -PathType Leaf) {
                Remove-FileWithOverwrite -Path $controlFile -Random $random
            }
        }
    } finally {
        $random.Dispose()
    }

    foreach ($directory in @($entries.Directories | Sort-Object Length -Descending)) {
        if (Test-Path -LiteralPath $directory -PathType Container) {
            [IO.Directory]::Delete($directory, $false)
        }
    }
    [IO.Directory]::Delete($root, $false)

    Show-Result -Message "requests-packages 已永久删除，未进入回收站。已安装的软件、config、resume_inbox 和两个 EXE 均已保留。"
    exit 0
} catch {
    Show-Result -Message "部署包删除未完成：$($_.Exception.Message)" -Icon 16
    exit 1
}
