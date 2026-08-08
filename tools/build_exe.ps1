# 打包 Boss 求职助手为两个独立 exe：
#   Boss登录浏览器.exe —— 启动登录专用 Edge（无控制台窗口，对应 tools\open_login_edge.py）
#   Boss求职助手.exe   —— 启动 Tkinter 控制台（无控制台窗口，对应 run_control_panel.py）
#
# 产物输出到项目根目录，双击即用；python run_control_panel.py 等命令行启动方式不受影响。
# 使用前请确认已安装 PyInstaller：python -m pip install pyinstaller
# 用法：powershell -ExecutionPolicy Bypass -File tools\build_exe.ps1

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$BuildDir = Join-Path $ProjectRoot "build"
$SpecDir = Join-Path $BuildDir "spec"
$WorkDir = Join-Path $BuildDir "work"
New-Item -ItemType Directory -Force $SpecDir, $WorkDir | Out-Null

# 每次都从受版本控制的透明 PNG 源图重建 ICO，避免源图更新后继续打包旧图标。
Write-Host "`n===== 生成应用图标 ====="
& python tools\make_icons.py
if ($LASTEXITCODE -ne 0) { throw "图标生成失败：python tools\make_icons.py" }

function Invoke-PyInstaller {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Entry,
        [string]$Icon,
        [switch]$Windowed
    )
    $arguments = @(
        "--onefile", "--clean", "--noconfirm",
        "--name", $Name,
        "--distpath", $ProjectRoot,
        "--specpath", $SpecDir,
        "--workpath", $WorkDir,
        # 函数内延迟导入的依赖，显式声明保证被收集。
        "--hidden-import=mysql.connector",
        "--hidden-import=pypdf",
        # OpenCC 含简体转换数据文件，整体收集避免运行期缺数据。
        "--collect-all", "opencc"
    )
    if ($Icon) {
        # 指定了 --specpath 后，相对路径按 spec 目录解析；必须用绝对路径。
        $arguments += "--icon"; $arguments += (Join-Path $ProjectRoot $Icon)
    }
    if ($Windowed) {
        # 无控制台窗口：双击 exe 不弹终端。
        $arguments += "--windowed"
        # GUI 窗口图标数据文件（frozen 下从 _MEIPASS/icons 读取）。
        $arguments += "--add-data"
        $arguments += "$(Join-Path $ProjectRoot 'assets\icons\boss_assistant.ico');icons"
    }
    $arguments += $Entry
    # 先删除旧产物再写入：全新文件会强制 Explorer 重新提取图标，
    # 避免同路径覆盖时图标缓存顽固显示旧图标。
    $output = Join-Path $ProjectRoot "$Name.exe"
    if (Test-Path $output) {
        Remove-Item $output -Force
    }
    Write-Host "`n===== 构建 $Name ====="
    & python -m PyInstaller @arguments
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败：$Name" }
}

Invoke-PyInstaller -Name "Boss登录浏览器" -Entry "tools\open_login_edge.py" -Windowed -Icon "assets\icons\boss_login.ico"
Invoke-PyInstaller -Name "Boss求职助手" -Entry "run_control_panel.py" -Windowed -Icon "assets\icons\boss_assistant.ico"

Write-Host "`n构建完成，产物："
Get-Item "Boss登录浏览器.exe", "Boss求职助手.exe" | Select-Object Name, @{N="大小MB"; E={[math]::Round($_.Length / 1MB, 1)}}, LastWriteTime
