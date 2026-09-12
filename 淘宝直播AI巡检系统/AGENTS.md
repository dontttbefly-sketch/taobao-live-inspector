# AGENTS.md — 淘宝直播 AI 巡检系统（给后续 AI 的交接说明）

> 首次接手本项目时，请先完整阅读本文件，再读 `/Users/bjb03909/Documents/Obsidian Vault/06/淘宝直播AI巡检系统.md`（完整项目笔记），然后看代码。

## 阅读优先级（必须遵守）

1. **现行实现依据**：本文件的「0z. 2026-08-10 第一阶段现行口径」，然后是当前代码、
   `config.example.yaml` 和测试。
2. **现行业务口径**：Obsidian 项目笔记顶部的「当前唯一口径」。
3. Obsidian 06 只有顶部「当前唯一口径」可作现行摘要；其后技术细节和「十b～十h」保留时间线史料，
   **只用于理解原因，不得作为当前实现依据**；如与现行口径冲突，一律以现行口径为准。

**禁止恢复的旧链路**：FunASR 作为默认转写、DeepSeek 补字、候选建议选择器、固定模板冒充
DeepSeek 分析、小时商品明细、技术碎片群卡、部分日报、历史修正卡、生产自动周报、
生产自动 Base 同步/建议反馈闭环，以及以 200 组件/30KB 作为当前卡片上限。

## 项目是什么

淘宝直播（单直播间多人轮播）的经营巡检系统。第一阶段生产主链路只做：
自动录制 → 严格小时窗口 → 千牛经营事实 + 飞书妙记 → DeepSeek 完整经营分析 →
每小时一张简报；真正结束后把完整小时产物聚合成每天一张经营日报。FunASR 只作受控备胎。

## 0z. 2026-08-10 第一阶段现行口径（最高优先）

- **第一阶段只有简报与日报**：周报、经营复盘中心自动同步、建议执行反馈、技术碎片卡、
  部分卡、修正卡不在默认生产链路。`watcher` 不再导入或调用旧技术碎片
  `pipeline`；兼容读取/手工脚本可保留，但不得自动触发。
- **小时事实**：按绝对整点排班窗口冻结；正式产物固定 10 项核心指标和两张双指标分钟趋势图
  （在线人数+进入次数、商品点击+成交金额）。
  小时商品明细/商品前三暂不采集、不展示，也不作为完整性门禁。
- **边界快照容差与事实重抓**（2026-08-13）：历史补抓边界快照容差由 120s 放宽到 300s
  （换班探测+ffmpeg 封存常落在整点后 2～2.5 分钟，120s 曾使两天 6 个小时窗口永久停在
  waiting_evidence）。waiting_evidence 的正式小时由 watcher 自动重抓事实（5 分钟一轮，
  退避 10m/30m/2h/6h，5 次后放弃），补齐后照常补发迟到小时卡；经营日已封存的不再补发。
  相邻小时边界快照首尾相接，日汇总守恒；重抓失败绝不覆盖已落库的部分产物。
- **小时简报顺序**：10 项核心数据 → 两张组合趋势图（在线+进入、点击+成交） → 飞书智能纪要 →
  DeepSeek 下一班次建议 → DeepSeek 本小时经营结论 → 数据峰值完整原话 → 可复用完整话术。
- **DeepSeek 是分析师**：接收完整小时经营事实、趋势、逐字稿和可用历史上下文，自由生成
  完整分析、1～3 条经营结论及建议，不再从候选中原样选择；程序只阻止把无来源的库存、
  优惠、订单、价格等具体经营信息写成事实。完整分析原文持久化并进入日报上下文/Markdown。
- **录像时间线是唯一取材依据**：每个分片的墙钟起止、媒体起止和缺口原子写入
  `timeline.json`。不得用文件 mtime、合并后时长或“后一段媒体前移”重建时间。单个正式
窗口累计缺口 **≤20 分钟**时可继续，但必须在 DeepSeek 上下文和 Markdown 明示
实际覆盖及每段缺口；群简报卡不展示详细“录音覆盖说明”段。**>20 分钟**只保存部分产物并等待/告警，不发正式简报。
- **完整才发送**：在上述明示可接受录像缺口之外，缺任一小时事实、转写或
  DeepSeek ready 产物就继续等待；不得用固定模板、fallback 文案或部分数据冒充正式
  简报/日报。投递不确定仍禁止自动重发。
- **日报身份**：正式身份是 `business_session_key`，不是任意技术 `liveId`；同一业务日的多个
  liveId/stream/主播合并。最后不足一小时的尾段单独转写和入日报，但不额外发送小时简报。
- **日报内容**：全日核心数据 → DeepSeek 全日经营结论 → 主播表现对比 → 小时经营趋势 →
  飞书妙记金句（最多 3 条）→ 下一场行动（最多 3 条）→ 数据来源与异常；完整 DeepSeek
  全日分析保存在 Markdown，不在群卡硬截断。
- **日报投递**：仅真正下播并观察期结束后创建；官方结算、全部小时、妙记和 DeepSeek 未齐
  就持续等待，30 分钟只提醒，绝不发送不完整日报。幂等键为 `daily:{business_session_key}`。
- **自动恢复与状态收敛**：`business_session_key` 是跨 liveId/stream/主播的唯一业务日身份；
  下播观察期内再开播会自动取消终结并重开聚合。重启后从 SQLite 接管原转写、DeepSeek、
  日报和投递任务；安全的状态矛盾自动收敛，无法安全判断的标记 `needs_attention`
  并停止正式日报。
- **发布与回滚**：生产切换只用 `scripts/release_local.py`。脚本拒绝带跟踪改动的工作区，
  依次执行全量测试（warning 当失败）、配置加载、SQLite quick/FK 检查和原生备份，再通过
  launchd 单实例切换；上线后必须通过进程、锁、录像行与当前 watcher 子进程归属、
  业务状态、数据库和录像增长门禁。
  失败回上一个已验证代码提交，**不自动回退数据库**。
- **验证状态**：2026-08-10 生产候选已通过 `548 passed`（`-W error`）、经营日 10 个
  重启边界、DeepSeek 超时/空响应/非法 JSON、0/30/90/300/1200/1200.001 秒录像缺口和
  日报唯一性回归。不得跳过发布脚本手工替换生产进程。

## 0z-a. 2026-08-13 极限词合规监听旁路（现行，默认 disabled）

- **独立 sidecar，不改变第一阶段主链**：入口为 `app.compliance.listener`，独占
  `data/compliance-listener.lock`；它只读当前明确录制的 live 身份，绝不导入/调用旧
  `pipeline`，也不得更新 `streams`、业务日、小时事实、妙记或 DeepSeek 状态。默认
  `compliance.mode=disabled`：可休眠但不得执行音频、模型、事件或消息工作。
- **专用持久化和私有运行文件**：SQLite 只新增
  `compliance_wordlist_versions`、`compliance_runtime_state`、`compliance_audio_jobs`、
  `compliance_audio_chains`、`compliance_audio_job_sources`、
  `compliance_audio_quarantines`、`compliance_events`；私有音频/评估资料在
  `data/compliance/`，日志为 `data/logs/compliance.log`。这些资料及收件人/词库 locator
  均不得写入代码、文档、日志或回复。
- **模型与词库边界**：正式识别只允许带动态热词和可审计时间戳的 SeACo Paraformer
  (`paraformer-zh`)；不可用时停止检测并进入技术告警，绝不以 SenseVoice 作主链或静默备胎。
  机器词库唯一来源是飞书“机器识别词库”工作表 `A1:D5000`，每 300 秒同步；无效、空或
  不完整读取必须保留上一有效版本。
- **授权边界**：`shadow` 可运行词库/音频/模型/事件/模拟卡片，但真实发送数必须为 0，且
  收件人保持空。只有完整 shadow 验收通过并取得一次新的明确用户批准后，才能重新解析唯一
  活跃联系人郭琳及其既有 P2P、核验目标 hash，并原子写入私有收件人和 `mode=live`；只允许
  新 live 事件发送，shadow/suppressed 历史永不补发。live→shadow 同样必须原子清空收件人并
  经 `scripts/release_local.py` 验证。唯一操作顺序见
  `docs/runbooks/live-compliance-alert.md`；尚未声称实测识别率或已上线。

## 历史基线（只用于追溯；与 0z 冲突时禁止执行）

- ✅ **淘宝登录态自动恢复（2026-08-07）**：`SESSION_EXPIRED/USER_VALIDATE` 已是明确鉴权错误，
  不再冒充经营数据缺失；系统使用隔离 Ego Lite 任务空间低频读取用户已登录会话，
  四层业务验证通过后原子写回配置并热切换，不重启录像。失效期间简报停在
  `waiting_auth`，录像/妙记继续；用户只需在 Ego Lite 登录，无需复制 Cookie 或手工重启。
  恢复任务持久化原始小时边界；过时群卡可抑制，但历史分钟趋势仍独立补抓落库
- ✅ **统一转写服务**：`app/transcription/` 定义稳定数据类型、妙记/FunASR Provider、小时音频构建和
  跨重启状态机；业务层不再直接等待远端 processing
- ✅ **妙记主链路**：每个小时窗口上传 16kHz 单声道 64kbps AAC。逐字稿已完成但
  智能纪要仍在生成时，在 600 秒简报截止内继续轮询；纪要完成即提前发送，到截止仍未完成则
  降级为逐字稿+妙记链接，不补发旧简报。完整产物入库后才删云盘源文件
- ✅ **备胎边界**：简报 600 秒仍无逐字稿自动使用 FunASR；正式复盘硬错误进入 blocked 并发红色告警，
  只有 `scripts/manage_transcription.py` 的明确人工命令可以重试飞书或确认 FunASR
- ✅ **AI 职责收口**：飞书智能纪要负责摘要/章节/金句；生产链路停止 DeepSeek 补字、摘要、主播复盘和
  质量评分，DeepSeek 只从候选集合选择受控下一小时建议，伪造字段会被拒绝，最多重试一次
- ✅ **卡片与保留**：简报图表后固定为“本小时智能纪要 → 高质可复用话术 →
  下一小时建议”；智能纪要卡片预览最多 3 个完整重点、每点最多 100 字，禁止截断半句，
  完整纪要保留在会议纪要链接和 Markdown；后两区原话/证据以 50 字为优先选句目标，
  **完整句优先于字数**，不得在卡片层硬截断；正式卡限制 180 组件/28KB；本地录像保留 1 天

- ✅ **简报卡片定稿**（git c1fc058）：Card 2.0 三类卡统一；简报双图带标签 + 高亮话术关联 + 语义补全放开；**简报按排班轮换触发**（force 补发本班次）
- ✅ **一场直播只发一张整场复盘卡**：以平台 `liveId` 为正式复盘单位；同一主播的本地技术碎片
  虚拟合并，每位主播只出现一次并展示官方上下钟经营指标
- ✅ **只由真实下播触发**：`streamStatus=0/roomStatus=2` 连续 3 次确认后创建持久化终结任务；
  确认次数跨重启保存且任何非 ended 状态清零；轮换/重启/恢复/媒体熔断只处理本地碎片，
  `notify_stream()` 永不发送正式复盘卡
- ✅ **结算等待与修正**：下播后按 0/2/5/10/20/30 分钟抓官方总账和主播指标；30 分钟仍缺失发部分卡；
  后续总账或主播指标变化按 `liveId + 快照哈希` 发差异修正卡，不重做内容生成
- ✅ 飞书妙记主转写；SenseVoice+VAD+标点受控备胎；三层千牛数据源；数据质量门禁
- ✅ roomstudio 生命周期状态优先于残留流 URL；新 liveId 自动发现；媒体字节停滞检测、候选流熔断与指数退避；接口网络重试和告警冷却
- ✅ 全量 529 项测试通过；15 小时 / 7 主播正式卡容量夹具低于 180 组件 / 28KB 新限制
- ✅ **2026-08-06 16:00 已一次性生产切换**：首个真实生产妙记 ready、逐字稿落库、云盘源文件删除、
  FunASR 未触发；launchd 新进程继续录制，切换录制空档约 7 秒
- ✅ **2026-08-06 19:11 卡片可读性标准已生产验收**：真实 60 分 40 秒简报中，图表后首块为
  智能纪要，实际顺序为“智能纪要 → 高质可复用话术 → 下一小时建议”；智能纪要 3 个完整重点，
  最长 60 字，会议纪要/逐字稿链接均保留
- ✅ **2026-08-07 FunASR 媒体格式修复已生产验证**：小时媒体是 AAC/M4A；备胎遇到非 RIFF
  输入时先转成临时 16kHz 单声道 WAV，转写成功或失败都清理临时文件。睡眠前遗留的 3 个任务
  已全部补转成功且未补发过期简报，当前录像正常续接
- ⏳ 待办：妙记/智能纪要稳定性与受控建议选择成功率继续观察；周报自动推送；
  业务 KPI 尚未提供，因此报告不得打“好/坏/达标”分
- 运行时数据在 `data/`（录像/转写/数据库/报告）；数据库 `data/inspection.db`（SQLite）

## 关键入口

| 入口 | 说明 |
| --- | --- |
| `config.yaml` | 所有配置：cookie/liveId/主播轮班/词典/llm/notify/briefing（敏感，勿外传） |
| `app/recorder/watcher.py` | 第一阶段生产主循环：录制→整点简报→业务日日报 |
| `app/pipeline.py` | 历史/手工兼容流水线；生产 watcher 不导入、不自动调用 |
| `app/metrics/qianniu.py` | 千牛三层数据源（累计快照/分钟趋势/冻结场次）与质量门禁 |
| `app/taobao_session/` | 淘宝鉴权信号、Ego Lite Provider、候选会话验证、原子激活与恢复状态机 |
| `app/transcription/` | 统一转写模型、飞书妙记 Provider、FunASR 备胎、持久化调度 |
| `app/lark_cli.py` | 共享 lark-cli JSON 执行器与错误分类 |
| `app/intelligence/` | DeepSeek 小时/全日完整经营分析、持久化结果、事实门禁与重试 |
| `app/business_facts.py` | 绝对小时窗口、10 项核心事实、完整性门禁与业务日聚合材料 |
| `app/review/platform.py` | 业务日级多 liveId 合并、完整性等待与唯一经营日报终结器 |
| `app/notify/feishu.py` | 飞书卡片+多维表格（Card 2.0 三类卡共享 `card_v2()`；复用 lark-cli，本机已授权「吴玄的飞书 CLI」） |
| `app/briefing.py` | 严格小时简报（10 项指标 + 2 张组合图 + 妙记 + DeepSeek + 峰值/话术） |
| `app/recorder/timeline.py` | 原子录像时间线、小时取材和 20 分钟缺口门禁 |
| `app/runtime/invariants.py` | 业务日跨重启状态审计与安全收敛 |
| `scripts/release_local.py` | 本机生产发布、验证和代码回滚唯一入口 |
| `app/highlight/peak.py` | 数据峰值证据（3 分钟窗口、peak_meta 结构化关联、时间轴质量门禁） |
| `app/asr/clean.py` | 旧数据/兼容模式的展示门禁；妙记生产转写不调 DeepSeek 补字 |
| `scripts/` | record_now / transcribe_batch / gen_report / update_cookie / update_liveid / dashboard |
| 环境 | `.venv/bin/python`（Python 3.12），ffmpeg 已装，ASR 模型已缓存 |

## 进程托管（已配置）：launchd LaunchAgent 崩溃自启+开机自启
#   plist: ~/Library/LaunchAgents/com.torras.live-inspection.plist
#   手动管理：launchctl unload/load ~/Library/LaunchAgents/com.torras.live-inspection.plist
#   查看：launchctl list | grep torras
# 常用命令

```bash
cd /Users/bjb03909/Desktop/淘宝直播2.0
.venv/bin/python -m app.recorder.watcher            # 启动巡检（已由 launchd 托管，见下）
.venv/bin/python scripts/record_now.py --probe 主播A  # 探测直播状态/流地址
.venv/bin/python scripts/transcribe_batch.py         # 批处理未分析场次
.venv/bin/python scripts/gen_report.py --stream 3    # 生成单场报告
.venv/bin/python scripts/gen_report.py --live-id 123 # 生成平台整场聚合报告
.venv/bin/python scripts/manage_transcription.py --list-blocked
.venv/bin/python scripts/manage_transcription.py --job <key> --retry-feishu
.venv/bin/python scripts/manage_transcription.py --job <key> --use-funasr
sqlite3 data/inspection.db "SELECT * FROM streams"   # 查场次
```

## 关键技术事实（改代码前必读）

0s. **2026-08-07 淘宝登录态自动恢复**：
   - `MtopSessionExpired/MtopUserValidationRequired` 必须继续显式抛出，禁止恢复为空 payload；
     所有 Mtop 调用会发布不含凭据的进程内鉴权信号
   - `taobao_session_state` 是登录故障真相源；状态为
     `healthy/auto_recovering/user_login_required/validating`，不保存 Cookie 原文
   - 只有直播详情、`iliad.totalStats`、分钟趋势、场次列表四层同时通过才能激活新会话；
     `config.yaml` 必须原子替换并保持 `0600`，运行客户端热切换，不许为此重启录像
   - 鉴权故障时，小时妙记产物仍落库，简报消费者转为 `waiting_auth`；2 小时内最多恢复
     2 张，更长中断只发 1 张恢复汇总。历史分钟趋势可补抓，没有小时边界快照时严禁把
     恢复时累计数据平均拆分
   - 登录态信号带客户端 epoch，热切换后忽略旧请求的迟到过期信号；红/绿恢复通知失败会用原幂等键重试
   - 首次失效立即红卡提醒，未恢复每 3 小时一次；用户只在 Ego Lite 完成登录/验证，
     系统不填账密、不过验证码、不抢夺用户浏览空间
   - 未来 Windows 云服务器只替换 `BrowserSessionProvider` 为 Chrome/Edge 持久 Profile，
     不复制本机 Profile/Cookie，云端首次仍需人工登录一次

0a. **2026-08-06 飞书妙记全面替换**：
   - `transcription_jobs` 是唯一任务真相；主状态为 queued/media_ready/uploaded/processing/ready，
     备胎/阻塞为 fallback_running/fallback_ready/blocked；清理与业务消费各有独立状态
   - lark-cli 当前版本没有 `--wait-ready`，禁止添加；watcher 根据 `next_poll_at` 非阻塞轮询
   - 飞书可能先返回空 summary/chapter、数分钟后才生成智能纪要；空字段不能立即判 ready。简报在 600 秒内
     继续轮询，完整纪要提前完成才发送；到截止只有逐字稿时才降级发送。智能纪要主链接使用同租户
     `/docx/<note_doc_token>`，逐字稿保留为独立入口
   - 小时逐字稿和最终复盘共用同一任务，不得重新上传整场；长直播按主播/小时展示多个妙记链接
   - 妙记逐字稿的说话人长段必须在 Provider 边界按明确句末标点拆成完整句，
     按字符比例分配句级时间戳；峰值关联、高质话术和建议证据共用该句子粒度
   - 高质话术与建议证据展示时，50 字只是优先目标而非硬上限；找不到 50 字内的
     完整句时保留最小完整语义单元，禁止“前49字+省略号”
   - 智能纪要数字句必须在逐字稿中找到数值等价来源；不合格句直接删，对应区块没有产物则隐藏
   - 简报卡的智能纪要只从完整产物中抽取前 3 个完整重点，每点最多 100 字；纯标题和无法完整收束的
     超长句跳过，禁止用省略号截半句；完整摘要、章节、金句仍保留在会议纪要链接和 Markdown
   - 章节深链只用已验证格式 `minute_url?t=<start_ms>`；链接优先级高于摘要，卡片超限先缩摘要
   - 飞书用户授权失效只告警，不自动登录/扩权；正式复盘不得因为 30 分钟经营数据截止而跳过妙记等待
   - 小时媒体是 AAC/M4A，FunASR 备胎不得把它直接交给只接受 RIFF WAV 的 `transcribe_wav`；
     Provider 必须先转为独立临时 16kHz 单声道 WAV，并在成功或异常路径都清理临时文件

0. **2026-08-02 架构加固（Codex 体检后全量落地）**：
   - **单实例锁**：watcher 启动时抢 `data/watcher.lock`（fcntl/msvcrt），抢不到即退出；
     launchd 独占托管，update_liveid.py 只停不拉（KeepAlive 自动拉起）
   - **流水线原子领取**：状态机 recorded→transcribing→transcribed→analyzing→analyzed→reporting→reported；
     每步 `store.claim_stream(id, from, to)` 原子 UPDATE，重复提交/双实例只执行一次
   - **幂等**：save_transcripts/save_highlights 先清旧再写；reviews 唯一索引（同场次一行）；
     飞书通知幂等键（notify_state.json notified_streams）
   - **live_id 固化**：streams.live_id 存开播时的 liveId，复盘/抓数据用它（防自动切换串数据）
   - **SQLite WAL + busy_timeout 8000 + 写锁**：单连接多线程安全
   - **简报转写持久化**：brief_transcripts 存带全场偏移时间戳的分片转写；下播复盘
     全覆盖且时长偏差<60s 时复用（pipeline._try_reuse_brief），不再重复 ASR
   - **健康检查**：每小时一次（磁盘<10GB/积压≥3场/15分钟无新分片）推告警卡片
   - **日志轮转** 5MB×5；FunASR disable_pbar；config.yaml 600 权限；
     迁移包默认排除敏感文件（--include-sensitive 才带）
   - **测试**：`tests/test_core.py` + `tests/test_feishu_cards.py`（状态时间、数据缺失、语义切分、
     高亮、话术幂等、卡片/图表/峰值/轮换简报等 102 项），`.venv/bin/python -m pytest tests/ -q`；
     已 git 初始化（.gitignore 排除 config.yaml/data）；2026-08-04 定稿存档点 c1fc058

0b. **2026-08-04 卡片 2.0 历史演进**（未被 0a/当前状态覆盖的事实才仍适用）：
   - 三类卡统一 Card 2.0（简报橙/复盘蓝/告警红），`card_v2()` 共享构造器；
     每卡唯一焦点 + 2-5 视觉块 + 状态标签（完整/部分/不可用），缺失显示"暂无"≠0
   - **简报图表**：在线人数折线 + 成交金额柱形拆成两张小图，上方必须用卡片 markdown 标签
     （VChart `title` 在飞书不渲染）；商品点击只进指标卡；断档补 null 不补 0；
     柱形自定义颜色必须用 `seriesField`+`color` 数组（`style.fill` 被飞书静默忽略，
     2026-08-04 探测确认，当前高亮粉 #FF5E9C）
   - **高亮话术关联**：峰值前 **3 分钟**窗口（WINDOW_BEFORE_MS）、每峰值最多 5 句
     （MAX_EXCERPTS / per_peak）、语义补全版、脏句降级待回听；同分钟表述"先后无法确定"；
     关联≠因果；时间轴门禁（墙钟与媒体时长偏差<60s + deal 序列连续覆盖）不过则降级
   - **旧 FunASR 语义补全**：数字/金额/时间/链接号/型号保真门禁仍可用于历史数据；
     **现行妙记主链路不再调 DeepSeek 做语义补全**
   - **受控建议单边界**：程序先从已验证原话生成候选 `advice_items`，DeepSeek 只能
     原样选择 1～3 条，不能改字或新增证据/指标；Markdown/Card 共用同一结构，失败后隐藏
   - **展示语料持久化**：妙记或人工确认的 FunASR 原文一次入库为展示语料；
     整场聚合只读已持久化产物，不重跑 AI，原始来源保留用于审计
   - **飞书投递歧义终态**（2026-08-06）：正式卡统一传 `--idempotency-key`；超时/断连/无有效
     `message_id` 记 `delivery_unknown` 且永不自动重发，只有远端明确拒绝才可重试。
   - **补全门禁容错**（2026-08-04 真实数据修复）：数字比较改为数值等价
     （十4→14、三5万→三万五千、7 172→71/72 属于 ASR 破坏还原，放行；17→16、
     1.2万→12万 拒绝）；乱码英文阈值 4→8 字符+品牌词白名单；坏行标
     【待回听确认】降级、其余保留，好行≥50% 才算块通过
   - **简报时间轴自洽**（2026-08-04）：简报高亮关联的 recording_end 用
     开播时刻+媒体实测时长推导（_media_end_epoch_ms），不受 ASR 延迟污染
     （转写耗时可达 20 分钟）；该场景墙钟偏差门禁恒过（自洽校验），
     **门禁的真实校验语义只对下播复盘有效**（复盘用真实 started/ended 时间轴）
   - **推送节奏**：排班轮换 force 发本班次简报再切场（修复简报饿死）；复盘卡只在正常下播发；
     轮换/启动补跑（_recover_pending_streams）`notify_card=False`
   - **update_cookie.py**：仅作 Ego Lite 不可用时的手工备用；stdin 读 cookie（不进 shell 历史），
     必须复用同一四层验证、原子写入和 `waiting_auth` 恢复状态机，验证失败不写入

0c. **2026-08-05 整场主播聚合复盘**（历史演进；未被 0a 覆盖的事实仍适用）：
   - `streams` 只表示本地技术碎片；正式卡和整场报告以 `liveId` 为单位
   - `platform_anchor_metrics` 按 `(live_id, anchor_id)` 唯一；优先用 `daibo_id` 映射主播，名称兜底
   - 金额/订单/件数/次数可跨上下钟片段合计；UV/人数多片段只能标为“分段人次”，不得冒充去重人数；
     比率和客单价在多片段时保持 `NULL/暂无`
   - `platform_review_jobs` 持久化触发、截止、重试、租约、状态；`platform_reviews` 冻结发送载荷，
     飞书失败或重启后复用同一份已生成结果，不重跑内容生成
   - `room_lifecycle_state` 保存严格连续的明确下播确认；网络错误、未知状态或重新在播都会清零
   - 部分卡每小时低频补抓整场与主播指标，变化发独立修正卡，不重跑 AI；完整旧值不被后续残缺响应降级
   - 正式卡幂等键 `review:{live_id}`；修正卡幂等键 `correction:{live_id}:{snapshot_hash}`，
     快照同时包含整场总账和各主播金额、转化、客单、点击、加购、互动、时长等指标
   - 卡片只保留全场 3 条高亮关联、3 条高质话术、3 条行动与每主播一个折叠区；完整长文留 Markdown，
     当前发送上限为 180 组件 / 28KB
   - 同一句相似话术跨碎片去重前必须通过数字等价门禁，16/17 等型号差异必须分别保留

1. **淘宝接口均为逆向**，已实测可用：
   - 流地址：`mtop.roomstudio.live.detail.get`（参数 `{"liveId": "..."}`，取 liveUrlHls）
   - 在播累计：`mtop.taobao.iliad.live.user.assistant.data.get`，`types=totalStats`。
     真实字段：online_uv/max_online_uv/uv/pv/pay_amt/pay_buyer_cnt/pay_item_qty 等；**没有订单数字段**
   - 分钟趋势：`mtop.taobao.tblive.portal.live.user.assistant.data.get`，核心 types 为 uv/itemClick/deal/heatScore
   - 下播冻结场次：generalQuery，dataApi=`live_overview_rt_content_v3`，按 content_id=liveId 精确匹配；
     字段含 look_uv/look_pv/max_online_uv/ipv/pay_amt/pay_buyer_cnt/pay_order_cnt/pay_item_qty
   - **每日经营数据**：`mtop.dreamweb.query.general.generalQuery`（直播中控台每日数据，
     2026-08-02 逆向确认；dataApi=zkt_zbgl_core_card_overview，
     param={startDate/endDate/cpStartDate/cpEndDate(YYYYMMDD), cycleCode:1d/7d,
     fieldColumns: 指标逗号列表}，param 是 JSON 字符串）
     → `app/metrics/daily.py`：fetch_daily / save_daily / fetch_and_save；库表 daily_metrics
     → 页面真实请求为 12 个字段；核心为 pay_amt_nd 成交金额、look_uv_nd 观看人数、
     pay_byr_cnt_nd / distinct 成交人数、mbr_cnt_incr_nd 新增会员。旧代码臆测的
     order_cnt_nd/ipv_nd/cvr_nd 等字段返回 null，禁止恢复使用
     → watcher 每日切日时自动抓昨日入库；周报含「每日经营数据」近 7 天趋势表
   - **主播单日指标**：同一 generalQuery，dataApi=`live_daibo_analysis_ind_list`
     （代播主播分析页「主播数据」tab，2026-08-02 逆向，数据与页面逐项一致）
     → `app/metrics/daibo.py`：fetch_daibo_daily(cfg, YYYYMMDD)（单日=当天开播主播，
     环比自动取前一天）；库表 daibo_daily（date+daibo_id 唯一）
     → 字段：look_uv/look_pv 观看、pay_amt 成交金额、pay_byr_cnt/ord_cnt/itm_qty 成交人/单/件、
     cvr_pay 转化率、atv 客单价、ipv_uv/ipv/ctr_itm 商品点击、cart_uv/pv 加购、
     atn_uv/atn_uv_rate 新增粉丝/转粉率、cmt/shr/fvr 评论/分享/点赞、look_time 时长
     （"37小时52分钟"格式→秒存 look_time_sec）；每主指标带 *_cp_rate 环比（"--"=无）
     → 注意：**param 不能带 count**（只返回总数）；look_time 需解析；配置接口
     mtop.taobao.dreamweb.data.center.config.detail（type=live_anchoranalysis_effect）
     可拿字段中文名；上下钟明细用 type 参数=live_daibo_analysis_content_ind_list
     → watcher 每日切日时自动抓昨日入库；周报含「主播单日表现（最新一天）」表
   - 接口可能随平台升级失效；失效时用浏览器 DevTools 抓真实请求换 API 名
2. **mtop token 刷新有 3 个坑**（详见 Obsidian 笔记踩坑 #7）：
   - 返回串是完整字符串 `FAIL_SYS_TOKEN_EXOIRED::令牌过期`（拼写错误少个 P）→ 必须前缀匹配
   - 必须全套 cookie 替换（_m_h5_tk/_m_h5_tk_enc/sgcookie 缺一不可，否则 FAIL_SYS_TOKEN_ILLEGAL）
   - session 请求必须显式传 `headers={"Cookie": self.cookie}`（session 头是旧值）
3. **生产主转写是飞书妙记**。SenseVoice + FSMN-VAD + CT-Transformer 只是 FunASR 备胎；
   简报只在 600 秒无妙记逐字稿时自动使用，正式复盘必须人工确认才可使用。
   备胎的 SenseVoice 用 MPS，VAD/标点固定 CPU；禁止把它恢复为默认主链路
4. **ffmpeg 录 HLS 不加 `-reconnect*`**（死循环）；录 mp4→TS 音频必须重编码 AAC
5. **FunASR 备胎性能**：MPS 约 1 小时音频 5 分钟；模型懒加载后常驻（~3.3GB 内存）
6. 飞书推送用 lark-cli；真实 chat_id 仅在敏感 config.yaml 中，不得写进文档、日志或回复
7. 千牛数据接口保持低频（每小时简报 + 下播后一次）；MtopClient 已做进程内 1.5 秒串行限速
9. **liveId 每场变化（已自动发现）**：接口 `mtop.taobao.dreamweb.live.list.query`
   （参数 roomNum=直播间编号 + 分页，响应 $.data.data[].id=liveId，startTime 毫秒上海时区，
   roomStatus 1=直播中）；watcher 流持续不可用时自动查询当天场次并切换（config taobao.room_num）；
   也可手动 `.venv/bin/python scripts/update_liveid.py <新liveId>`
8. **接口异常自动告警**：探测连续失败 3 次（约 3 分钟）自动推飞书告警卡片（watcher._send_alert），触发后计数重置；接口变更时先看告警卡片 + data/logs/watcher.log 定位
10. **下播判断不能看 URL 是否存在**：淘宝下播详情仍可能保留过期 `liveUrl/liveUrlHls`；
    `streamStatus=0/roomStatus=2` 才是明确结束，`streamStatus=1/roomStatus=1` 才是在播。
    Recorder 的失败计数跨候选累计，只有媒体文件真实增长才清零；禁止在 `switch_url` 清零。

## 安全红线

- `config.yaml` 含千牛 cookie 和 DeepSeek key，**不得**写入文档/笔记/日志输出，回复中掩码显示
- 直播录制内容仅用于自家主播内部复盘，不对外
- 只读查询可随时做；写操作（发消息、改配置、删除数据）需用户明确授权
- 报告合规：不得虚构库存、限量名额、订单、购买人数、优惠规则、用户心理；
  数据缺失显示“暂无”而不是 0，相关性不得写成因果

## 下一步路线（当前）

- 观察妙记成功率、智能纪要生成延迟、授权失效告警和 FunASR 是否误触发
- 继续观察平台 T+ 结算时延与整场/主播字段稳定性
- 周报自动推送飞书（生成已有 `gen_report.py --week`，推送未做）
- 千牛更多指标类型（接口已通）
- 上云部署；主播能力档案；Web 看板；周报推送

## 经营复盘中心（2026-08-05 上线）

- 新建飞书 Base **「淘宝直播经营复盘中心」**，与旧「淘宝直播AI巡检统计」技术碎片表独立，
  Base token 与各表 ID 仅存 `data/notify_state.json`，不得写进代码、文档或回复。
- 六张业务表：整场直播复盘、主播日表现、主播周表现、高亮话术库、复盘行动跟进、数据同步与异常；
  另有「经营总览」仪表盘和今日经营/主播日环比/主播周环比/待执行行动/待处理异常/高质话术视图。
- 数据模块：`app/review_center/data.py`；飞书建库与按业务键安全 upsert：
  `app/notify/review_center.py`；手动回填：`scripts/sync_review_center.py --scope all --strict`。
- 自动节奏：watcher 每小时抓当天主播日数据（状态=进行中）并同步，日切后抓昨日冻结值；
  整场正式复盘和冻结修正成功后同步 platform 范围。Base 失败只记日志/异常表，绝不能中断录像、ASR 或正式卡。
- 口径：日环比仅比较前一自然日，任一缺失=暂无，昨日 0 且当前>0=新增；周环比仅在两周覆盖
  完全相同的有效日集合时计算，缺日绝不补零。人工评价/反馈备注，以及行动的负责人、截止、状态、结果、备注
  始终由人优先，程序不得覆盖。
