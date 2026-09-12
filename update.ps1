# ============================================================
# update.ps1 — Windows 云服务器自动更新脚本(成熟版)
# 借鉴参考项目(飞书webhook机器人分发)的成熟架构:
#   git 对比 → staging 预验证 → 停止任务 → 覆盖正式目录 → 重启任务
# 技术栈无关:验证命令在 app.config 里配置(Python/Node/Java 均可)
#
# 配合 Windows 任务计划程序,以 SYSTEM 账号每 5~10 分钟运行一次
# ============================================================

[CmdletBinding()]
param(
  [string]$ProjectDir = "",
  [string]$Branch = "",
  [string]$ConfigFile = ""
)

$ErrorActionPreference = "Stop"

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

$repoDir       = if ($ProjectDir) { $ProjectDir } else { Get-ConfigValue "REPO_DIR" "C:\Services\app" }
$branch        = if ($Branch)     { $Branch }     else { Get-ConfigValue "BRANCH" "main" }
$remoteName    = Get-ConfigValue "REMOTE_NAME" "origin"
$gitPath       = Get-ConfigValue "GIT_PATH" "C:\Program Files\Git\cmd\git.exe"
$sshWrapper    = Get-ConfigValue "SSH_WRAPPER" ""
# 验证命令:在 staging 目录执行的"依赖安装 + 测试",按技术栈填,留空则跳过验证
$installCmd    = Get-ConfigValue "INSTALL_CMD" ""   # 例如: npm ci  或  pip install -r requirements.txt
$testCmd       = Get-ConfigValue "TEST_CMD" ""      # 例如: npm test 或  python -m pytest
# 需要停止/重启的计划任务名(逗号分隔,参考项目里是 FeishuRulesSyncTask 等)
$taskNamesRaw  = Get-ConfigValue "TASK_NAMES" ""
$logFile       = Get-ConfigValue "LOG_FILE" (Join-Path $repoDir "logs\deploy.log")
$timeoutSec    = [int](Get-ConfigValue "COMMAND_TIMEOUT_SECONDS" "120")

$taskNames = @($taskNamesRaw -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })

# ---------- 3. 日志 ----------
$logDir = Split-Path $logFile -Parent
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

function Write-DeployLog($msg, $level = "INFO") {
    $line = "{0} [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $level, $msg
    try { Add-Content -Path $logFile -Value $line -Encoding UTF8 } catch { }
    Write-Host $line
}

# ---------- 4. 命令执行(带超时,超时杀进程树)----------
function Invoke-LoggedCommand {
    param([string]$FilePath, [string[]]$Arguments, [string]$WorkingDirectory)

    Write-DeployLog ("RUN {0} {1} (cwd={2})" -f $FilePath, ($Arguments -join " "), $WorkingDirectory)

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $psi.WorkingDirectory = $WorkingDirectory
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.Arguments = (($Arguments | ForEach-Object { if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ } }) -join " ")

    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi

    try {
        $null = $proc.Start()
        $stdoutTask = $proc.StandardOutput.ReadToEndAsync()
        $stderrTask = $proc.StandardError.ReadToEndAsync()

        if (-not $proc.WaitForExit($timeoutSec * 1000)) {
            Write-DeployLog "命令超时(${timeoutSec}s),终止进程树: $FilePath" "ERROR"
            $taskkill = Join-Path $env:WINDIR "System32\taskkill.exe"
            & $taskkill /PID $proc.Id /T /F 2>$null | Out-Null
            $null = $proc.WaitForExit(10000)
            throw "Command timed out: $FilePath $($Arguments -join ' ')"
        }

        $exitCode = $proc.ExitCode
        $stdout = if ($stdoutTask) { $stdoutTask.Result } else { "" }
        $stderr = if ($stderrTask) { $stderrTask.Result } else { "" }

        foreach ($l in @($stdout -split "`r?`n" | Where-Object { $_ })) { Write-DeployLog ("  " + $l) }
        foreach ($l in @($stderr -split "`r?`n" | Where-Object { $_ })) { Write-DeployLog ("  stderr: " + $l) }

        if ($exitCode -ne 0) {
            throw "命令失败(exit=$exitCode): $FilePath $($Arguments -join ' ')"
        }
    } finally {
        if ($proc) { $proc.Dispose() }
    }
}

# ---------- 5. 工具定位 ----------
function Resolve-Executable {
    param([string]$PreferredPath, [string[]]$Names)
    if ($PreferredPath -and (Test-Path -LiteralPath $PreferredPath)) { return $PreferredPath }
    foreach ($n in $Names) {
        $cmd = Get-Command $n -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    throw "找不到可执行文件: $PreferredPath 或 $($Names -join ', ')"
}

# 解析命令字符串为 [程序, 参数数组],支持引号包裹的路径(如 "C:\Program Files\nodejs\npm.cmd" ci)
function Split-CommandLine {
    param([string]$CommandLine)
    $parts = @()
    $current = ""
    $inQuote = $false
    foreach ($ch in $CommandLine.ToCharArray()) {
        if ($ch -eq '"') { $inQuote = -not $inQuote; continue }
        if ($ch -eq ' ' -and -not $inQuote) {
            if ($current) { $parts += $current; $current = "" }
            continue
        }
        $current += $ch
    }
    if ($current) { $parts += $current }
    if ($parts.Count -eq 0) { return @("", @()) }
    if ($parts.Count -eq 1) { return @($parts[0], @()) }
    $args = @()
    for ($i = 1; $i -lt $parts.Count; $i++) { $args += $parts[$i] }
    return @($parts[0], $args)
}

$gitExe = Resolve-Executable $gitPath @("git.exe", "git")

# PowerShell 5.1 + ErrorActionPreference Stop 会把 git 写到 stderr 的
# "From gitee.com..." 进度行当成终止错误。只在 git 调用期间放宽。
function Invoke-Git {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [switch]$PassThru
    )
    $saved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        if ($PassThru) {
            return & $gitExe @Arguments
        }
        & $gitExe @Arguments 2>&1 | Out-Null
        return [int]$LASTEXITCODE
    } finally {
        $ErrorActionPreference = $saved
    }
}

# 若配置了 SSH wrapper,设置 GIT_SSH(SYSTEM 账号凭据问题的关键)
if ($sshWrapper -and (Test-Path $sshWrapper)) {
    $env:GIT_SSH = $sshWrapper
    Write-DeployLog "GIT_SSH = $sshWrapper"
} elseif ($sshWrapper) {
    Write-DeployLog "SSH_WRAPPER 路径不存在,跳过: $sshWrapper" "WARN"
}

# ---------- 6. 并发锁(防止上一轮没跑完,新一轮又启动)----------
$lockPath = Join-Path $env:TEMP "auto-update-deploy.lock"
$lockStream = $null
try {
    $lockStream = [System.IO.File]::Open($lockPath, [System.IO.FileMode]::OpenOrCreate,
        [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
} catch {
    Write-DeployLog "上一轮部署仍在运行,本次跳过。" "WARN"
    exit 0
}

try {
    # ---------- 7. 前置检查 ----------
    if (-not (Test-Path (Join-Path $repoDir ".git"))) {
        Write-DeployLog "[ERROR] 不是 git 仓库: $repoDir (请先 git clone,或检查 REPO_DIR)" "ERROR"
        exit 1
    }

    # 解析远程真实 URL(ls-remote / clone 必须用 URL,不依赖 remote 名称)
    $remoteUrl = ""
    try {
        $remoteUrl = ((Invoke-Git -PassThru -Arguments @("-C", $repoDir, "remote", "get-url", $remoteName) | Select-Object -First 1).ToString().Trim())
    } catch { }
    if (-not $remoteUrl) {
        Write-DeployLog "无法从仓库解析远程地址(remote: $remoteName),请检查 REMOTE_NAME 配置。" "ERROR"
        exit 1
    }
    Write-DeployLog "远程仓库: $remoteUrl"

    # ---------- 8. 对比本地与远端 commit ----------
    Write-DeployLog "检查更新开始。"
    $localCommit = ""
    try {
        $localCommit = ((Invoke-Git -PassThru -Arguments @("-C", $repoDir, "rev-parse", "HEAD") | Select-Object -First 1).ToString().Trim())
    } catch {
        Write-DeployLog "读取本地 commit 失败,继续执行(会重新对齐远端): $($_.Exception.Message)" "WARN"
    }
    Write-DeployLog "本地 commit: $localCommit"

    # 用 ls-remote 直接查远端,不依赖本地 fetch(参考项目做法)
    $remoteCommit = ""
    try {
        $remoteCommit = ((Invoke-Git -PassThru -Arguments @("ls-remote", $remoteUrl, "refs/heads/$branch") | Select-Object -First 1).ToString().Trim())
        if ($remoteCommit -match "^\s*$") { throw "ls-remote 返回空" }
        $remoteCommit = ($remoteCommit -split "\s+")[0]
    } catch {
        Write-DeployLog "查询远端 commit 失败: $($_.Exception.Message)" "ERROR"
        Write-DeployLog "提示:若是 SSH 仓库,检查 gitee-ssh.cmd 和部署 key 是否配置正确。" "ERROR"
        exit 1
    }
    Write-DeployLog "远端 commit: $remoteCommit"

    if ($localCommit -eq $remoteCommit) {
        Write-DeployLog "没有新版本,无需更新。"
        exit 0
    }
    Write-DeployLog "发现新版本,开始部署流程。"

    # ---------- 9. staging 预验证(关键:坏代码不碰正式目录)----------
    $stagingDir = Join-Path (Split-Path $repoDir -Parent) "_staging_$([System.IO.Path]::GetFileName($repoDir))"
    $needStagingClone = -not (Test-Path (Join-Path $stagingDir ".git"))

    Write-DeployLog "staging 验证阶段开始 (目录: $stagingDir)"

    if ($needStagingClone) {
        # 首次:直接 clone(用真实 URL)
        if ((Invoke-Git -Arguments @("clone", "-b", $branch, $remoteUrl, $stagingDir)) -ne 0) { throw "staging clone 失败" }
        Write-DeployLog "staging 目录已 clone。"
    } else {
        Invoke-Git -Arguments @("-C", $stagingDir, "fetch", $remoteUrl, $branch) | Out-Null
        Invoke-Git -Arguments @("-C", $stagingDir, "checkout", "-B", $branch, "FETCH_HEAD") | Out-Null
        Invoke-Git -Arguments @("-C", $stagingDir, "reset", "--hard", "FETCH_HEAD") | Out-Null
        Write-DeployLog "staging 目录已同步到最新。"
    }

    # 在 staging 跑验证命令(依赖安装 + 测试)
    if ($installCmd) {
        Write-DeployLog "staging 安装依赖: $installCmd"
        $ic = Split-CommandLine $installCmd
        Invoke-LoggedCommand (Resolve-Executable $ic[0] @($ic[0])) @($ic[1]) $stagingDir
    }
    if ($testCmd) {
        Write-DeployLog "staging 运行测试: $testCmd"
        $tc = Split-CommandLine $testCmd
        Invoke-LoggedCommand (Resolve-Executable $tc[0] @($tc[0])) @($tc[1]) $stagingDir
    }

    Write-DeployLog "staging 验证通过,开始应用到正式目录。"
    $previousCommit = $localCommit

    # ---------- 10. 停止业务任务 ----------
    foreach ($t in $taskNames) {
        $task = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
        if ($task) {
            Write-DeployLog "停止计划任务: $t"
            Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 2
        } else {
            Write-DeployLog "计划任务不存在,跳过: $t" "WARN"
        }
    }

    # ---------- 11. 覆盖正式目录(带回滚保护)----------
    try {
        if ((Invoke-Git -Arguments @("-C", $repoDir, "fetch", $remoteUrl, $branch)) -ne 0) { throw "git fetch 失败" }
        Invoke-Git -Arguments @("-C", $repoDir, "checkout", "-B", $branch, "FETCH_HEAD") | Out-Null
        if ((Invoke-Git -Arguments @("-C", $repoDir, "reset", "--hard", "FETCH_HEAD")) -ne 0) { throw "git reset 失败" }
        Write-DeployLog "正式目录已覆盖为新版本。"

        # 正式目录也跑一次依赖安装(必要,因为 node_modules 等不提交)
        if ($installCmd) {
            Write-DeployLog "正式目录安装依赖: $installCmd"
            $ic = Split-CommandLine $installCmd
            Invoke-LoggedCommand (Resolve-Executable $ic[0] @($ic[0])) @($ic[1]) $repoDir
        }
        # 正式目录测试(可选,参考项目会跑,保证运行目录无异常)
        if ($testCmd) {
            Write-DeployLog "正式目录运行测试: $testCmd"
            $tc = Split-CommandLine $testCmd
            Invoke-LoggedCommand (Resolve-Executable $tc[0] @($tc[0])) @($tc[1]) $repoDir
        }
    } catch {
        Write-DeployLog "正式目录更新失败,尝试回滚到旧版本: $previousCommit" "ERROR"
        if ($previousCommit) {
            try {
                Invoke-Git -Arguments @("-C", $repoDir, "reset", "--hard", $previousCommit) | Out-Null
                Write-DeployLog "已回滚到 $previousCommit"
            } catch {
                Write-DeployLog "回滚也失败,请人工检查!" "ERROR"
            }
        }
        throw
    }

    # ---------- 12. 重启业务任务 ----------
    foreach ($t in $taskNames) {
        $task = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
        if ($task) {
            Write-DeployLog "启动计划任务: $t"
            Start-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 2
        }
    }

    Write-DeployLog "部署完成: $remoteCommit"
} catch {
    Write-DeployLog "部署失败: $($_.Exception.Message)" "ERROR"
    # 失败时也尽量把任务拉起来,避免服务长时间停止
    foreach ($t in $taskNames) {
        Start-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
    }
    exit 1
} finally {
    if ($lockStream) { $lockStream.Dispose() }
}
