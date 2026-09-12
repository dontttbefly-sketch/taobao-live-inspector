# 迁移到 Windows 指南

把整套「淘宝直播 AI 巡检系统」从 Mac 迁移到 Windows（纯 CPU 版）。

发给公司 Windows 云主机时，解压后只跑一条命令：`运行.bat`。

## 一、Mac 端：制作迁移包（30 秒）

```bash
cd /Users/bjb03909/Desktop/淘宝直播2.0
.venv/bin/python scripts/make_migration_pack.py --cloud
# 输出：淘宝直播AI巡检系统_云端包_YYYYMMDD.zip
# 解压后双击 运行.bat
```

- 录像文件不打包（体积大）。需要的话加 `--include-recordings`（包会很大，建议用移动硬盘/U 盘传）
- 默认迁移包**不含** `config.yaml`、数据库、日志或通知状态。新主机从
  `config.example.yaml` 建立私有 `config.yaml` 并逐项填写。
- 如业务确实需要迁移受保护状态，必须显式使用迁移脚本的敏感文件选项、加密传输，并在新主机
  受限写入后删除中间副本。即使如此也绝不跨机器复制浏览器 Profile、原始 Cookie 或飞书授权目录。
- 把 zip 拷到 Windows 电脑（U 盘 / 网盘 / 微信文件传输均可），敏感材料不能通过聊天或工单传递。

## 二、Windows 端：安装（约 20 分钟，全程自动）

1. 解压 zip 到任意目录（如 `D:\淘宝直播AI巡检`，路径别带空格更稳）
2. 双击运行 `scripts\windows\setup_windows.bat`
   - 自动安装：Python 3.12、ffmpeg、Node.js + lark-cli（飞书推送）
   - 自动创建虚拟环境并安装全部依赖（首次下载约 3GB，需联网）
   - 自动校验 config.yaml
3. 在新主机重新完成飞书授权：运行 `lark-cli auth login`，再以
   `lark-cli auth status` 确认。不要复制任何旧主机的授权目录。
4. 为淘宝登录恢复在 Windows 新建 Chrome 或 Edge 的**持久 Profile**，并通过现有
   `BrowserSessionProvider` 边界使用它。首次登录、验证均由操作者在新 Profile 中完成；
   **禁止**从 Mac 复制浏览器 Profile，也禁止复制、粘贴或导出原始 Cookie 值。
5. 在任何服务安装前，对私有测试音频完成真实模型预检：

   ```bat
   .venv\Scripts\python.exe scripts\preflight_compliance_model.py --audio data\compliance\evaluation\preflight.wav --terms-file data\compliance\evaluation\enabled_terms.txt
   ```

   只有 SeACo Paraformer 的动态热词和时间戳契约通过后，才继续部署；失败时保持
   `compliance.mode: disabled`，不得改用其他模型。

## 三、启动（以后每次）

安装为两个受监督服务：主 `watcher` 与独立 `compliance listener`；两者必须分别配置
崩溃重启、工作目录和私有日志。不要把监听器塞进 watcher 进程，也不要用按映像名的
`taskkill` 停止 Python。

本地手工启动仅用于受控排查：双击 `scripts\windows\start_watcher.bat`，它会启动两个
独立入口。生产切换仍只允许通过对应平台的受监督服务流程；停止时按服务名分别停止。

## 四、Windows 注意事项

| 事项 | 说明 |
|---|---|
| **转写速度** | 无独显用 CPU：约 1 小时音频 20-40 分钟。每天 15-20 小时素材，建议夜里转（睡前启动 `transcribe_batch.py`，起床就好了） |
| **简报/复盘** | 直播中简报（30 分钟一条）照常；完整复盘可能在第二天早上出（转写需要时间） |
| **淘宝会话** | 在新机器的 Chrome/Edge 持久 Profile 中人工重新登录；只经 Provider 边界恢复会话，绝不复制 Mac 浏览器资料或原始 Cookie。 |
| **录像保留** | 自动清理策略照常（默认保留 7 天），纯 CPU 机器磁盘按 3-5GB/天 估算 |
| **内存** | ASR 模型占约 3.5GB，16G 内存机器没问题 |
| **合盖** | Windows 笔记本合盖默认睡眠，请在电源设置里改为"合盖不睡眠"或插电使用 |

## 五、常用命令（Windows 版）

```bat
:: 批处理未分析场次（夜间转写用）
.venv\Scripts\python.exe scripts\transcribe_batch.py

:: 切直播间 liveId（换场次时）
.venv\Scripts\python.exe scripts\update_liveid.py 4223058047060632

:: 生成周报（自动清理过期录像）
.venv\Scripts\python.exe scripts\gen_report.py --week 7

:: 查数据
sqlite3 data\inspection.db "SELECT * FROM streams"
```

## 六、迁移包内容

- 默认仅含代码（`app/`、`scripts/`）与报告（`data/reports/`）；`config.yaml`、
  `inspection.db`、日志和通知状态均不随默认包迁移。显式敏感迁移必须加密、受限保管、写入后
  删除中间副本；不要在工单、聊天或命令行参数中粘贴令牌、会话或 Cookie。
- 录像默认不带（可手动拷贝 data/recordings/ 过去，历史录像不影响新场次）
- ASR 模型无需手动迁移：首次转写会自动从网上下载（约 1.2GB）
