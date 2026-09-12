"""AI 复盘分析：用 LLM 把一场直播的机器提取数据变成有洞察的复盘报告

输入（全部来自数据库，无需人工）：
- 场次信息（时长/句数/字数/高亮数）
- 高亮切片全文（含时间戳、类型、评分）
- 话术类别分布与高频话术
- 开场 2 分钟 + 结尾 2 分钟转写
- 全场转写抽样（控制 token）

输出：证据化结构 Markdown，固定分为数据可靠性、事实、内容、证据、假设和行动。

配置（config.yaml -> llm，兼容旧 talktrack.llm）：
  provider: deepseek | dashscope
  api_key: ""      # 留空则跳过 AI 分析，报告保持纯数据版
  model: "deepseek-chat"
失败时优雅降级：报告照常生成，只缺 AI 分析节。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Callable, Literal, Optional
from zoneinfo import ZoneInfo

from ..llm_client import (
    LLMCircuitOpen,
    _LLM_CIRCUITS,
    chat_completion as _shared_chat_completion,
    llm_config,
)
from app.intelligence.prompts.methodology import COACH_METHODOLOGY

log = logging.getLogger("review.ai")
SHANGHAI = ZoneInfo("Asia/Shanghai")

OPENING_SEC = 120   # 开场/结尾窗口（秒）
MAX_MID_CHARS = 4000  # 全场抽样文本上限
MAX_HL_CHARS = 5000   # 高亮文本上限

REPORT_TAGS = ("数据可靠性", "本场事实", "内容与话术", "高亮证据", "风险与假设", "下一场行动")

def chat_completion(cfg: dict, messages: list[dict], temperature: float = 0.3,
                    max_tokens: int = 2000,
                    thinking: Literal["enabled", "disabled"] = "enabled") -> str:
    """Historical string-returning wrapper around the shared structured client."""
    return _shared_chat_completion(
        cfg, messages, temperature=temperature, max_tokens=max_tokens, thinking=thinking,
    ).content


def _sample_transcripts(transcripts: list, opening_sec: int = OPENING_SEC) -> dict:
    """从逐句转写中提取：开场段 / 结尾段 / 中间抽样，控制 token"""
    opening_ms = opening_sec * 1000
    if not transcripts:
        return {"opening": [], "ending": [], "middle": ""}

    dur_end = transcripts[-1][1] or 0
    opening = [t for t in transcripts if t[0] < opening_ms]
    ending = [t for t in transcripts if t[0] >= dur_end - opening_ms]

    # 中间抽样：均匀取，总字数不超上限
    rest = [t for t in transcripts if t not in opening and t not in ending]
    mid_text, total = [], 0
    step = max(1, len(rest) // 40)
    for i in range(0, len(rest), step):
        line = rest[i][2]
        if total + len(line) > MAX_MID_CHARS:
            break
        mid_text.append(line)
        total += len(line)

    def fmt(rows: list) -> str:
        return "\n".join(f"[{r[0] // 1000 // 60:02d}:{r[0] // 1000 % 60:02d}] {r[2]}" for r in rows)

    return {"opening": fmt(opening), "ending": fmt(ending), "middle": "\n".join(mid_text)}


def _metrics_text(cfg: dict, store, stream_id: int) -> str:
    """从 stream_metrics 读取经营数据：聚合 + 小时级趋势 + 关键波动
    （供 LLM 做话术↔数据关联；2026-08-02 按反馈增强：不再只给聚合值）"""
    import datetime
    from ..metrics.qianniu import get_metrics, format_metrics_text
    m = get_metrics(store, stream_id)
    if not m:
        return "（本场未抓取到经营数据）"
    lines = [format_metrics_text(m)]
    raw = m.get("raw") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}

    # hybrid 存储把分钟序列放在 raw.series；旧格式则直接是类型字典。
    if isinstance(raw.get("series"), dict):
        raw = raw["series"]

    def _hms(t_ms: int) -> str:
        return datetime.datetime.fromtimestamp(int(t_ms) / 1000, SHANGHAI).strftime("%H:%M")

    # 0) 同一时间轴的分钟事实表：让模型能核对“话术时间点 vs 数据时点”，
    # 但提示中明确这只能说明先后/重合，不能证明因果。
    minute_maps: dict[str, dict[int, float]] = {"uv": {}, "itemClick": {}, "deal": {}}
    for typ, key in (("uv", "online"), ("itemClick", "value"), ("deal", "amount")):
        for row in raw.get(typ) or []:
            try:
                minute_maps[typ][int(row.get("time"))] = float(row.get(key))
            except (TypeError, ValueError):
                continue
    minute_times = sorted(set().union(*(values.keys() for values in minute_maps.values())))
    if minute_times:
        if len(minute_times) > 30:
            indexes = {round(i * (len(minute_times) - 1) / 29) for i in range(30)}
            selected_times = [minute_times[i] for i in sorted(indexes)]
        else:
            selected_times = minute_times
        minute_lines = [
            "| 时间 | 在线 | 点击 | 成交(元) |",
            "| --- | --- | --- | --- |",
        ]
        for timestamp in selected_times:
            def _cell(typ: str, digits: int = 0) -> str:
                value = minute_maps[typ].get(timestamp)
                if value is None:
                    return "暂无"
                return f"{value:.{digits}f}"
            minute_lines.append(
                f"| {_hms(timestamp)} | {_cell('uv')} | {_cell('itemClick')} | {_cell('deal', 2)} |"
            )
        lines.append(
            "分钟级同轴事实表（仅用于核对时间先后或是否重合，不代表因果）：\n"
            + "\n".join(minute_lines)
        )

    # 1) 小时级聚合表（成交/点击/平均在线），让 AI 看到全场走势
    hours: dict[str, dict] = {}
    for typ, key, label in (("uv", "online", "在线"), ("itemClick", "value", "点击"),
                            ("deal", "amount", "成交")):
        for r in raw.get(typ) or []:
            t = r.get("time", "")
            v = r.get(key, "")
            if not t or v in ("", "null"):
                continue
            try:
                hh = datetime.datetime.fromtimestamp(int(t) / 1000, SHANGHAI).strftime("%H时")
            except Exception:
                continue
            h = hours.setdefault(hh, {"在线": [], "点击": [], "成交": []})
            try:
                h[label].append(float(v))
            except ValueError:
                pass
    if hours:
        h_lines = ["| 时段 | 平均在线 | 点击 | 成交(元) |", "| --- | --- | --- | --- |"]
        for hh in sorted(hours):
            h = hours[hh]
            avg_uv = sum(h["在线"]) / len(h["在线"]) if h["在线"] else 0
            h_lines.append(f"| {hh} | {avg_uv:.1f} | {sum(h['点击']):.0f} | {sum(h['成交']):.2f} |")
        lines.append("小时级数据表：\n" + "\n".join(h_lines))
        lines.append("小时表覆盖提醒：首尾小时通常是不完整时段；未覆盖完整整点小时，不得据此比较小时表现或判断升降。")

    # 2) 关键波动：在线/点击/成交的峰值与谷值时刻（前后 3 分钟均值对比）
    for typ, key, label in (("uv", "online", "在线人数"), ("itemClick", "value", "商品点击"),
                            ("deal", "amount", "成交金额")):
        rows = raw.get(typ) or []
        pts = []
        for r in rows:
            t, v = r.get("time", ""), r.get(key, "")
            if not t or v in ("", "null"):
                continue
            try:
                pts.append((int(t), float(v)))
            except ValueError:
                pass
        if len(pts) < 5:
            continue
        pts.sort()
        mx = max(pts, key=lambda x: x[1])
        if mx[1] > 0:
            lines.append(f"{label}峰值：{_hms(mx[0])} = {mx[1]:.0f}")
        # 谷值：找最长的连续 0 段（有意义的流量真空）
        zero_run, best_run = 0, (0, 0)
        for p in pts:
            if p[1] == 0:
                zero_run += 1
            else:
                if zero_run >= 5 and zero_run > best_run[1]:
                    best_run = (p[0], zero_run)
                zero_run = 0
        if zero_run >= 5 and zero_run > best_run[1]:
            best_run = (pts[-1][0], zero_run)
        if best_run[1] >= 5:
            lines.append(f"{label}低谷：{_hms(best_run[0] - best_run[1] * 60000)}~{_hms(best_run[0])} "
                         f"连续 {best_run[1]} 分钟为 0")
    return "\n".join(lines)


def _user_system_prompt(cfg: dict) -> str:
    """config.yaml -> llm.system_prompt 用户自定义要求（会附加到内置系统提示词之后）。
    让不懂代码的运营也能直接给 DeepSeek 加要求（分析重点/语气/格式偏好等）"""
    return str((cfg.get("llm", {}) or {}).get("system_prompt", "") or "").strip()


def build_prompt(cfg: dict, summary: dict) -> list[dict]:
    """构造 LLM 提示词"""
    transcripts = summary["_transcripts"]
    samp = _sample_transcripts(transcripts)
    from ..asr.clean import (OUTWARD_DISPLAY_REPAIRED,
                             persisted_display_record_text,
                             outward_display_text, prepare_display_text)
    content_is_repaired = summary.get("_content_is_repaired") is True
    for k in ("opening", "ending", "middle"):
        v = samp.get(k)
        if v:
            samp[k] = (outward_display_text(
                v, cfg, provenance=OUTWARD_DISPLAY_REPAIRED,
                max_chars=MAX_MID_CHARS)
                if content_is_repaired else prepare_display_text(
                    v, cfg, max_chars=MAX_MID_CHARS, line_limit=None))

    hl_lines = []
    used_chars = 0
    from ..asr.clean import DISPLAY_FALLBACK, truncate_display_text
    for i, h in enumerate(summary["top_highlights"], 1):
        reasons = h.get("reasons", "")
        body = persisted_display_record_text(
            cfg, h, meta_field=("quality_meta" if h.get("kind") == "quality"
                               else "peak_meta"))
        if body == DISPLAY_FALLBACK or used_chars >= MAX_HL_CHARS:
            continue
        if used_chars + len(body) > MAX_HL_CHARS:
            body = truncate_display_text(body, max_chars=MAX_HL_CHARS - used_chars)
        if body == DISPLAY_FALLBACK:
            continue
        used_chars += len(body)
        hl_lines.append(f"{i}. [{h['time']}] {reasons}（{h['score']:.1f}）: {body}")
    hl_text = "\n".join(hl_lines) if hl_lines else "（无可确认的高亮转写）"

    cat = summary.get("categories", {})
    cat_text = "、".join(f"{k} {v}次" for k, v in cat.items() if v) or "无"
    top_tracks = summary.get("top_talktracks", [])
    tt_rows = []
    for item in top_tracks[:8]:
        body = persisted_display_record_text(
            cfg, item, field="text", max_chars=500)
        if body:
            tt_rows.append(
                f"- {item['category']}（历史累计复用 {item['use_count']} 次）：{body}")
    tt_text = "\n".join(tt_rows) or "（无）"

    sys_prompt = (
        "你是直播电商巡检分析师。当前尚未提供主播排班和业务 KPI，因此不得评价本场『行/不行』、"
        "『优秀/不佳』或是否达标，只能做事实复盘。你只能使用输入中的经营数据和完整转写；"
        "当前没有主播排班，系统的开始/结束时间只能称为『本地录制区间』，不得称为主播实际开播、"
        "在场或下播时间，也不得猜测差异时段是挂机或非直播流量。"
        "引用补全展示文本必须带时间点并保持输入原意。数据缺失、口径为部分数据或来源时段不同，必须首先说明，"
        "不得用 0 代替缺失值，也不得据此推断转化表现。\n"
        "严格遵守合规边界：不得编造库存、限量名额、订单、购买人数、优惠规则或用户心理；"
        "不得建议主播说未经证实的『只剩X件』『已有X人下单』『最后X个名额』。"
        "观察、假设和结论必须分开：相关性不能写成因果；每个假设都要写出还需核验什么。"
        "没有分钟级话术与成交对齐证据时，禁止写某话术带来、提升、降低或未带来转化；"
        "不得凭空规定『控制在X次』『开播后X分钟内』等数字化动作门槛。"
        "【下一场行动】中禁止自行创建10/5/1分钟等提醒频次或任何N分钟/N小时期限；"
        "只使用事件触发条件。"
        "不得写用户会困惑、有压力、体验更好/更差等主观感受，也不得把此类表述包装成行动理由。"
        "不得猜测主播疲劳、紧张、疏忽等内部状态。时间简称与完整写法指向同一时刻时（如11:59与59分），"
        "不能称为规则不一致。"
        "转写来自自动语音识别，音近字、数字断裂、异常英文等只能标为『ASR疑点、需回听』，"
        "未经回听不得认定为主播口误、规则矛盾或错误表述，也不得把疑似错句整理成确定的活动规则。\n"
    ) + COACH_METHODOLOGY + (
        "\n输出严格 Markdown，总字数 900-1400 个中文字符，且按以下六个标记逐段输出，缺一不可："
        "【数据可靠性】说明来源、覆盖时段、可用字段、缺失字段及哪些分析受限；"
        "【本场事实】完整复述核心经营数据与走势，不做价值判断；"
        "【内容与话术】按上述教练维度逐条对照，未观测项写「本场未观测」；概括本场商品/活动内容、"
        "话术结构与重复模式，不把历史累计话术冒充本场原话；"
        "【高亮证据】给出 3-5 个时间点和完整语义引用，解释为何值得关注；"
        "【风险与假设】逐条写『已观察到 / 可能假设 / 待核验』，不得下无证据因果结论；"
        "【下一场行动】给 3-5 条可执行动作，每条说明触发时机和依据，口播必须诚实且不虚构数字。"
    )
    extra = _user_system_prompt(cfg)
    if extra:
        sys_prompt += "\n" + extra
    user_prompt = f"""请为以下这场直播写专业复盘分析。

## 场次信息
主播：{summary['anchor_name']}　场次：#{summary['stream_id']}
本地录制区间 {summary['started_at']} ~ {summary['ended_at']}，本地录制时长 {summary['duration_text']}
（没有排班，以上时间不得解释为主播实际开播/下播或在场时段）
转写 {summary['sentence_count']} 句 / {summary['char_count']} 字，高亮 {summary['highlight_count']} 个

## 经营数据（千牛数据大屏）
{summary.get('metrics_text', '（无）')}

## 话术类别分布
{cat_text}

## 本场识别到的话术（括号内仅为历史累计复用次数）
{tt_text}

## 高亮片段（含时间戳）
{hl_text}

## 开场 2 分钟
{samp['opening'] or '（无）'}

## 结尾 2 分钟
{samp['ending'] or '（无）'}

## 全场转写抽样
{samp['middle'] or '（无）'}

请按六段格式输出完整复盘；引用原文和数据时标注时间点。"""
    return [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt},
    ]


def extract_points(text: str, preserve_newlines: bool = False) -> dict[str, str]:
    """从 AI 输出中提取结构段落；默认压平换行，保持既有调用兼容。"""
    points: dict[str, str] = {}
    for tag in (*REPORT_TAGS, "整体评价", "亮点", "问题", "改进建议", "结论"):
        m = re.search(rf"【{tag}】\s*(.+?)(?=【|$)", text, re.S)
        if m:
            value = m.group(1).strip()
            points[tag] = value if preserve_newlines else value.replace("\n", " ")
    return points


def _compliance_problems(text: str, tags: tuple[str, ...], min_chars: int) -> list[str]:
    problems = [f"缺少【{tag}】" for tag in tags if f"【{tag}】" not in text]
    if len(text.strip()) < min_chars:
        problems.append(f"内容过短（{len(text.strip())} 字）")
    forbidden = (
        r"只剩\s*\d+\s*(?:件|单|个|名)", r"已有\s*\d+\s*人.*(?:下单|购买)",
        r"最后\s*\d+\s*(?:件|个|名|单)", r"库存(?:仅|只剩)?\s*\d+",
    )
    for pattern in forbidden:
        if re.search(pattern, text):
            problems.append(f"包含未经证实的稀缺/订单数字：{pattern}")
    # 用户心理不可由口播和经营数据反推；任何此类分析都要求重写为可观察行为。
    if re.search(r"(?:用户|消费者|下单|购买)?心理", text):
        problems.append("包含未经证实的用户/下单心理推断")
    if re.search(
        r"(?:强化|降低|提升|影响|激发|迎合|改变).{0,12}(?:用户|观众|消费者)"
        r".{0,10}(?:信任|兴趣|意愿|偏好|认知|感受|需求)", text,
    ):
        problems.append("包含未经证实的用户认知/信任/意愿推断")
    if re.search(
        r"(?:用户|观众|消费者).{0,12}(?:困惑|压力|体验|信任|兴趣|意愿|认知|感受|理解|误解|决策)",
        text,
    ):
        problems.append("包含未经证实的用户感受/体验推断")
    if re.search(r"(?:增强|提升|降低).{0,10}(?:可信度|信任感)", text):
        problems.append("包含未经证实的可信度/信任判断")
    if "假下播" in text:
        problems.append("使用了无证据且带定性的『假下播』标签")
    if re.search(r"(?:拉新|分享|互动|催付|讲解|引导).{0,8}(?:不足|较差|不佳)|缺乏.{0,8}(?:拉新|分享).{0,8}(?:引导|激励)|(?:互动|流量|转化).{0,8}(?:活跃度)?低", text):
        problems.append("没有基准却使用『不足/较差』等价值判断")
    if re.search(r"10\s*时.{0,30}(?:流量减少|转化停滞|表现下降|转化下降)", text):
        problems.append("用仅覆盖开头几分钟的10时数据判断整点表现")
    if re.search(r"结尾\s*2\s*分钟.{0,45}(?:对应|重合|带来|促成).{0,12}成交峰值", text):
        problems.append("把未重合的结尾2分钟与成交峰值强行关联")
    if "稀缺性表述" in text:
        problems.append("把普通福利/价格表述无依据标成稀缺性")
    if re.search(r"主播.{0,12}(?:疲劳|紧张|疏忽|记错|忘记)", text):
        problems.append("包含未经证实的主播内部状态推断")
    for match in re.finditer(r"退款少于两次|退款次数限制|退款后仍可参与抽奖", text):
        context = text[max(0, match.start() - 45):match.end() + 45]
        if not re.search(r"ASR|疑点|回听|核验|确认|是否|语义不清|原话|引号", context):
            problems.append("把 ASR 可疑句整理成了确定的退款规则")
            break
    for match in re.finditer(r"主播实际(?:开播|在场|下播|结束)|挂机时段|非直播时段流量", text):
        before = text[max(0, match.start() - 35):match.start()]
        if not re.search(r"非|不是|不代表|不得|不能|不可|无法|无排班|未提供排班", before):
            problems.append("没有排班却把本地录制区间解释为主播实际时段")
            break
    same_time_forms = (
        re.search(r"(?:11\s*点\s*59|11[:：]59)", text)
        and re.search(r"(?:59\s*分|59\s*就)\s*开奖", text)
    )
    if same_time_forms:
        for match in re.finditer(r"(?:开奖时间|时间表述).{0,35}(?:不一致|差异|矛盾)", text):
            context = text[max(0, match.start() - 35):match.end()]
            if not re.search(r"不能|不应|不得|并非|不构成|不属于|无需", context):
                problems.append("把同一开奖时间的完整/简称表述误判为不一致")
                break
    if re.search(r"(?:11\s*点\s*59|11[:：]59|59\s*开奖).{0,80}00[:：]59", text):
        problems.append("把墙上时钟的 23:59 错写成 00:59")
    if (re.search(r"(?:主播)?口误|规则(?:矛盾|不一致)|口径不一致", text)
            and not re.search(r"(?:ASR|自动转写|语音识别).{0,20}(?:疑点|误差|错误|需回听)", text)):
        problems.append("把可能的 ASR 误差直接认定为主播口误/规则矛盾")
    # 没有分钟级话术-成交对齐与对照实验时，不能把内容动作归因到经营结果。
    outcome_patterns = (
        r"(?:带来|带动|促进|提升|拉动|降低|拉低|影响|导致|驱动|促成|刺激|促使).{0,18}(?:转化|成交|下单|销量|关注|分享)",
        r"(?:转化|成交|下单|销量).{0,18}(?:由|得益于|源于|归因于)",
        r"未带来.{0,18}(?:提升|增长|转化|成交)",
    )
    for pattern in outcome_patterns:
        for match in re.finditer(pattern, text):
            before = text[max(0, match.start() - 45):match.start()]
            after = text[match.end():match.end() + 45]
            # “无法判断是否影响转化”是数据限制，不是因果结论；风险段中明确标为
            # 可能假设且附待核验项也允许保留。
            negated = re.search(r"无法|不能|不可|禁止|不应|不推断|尚不能|无.{0,8}证据|是否", before)
            hypothesis = (re.search(r"可能假设|假设|可能", before)
                          and re.search(r"待核验|需核验|无法证实|无法验证|尚无证据", after))
            if not negated and not hypothesis:
                problems.append(f"包含无对齐证据的经营结果归因：{pattern}")
                break
    actions = extract_points(text).get("下一场行动", "") or extract_points(text).get("下一小时建议", "")
    if re.search(r"(?:控制在|限定为|最多|至少)\s*\d+\s*(?:次|分钟|小时)", actions):
        problems.append("行动建议包含无依据的数字化门槛")
    if re.search(r"\d+\s*(?:分钟|小时)(?:内|以内)?", actions):
        problems.append("行动建议包含输入未提供依据的分钟门槛")
    return problems


def _metric_consistency_problems(text: str, metrics: dict | None) -> list[str]:
    """逐项核对 AI 复述的经营指标，避免模型改数值或编造指标分母。"""
    if not metrics:
        return []
    problems: list[str] = []
    fields = {
        "最高在线": ("max_online_uv", 0, 0.0),
        "观看人数": ("viewer_uv", 0, 0.0),
        "观看次数": ("viewer_pv", 0, 0.0),
        "时段进入人次": ("visitor_total", 0, 0.0),
        "商品点击次数": ("ipv_total", 0, 0.0),
        "商品点击人数": ("ipv_uv", 0, 0.0),
        "成交金额": ("pay_amt", 2, 0.01),
        "成交人数": ("buyer_cnt", 0, 0.0),
        "成交订单数": ("order_cnt", 0, 0.0),
        "成交件数": ("item_qty", 0, 0.0),
        "新增粉丝": ("atn_uv", 0, 0.0),
        "评论人数": ("comment_uv", 0, 0.0),
        "点赞人数": ("favor_uv", 0, 0.0),
        "分享人数": ("share_uv", 0, 0.0),
        "成交转化率": ("pay_byr_rate", 2, 0.01),
        "商品点击率": ("ipv_uv_rate", 2, 0.01),
    }
    missing_claim = re.compile(r"(?:缺失字段|字段缺失|无数据)[^。；;\n]{0,120}")
    for claim in missing_claim.findall(text):
        for label, (key, _digits, _tolerance) in fields.items():
            if metrics.get(key) is not None and label in claim:
                problems.append(f"把已有指标误报为缺失：{label}")
    raw = metrics.get("raw") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    series = raw.get("series", raw) if isinstance(raw, dict) else {}
    if isinstance(series, dict):
        for series_key, label in (("deal", "成交"), ("uv", "在线"), ("itemClick", "点击")):
            if not series.get(series_key):
                continue
            pattern = rf"(?:缺失|没有|未提供|无).{{0,18}}分钟级[^。\n]{{0,20}}{label}(?:明细|数据)?"
            for match in re.finditer(pattern, text):
                context = text[max(0, match.start() - 20):match.end() + 25]
                # 有分钟趋势不等于已经完成“话术-成交”对齐；后者仍可如实写缺失。
                if re.search(r"对齐|匹配|话术", context):
                    continue
                problems.append(f"已有分钟{label}趋势却误报为缺失")
                break
    for label, (key, _digits, tolerance) in fields.items():
        expected = metrics.get(key)
        if expected is None:
            continue
        expected = float(expected) * 100 if key.endswith("_rate") else float(expected)
        pattern = rf"{label}(?:（[^）]*）)?\s*(?:为|[:：])?\s*([\d,]+(?:\.\d+)?)\s*%?"
        for match in re.finditer(pattern, text):
            context = text[max(0, match.start() - 25):match.end() + 25]
            # 小时表、峰谷和增量是局部值，不能拿场次累计值去判错。
            if re.search(r"\d{1,2}\s*时|低谷|峰值|增量|变化|分钟|分时|该时段|本小时", context):
                continue
            actual = float(match.group(1).replace(",", ""))
            if abs(actual - expected) > tolerance:
                problems.append(
                    f"{label}复述错误：报告写 {actual:g}，输入为 {expected:g}"
                )
                break
    if re.search(
        r"成交转化率.{0,24}(?:按|=|等于|即).{0,12}(?:成交人数.{0,6})?"
        r"(?:观看人数|观看次数|商品点击次数)", text,
    ):
        problems.append("成交转化率分母口径错误，应为成交人数/商品点击人数")
    if re.search(r"商品点击率.{0,24}(?:按|=|等于|即).{0,20}(?:/|除以)", text):
        problems.append("擅自解释商品点击率分母；应保留为千牛平台字段")
    return list(dict.fromkeys(problems))


def _sanitize_action_timing(text: str) -> str:
    """移除模型凭空创建的分钟/小时动作门槛，避免合格正文因单个坏建议整段降级。"""
    section = re.search(r"【(下一场行动|下一小时建议)】\s*(.*?)(?=\n【|$)", text, re.S)
    if not section:
        return text
    tag, body = section.group(1), section.group(2).strip()
    items = re.split(r"(?m)(?=^\s*\d+[.、]\s*)", body)
    items = [item.strip() for item in items if item.strip()]
    if not items:
        return text
    duration = re.compile(r"\d+\s*(?:分钟|小时)(?:内|以内)?")
    kept = [item for item in items if not duration.search(item)]
    if len(kept) >= 2:
        cleaned = []
        for index, item in enumerate(kept, 1):
            item = re.sub(r"^\s*\d+[.、]\s*", "", item)
            cleaned.append(f"{index}. {item}")
        new_body = "\n".join(cleaned)
    else:
        if tag == "下一小时建议":
            defaults = (
                "首次完整讲解活动规则时，仅复述已由运营确认的条件；信息未确认就明确待核验。",
                "发现音近字、数字断裂或异常英文时，先标记ASR疑点并回听，不把转写错字当成口误。",
                "取得同口径时段数据后再对照话术时间点；证据不足时继续记录，不判断转化因果。",
            )
        else:
            defaults = (
                "开播前由运营核对活动规则与商品信息，主播首次完整讲解时使用同一版确认口径。",
                "复盘发现音近字、数字断裂或异常英文时，先回听原音再更新规则与话术结论。",
                "取得同口径分钟数据后再对照话术时间点；没有对齐证据时只记录事实，不判断转化因果。",
            )
        new_body = "\n".join(f"{i}. {item}" for i, item in enumerate(defaults, 1))
    return text[:section.start(2)] + new_body + text[section.end(2):]


def _sanitize_subjective_claims(text: str) -> str:
    """逐句剔除无行为证据的用户心理/体验套话，保留所在结构段和其他事实。"""
    subjective = re.compile(
        r"(?:用户|观众|消费者).{0,18}(?:困惑|压力|体验|信任|兴趣|意愿|认知|感受|理解|误解|决策)"
        r"|(?:增强|提升|降低).{0,10}(?:可信度|信任感)"
        r"|(?:拉新|分享|互动|催付|讲解|引导).{0,8}(?:不足|较差|不佳)"
        r"|缺乏.{0,8}(?:拉新|分享).{0,8}(?:引导|激励)"
        r"|(?:互动|流量|转化).{0,8}(?:活跃度)?低"
    )
    parts = re.split(r"(【[^】]+】)", text)
    for index in range(2, len(parts), 2):
        body = parts[index]
        sentences = re.split(r"(?<=[。！？!?])", body)
        kept = [sentence for sentence in sentences if not subjective.search(sentence)]
        parts[index] = "".join(kept)
    return "".join(parts)


def _sanitize_schedule_claims(text: str) -> str:
    """没有排班时，删除把本地录制区间冒充主播实际班次的句子并补充安全口径。"""
    forbidden = re.compile(r"主播实际(?:开播|在场|下播|结束)|挂机时段|非直播时段流量")
    negated = re.compile(r"非|不是|不代表|不得|不能|不可|无法|无排班|未提供排班")
    parts = re.split(r"(【[^】]+】)", text)
    changed = False
    for index in range(2, len(parts), 2):
        sentences = re.split(r"(?<=[。！？!?])", parts[index])
        kept = []
        for sentence in sentences:
            match = forbidden.search(sentence)
            if match and not negated.search(sentence[:match.start()]):
                changed = True
                continue
            kept.append(sentence)
        parts[index] = "".join(kept)
    cleaned = "".join(parts)
    if changed and "【数据可靠性】" in cleaned:
        cleaned = cleaned.replace(
            "【数据可靠性】",
            "【数据可靠性】\n本地录制区间仅代表系统录制覆盖，不代表主播实际班次。",
            1,
        )
    return cleaned


def _sanitize_unsupported_outcomes(text: str) -> str:
    """删除把话术直接归因到成交/关注结果、且没有否定或待核验限定的句子。"""
    causal = re.compile(
        r"(?:带来|带动|促进|提升|拉动|降低|拉低|影响|导致|驱动|促成|刺激|促使)"
        r".{0,18}(?:转化|成交|下单|销量|关注|分享)"
        r"|(?:转化|成交|下单|销量).{0,18}(?:由|得益于|源于|归因于)"
        r"|未带来.{0,18}(?:提升|增长|转化|成交)"
    )
    safe = re.compile(r"无法|不能|不可|禁止|不应|不推断|尚不能|无.{0,8}证据|是否|待核验|需核验|无法验证|可能假设")
    parts = re.split(r"(【[^】]+】)", text)
    for index in range(2, len(parts), 2):
        sentences = re.split(r"(?<=[。！？!?])", parts[index])
        parts[index] = "".join(
            sentence for sentence in sentences
            if not (causal.search(sentence) and not safe.search(sentence))
        )
    return "".join(parts)


def _grounded_report(summary: dict, metrics: dict | None, cfg: dict | None = None) -> str:
    """LLM 不可用/不合规时的完整证据版，不把整份报告降成一行错误提示。"""
    from ..metrics.qianniu import format_metrics_text

    cfg = cfg or {}
    metrics_text = format_metrics_text(metrics) if metrics else "（未抓取到经营数据）"
    categories = summary.get("categories") or {}
    category_text = "、".join(f"{name}{count}次" for name, count in categories.items() if count) or "暂无"
    highlights = summary.get("top_highlights") or []
    from ..asr.clean import persisted_display_record_text
    evidence = []
    for index, item in enumerate(highlights[:5], 1):
        body = persisted_display_record_text(
            cfg, item, meta_field=("quality_meta" if item.get("kind") == "quality"
                                   else "peak_meta"), max_chars=360)
        parts = [body] if body else []
        if not parts:
            continue
        evidence.append(
            f"{index}. [{item.get('time') or '时间暂无'}] {' / '.join(parts)}"
            f"（系统标注：{item.get('reasons') or '未分类'}；评分{float(item.get('score') or 0):.1f}）"
        )
    evidence_text = "\n".join(evidence) or "（本地录制段内暂无可引用高亮；继续保留原始转写供回听。）"
    tracks = summary.get("top_talktracks") or []
    track_rows = []
    for item in tracks[:6]:
        body = persisted_display_record_text(
            cfg, item, field="text", max_chars=500)
        if body:
            track_rows.append(f"{item.get('category') or '未分类'}：{body}")
    track_text = "；".join(track_rows) or "本段未形成可复用话术条目。"
    return (
        "【数据可靠性】\n"
        f"{metrics_text}\n本地录制区间仅代表系统录制覆盖，不代表主播实际班次。"
        "缺失值保持为“暂无”，不以0替代；分钟趋势只能核对时间先后，不能证明话术与成交的因果。\n\n"
        "【本场事实】\n"
        f"主播标识：{summary.get('anchor_name') or '未配置'}；场次#{summary.get('stream_id')}；"
        f"本地录制区间：{summary.get('started_at') or '暂无'}至{summary.get('ended_at') or '暂无'}；"
        f"录制时长：{summary.get('duration_text') or '暂无'}。转写{summary.get('sentence_count', 0)}句/"
        f"{summary.get('char_count', 0)}字，高亮{summary.get('highlight_count', 0)}个，"
        f"本场识别话术{summary.get('new_talktracks', 0)}条。话术类别：{category_text}。\n\n"
        "以上计数分别来自平台接口、数据库转写和规则分类；来源不同的字段不相互替代，后续补数时应保留原始口径与抓取时间。\n\n"
        "【内容与话术】\n"
        f"以下仅为本地转写与系统分类中的可观察内容，不代表历史场次：{track_text}"
        "自动转写中的音近字、数字断裂和异常英文均属于ASR疑点，未经回听不整理为确定规则。\n\n"
        "【高亮证据】\n"
        f"{evidence_text}\n\n"
        "【风险与假设】\n"
        "已观察到：经营数据、话术分类和高亮证据的来源口径不同，且自动转写可能含识别误差。"
        "可能假设：本证据版不生成话术效果假设，避免把同时发生误写成因果。"
        "待核验：回听ASR疑点；等待平台冻结数据补齐缺失指标；有同口径分钟数据后再核对话术时间点。\n\n"
        "【下一场行动】\n"
        "1. 开播前由运营核对商品、优惠、抽奖与发货规则，主播首次完整讲解时使用同一确认口径。\n"
        "2. 出现音近字、数字断裂或异常英文时，先回听原音，再更新规则与话术结论。\n"
        "3. 平台冻结数据返回后补齐订单数、点击人数等缺失字段，并保留来源与覆盖时段。\n"
        "4. 取得同口径分钟数据后再对照话术时间点；证据不足时只记录事实，不评价转化效果。"
    )



def _review_or_repair(cfg: dict, messages: list[dict], text: str,
                      tags: tuple[str, ...], min_chars: int,
                      max_tokens: int,
                      extra_check: Callable[[str], list[str]] | None = None, *,
                      thinking: Literal["enabled", "disabled"] = "enabled") -> str:
    """结构/合规审校；不合格时让同一模型带着明确问题重写一次。"""
    candidate = text
    for attempt in range(4):
        candidate = _sanitize_schedule_claims(candidate)
        candidate = _sanitize_subjective_claims(candidate)
        candidate = _sanitize_unsupported_outcomes(candidate)
        candidate = _sanitize_action_timing(candidate)
        problems = _compliance_problems(candidate, tags, min_chars)
        if extra_check:
            problems.extend(extra_check(candidate))
        if not problems:
            return candidate
        if attempt >= 3:
            raise RuntimeError("AI 输出四次未通过结构/合规审校：" + "；".join(problems))
        log.warning("AI 输出审校未通过，触发第 %d 次重写：%s",
                    attempt + 1, "；".join(problems))
        candidate = chat_completion(cfg, [
            *messages,
            {"role": "assistant", "content": candidate},
            {"role": "user", "content": (
                "上版未通过系统审校：" + "；".join(problems) +
                "。请完全重写，严格保留规定的所有分段，不引用或建议任何未经输入证实的库存、"
                "名额、订单或用户心理；没有分钟级对齐证据时不要把话术归因于转化/成交结果，"
                "不要推断用户信任、兴趣、意愿或认知，也不要凭空规定动作次数；"
                "不得写用户会困惑、有压力或体验变好/变差；"
                "【下一场行动】里删除所有N分钟/N小时安排（包括10/5/1分钟提醒），改成事件触发；"
                "不要猜测主播疲劳、紧张或疏忽；同一时刻的完整写法与简称不能判为矛盾；"
                "没有排班，本地开始/结束只能称为录制区间，不能称主播实际时段或挂机；"
                "行动触发时机请优先写成『首次完整讲解后』『用户追问时』等事件条件；"
                "转写疑似错字只能标记为 ASR 疑点并要求回听，不能据此整理出确定规则；"
                "不要使用『假下播』等定性标签，也不要解释审校过程。"
            )},
        ], temperature=0.0, max_tokens=max_tokens, thinking=thinking)
    return candidate


def generate_ai_review(cfg: dict, store, stream_id: int) -> str:
    """生成 AI 复盘分析 Markdown。未配置 key 或调用失败时返回提示文本（不抛异常）"""
    llm = llm_config(cfg)
    if not (llm.get("api_key") or "").strip():
        return "> ⚠️ 未配置 LLM API Key（config.yaml → llm.api_key），跳过 AI 分析。配置后每场自动生成。"

    from ..notify.feishu import build_summary  # 复用摘要构建

    summary = None
    metrics = None
    try:
        summary = build_summary(cfg, store, stream_id)
        summary["_transcripts"] = store.get_display_sentences(stream_id)
        summary["_content_is_repaired"] = True
        # 只把本场实际出现/更新的话术传给 AI，历史库不能冒充本场内容。
        summary["top_talktracks"] = [
            {"category": t["category"], "use_count": t["use_count"],
             "text": t["text"], "text_provenance": t["text_provenance"]}
            for t in store.get_talktracks(anchor_id=store.get_stream(stream_id)["anchor_id"])
            if t["stream_id"] == stream_id
            and str(t["text_provenance"] or "") == "display_repaired"
        ][:15]
        summary["metrics_text"] = _metrics_text(cfg, store, stream_id)
        messages = build_prompt(cfg, summary)
        text = chat_completion(cfg, messages, temperature=0.2, max_tokens=3200)
        from ..metrics.qianniu import get_metrics
        metrics = get_metrics(store, stream_id)
        text = _review_or_repair(
            cfg, messages, text, REPORT_TAGS, 700, 3200,
            extra_check=lambda candidate: _metric_consistency_problems(candidate, metrics),
        )
        log.info("场次 #%d AI 复盘分析完成（%d 字）", stream_id, len(text))
        return text
    except Exception as e:
        log.warning("场次 #%d AI 复盘分析失败，切换完整证据版: %s", stream_id, e)
        if summary is None:
            summary = build_summary(cfg, store, stream_id)
            summary["top_talktracks"] = []
        if metrics is None:
            from ..metrics.qianniu import get_metrics
            metrics = get_metrics(store, stream_id)
        return _grounded_report(summary, metrics, cfg)


def _anchor_metrics_text(metrics: dict) -> str:
    """主播上下钟官方指标文本；缺失与真实 0 严格区分。"""
    def show(key: str, unit: str = "", decimals: int = 0) -> str:
        value = metrics.get(key)
        if value is None:
            return "暂无"
        return f"{float(value):,.{decimals}f}{unit}"

    return (
        f"数据状态：{metrics.get('data_state') or 'unknown'}；"
        f"来源：{metrics.get('source') or '暂无'}；"
        f"成交金额 {show('pay_amt', '元', 2)}；观看人数 {show('look_uv', '人')}；"
        f"观看次数 {show('look_pv', '次')}；成交人数 {show('pay_byr_cnt', '人')}；"
        f"成交订单 {show('pay_ord_cnt', '单')}；成交件数 {show('pay_itm_qty', '件')}；"
        f"商品点击人数 {show('ipv_uv', '人')}；商品点击次数 {show('ipv', '次')}；"
        f"成交转化率 {show('cvr_pay', '%', 2)}；客单价 {show('atv', '元', 2)}。"
    )


def _outward_anchor_rows(cfg: dict, rows: list[dict], *, field: str,
                         meta_field: str = "", limit: int = 240) -> list[tuple[str, str]]:
    from ..asr.clean import outward_record_text

    visible: list[tuple[str, str]] = []
    for row in rows[:limit]:
        body = outward_record_text(
            cfg, row, field=field, meta_field=meta_field, max_chars=500)
        if body:
            visible.append((str(row.get("clock") or "时间暂无"), body))
    return visible


def _grounded_anchor_review(cfg: dict, summary: dict) -> str:
    metrics = summary.get("metrics") or {}
    transcript_rows = _outward_anchor_rows(
        cfg, summary.get("transcripts") or [], field="text", limit=5)
    quotes = "\n".join(
        f"- [{clock}] {body}" for clock, body in transcript_rows
    ) or "- 【待回听确认】"
    categories = "、".join(
        f"{name}{count}次" for name, count in (summary.get("categories") or {}).items()
    ) or "暂无"
    duration_min = float(summary.get("duration_sec") or 0) / 60
    return (
        "【数据可靠性】\n"
        f"{_anchor_metrics_text(metrics)}同一主播的上下钟分段已按平台场次归并；人数类分段合计"
        "可能包含跨班次重复用户。缺失项保持为“暂无”，不按时长分摊整场总账。\n\n"
        "【本场事实】\n"
        f"主播：{summary.get('anchor_name') or '未知主播'}；本平台场次内共覆盖"
        f"{int(summary.get('fragment_count') or 0)}个本地录像碎片，有效媒体时长"
        f"{duration_min:.1f}分钟；转写{int(summary.get('sentence_count') or 0)}句/"
        f"{int(summary.get('char_count') or 0)}字。{_anchor_metrics_text(metrics)}\n\n"
        "【内容与话术】\n"
        f"系统分类计数：{categories}。以下内容已按真实墙钟时间合并，不把技术碎片当成独立班次。\n"
        f"{quotes}\n\n"
        "【高亮证据】\n"
        f"{quotes}\n以上只证明该主播在对应时间说过这些内容；若附近存在经营高点，也只表示时间先后，"
        "不代表话术导致成交或点击。\n\n"
        "【风险与假设】\n"
        "已观察到：录像可能因轮换、休眠或网络故障被技术切分。可能假设：本证据版不生成"
        "话术效果假设。待核验：自动转写疑点需回听；官方结算缺失项等待平台回填。\n\n"
        "【下一场行动】\n"
        "1. 首次完整讲解商品时，按已确认口径说明产品结构、适用场景和操作动作。\n"
        "2. 用户追问活动或商品参数时，复述已确认规则；未确认信息明确待核验。\n"
        "3. 下一场继续记录话术时间点与同口径经营数据，只比较时间先后，不判断无证据因果。"
    )


def build_anchor_prompt(cfg: dict, summary: dict) -> list[dict]:
    """为同一主播在一个 liveId 内的全部碎片构造一次合并复盘提示词。"""
    transcript_rows = _outward_anchor_rows(
        cfg, summary.get("transcripts") or [], field="text")
    transcript_text = "\n".join(
        f"[{clock}] {body}" for clock, body in transcript_rows
    ) or "（无可用转写）"
    quality_rows = _outward_anchor_rows(
        cfg, summary.get("quality_highlights") or [], field="transcript",
        meta_field="quality_meta", limit=5)
    quality_text = "\n".join(
        f"[{clock}] {body}" for clock, body in quality_rows
    ) or "（暂无）"
    data_rows = _outward_anchor_rows(
        cfg, summary.get("data_highlights") or [], field="transcript",
        meta_field="peak_meta", limit=5)
    data_text = "\n".join(
        f"[{clock}] {body}" for clock, body in data_rows
    ) or "（暂无）"
    system = (
        "你是直播电商巡检分析师。请只分析同一平台直播场次内该主播全部已覆盖片段，"
        "技术碎片已经按墙钟顺序合并，不得把碎片当成多位主播或多个独立复盘。"
        "没有 KPI 时不得评价好坏或达标。缺失值不是0，人数类上下钟分段合计可能重复，"
        "不得擅自改写为整场去重人数。相关性不代表因果；不得虚构库存、订单、优惠、购买人数"
        "或用户心理。输出必须严格包含六段：数据可靠性、本场事实、内容与话术、高亮证据、"
        "风险与假设、下一场行动。"
    )
    extra = _user_system_prompt(cfg)
    if extra:
        system += "\n" + extra
    user = (
        f"主播：{summary.get('anchor_name') or '未知主播'}\n"
        f"平台场次：{summary.get('live_id') or '暂无'}\n"
        f"覆盖时间：{summary.get('started_at') or '暂无'} ~ {summary.get('ended_at') or '暂无'}\n"
        f"有效媒体时长：{float(summary.get('duration_sec') or 0) / 60:.1f}分钟\n"
        f"经营数据：{_anchor_metrics_text(summary.get('metrics') or {})}\n\n"
        f"数据关联话术：\n{data_text}\n\n优质话术：\n{quality_text}\n\n"
        f"合并转写：\n{transcript_text}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def generate_anchor_review(cfg: dict, anchor_summary: dict) -> str:
    """同一主播全部技术碎片只生成一份最终 AI 复盘。"""
    llm = llm_config(cfg)
    if not (llm.get("api_key") or "").strip():
        return _grounded_anchor_review(cfg, anchor_summary)
    messages = build_anchor_prompt(cfg, anchor_summary)
    metrics = anchor_summary.get("metrics") or {}
    normalized_metrics = {
        "pay_amt": metrics.get("pay_amt"),
        "viewer_uv": metrics.get("look_uv"),
        "viewer_pv": metrics.get("look_pv"),
        "buyer_cnt": metrics.get("pay_byr_cnt"),
        "order_cnt": metrics.get("pay_ord_cnt"),
        "item_qty": metrics.get("pay_itm_qty"),
        "ipv_uv": metrics.get("ipv_uv"),
        "ipv_total": metrics.get("ipv"),
        "pay_byr_rate": metrics.get("cvr_pay"),
    }
    try:
        text = chat_completion(cfg, messages, temperature=0.2, max_tokens=3200)
        return _review_or_repair(
            cfg, messages, text, REPORT_TAGS, 700, 3200,
            extra_check=lambda candidate: _metric_consistency_problems(
                candidate, normalized_metrics),
        )
    except Exception as exc:
        log.warning("主播 %s 聚合 AI 复盘失败，切换完整证据版: %s",
                    anchor_summary.get("anchor_name") or "未知", exc)
        return _grounded_anchor_review(cfg, anchor_summary)
