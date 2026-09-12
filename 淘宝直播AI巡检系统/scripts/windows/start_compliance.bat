@echo off
chcp 65001 >nul
title 淘宝直播极限词监听 - 启动器
cd /d "%~dp0\..\.."
if errorlevel 1 exit /b 1

if not exist ".venv\Scripts\python.exe" (
    echo 启动失败：虚拟环境不存在，请先运行 setup_windows.bat
    exit /b 1
)
.venv\Scripts\python.exe scripts\windows\prepare_runtime.py
if errorlevel 1 (
    echo 启动失败：无法安全准备私有日志目录
    exit /b 1
)

echo 启动淘宝直播极限词监听...
start "淘宝直播极限词监听" /min cmd /c ".venv\Scripts\python.exe -m app.compliance.listener >> data\logs\compliance.log 2>&1"
if errorlevel 1 exit /b 1
echo 已启动，日志：data\logs\compliance.log
echo 监听器由独立锁保护；请通过服务管理器停止对应服务。
