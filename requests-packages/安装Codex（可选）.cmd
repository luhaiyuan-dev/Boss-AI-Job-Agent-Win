@echo off
chcp 65001 >nul
set "BUNDLE_DIR=%~dp0"
pwsh.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%BUNDLE_DIR%scripts\Install-Codex-Optional.ps1"
set "EXIT_CODE=%ERRORLEVEL%"
echo.
if not "%EXIT_CODE%"=="0" echo Codex 可选安装未完成。
pause
exit /b %EXIT_CODE%
