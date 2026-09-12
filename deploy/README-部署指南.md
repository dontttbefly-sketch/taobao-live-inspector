# Gitee 自动部署指南(图文式 · 成熟版)

> **一句话理解整个系统:**
> Mac 是你写代码的"工作室",Gitee 是"快递中转站",Windows 云服务器是"自动收货仓库"。
> 你每次 push 新代码,Windows 端定时自动检查 → 发现新版 → **先在草稿区验证(测试通过才敢换)** → 停服务 → 覆盖源码 → 重启服务。

```
Mac(写代码)  ──上传──▶  Gitee(中转站)  ──自动拉取──▶  Windows 云服务器(跑程序)
  改代码               私有仓库                每 1 分钟自动检查
  git push                  │                  发现新版 → staging验证 → 停服 → 覆盖 → 重启
                            └── SSH 部署 key(只读) ──▶ Windows 免密访问
```

> 本方案借鉴了你已有的「飞书webhook机器人分发」项目(Mac→Gitee→Windows 自动部署),它已经跑通了全流程。
> 四样关键经验直接继承:① **staging 预验证**(坏代码不碰正式目录)② **失败回滚** ③ **SYSTEM 账号运行计划任务**(不用 Windows 登录密码)④ **SSH 部署 key 免密**。

---

# ⭐ 本项目(淘宝直播AI巡检系统)专属说明

**部署前必读,它和通用流程有 4 处不同:**

| 项目 | 本项目情况 |
|---|---|
| 技术栈 | Python 3.12,**无 Web 服务**,两个常驻进程:`watcher`(主巡检)+ `compliance`(合规旁路) |
| 首次安装 | 项目自带 `setup_windows.bat`,会自动装 Python/ffmpeg/Node + 3GB 依赖(约 10-20 分钟),**不需要手动逐个装** |
| config.yaml | ⚠️ **含真实 cookie/API key,已从 git 排除**!Windows 端从 `config.example.yaml` 复制后手动填 |
| 业务任务 | 注册**两个**计划任务 `TaobaoWatcherTask` + `TaobaoComplianceTask`(不是 1 个) |

> 通用流程(第一部分/第二部分)照做,但 Windows 端请按**下方"第三部分"的实际命令**操作,那里已经按本项目写好了。

---

# 第一部分:Mac 端操作(3 步)

## 第 1 步:在 Gitee 创建私有仓库

> 💡 **先想清楚:仓库建在谁账号下?(关键!)**
> push 代码需要仓库的"写权限"。**谁负责日常改代码并 push,就用谁的 Gitee 账号建仓库**——仓库主人天然有推送权限,最丝滑。
> - 如果这套系统是**交给业务方部署、由对方日常更新代码** → 用**对方的 Gitee 账号**建仓库(推荐)
> - 如果你自己就是日常开发者 → 用你自己的账号建仓库
> - 如果你偶尔也要 push 代码 → 让仓库主人把你加成仓库"协作者"(仓库 → 管理 → 仓库成员管理 → 添加仓库成员,角色选"开发者"或以上)
> - Windows 服务器**不需要**任何账号——它用"只读部署公钥"拉代码(见第二部分第 2 步),挂在仓库的"部署公钥管理"里,谁建仓都一样

1. 打开浏览器,访问 **https://gitee.com** ,用**上面确定的账号**登录(没账号先注册)。
2. 点右上角 **「+」→「新建仓库」**。
3. 填写:
   - **仓库名称**:例如 `taobao-ai-inspection`
   - **是否开源**:选 **私有**(代码只有你能看)
   - **初始化仓库**:⚠️ 不要勾选"使用 Readme 文件初始化这个仓库"(要空仓库)
4. 点 **「创建」**,复制仓库地址(形如 `https://gitee.com/用户名/taobao-ai-inspection.git`),备用。

## 第 2 步:本地项目关联 Gitee

打开 Mac 的「终端」,执行(把地址换成你自己的):

```bash
cd /Users/bottom_/kong/主播项目

git init
git remote add origin https://gitee.com/用户名/taobao-ai-inspection.git
git branch -M main
```

## 第 3 步:第一次上传 + 日常流程

```bash
git add .
git commit -m "第一次上传"
git push -u origin main
```

> ⚠️ push 时密码不是登录密码,而是「私人令牌」:登录 Gitee → 头像 → **设置 → 安全设置 → 私人令牌 → 生成新令牌**,权限勾 `projects`,生成后复制保存(只显示一次)。
> 日常改代码:改完执行 `git add . && git commit -m "改了啥" && git push`,剩下交给 Windows 自动处理。

---

# 第二部分:Windows 云服务器端操作(首次部署,一次性)

> 核心思路(参考项目的精华):**任务计划程序 + SYSTEM 账号**,不需要 Windows 登录密码、不保留任何窗口、开机自启。
> **SSH 部署 key**:SYSTEM 账号读不到你的 HTTPS 凭据,所以单独配一把**只读** SSH key 给 Windows 用。

## 第 1 步:安装 Git for Windows

1. 服务器浏览器打开 **https://git-scm.com/download/win** 下载安装,一路 Next。
2. 按 `Win + R` 输入 `cmd` 回车,执行 `git --version`,有版本号即成功。

## 第 2 步:生成 SSH 部署 key(只读)

1. 服务器上打开**管理员 PowerShell**(右键开始菜单 → Windows PowerShell(管理员)),执行:

```powershell
# 生成部署专用 key(一路回车即可,不要设密码)
ssh-keygen -t ed25519 -f C:\Users\Administrator\.ssh\id_ed25519_gitee_deploy -N ""
```

2. 查看公钥内容并复制:

```powershell
Get-Content C:\Users\Administrator\.ssh\id_ed25519_gitee_deploy.pub
```

3. 登录 Gitee → 进入你的仓库 → **管理 → 部署公钥管理 → 添加部署公钥**:
   - 标题:随便填(如 `windows-server`)
   - 公钥:粘贴刚才复制的内容
   - **⚠️ 重要:勾选「只读」**(不给它写权限,更安全)

## 第 3 步:创建 SSH wrapper(让 SYSTEM 也能用这把 key)

继续在管理员 PowerShell 执行(注意替换成你的实际 key 路径):

```powershell
@'
@echo off
"C:\Program Files\Git\usr\bin\ssh.exe" -i "C:\Users\Administrator\.ssh\id_ed25519_gitee_deploy" -o UserKnownHostsFile="C:\Services\gitee-known_hosts" -o StrictHostKeyChecking=accept-new %*
'@ | Set-Content -Path "C:\Services\gitee-ssh.cmd" -Encoding ASCII
```

先建目录并测试:

```powershell
New-Item -ItemType Directory -Force C:\Services | Out-Null
$env:GIT_SSH = "C:\Services\gitee-ssh.cmd"
# 测试能否免密访问 Gitee(把地址换成你的)
git ls-remote git@gitee.com:你的用户名/myapp.git refs/heads/main
```

> ✅ 能显示一串 commit 哈希(如 `abc123...` 加 `refs/heads/main`)就说明免密成功。

## 第 4 步:克隆仓库

```powershell
cd C:\Services
git clone git@gitee.com:你的用户名/myapp.git app
```

> 用 SSH 地址(不是 https)。克隆后 `C:\Services\app` 就是正式运行目录。

## 第 5 步:安装程序运行环境(按你的技术栈)

| 技术栈 | 安装 | 确认命令 |
|---|---|---|
| Python | https://www.python.org/downloads/ (勾 Add to PATH) | `python --version` |
| Node.js | https://nodejs.org/ (LTS) | `node --version` |
| Java | https://www.oracle.com/java/technologies/downloads/ | `java --version` |

## 第 6 步:准备脚本和配置(注意:配置不进仓库)

`update.ps1` 和 `app.config.example` 已经随仓库克隆下来了,不需要手动复制。
但 **`app.config` 被 .gitignore 排除了**(防止服务器配置被覆盖),需要你手动复制一份:

```powershell
cd C:\Services\app
Copy-Item app.config.example app.config
notepad app.config
```

**打开 `app.config`,按你的技术栈改 3 个地方:**

| 配置项 | Python 例子 | Node 例子 | Java 例子 |
|---|---|---|---|
| `INSTALL_CMD` | `pip install -r requirements.txt` | `npm ci` | `mvn dependency:go-offline` |
| `TEST_CMD` | `python -m pytest` | `npm test` | `mvn test` |
| `TASK_NAMES` | `MyAppTask` | `MyAppTask` | `MyAppTask` |

> ⚠️ `SSH_WRAPPER` 记得填成你第 3 步创建的 `C:\Services\gitee-ssh.cmd`(示例文件里已填好)。
> 没有测试也可以:把 `TEST_CMD` 留空,脚本就只做 staging 安装验证。但建议至少留一个启动验证(见第 7 步)。

## 第 7 步:把程序注册成计划任务(开机自启 + 崩溃自动重启)

管理员 PowerShell 执行(把命令里的 `python app.py` 换成你的启动命令):

```powershell
$ProjectDir = "C:\Services\app"

# 启动命令:python app.py / node server.js / java -jar app.jar
$StartCmd = "python app.py"

$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -Command `"Set-Location '$ProjectDir'; & cmd /c '$StartCmd' *>> '$ProjectDir\logs\app.log'`""
$Trigger = New-ScheduledTaskTrigger -AtStartup
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)

New-Item -ItemType Directory -Force "$ProjectDir\logs" | Out-Null
Register-ScheduledTask -TaskName "MyAppTask" -Action $Action -Trigger $Trigger -Settings $Settings -User "SYSTEM" -RunLevel Highest -Force
Start-ScheduledTask -TaskName "MyAppTask"
```

验证是否在跑:

```powershell
Get-ScheduledTask -TaskName "MyAppTask"   # State 应为 Running
Get-Content C:\Services\app\logs\app.log -Tail 20   # 看程序日志
```

> 用 `SYSTEM` 账号:不输入 Windows 密码、无人登录也运行、开机自启、崩溃自动重启。
> 如果 `app.config` 的 `TASK_NAMES` 填了这个任务名,更新时脚本会先停它再覆盖再启动。

## 第 8 步:注册自动更新任务(核心!每 1 分钟检查一次)

管理员 PowerShell 执行:

```powershell
# 创建部署 wrapper(指向 update.ps1)
@'
@echo off
cd /d C:\Services\app
C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Services\app\update.ps1"
exit /b %ERRORLEVEL%
'@ | Set-Content -Path "C:\Services\auto-update.cmd" -Encoding ASCII

# 注册每 1 分钟一次的自动更新任务
schtasks /Create /TN "AutoUpdate" /SC MINUTE /MO 1 /TR "C:\Services\auto-update.cmd" /RU SYSTEM /RL HIGHEST /F
```

## 第 9 步:验证全流程(必做!)

1. **手动触发一次更新检查**:

```powershell
schtasks /Run /TN "AutoUpdate"
Start-Sleep -Seconds 15
Get-Content C:\Services\app\logs\deploy.log -Tail 30
```

应看到:`没有新版本,无需更新。`(说明链路通)

2. **改一行代码测试**:Mac 上改一行代码 push,等 5~10 分钟,再看:

```powershell
Get-Content C:\Services\app\logs\deploy.log -Tail 30
```

应看到:`发现新版本` → `staging 验证通过` → `正式目录已覆盖为新版本` → `部署完成: <commit>`

3. 程序运行效果已变成新代码 → 大功告成!🎉

---

# 第三部分:本项目(淘宝直播AI巡检系统)专属部署步骤

> 第二部分是通用流程,这一部分直接给出**本项目的最终命令**,Windows 端按这里做即可。
> 前两步(SSH key、克隆仓库)通用流程已包含,这里从"首次安装"开始。

## ⭐ 如果这台服务器已经在跑「飞书webhook机器人分发」(同机部署)

你的服务器上有参考项目配好的 SSH wrapper(`C:\Services\gitee-ssh.cmd` + `id_ed25519_gitee_feishu`),**但主播项目不能用它**,要**新建一把独立部署 key**:

> ⚠️ 为什么不能复用?Gitee 规则:**同一把公钥全局只能添加一次**。`id_ed25519_gitee_feishu.pub` 已加在你的仓库上,无法再加到对方仓库,会报"当前公钥已被他人使用"。
> 用独立 key 反而更好:两个项目各管各的钥匙,泄露/吊销互不影响。

**第 1 步:服务器上生成新 key(约 1 分钟)**

> ⚠️ **私钥必须"无口令"(空 passphrase)**——计划任务以 SYSTEM 身份无人值守运行,没人输密码。
> 用 PowerShell 生成时**不要用 `-N '""'`**(会被当成字面引号变成口令锁!踩过坑),
> 用下面这种"交互式回车"最稳妥:

```powershell
# 用完整路径调用(Git 自带 ssh-keygen;PowerShell PATH 里通常没有它)
& "C:\Program Files\Git\usr\bin\ssh-keygen.exe" -t ed25519 -f C:\Users\Administrator\.ssh\id_ed25519_gitee_taobao -C "deploy key for tao_bao_zhibo"
# 注意:不要加 -N 参数!它会提示 Enter passphrase,直接按两次回车(留空)即可
# 看到 "Your public key has been saved" 即成功

# 【关键】验证私钥无口令(能无密码读出公钥 = 没上锁):
& "C:\Program Files\Git\usr\bin\ssh-keygen.exe" -y -f C:\Users\Administrator\.ssh\id_ed25519_gitee_taobao

# 查看新公钥内容(复制整串 ssh-ed25519 AAAA...)
type C:\Users\Administrator\.ssh\id_ed25519_gitee_taobao.pub
```

**第 2 步:把新公钥加到对方仓库**

登录 Gitee → `tao_bao_zhibo` → **管理 → 部署公钥管理 → 添加部署公钥**,粘贴第 1 步复制的**新公钥**,勾选**只读** → 添加。
(由你作为管理员直接操作即可)

**第 3 步:创建主播项目专用 wrapper**

```powershell
@'
@echo off
"C:\Program Files\Git\usr\bin\ssh.exe" -i "C:\Users\Administrator\.ssh\id_ed25519_gitee_taobao" -F NUL -o UserKnownHostsFile="C:\Services\gitee-known_hosts" -o StrictHostKeyChecking=accept-new %*
'@ | Set-Content -Path "C:\Services\gitee-taobao-ssh.cmd" -Encoding ASCII
```

> ⚠️ **`-F NUL` 是必须加的!**(不加会静默失败)
> 服务器 `~/.ssh/config` 里可能有一段 `Host gitee.com → IdentityFile 飞书 key + IdentitiesOnly yes`,
> 它会让所有 ssh 连接**强制用飞书 key**。主播项目要的是 taobao key,
> 被 config 劫持后拉不到代码、自动更新静默失败。
> `-F NUL` = "忽略 ~/.ssh/config",强制只用 wrapper 里指定的 taobao key。
> 参考项目的 `gitee-ssh.cmd` **也建议加上 `-F NUL`**(它 -i 已指定飞书 key,加了行为不变,但能彻底排除 config 干扰)。
> 如果 `-F NUL` 报错,改用 `-o IdentitiesOnly=no`。

**第 4 步:验证能拉代码**

```powershell
$env:GIT_SSH = "C:\Services\gitee-taobao-ssh.cmd"
git ls-remote git@gitee.com:wu-xuan-666/tao_bao_zhibo.git refs/heads/main
```

能显示 commit 哈希 = 权限通 ✅

**第 5 步:克隆项目(用新 wrapper)**

```powershell
cd C:\Services
$env:GIT_SSH = "C:\Services\gitee-taobao-ssh.cmd"
git clone git@gitee.com:wu-xuan-666/tao_bao_zhibo.git app
```

`app.config` 里的 `SSH_WRAPPER` 默认已是 `C:\Services\gitee-taobao-ssh.cmd`,**不用改**。

---

## 第 A 步:首次安装环境(只需一次,约 20 分钟)

```powershell
cd C:\Services\app\淘宝直播AI巡检系统
# 项目自带安装脚本:自动装 Python 3.12 / ffmpeg / Node.js / lark-cli + 建 venv + 装 3GB 依赖
.\scripts\windows\setup_windows.bat
```

安装完成后确认依赖就绪:

```powershell
.\.venv\Scripts\python.exe -c "import torch, funasr; print('依赖 OK')"
```

## 第 B 步:创建 config.yaml(手动填,不走 git)

```powershell
cd C:\Services\app\淘宝直播AI巡检系统
Copy-Item config.example.yaml config.yaml
notepad config.yaml
```

> ⚠️ 逐项填写真实值:淘宝 cookie、DeepSeek API key、飞书 chat_id 等。
> 这是**唯一**需要在服务器上手动操作的地方;填好后它永远不会被自动更新覆盖(已被 .gitignore 排除)。

## 第 C 步:注册两个业务计划任务(开机自启 + 崩溃自动重启)

管理员 PowerShell 执行(整段复制):

```powershell
$AppDir = "C:\Services\app\淘宝直播AI巡检系统"
New-Item -ItemType Directory -Force "$AppDir\data\logs" | Out-Null

# 通用设置:SYSTEM 账号、开机自启、崩溃后 1 分钟重启
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
$Trigger = New-ScheduledTaskTrigger -AtStartup

# 任务 1:主巡检 watcher
$WatchCmd = "Set-Location '$AppDir'; .\.venv\Scripts\python.exe scripts\windows\prepare_runtime.py; .\.venv\Scripts\python.exe -m app.recorder.watcher *>> 'data\logs\watcher.log'"
$WatchAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -Command `"$WatchCmd`""
Register-ScheduledTask -TaskName "TaobaoWatcherTask" -Action $WatchAction -Trigger $Trigger -Settings $Settings -User "SYSTEM" -RunLevel Highest -Force

# 任务 2:合规旁路 compliance listener
$CompCmd = "Set-Location '$AppDir'; .\.venv\Scripts\python.exe scripts\windows\prepare_runtime.py; .\.venv\Scripts\python.exe -m app.compliance.listener *>> 'data\logs\compliance.log'"
$CompAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -Command `"$CompCmd`""
Register-ScheduledTask -TaskName "TaobaoComplianceTask" -Action $CompAction -Trigger $Trigger -Settings $Settings -User "SYSTEM" -RunLevel Highest -Force

# 启动两个任务
Start-ScheduledTask -TaskName "TaobaoWatcherTask"
Start-ScheduledTask -TaskName "TaobaoComplianceTask"
```

验证:

```powershell
Get-ScheduledTask -TaskName "TaobaoWatcherTask", "TaobaoComplianceTask"   # State 都应为 Running
Get-Content C:\Services\app\淘宝直播AI巡检系统\data\logs\watcher.log -Tail 20
Get-Content C:\Services\app\淘宝直播AI巡检系统\data\logs\compliance.log -Tail 20
```

> ⚠️ **不要用 taskkill 按进程名杀 Python!** 本项目两个进程有独立锁保护。
> 本方案的自动更新脚本用的是"任务级停止"(Stop-ScheduledTask),安全,不会误杀。

## 第 D 步:检查 app.config(自动更新脚本的配置)

```powershell
notepad C:\Services\app\app.config
```

应确认这三处(模板已填好,只需核对):

| 配置项 | 本项目的值 |
|---|---|
| `REPO_DIR` | `C:\Services\app` |
| `SSH_WRAPPER` | `C:\Services\gitee-ssh.cmd` |
| `TASK_NAMES` | `TaobaoWatcherTask, TaobaoComplianceTask` |
| `INSTALL_CMD` | 留空(依赖已由 setup_windows.bat 装好,自动更新不重装) |
| `TEST_CMD` | 留空(本项目无自动化测试) |

## 第 E 步:注册自动更新任务 + 验证(同通用流程第 8、9 步)

```powershell
# 注册每 1 分钟一次的自动更新任务
@'
@echo off
cd /d C:\Services\app
C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Services\app\update.ps1"
exit /b %ERRORLEVEL%
'@ | Set-Content -Path "C:\Services\auto-update.cmd" -Encoding ASCII

schtasks /Create /TN "AutoUpdate" /SC MINUTE /MO 1 /TR "C:\Services\auto-update.cmd" /RU SYSTEM /RL HIGHEST /F

# 手动触发一次验证
schtasks /Run /TN "AutoUpdate"
Start-Sleep -Seconds 15
Get-Content C:\Services\app\logs\deploy.log -Tail 30
```

应看到:`没有新版本,无需更新。` 说明链路通了。

最后做完整验证:Mac 改一行代码 push → 1 分钟后自动更新任务检查 → 两个业务任务自动重启成新代码 → 收工 🎉

---

# 第四部分:飞书授权到期提醒(必装,防静默罢工)

> **为什么要装**:你的系统用 lark-cli 以**个人身份**操作飞书多维表格/妙记,**授权约 7 天过期**。
> 过期后表格写入、妙记转写会静默失败——**而且不报错、不提醒,你根本不知道**。
> 这个提醒脚本每天检查一次,到期前 3 天用 **bot 身份**发飞书消息通知你"该扫码了"。
> (bot 身份不依赖个人授权,所以授权过期了提醒照样发得出来,不会死循环)

## 第 G 步:配置提醒接收人(默认已填吴玄)

打开 `C:\Services\app\app.config`,提醒接收方式二选一(模板已预留):

```powershell
notepad C:\Services\app\app.config
```

| 配置项 | 填什么 | 当前值 |
|---|---|---|
| `LARK_WARN_USER_ID` | **私聊提醒**给谁(填 `ou_xxx` 用户 open_id),优先使用 | 已填吴玄:`ou_d6d9372a6311e324df4875f582dcad26`(运营助理中心-AI组,已用 lark-cli 搜索确认) |
| `LARK_WARN_CHAT_ID` | 或提醒发到哪个群(填 `oc_xxx`),两者都填时用 USER_ID | 留空 |
| `LARK_WARN_DAYS` | 提前几天提醒,默认 `3` | `3` |

> 想换成提醒别人:在服务器执行 `lark-cli contact +search-user --query "姓名" --as user`,从返回里取 `open_id` 填入即可。

## 第 H 步:注册每日提醒任务(每天早上 9 点)

管理员 PowerShell 执行:

```powershell
# 创建提醒 wrapper
@'
@echo off
cd /d C:\Services\app
C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Services\app\check-lark-auth.ps1"
exit /b %ERRORLEVEL%
'@ | Set-Content -Path "C:\Services\lark-auth-reminder.cmd" -Encoding ASCII

# 注册每天早上 9 点运行
schtasks /Create /TN "LarkAuthReminder" /SC DAILY /ST 09:00 /TR "C:\Services\lark-auth-reminder.cmd" /RU SYSTEM /RL HIGHEST /F
```

## 第 I 步:手动验证一次提醒

```powershell
schtasks /Run /TN "LarkAuthReminder"
Start-Sleep -Seconds 10
Get-Content C:\Services\app\logs\deploy.log -Tail 10
```

- 授权还早 → 日志出现:`授权状态正常,无需提醒`
- 快到 7 天 → 日志出现:`授权即将到期(剩 X 天),发送提醒`,并且**吴玄会收到私聊提醒**(或你配置的接收人/群)

> **扫码续期**:收到提醒后,在服务器上执行 `lark-cli auth login` 扫码一次,授权自动续期约 7 天。
> 提醒会每天重复直到你扫码,所以**不会悄悄漏掉**。

---

# 常见问题(FAQ)

| 问题 | 原因 | 解决办法 |
|---|---|---|
| deploy.log 没生成 | 计划任务没跑起来 | `schtasks /Query /TN "AutoUpdate"` 看 Last Run Result |
| git ls-remote 失败 | SSH key 没配好 | 用第 3 步的命令手动测,看报错 |
| staging 测试失败 | 新代码测试没过 | 坏代码不会上线!回 Mac 修好再 push |
| 更新后程序没变 | TASK_NAMES 没填对 | 检查 app.config 的 TASK_NAMES 是否和注册的任务名一致 |
| 想立即更新一次 | 不想等 1 分钟 | `schtasks /Run /TN "AutoUpdate"` |
| 任务结果 0x800710E0 | 上一轮还没跑完 | 正常,下一轮会自动继续;看 deploy.log 是否卡在某条命令 |
| 改了 requirements.txt 但没生效 | 自动更新不重装依赖(设计如此) | 手动重装一次:cd 到 `C:\Services\app\淘宝直播AI巡检系统` 后执行 `.\.venv\Scripts\python.exe -m pip install -r requirements.txt` |
| schedules/ 排期文件被覆盖 | 排期文件进了 git,自动更新用 Mac 版本覆盖服务器 | 如果排期只在服务器上维护,把 `schedules/` 加入 .gitignore 并在服务器保留一份;如果排期在 Mac 维护,则保持现状 |
| 收到"飞书授权即将到期"提醒 | lark-cli 个人授权约 7 天过期,属正常周期 | 在服务器执行 `lark-cli auth login` 扫码一次,自动续期约 7 天;提醒每天重复直到扫码,不会漏 |
| 表格写入/妙记转写失败,日志显示"授权失效" | 个人授权已过期,提醒可能没配或没注意 | 立即 `lark-cli auth login` 扫码;然后检查 `LARK_WARN_CHAT_ID` 是否配好(见第四部分) |

## 排障命令速查(管理员 PowerShell)

```powershell
# 看自动更新任务最近一次结果(应为 0)
schtasks /Query /TN "AutoUpdate" /V /FO LIST

# 看部署日志
Get-Content C:\Services\app\logs\deploy.log -Tail 100

# 手动触发一次
schtasks /Run /TN "AutoUpdate"

# 看业务任务状态
Get-ScheduledTask -TaskName "MyAppTask"
```

---

# ⚠️ 三条铁律

1. **Windows 服务器上永远不要手动改源码。** 任何本地改动都会在下次自动更新时被强制覆盖丢弃。
2. **数据库、上传的图片、日志等运行时文件**,必须放在 `.gitignore` 排除的目录里(项目已配好),否则会被覆盖或推送到仓库。
3. **密码类信息(数据库密码、密钥)不要写进代码**——用 `.env` 文件(已被 .gitignore 排除),在 Windows 服务器上单独创建。

---

# 附录:文件清单

| 文件 | 作用 | 放哪 | 是否进仓库 |
|---|---|---|---|
| `update.ps1` | 自动更新脚本(staging验证→停服→覆盖→重启,带回滚) | Mac 项目 + Windows C:\Services\app | ✅ 是 |
| `app.config.example` | 配置模板(目录/分支/SSH wrapper/验证命令/任务名) | Mac 项目 + Windows C:\Services\app | ✅ 是 |
| `app.config` | 真实配置(Windows 端从 example 复制后填写) | 仅 Windows C:\Services\app | ❌ 否(gitignore 排除) |
| `.gitignore` | 排除运行时文件,保护数据 | Mac 项目(推送到 Gitee) | ✅ 是 |
| `deploy/README-部署指南.md` | 本文档 | Mac 项目 | ✅ 是 |

> 为什么 `app.config` 不进仓库?因为 `git reset --hard` 会把仓库里的文件覆盖到服务器。
> 如果把服务器配置放仓库里,每次自动更新都会把你的配置冲掉。所以:模板(`.example`)进仓库,真实配置只放服务器。

---

# 附:如果你已有「飞书webhook机器人分发」项目

那个项目的 `scripts/windows/deploy-from-gitee.ps1`、`install-services.ps1`、`docs/WINDOWS_SERVER_DEPLOY.md`、`docs/WINDOWS_AUTO_UPDATE_TECHNICAL.md` 就是本方案的原型,四件套已全部消化进本方案:

| 参考项目的做法 | 本方案的继承方式 |
|---|---|
| SYSTEM 账号计划任务 | 第 7、8 步全部用 SYSTEM 运行,无需登录密码 |
| gitee-ssh.cmd SSH wrapper + 只读部署 key | 第 2、3 步完整复刻 |
| staging 目录 npm ci + npm test 预验证 | update.ps1 内置,命令由 app.config 按技术栈配置 |
| 失败回滚到旧 commit | update.ps1 内置,正式目录更新失败自动回滚 |
| 命令超时 + 进程树清理 | update.ps1 内置 COMMAND_TIMEOUT_SECONDS=120 |
| 并发锁防重入 | update.ps1 内置文件锁 |
| deploy.log 结构化日志 | update.ps1 内置,同款格式 |
