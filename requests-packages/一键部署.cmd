@echo off
chcp 65001 >nul
set "BUNDLE_DIR=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%BUNDLE_DIR%scripts\Setup.ps1"
set "EXIT_CODE=%ERRORLEVEL%"
echo.
if not "%EXIT_CODE%"=="0" echo 部署未完成，请查看 requests-packages\logs 中最新日志。
pause
exit /b %EXIT_CODE%
