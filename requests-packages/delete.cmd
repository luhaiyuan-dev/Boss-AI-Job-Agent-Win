@echo off
chcp 65001 >nul
setlocal
set "BUNDLE_DIR=%~dp0"
set "PWSH_EXE="
for /f "delims=" %%I in ('where pwsh.exe 2^>nul') do if not defined PWSH_EXE set "PWSH_EXE=%%I"
if not defined PWSH_EXE set "PWSH_EXE=%~d0\BossJobAssistant\PowerShell\7\pwsh.exe"
if not exist "%PWSH_EXE%" set "PWSH_EXE=%ProgramFiles%\PowerShell\7\pwsh.exe"
if not exist "%PWSH_EXE%" (
  echo 未找到 PowerShell 7，无法安全删除部署包。
  echo 请确认“一键部署.cmd”已经成功执行。
  pause
  exit /b 1
)
start "" /b "%PWSH_EXE%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%BUNDLE_DIR%scripts\Remove-DeploymentPackage.ps1"
if errorlevel 1 (
  echo 无法启动部署包删除程序。
  pause
  exit /b 1
)
exit /b 0
