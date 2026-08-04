@echo off
chcp 65001 >nul
set "PROJECT_DIR=%~dp0.."
set "PYTHON=%PROJECT_DIR%\.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
  echo 未找到项目虚拟环境。请先双击 requests-packages\一键部署.cmd。
  pause
  exit /b 1
)
cd /d "%PROJECT_DIR%"
"%PYTHON%" "%PROJECT_DIR%\tools\open_login_edge.py"
set "EXIT_CODE=%ERRORLEVEL%"
echo.
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
