# ============================================================
# check-lark-auth.ps1 — 飞书授权到期提醒脚本
#
# 为什么需要它:
#   lark-cli 的 user 授权(refresh token)约 7 天过期,过期后
#   多维表格/妙记等 --as user 操作会静默失败。
#   本脚本每天检查一次剩余天数,快到期时用 **bot 身份**发飞书提醒
#   (bot 不依赖 user 授权,所以授权过期也能发得出提醒,不会死循环)。
#
# 配合 Windows 任务计划程序每天运行一次(建议早上 9 点)。
# ============================================================

[CmdletBinding()]
param(
  [string]$ConfigFile = "",
  [string]$UserId = "",
  [string]$ChatId = "",
  [int]$WarnDays = 0
)

$ErrorActionPreference = "Stop"

# ---------- 7. 发送提醒(用 bot 身份,不依赖 user 授权)----------
# 定义在前:脚本主体会调用它
# 支持两种接收方式:优先私聊用户(LARK_WARN_USER_ID),备选发群(LARK_WARN_CHAT_ID)
function Send-Reminder {
    param([string]$Message)
    try {
        if ($UserId) {
            $result = & lark-cli im +messages-send --as bot --user-id $UserId --text $Message 2>&1 | Out-String
        } elseif ($ChatId) {
            $result = & lark-cli im +messages-send --as bot --chat-id $ChatId --text $Message 2>&1 | Out-String
        } else {
            Write-Log "未配置 LARK_WARN_USER_ID / LARK_WARN_CHAT_ID,无法发送提醒" "WARN"
            return
        }
        if ($LASTEXITCODE -ne 0) {
            Write-Log "提醒发送失败: $result" "ERROR"
        } else {
            Write-Log "提醒已发送: $Message" "INFO"
        }
    } catch {
        Write-Log "提醒发送异常: $($_.Exception.Message)" "ERROR"
    }
}

# ---------- 1. 定位配置文件 ----------
if (-not $ConfigFile) { $ConfigFile = Join-Path $PSScriptRoot "app.config" }
if (-not (Test-Path $ConfigFile)) {
    Write-Host "[ERROR] 找不到配置文件: $ConfigFile"
    exit 1
}

# ---------- 2. 读取配置 ----------
$config = @{}
Get-Content $ConfigFile | Where-Object {
    $_ -match "^\s*[A-Z_]+\s*=" -and $_ -notmatch "^\s*#"
} | ForEach-Object {
    if ($_ -match "^\s*([A-Z_]+)\s*=\s*(.*?)\s*$") {
        $config[$matches[1]] = $matches[2]
    }
}

function Get-ConfigValue($key, $default) {
    if ($config.ContainsKey($key) -and $config[$key]) { return $config[$key] }
    return $default
}

$repoDir     = Get-ConfigValue "REPO_DIR" "C:\Services\app"
$logFile     = Get-ConfigValue "LOG_FILE" (Join-Path $repoDir "logs\deploy.log")
$sshWrapper  = Get-ConfigValue "SSH_WRAPPER" ""
$logDir      = Split-Path $logFile -Parent

# 提醒配置:优先私聊用户,备选发群
if (-not $UserId) { $UserId = Get-ConfigValue "LARK_WARN_USER_ID" "" }
if (-not $ChatId) { $ChatId = Get-ConfigValue "LARK_WARN_CHAT_ID" "" }
if ($WarnDays -le 0) { $WarnDays = [int](Get-ConfigValue "LARK_WARN_DAYS" "3") }

# ---------- 3. 日志 ----------
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

function Write-Log($msg, $level = "INFO") {
    $line = "{0} [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $level, $msg
    try { Add-Content -Path $logFile -Value $line -Encoding UTF8 } catch { }
    Write-Host $line
}

# ---------- 4. 前置检查 ----------
if (-not $UserId -and -not $ChatId) {
    Write-Log "未配置 LARK_WARN_USER_ID / LARK_WARN_CHAT_ID,跳过提醒(只检查不提醒)。请检查 app.config" "WARN"
    $UserId = $null
    $ChatId = $null
}

if (-not (Get-Command lark-cli -ErrorAction SilentlyContinue)) {
    Write-Log "找不到 lark-cli 命令,跳过检查(请确认已安装并加入 PATH)" "ERROR"
    exit 1
}

# ---------- 5. 读取授权状态 ----------
Write-Log "检查飞书授权状态..."
$raw = ""
try {
    $raw = (& lark-cli auth status 2>&1 | Out-String)
} catch {
    Write-Log "lark-cli auth status 执行失败: $($_.Exception.Message)" "ERROR"
    exit 1
}

# 解析 JSON:找 refreshExpiresAt / tokenStatus
$expiresAt = ""
$tokenStatus = ""
try {
    $obj = $raw | ConvertFrom-Json
    if ($obj.identities.user) {
        $expiresAt   = [string]$obj.identities.user.refreshExpiresAt
        $tokenStatus = [string]$obj.identities.user.tokenStatus
    }
} catch {
    Write-Log "解析 auth status 输出失败(输出可能非 JSON),原始内容: $($raw.Substring(0, [Math]::Min(200, $raw.Length)))" "ERROR"
    exit 1
}

if (-not $expiresAt) {
    Write-Log "未找到 refreshExpiresAt,可能未登录或格式变化,请手动运行 lark-cli auth status 检查" "ERROR"
    exit 1
}

Write-Log "token 状态: $tokenStatus, refresh 过期时间: $expiresAt"

# ---------- 6. 计算剩余天数 ----------
$expireDate = $null
try {
    $expireDate = [datetime]::Parse($expiresAt)
} catch {
    Write-Log "解析过期时间失败: $expiresAt" "ERROR"
    exit 1
}

$daysLeft = [Math]::Floor(($expireDate - (Get-Date)).TotalDays)
Write-Log "距离 refresh token 过期还有 $daysLeft 天(阈值 $WarnDays 天)"

# 已过期:必须提醒(等级更高)
if ($daysLeft -lt 0) {
    Write-Log "⚠️ 授权已过期!请尽快重新扫码: lark-cli auth login" "ERROR"
    if ($UserId -or $ChatId) {
        Send-Reminder "🚨 飞书授权已过期($daysLeft 天前),请尽快在服务器上重新扫码登录:
  lark-cli auth login"
        Write-Log "已发送过期提醒" "WARN"
    }
    exit 0
}

# 剩余天数 <= 阈值:提醒
if ($daysLeft -le $WarnDays) {
    Write-Log "⚠️ 授权即将到期(剩 $daysLeft 天),发送提醒"
    if ($UserId -or $ChatId) {
        Send-Reminder "⏰ 飞书授权将在 $daysLeft 天后过期($expiresAt 之前),请尽快在服务器上重新扫码登录:
  lark-cli auth login
(扫码后授权自动续期约 7 天)"
        Write-Log "已发送到期提醒" "WARN"
    }
} else {
    Write-Log "授权状态正常,无需提醒"
}

exit 0
