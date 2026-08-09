@echo off
chcp 65001 >nul
set "PROJECT_DIR=%~dp0.."
set "APP_EXE=%PROJECT_DIR%\Boss求职助手.exe"
if not exist "%APP_EXE%" (
  echo 未找到 Boss求职助手.exe。
  echo 请把两个 EXE 复制到 requests-packages 的父目录后重试。
  pause
  exit /b 1
)
start "Boss 求职助手" "%APP_EXE%"
