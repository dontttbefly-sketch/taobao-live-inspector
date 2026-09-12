@echo off
chcp 65001 >nul
title 淘宝直播AI巡检 - 启动器
echo 启动淘宝直播 AI 巡检...
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

REM 两个独立入口各自用进程锁保护，禁止按映像名终止其他 Python 工作。
start "淘宝直播AI巡检" /min cmd /c ".venv\Scripts\python.exe -m app.recorder.watcher >> data\logs\watcher.log 2>&1"
if errorlevel 1 exit /b 1
call "%~dp0start_compliance.bat"
if errorlevel 1 exit /b 1
echo 已启动，日志：data\logs\watcher.log
echo 合规监听日志：data\logs\compliance.log
echo 查看状态：type data\logs\watcher.log
echo 停止巡检：通过服务管理器停止对应的 watcher 或 compliance 服务
pause
