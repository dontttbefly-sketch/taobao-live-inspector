@echo off
chcp 65001 >nul
cd /d "%~dp0\..\.."
if errorlevel 1 (
    echo   无法进入项目目录
    exit /b 1
)
title 淘宝直播AI巡检系统 - Windows 一键安装
echo ================================================
echo   淘宝直播 AI 巡检系统 Windows 安装脚本
echo ================================================
echo.

REM ---------- 1. 检查 Python ----------
echo [1/6] 检查 Python 3.12...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo   未找到 Python，正在安装 Python 3.12（winget）...
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
    echo   安装完成后请重新打开终端再运行本脚本
    pause
    exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo   检测到 Python %PYVER%

python scripts\windows\prepare_runtime.py
if errorlevel 1 (
    echo   私有日志目录创建或权限验证失败，安装已停止
    exit /b 1
)

REM ---------- 2. 检查 ffmpeg ----------
echo [2/6] 检查 ffmpeg...
ffmpeg -version >nul 2>&1
if %errorlevel% neq 0 (
    echo   未找到 ffmpeg，正在安装（winget）...
    winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
    echo   安装完成后请重新打开终端再运行本脚本
    pause
    exit /b 1
)
echo   ffmpeg OK

REM ---------- 3. 检查 Node.js（飞书推送用）----------
echo [3/6] 检查 Node.js...
node --version >nul 2>&1
if %errorlevel% neq 0 (
    echo   未找到 Node.js，正在安装（winget）...
    winget install -e --id OpenJS.NodeJS.LTS --accept-source-agreements --accept-package-agreements
    echo   安装完成后请重新打开终端再运行本脚本
    pause
    exit /b 1
)
npm install -g @larksuite/cli >nul 2>&1
echo   Node.js + lark-cli OK

REM ---------- 4. 创建虚拟环境并安装依赖 ----------
echo [4/6] 创建虚拟环境并安装依赖（首次约 10-20 分钟，需联网下载约 3GB）...
python -m venv .venv
if %errorlevel% neq 0 (
    echo   虚拟环境创建失败，请检查 Python 是否可正常执行
    pause
    exit /b 1
)
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo   依赖安装失败，请检查网络后重试
    pause
    exit /b 1
)

REM ---------- 5. 飞书授权 ----------
echo [5/6] 检查飞书授权（lark-cli）...
lark-cli auth status >nul 2>&1
if %errorlevel% neq 0 (
    echo   ⚠ 未检测到飞书授权，请执行：lark-cli auth login
    echo   请在这台新主机完成新的授权；不要复制旧主机的授权目录、浏览器资料或 Cookie。
)

REM ---------- 6. 验证配置 ----------
echo [6/6] 验证配置...
python -c "import yaml, json; cfg = yaml.safe_load(open('config.yaml', encoding='utf-8')); print('   配置 OK: liveId=%s, 简报=%s分钟' % (cfg['taobao'].get('live_id','?'), cfg['briefing'].get('interval_sec',0)//60))" 2>nul
if %errorlevel% neq 0 (
    echo   配置读取失败，请确认 config.yaml 存在且格式正确
    pause
    exit /b 1
)

echo.
echo ================================================
echo   安装完成！启动巡检：双击 start_watcher.bat
echo   或运行：start_watcher.bat
echo   查看状态：type data\logs\watcher.log
echo ================================================
pause
