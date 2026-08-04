@echo off
chcp 65001 >nul
set "PROJECT_DIR=%~dp0.."
set "PYTHONW=%PROJECT_DIR%\.venv\Scripts\pythonw.exe"
if not exist "%PYTHONW%" (
  echo 未找到项目虚拟环境。请先双击 requests-packages\一键部署.cmd。
  pause
  exit /b 1
)
cd /d "%PROJECT_DIR%"
start "Boss 求职助手" "%PYTHONW%" "%PROJECT_DIR%\run_control_panel.py"
