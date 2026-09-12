@echo off
chcp 65001 >nul
title 淘宝直播AI巡检 - 一键运行
cd /d "%~dp0"
if exist "淘宝直播AI巡检系统\scripts\windows\run.bat" (
    cd /d "%~dp0淘宝直播AI巡检系统"
)
if exist "scripts\windows\run.bat" (
    call scripts\windows\run.bat
    exit /b %errorlevel%
)
echo 未找到 scripts\windows\run.bat，请先解压完整迁移包。
pause
exit /b 1
