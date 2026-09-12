# 淘宝直播 AI 巡检系统

> **在线演示：** [dontttbefly-sketch.github.io/taobao-live-inspector](https://dontttbefly-sketch.github.io/taobao-live-inspector/)
> 演示页展示系统流水线与一份复盘报告的完整结构（数据为虚构样本）。

**公开版本说明：** 仓库中的排班表为虚构示例，仅用于说明数据格式；真实配置（直播间地址、平台凭据、飞书 token）通过 `config.yaml` 本地注入，不进入版本库。


全天候自动监测主播直播：**自动录制 → 语音转写 → 高亮切片 → 主播话术库 → 复盘报告**。
无需区分班次与时间跨度，机器 24 小时巡检，组长打开报告就能复盘。

```
开播监听 ──> 自动拉流录制 ──> FunASR 转写(带逐句时间戳)
                                   │
                    ┌──────────────┴──────────────┐
                    ▼                             ▼
            高亮切片检测(关键词+声学)          话术自动入库(分类/去重/统计)
                    │                             │
                    └──────────────┬──────────────┘
                                   ▼
                        复盘报告(每场) + 周报(聚合)
```

## 目录结构

```
├── config.example.yaml     # 配置模板（复制为 config.yaml 使用）
├── app/
│   ├── main.py 不存在；巡检入口：python -m app.recorder.watcher
│   ├── config.py           # 配置加载
│   ├── db.py               # SQLite（anchors/streams/transcripts/highlights/talktracks）
│   ├── pipeline.py         # 流水线编排：转写→高亮→话术→报告
│   ├── recorder/           # mtop 签名拉流 / 开播监听 / ffmpeg 录制
│   ├── asr/                # FunASR 转写
│   ├── highlight/          # 关键词 + 声学高亮检测
│   ├── talktrack/          # 话术分类 + 去重入库
│   └── review/             # 复盘报告 + 周报
├── scripts/                # 命令行入口
└── data/                   # 录像、转写、数据库、报告（运行时生成）
```

## 安装（macOS，Apple Silicon）

```bash
# 1. 依赖：ffmpeg + Python 3.12（系统 python3 是 3.9，必须装新版）
brew install ffmpeg python@3.12

# 2. 虚拟环境
cd 淘宝直播2.0
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt   # 首次安装约 2-3GB（torch）

# 3. 配置
cp config.example.yaml config.yaml
```

## 配置（config.yaml）

### 主播列表（单直播间多人轮播）

直播间链接 `https://tbzb.taobao.com/live?liveId=xxx` 里的 `liveId` 就是直播间 ID。
同一直播间多人轮播时，**所有主播共用同一个 liveId**（填 `taobao.live_id` 统一配置），
每个主播配 `shift` 轮班表，录制场次自动归属到对应主播，个人话术库按主播分开统计。

```yaml
anchors:
  - name: "主播A"
    taobao_user_id: ""
    live_id: ""          # 留空，用下方 taobao.live_id
    shift: "09:00-12:00" # 轮班时间，支持跨天 "22:00-02:00"
    enabled: true
  - name: "主播B"
    shift: "12:00-15:00"
    enabled: true

taobao:
  live_id: "4227295563450020"   # 直播间统一 liveId
```

### 淘宝登录态（必填，用于拉流）

1. 电脑浏览器登录 [淘宝](https://www.taobao.com)（手机号登录即可）
2. 安装浏览器扩展 **Cookie-Editor**（Chrome/Edge 应用商店搜索）
3. 打开淘宝任意页面 → 点扩展图标 → Export（JSON 或字符串模式均可）
4. 把 cookie 字符串填进 `config.yaml` 的 `taobao.cookie`，最少包含：

```
_m_h5_tk=xxx; _m_h5_tk_enc=xxx; cookie2=xxx; _tb_token_=xxx; unb=xxx; tracknick=xxx; uc1=xxx
```

> cookie 有有效期（一般几天到几周），失效后重新导出即可。
> 程序会自动用服务端下发的 `_m_h5_tk` token 签名，匿名也能请求，但登录态更稳。

### 高亮词典（可自定义）

`highlight.categories` 按业务调整：逼单/催付/福利/互动/产品 各类关键词，命中即高亮信号。
跑两周后根据实际转化数据增删词。

### 千牛经营数据复盘（下播后自动抓取）

系统按三层只读数据源取数，并明确显示口径：

- 在播累计：`iliad ... data.get / totalStats`，取当前在线、最高在线、观看人数、成交金额、成交人数、成交件数等；实时接口**不返回订单数**。
- 小时趋势：`tblive.portal ... data.get`，取分钟在线、商品点击、成交增量，用于每小时走势。
- 下播冻结场次：`generalQuery / live_overview_rt_content_v3`，按 `content_id=liveId` 精确匹配，补齐真实成交订单数。

接口缺失字段统一保存为 `NULL`、卡片显示“暂无”，绝不冒充 0。报告同时写明来源、覆盖时段、数据状态和限制；数据不完整时 AI 禁止评价表现或推断因果。所有 mtop 请求在 watcher 进程内串行限速，风控/字段漂移会进入健康告警。

### 录像自动清理（保留策略）

`recorder.retention_days`（默认 7，0=不清理）：录像只保留最近 N 天，
**周报生成后自动清理 + 巡检每日自动检查**一次过期录像（mp4/wav/srt/分片目录）。
删除的只是原始文件，转写/高亮/话术/指标/报告全部保留在数据库里，分析成果不丢。

### 直播中简报（实时监测）

```yaml
briefing:
  enabled: true
  interval_sec: 3600   # 直播中每小时推一条完整简报（0=关闭）
  part_min_age: 120    # 分片闭合判定
```

直播进行中，每小时对已录完的分片做增量转写 + AI 简报，推飞书群。内容固定包含：数据状态、本小时事实、内容摘要、完整证据片段、下一小时建议。没有证据时明确写“暂无”，不猜库存、订单、用户心理或转化原因。
依赖分片录制（`recorder.segment_seconds` 需 > 0）；下播后完整复盘不受影响。

### 长期运行容错

- 下播状态以 `streamStatus/roomStatus` 为准，明确下播时忽略接口残留的过期直播 URL。
- 直播列表每 3 分钟低频发现新的明确在播 liveId，旧场自动收尾、新场自动建档。
- ffmpeg 不只检查进程是否存活，还检查媒体字节是否持续增长；卡死 90 秒后切候选流，全部失败按 30–300 秒指数退避。
- 淘宝 DNS/超时统一重试并转换为可控错误；同类飞书告警 1 小时内只发一次。
- 简报失败不会提前消费分片，5 分钟后自动重试；下播时会避免简报与分片清理互相破坏。

### 可选：LLM 金句抽取

`talktrack.llm.api_key` 填入 DeepSeek/通义 的 key 后启用（否则走纯规则分类，效果也已可用）。

### 大模型复盘分析（推荐配置）

```yaml
llm:
  provider: "deepseek"        # deepseek / dashscope
  api_key: "sk-xxx"           # 填了之后每场自动生成 AI 复盘分析
  base_url: "https://api.deepseek.com"
  model: "deepseek-chat"
```

每场直播的复盘报告自动增加完整 **AI 复盘分析**：数据可靠性 / 本场事实 / 内容与话术 / 高亮证据 / 风险与假设 / 下一场行动。当前未配置 KPI 时不打“行/不行”分；观察、假设、结论严格分开，合规审校禁止虚构库存、名额、订单和用户心理。飞书卡片同步展示六段全文，不再按 60/80/450 字硬截断。
未配置 key 或调用失败时自动降级为纯数据版报告，不影响流水线。每场成本约几分钱。

### 飞书通知（卡片 + 多维表格）

复用本机已授权的 lark-cli，无需额外凭证：

```yaml
notify:
  feishu:
    enabled: true
    chat_id: "oc_xxxx"   # 推送群 ID（留空则只写多维表格不推卡片）
```

- 每场直播复盘完成自动：**往群里推复盘卡片**（时长/高亮 TOP3/话术类别）+ **多维表格追加一行**（场次/主播/时长/高亮/各类别话术数）
- 多维表格首次运行自动创建（"淘宝直播AI巡检统计"），token 存 `data/notify_state.json`
- 机器人需在目标群里（群设置 → 群机器人 → 添加应用，选择你的 CLI 应用）

## 使用

```bash
# 1. 全天候 AI 巡检（推荐，常驻后台）：监听开播、自动录制、自动全流程分析
nohup .venv/bin/python -m app.recorder.watcher > data/logs/watcher.log 2>&1 &

# 2. 手动立即录制（也支持绕过接口直接给流地址）
.venv/bin/python scripts/record_now.py --anchor 主播A            # 接口探测 + 录制
.venv/bin/python scripts/record_now.py --url "http://..." --name 主播A   # 手动流地址
.venv/bin/python scripts/record_now.py --probe 主播A            # 只探测流地址

# 3. 批处理：把已录完的场次跑完 转写→高亮→话术→报告
.venv/bin/python scripts/transcribe_batch.py            # 所有 recorded 场次
.venv/bin/python scripts/transcribe_batch.py --retry    # 加 --retry 重试失败场次

# 4. 生成报告
.venv/bin/python scripts/gen_report.py --stream 3       # 单场复盘
.venv/bin/python scripts/gen_report.py --week 7         # 周报
.venv/bin/python scripts/gen_report.py --all            # 全部已转写场次

# 5. 看数据
sqlite3 data/inspection.db "SELECT * FROM talktracks ORDER BY use_count DESC LIMIT 20"
```

## 输出

- `data/recordings/`：每场直播 mp4（多分片自动合并）、SRT 字幕、转写 JSON
- `data/reports/`：每场复盘报告 `stream_XXXX_主播.md` + 周报 `weekly_YYYYMMDD.md`
- `data/inspection.db`：全部结构化数据（SQLite 直接查询/接入报表工具）

每场报告包含：场次信息 / 高亮时刻清单（时间点+类型+评分+内容，可回看录像对应位置）/ 本场新增话术 / 复用高频话术。

## 性能参考（Apple Silicon 实测）

- 转写：SenseVoice 主模型使用 Apple Silicon MPS；FSMN-VAD 与 CT-Transformer 标点固定 CPU，避免长直播后 MPS 性能退化
- 模型：SenseVoice 约 645MB；另保留 VAD 与标点模型。旧 Paraformer 缓存不再需要
- 录像：按 1Mbps 估算每天约 7-10GB，建议定期清理 `data/recordings` 或接 NAS

## 已知限制与注意

1. **淘宝 mtop 接口是逆向接口**：接口名/签名可能随平台升级变化，届时用浏览器 DevTools
   （直播间 Network 面板）抓真实请求确认接口名，改 `config.yaml` 的 `taobao.api` 即可；
   应急可直接用 `record_now.py --url` 手动录制，流水线不停摆。
2. **风控**：接口调用保持低频（默认 60s 轮询），cookie 失效或触发滑块验证时
   程序会明确报错提示重新导出 cookie。
3. **高亮"是什么"是业务定义**：默认词典偏"转化信号"，建议跑两周后按实际数据调整。
4. **合规**：仅用于自家主播内部复盘，建议开播前向主播说明录制用途。
5. macOS 13 及以下装 torch 需锁旧版本（见 FunASR 官方文档）。

## 常见问题

- **转写结果是一整句/无标点**：确认 `config.yaml` 的 `asr` 下 `vad_model`/`punc_model` 已配置
  （一体模型必须显式挂子模型才输出逐句时间戳）。
- **探测不到流地址**：先 `record_now.py --probe 主播A` 看接口返回；检查 cookie 是否过期。
- **报告里高亮少**：调小 `highlight.merge_window`（默认 30 秒），或往词典补关键词。


## Windows 迁移（无独显纯 CPU 方案）

1. Mac 端打包：`.venv/bin/python scripts/make_migration_pack.py`（代码+配置+数据库+话术库+报告，不含录像）
2. Windows 解压后运行 `scripts/windows/setup_windows.bat`（自动装 Python/ffmpeg/Node/lark-cli + 依赖）
3. 启动：`scripts/windows/start_watcher.bat`
4. 详细步骤见 `scripts/windows/迁移到Windows.md`

注意事项：无独显用 CPU 转写（1 小时音频约 20-40 分钟，建议夜间批量）；迁移后建议在新机器重新导出淘宝 cookie；飞书授权可复制 Mac 的 `~/.lark-cli` 或重新 `lark-cli auth login`。
