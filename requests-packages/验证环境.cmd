@echo off
chcp 65001 >nul
set "BUNDLE_DIR=%~dp0"
set "PWSH_EXE="
for /f "delims=" %%I in ('where pwsh.exe 2^>nul') do if not defined PWSH_EXE set "PWSH_EXE=%%I"
if not defined PWSH_EXE set "PWSH_EXE=%~d0\BossJobAssistant\PowerShell\7\pwsh.exe"
if not exist "%PWSH_EXE%" (
  echo 未找到 PowerShell 7，请先双击“一键部署.cmd”。
  pause
  exit /b 1
)
"%PWSH_EXE%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%BUNDLE_DIR%scripts\Verify-Environment.ps1"
set "EXIT_CODE=%ERRORLEVEL%"
echo.
pause
exit /b %EXIT_CODE%
