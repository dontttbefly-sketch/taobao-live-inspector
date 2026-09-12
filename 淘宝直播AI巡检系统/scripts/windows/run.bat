@echo off
chcp 65001 >nul
title 淘宝直播AI巡检 - 一键运行
cd /d "%~dp0\..\.."
if errorlevel 1 exit /b 1

set "PATH=%PATH%;%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%LOCALAPPDATA%\Programs\Python\Launcher;%ProgramFiles%\Python312;%ProgramFiles%\nodejs;%APPDATA%\npm;%ProgramFiles%\Gyan\FFmpeg\bin;C:\ffmpeg\bin"

where python >nul 2>&1
if errorlevel 1 (
    echo 未找到 Python，正在安装 Python 3.12...
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
)

where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo 未找到 ffmpeg，正在安装...
    winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
)

where node >nul 2>&1
if errorlevel 1 (
    echo 未找到 Node.js，正在安装...
    winget install -e --id OpenJS.NodeJS.LTS --accept-source-agreements --accept-package-agreements
)

where lark-cli >nul 2>&1
if errorlevel 1 (
    echo 正在安装飞书 CLI...
    npm install -g @larksuite/cli
)

echo 正在启动淘宝直播 AI 巡检...
python scripts\windows\run.py
if errorlevel 1 (
    py -3 scripts\windows\run.py
)
if errorlevel 1 (
    echo 启动失败。请确认已安装 Python 3.12 / ffmpeg / Node.js，并重新运行 运行.bat
    pause
    exit /b 1
)
pause
