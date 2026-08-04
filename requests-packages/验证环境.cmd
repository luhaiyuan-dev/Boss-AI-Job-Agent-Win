@echo off
chcp 65001 >nul
set "BUNDLE_DIR=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%BUNDLE_DIR%scripts\Verify-Environment.ps1"
set "EXIT_CODE=%ERRORLEVEL%"
echo.
pause
exit /b %EXIT_CODE%
