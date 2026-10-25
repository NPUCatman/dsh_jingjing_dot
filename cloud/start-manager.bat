@echo off
setlocal
chcp 65001 >nul
title 小鲸鱼云端管理 - 关闭此窗口将断开 SSH
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo [未启动] 找不到项目 Python 环境，请先运行项目 setup.ps1。
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -u "%~dp0manager.py"
if errorlevel 1 (
  echo.
  echo 启动未完成。请阅读上方提示，修复后再启动。
  pause
  exit /b 1
)
endlocal
