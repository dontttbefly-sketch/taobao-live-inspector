"""数据驱动高亮：以经营数据峰值为锚点，摘取对应的主播话术。"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("highlight.peak")

WINDOW_BEFORE_MS = 3 * 60 * 1000    # 峰值前 3 分钟（2026-08-04 用户确认：贴近峰值的那几分钟）
WINDOW_AFTER_MS = 60 * 1000         # 只保留峰值所在自然分钟作上下文
MERGE_MS = 10 * 60 * 1000           # 同类高点 10 分钟内只保留最强一个
MAX_EXCERPTS = 3
MAX_EXCERPT_CHARS = 180
MIN_EXCERPT_GAP_MS = 25 * 1000

# 数据源 -> (类型名, 主值字段, 基础评分, 标签, 单位)
PEAK_SOURCES = (
    ("deal", "amount", 6.0, "成交峰值", "元"),
    ("itemClick", "value", 5.0, "点击峰值", "次"),
    ("uv", "online", 4.0, "在线峰值", "人"),
)
DATA_PEAK_LABELS = tuple(label for _, _, _, label, _ in PEAK_SOURCES)
SHANGHAI = ZoneInfo("Asia/Shanghai")

# 不依赖具体品类的直播讲解信号；业务词典会在调用处补充。
HOST_SIGNALS = (
    "宝贝", "直播间", "链接", "下单", "拍", "价格", "活动", "优惠", "礼赠",
    "福利", "手机壳", "壳", "充电宝", "三合一", "防摔", "磁吸", "颜色", "库存",
    "抽奖", "价保", "商品", "系列", "款", "礼物",
)
NOISE_PATTERNS = (
    r"[A-Z]{5,}",
    r"走机场|开会|上班|我们俩很累|谁还会|你点白了吗|我说你要吃得好",
)


def _parse_raw(raw: dict) -> dict[str, list[tuple[int, float]]]:
    """raw 分钟桶 → {类型: [(分钟起始ms, 值), ...]}，按时间排序。"""
    import json

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    # 下播复盘的 hybrid/frozen 数据把分钟趋势放在 raw.series；保留对旧版直接
    # 放在 raw 根节点的兼容，避免历史数据无法生成峰值话术。
    if isinstance(raw, dict) and isinstance(raw.get("series"), dict):
        raw = raw["series"]
    if not isinstance(raw, dict):
        raw = {}
    out: dict[str, list[tuple[int, float]]] = {}
    for typ, key, _, _, _unit in PEAK_SOURCES:
        minute_values: dict[int, float] = {}
        for bucket in raw.get(typ) or []:
            ts, value = bucket.get("time", ""), bucket.get(key, "")
            if not ts or value in ("", "null"):
                continue
            try:
                minute_values[int(ts)] = float(value)
            except (ValueError, TypeError):
                continue
        out[typ] = sorted(minute_values.items())
    return out


def _as_epoch_ms(value) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return int(value if value >= 100_000_000_000 else value * 1000)
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return int(parsed.timestamp() * 1000)


def _minute_iso(epoch_ms: int) -> str:
    value = datetime.fromtimestamp(epoch_ms / 1000, SHANGHAI).replace(second=0, microsecond=0)
    return value.isoformat(timespec="minutes")


def _minute_hm(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, SHANGHAI).strftime("%H:%M")


def _format_value(typ: str, value: float) -> str:
    return f"{value:.2f}" if typ == "deal" else f"{value:.0f}"


def _timeline_quality(series: dict[str, list[tuple[int, float]]],
                      recording_start_ms: int | None,
                      recording_end_ms: int | None,
                      duration_sec: float | None,
                      coverage_start_ms: int | None = None,
                      coverage_end_ms: int | None = None) -> dict:
    """校验分钟数据和媒体是否位于同一条可审计时间轴。

    原点与墙钟偏差用 `recording_start/end_ms`（媒体相对时间的起点 = 开播时刻）；
    成交序列覆盖范围默认按整个录制区间校验，简报等只抓取近期窗口的场景可显式
    传入 `coverage_start/end_ms`（分钟序列实际窗口），避免长场次误判不覆盖。
    """
    issues: list[str] = []
    wall_span_sec = None
    wall_media_deviation_sec = None
    if recording_start_ms is None or recording_end_ms is None or duration_sec is None:
        issues.append("缺少录制开始、结束或媒体时长")
    elif recording_end_ms <= recording_start_ms or duration_sec <= 0:
        issues.append("录制时间范围或媒体时长无效")
    else:
        wall_span_sec = (recording_end_ms - recording_start_ms) / 1000
        wall_media_deviation_sec = abs(wall_span_sec - float(duration_sec))
        if wall_media_deviation_sec >= 60:
            issues.append("墙钟跨度与媒体时长偏差不小于60秒")

    deal_minutes = sorted({timestamp - timestamp % 60_000
                           for timestamp, _value in series.get("deal", [])})
    deal_continuous = bool(deal_minutes) and all(
        current - previous == 60_000
        for previous, current in zip(deal_minutes, deal_minutes[1:])
    )
    deal_covered = False
    expected_start = expected_end = None
    cov_start = coverage_start_ms if coverage_start_ms is not None else recording_start_ms
    cov_end = coverage_end_ms if coverage_end_ms is not None else recording_end_ms
    if cov_start is not None and cov_end is not None:
        expected_start = cov_start - cov_start % 60_000
        expected_end = cov_end - cov_end % 60_000
        deal_covered = bool(deal_minutes) and (
            deal_minutes[0] <= expected_start and deal_minutes[-1] >= expected_end
        )
    if not deal_continuous:
        issues.append("成交分钟序列不连续")
    if not deal_covered:
        issues.append("成交分钟序列未覆盖录制区间")

    return {
        "association_allowed": not issues,
        "timezone": "Asia/Shanghai",
        "timeline_ok": not any("录制" in issue or "墙钟" in issue for issue in issues),
        "deal_series_ok": deal_continuous and deal_covered,
        "wall_span_sec": round(wall_span_sec, 3) if wall_span_sec is not None else None,
        "media_duration_sec": round(float(duration_sec), 3) if duration_sec is not None else None,
        "wall_media_deviation_sec": (
            round(wall_media_deviation_sec, 3)
            if wall_media_deviation_sec is not None else None
        ),
        "deal_first_minute": _minute_iso(deal_minutes[0]) if deal_minutes else None,
        "deal_last_minute": _minute_iso(deal_minutes[-1]) if deal_minutes else None,
        "expected_first_minute": _minute_iso(expected_start) if expected_start is not None else None,
        "expected_last_minute": _minute_iso(expected_end) if expected_end is not None else None,
        "issues": issues,
    }


def has_data_peak_reason(reasons) -> bool:
    """判断高亮原因是否属于成交/点击/在线的数据峰值。"""
    import json

    if isinstance(reasons, str):
        try:
            reasons = json.loads(reasons)
        except ValueError:
            reasons = [reasons]
    if not isinstance(reasons, (list, tuple)):
        reasons = [reasons]
    return any(str(reason).startswith(DATA_PEAK_LABELS) for reason in reasons)


def _relative_hms(ms: int) -> str:
    hours, remainder = divmod(max(int(ms), 0), 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds = remainder // 1_000
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _candidate_score(text: str, start_ms: int, end_ms: int, peak_ms: int,
                     categories: dict[str, list[str]]) -> float | None:
    """数据榜和优质榜复用同一内容质量门禁，距离只作次排序。"""
    from .quality import quality_candidate
    candidate = quality_candidate(start_ms, end_ms, text)
    if not candidate:
        return None
    midpoint = (start_ms + end_ms) // 2
    proximity = max(0.0, 1.0 - abs(midpoint - peak_ms) / (WINDOW_BEFORE_MS + WINDOW_AFTER_MS))
    return float(candidate["rule_score"]) + proximity * 12


def _prominent_candidates(typ: str, points: list[tuple[int, float]],
                          limit: int) -> list[tuple[int, float, float]]:
    """局部极值 + 稳健基线；避免用全局平均错过点击/在线的真实抬升。"""
    import statistics

    positive = [value for _timestamp, value in points if value > 0]
    if not positive:
        return []
    baseline = statistics.median(positive if typ == "deal" else [v for _, v in points])
    ranked: list[tuple[int, float, float]] = []
    for index, (timestamp, value) in enumerate(points):
        if value <= 0:
            continue
        previous = points[index - 1][1] if index else value
        following = points[index + 1][1] if index + 1 < len(points) else value
        if value < previous or value < following:
            continue
        if typ == "deal":
            # 成交是稀疏事件：保留非零局部高点，强度按非零中位数计算。
            prominence = value / max(float(baseline), 1.0)
        else:
            uplift = value - float(baseline)
            required = max(2.0, abs(float(baseline)) * 0.15)
            if uplift < required:
                continue
            prominence = 1.0 + uplift / max(abs(float(baseline)), 1.0)
        ranked.append((timestamp, value, prominence))
    return sorted(ranked, key=lambda item: (-item[2], -item[1]))[:limit]


def _similar(text_a: str, text_b: str) -> float:
    """轻量相似度：归一化后完全相同/包含按 1.0，否则用 rapidfuzz 模糊比对。"""
    import re as _re

    def _norm(value: str) -> str:
        return _re.sub(r"[\s，。！？、；：,.!?;:]+", "", value or "")

    a, b = _norm(text_a), _norm(text_b)
    if not a or not b:
        return 0.0
    if a == b or a in b or b in a:
        return 1.0
    from rapidfuzz import fuzz
    return fuzz.ratio(a, b) / 100.0


def select_peak_excerpts(sentences: list[tuple[int, int, str]], start_ms: int,
                         end_ms: int, peak_ms: int,
                         categories: dict[str, list[str]] | None = None,
                         latest_end_ms: int | None = None) -> list[tuple[int, int, str]]:
    """从关联窗口挑选少量完整主播句，供展示与 AI 证据使用。"""
    candidates: list[tuple[float, int, int, str]] = []
    for sentence_start, sentence_end, text in sentences:
        if sentence_end < start_ms or sentence_start > end_ms:
            continue
        if latest_end_ms is not None and sentence_end > latest_end_ms:
            continue
        score = _candidate_score(text, sentence_start, sentence_end, peak_ms, categories or {})
        if score is not None:
            candidates.append((score, sentence_start, sentence_end, text.strip()))

    selected: list[tuple[int, int, str]] = []
    for _score, sentence_start, sentence_end, text in sorted(
            candidates, key=lambda item: (-item[0], item[1])):
        if any(abs(sentence_start - chosen_start) < MIN_EXCERPT_GAP_MS
               for chosen_start, _chosen_end, _chosen_text in selected):
            continue
        # 与已选摘录高度重复的句子（同义/包含关系）只保留一次，避免“16号链接，宝宝们，16号链接”式重复。
        if any(_similar(text, chosen_text) >= 0.9
               for _chosen_start, _chosen_end, chosen_text in selected):
            continue
        selected.append((sentence_start, sentence_end, text))
        if len(selected) >= MAX_EXCERPTS:
            break
    return sorted(selected, key=lambda item: item[0])


def select_nearby_excerpts(sentences: list[tuple[int, int, str]], start_ms: int,
                           end_ms: int, peak_ms: int, *,
                           latest_end_ms: int | None = None) -> list[tuple[int, int, str]]:
    """选取峰值附近可完整展示的原话，不要求其达到可复用话术质量线。"""
    from ..asr.clean import can_show_raw_excerpt

    candidates: list[tuple[int, int, int, str]] = []
    for sentence_start, sentence_end, text in sentences:
        if sentence_end < start_ms or sentence_start > end_ms:
            continue
        if latest_end_ms is not None and sentence_end > latest_end_ms:
            continue
        body = str(text or "").strip()
        if not can_show_raw_excerpt(body):
            continue
        midpoint = (sentence_start + sentence_end) // 2
        candidates.append((abs(midpoint - peak_ms), sentence_start, sentence_end, body))

    selected: list[tuple[int, int, str]] = []
    for _distance, sentence_start, sentence_end, text in sorted(candidates):
        if any(abs(sentence_start - chosen_start) < MIN_EXCERPT_GAP_MS
               for chosen_start, _chosen_end, _chosen_text in selected):
            continue
        if any(_similar(text, chosen_text) >= 0.9
               for _chosen_start, _chosen_end, chosen_text in selected):
            continue
        selected.append((sentence_start, sentence_end, text))
        if len(selected) >= MAX_EXCERPTS:
            break
    return sorted(selected, key=lambda item: item[0])


def format_peak_excerpts(excerpts: list[tuple[int, int, str]]) -> str:
    """展示字段只存带真实时间点的短句，绝不保存整段窗口拼接文本。"""
    return "\n".join(f"[{_relative_hms(start_ms)}] {text}" for start_ms, _end_ms, text in excerpts)


def detect_data_peaks(metrics: dict, sentences: list[tuple[int, int, str]],
                      recording_start_ms: int | str | None = None,
                      top_n: int = 3,
                      categories: dict[str, list[str]] | None = None,
                      recording_end_ms: int | str | None = None,
                      duration_sec: float | None = None,
                      coverage_start_ms: int | None = None,
                      coverage_end_ms: int | None = None,
                      text_provenance: str = "raw_asr") -> list[dict]:
    """检测数据峰值，并在质量门禁通过时生成可审计时间关联。

    `recording_start_ms` 同时是媒体相对时间的原点；可传毫秒时间戳或上海时区
    时间字符串。简报可临时调用，传齐开始、结束、媒体时长即可得到与下播持久化
    相同结构的 `peak_meta`。分钟序列只覆盖近期窗口时，可显式传入
    `coverage_start/end_ms`（序列实际窗口）作为覆盖校验范围。
    """
    if not metrics:
        return []
    series = _parse_raw(metrics.get("raw") or {})
    if not any(series.values()):
        return []

    start_epoch_ms = _as_epoch_ms(recording_start_ms)
    end_epoch_ms = _as_epoch_ms(recording_end_ms)
    normalized: dict[str, list[tuple[int, float]]] = {}
    for typ, points in series.items():
        normalized[typ] = [
            (timestamp if timestamp >= 100_000_000_000 or start_epoch_ms is None
             else start_epoch_ms + timestamp, value)
            for timestamp, value in points
        ]
    quality = _timeline_quality(
        normalized, start_epoch_ms, end_epoch_ms, duration_sec,
        coverage_start_ms=coverage_start_ms, coverage_end_ms=coverage_end_ms)

    peaks: list[dict] = []
    for typ, _key, base_score, label, unit in PEAK_SOURCES:
        points = normalized.get(typ) or []
        if len(points) < 2:
            continue
        # 峰值只在本场录制窗口内找：简报的分钟序列覆盖近期 2 小时，
        # 可能包含开播前/下播后的数据，场外峰值不能当选（2026-08-04）。
        if start_epoch_ms is not None and end_epoch_ms is not None:
            points = [point for point in points
                      if start_epoch_ms <= point[0] <= end_epoch_ms]
        if len(points) < 2:
            continue
        # 首尾不完整自然分钟含本地录制区间外的平台数据，不做峰值。
        if start_epoch_ms is not None:
            full_start = (start_epoch_ms // 60_000 + 1) * 60_000
            points = [point for point in points if point[0] >= full_start]
        if end_epoch_ms is not None:
            full_end = end_epoch_ms // 60_000 * 60_000
            points = [point for point in points if point[0] < full_end]
        if len(points) < 2:
            continue
        for minute_ms, value, prominence in _prominent_candidates(typ, points, top_n):
            peaks.append({
                "minute_ms": minute_ms,
                "value": value,
                "typ": typ,
                "unit": unit,
                "label": label,
                "score": base_score,
                "prominence": prominence,
            })

    # 先保留每类最强一个，再用剩余名额补全局，防止三个弱成交
    # 高点把明显点击/在线抬升全部挤掉。
    peaks.sort(key=lambda item: (-item["prominence"], -item["score"], -item["value"]))
    merged: list[dict] = []
    for peak in peaks:
        if any(peak["typ"] == chosen["typ"] and
               abs(peak["minute_ms"] - chosen["minute_ms"]) < MERGE_MS
               for chosen in merged):
            continue
        merged.append(peak)

    diverse: list[dict] = []
    for typ, _key, _score, _label, _unit in PEAK_SOURCES:
        first = next((peak for peak in merged if peak["typ"] == typ), None)
        if first:
            diverse.append(first)
    for peak in merged:
        if peak not in diverse:
            diverse.append(peak)

    out: list[dict] = []
    for peak in diverse[:top_n]:
        absolute_ms = peak["minute_ms"]
        is_absolute = absolute_ms >= 100_000_000_000
        if is_absolute and start_epoch_ms is not None:
            peak_ms = absolute_ms - start_epoch_ms
        elif not is_absolute:
            peak_ms = absolute_ms
        else:
            peak_ms = 0

        start_ms = max(0, peak_ms - WINDOW_BEFORE_MS)
        end_ms = peak_ms - peak_ms % 60_000 + WINDOW_AFTER_MS
        excerpts: list[tuple[int, int, str]] = []
        if start_epoch_ms is not None:
            # 即使分钟数据覆盖门禁未通过，带真实时间戳的近邻原话仍可供简报展示；
            # 但它与通过完整门禁的“关联证据”分开存储，不能被 AI 当作峰值归因引用。
            # 候选话术最晚可在峰值所在自然分钟结束；之后的句子不构成前置关联。
            peak_minute_end_ms = peak_ms - peak_ms % 60_000 + 60_000
            if quality["association_allowed"]:
                excerpts = select_peak_excerpts(
                    sentences, start_ms, end_ms, peak_ms, categories,
                    latest_end_ms=peak_minute_end_ms,
                )
            else:
                excerpts = select_nearby_excerpts(
                    sentences, start_ms, end_ms, peak_ms,
                    latest_end_ms=peak_minute_end_ms,
                )

        excerpt_meta: list[dict] = []
        kept_excerpts: list[tuple[int, int, str]] = []
        for sentence_start, sentence_end, text in excerpts:
            sentence_end_absolute = start_epoch_ms + sentence_end
            same_minute = (
                sentence_end_absolute - sentence_end_absolute % 60_000
                == absolute_ms - absolute_ms % 60_000
            )
            lag_sec = 0.0 if same_minute else max(
                0.0, (absolute_ms - sentence_end_absolute) / 1000
            )
            if not same_minute and lag_sec <= 0:
                # 句子结束晚于峰值时刻且不在同一分钟：不能写成“之后 N 秒”的前置关联
                continue
            kept_excerpts.append((sentence_start, sentence_end, text))
            excerpt_meta.append({
                "start_ms": sentence_start,
                "end_ms": sentence_end,
                "start": _relative_hms(sentence_start),
                "end": _relative_hms(sentence_end),
                "text": text,
                "text_provenance": text_provenance,
                "relation": "同分钟" if same_minute else "之后",
                "lag_sec": round(lag_sec, 3),
            })

        peak_minute = _minute_iso(absolute_ms) if is_absolute else None
        peak_label = _minute_hm(absolute_ms) if is_absolute else _relative_hms(peak_ms)[:5]
        formatted_value = _format_value(peak["typ"], peak["value"])
        association_excerpts = excerpt_meta if quality["association_allowed"] else []
        nearby_excerpts = excerpt_meta if not quality["association_allowed"] else []
        peak_meta = {
            "version": 1,
            "type": peak["typ"],
            "minute": peak_minute,
            "minute_ms": absolute_ms if is_absolute else None,
            "value": round(peak["value"], 2) if peak["typ"] == "deal" else peak["value"],
            "unit": peak["unit"],
            "window": {"start_ms": start_ms, "end_ms": end_ms},
            "excerpts": association_excerpts,
            "text_provenance": text_provenance,
            "relation": association_excerpts[0]["relation"] if association_excerpts else None,
            "lag_sec": association_excerpts[0]["lag_sec"] if association_excerpts else None,
            "quality": dict(quality),
        }
        if nearby_excerpts:
            peak_meta["nearby_excerpts"] = nearby_excerpts
        if quality["association_allowed"] and not excerpt_meta:
            peak_meta["quality"]["issues"] = [
                *peak_meta["quality"]["issues"], "峰值前及同分钟无合格话术",
            ]

        text = (format_peak_excerpts(kept_excerpts)
                if quality["association_allowed"] else "")
        boost = 0.5 if peak["value"] >= (1000 if peak["typ"] == "deal" else 100) else 0.0
        out.append({
            "start_ms": start_ms,
            "end_ms": end_ms,
            "score": round(min(6.5, peak["score"] + boost), 1),
            "reasons": [f"{peak['label']} {peak_label}（{formatted_value}{peak['unit']}）"],
            "transcript": text,
            "peak_meta": peak_meta,
        })
        log.info("数据高亮：%s %s %s%s -> %d 条可用摘录", peak["label"],
                 peak_label, formatted_value, peak["unit"], len(excerpts))
    return out


def parse_peak_meta(value) -> dict:
    """解析历史/当前 `peak_meta`，无效或旧数据返回空字典。"""
    import json

    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def sanitize_peak_meta_for_outward(meta: dict, cfg: dict) -> dict:
    """复制并净化 peak_meta；只接受已持久化的展示摘录。"""
    source = parse_peak_meta(meta)
    if not source:
        return {}
    from copy import deepcopy
    from ..asr.clean import OUTWARD_DISPLAY_REPAIRED, outward_display_text

    clean = deepcopy(source)
    provenance = str(clean.get("text_provenance") or "")
    excerpts = []
    for raw in clean.get("excerpts") or []:
        if not isinstance(raw, dict):
            continue
        item_provenance = str(raw.get("text_provenance") or provenance)
        body = (outward_display_text(
            str(raw.get("text") or ""), cfg,
            provenance=OUTWARD_DISPLAY_REPAIRED, max_chars=180)
            if item_provenance == OUTWARD_DISPLAY_REPAIRED else "")
        if not body:
            continue
        excerpts.append({
            **raw, "text": body,
            "text_provenance": OUTWARD_DISPLAY_REPAIRED,
        })
    clean["excerpts"] = excerpts
    clean["text_provenance"] = OUTWARD_DISPLAY_REPAIRED
    return clean


def format_peak_evidence(meta: dict, max_items: int = 3, *,
                         cfg: dict | None = None) -> list[str]:
    """把结构化关联转成不含因果措辞的可展示证据。

    peak_meta 可能来自旧库原始 ASR；没有持久化展示来源的摘录直接过滤。
    """
    meta = parse_peak_meta(meta)
    from ..asr.clean import OUTWARD_DISPLAY_REPAIRED, outward_display_text
    provenance = str(meta.get("text_provenance") or "")
    peak_time = str(meta.get("minute") or "")
    if "T" in peak_time:
        peak_time = peak_time.split("T", 1)[1][:5]
    value = meta.get("value")
    unit = str(meta.get("unit") or "")
    typ = str(meta.get("type") or "")
    if value is None:
        value_text = "暂无"
    else:
        value_text = _format_value(typ, float(value)) + unit
    peak_kind = {"deal": "成交", "itemClick": "点击", "uv": "在线"}.get(typ, "数据")
    lines: list[str] = []
    for excerpt in (meta.get("excerpts") or [])[:max_items]:
        item_provenance = str(excerpt.get("text_provenance") or provenance)
        body = (outward_display_text(
            str(excerpt.get("text") or ""), cfg or {},
            provenance=OUTWARD_DISPLAY_REPAIRED, max_chars=180)
            if item_provenance == OUTWARD_DISPLAY_REPAIRED else "")
        if not body:
            continue
        relation = excerpt.get("relation")
        lag = float(excerpt.get("lag_sec") or 0)
        if relation == "同分钟":
            timing = (
                f"该话术与 {peak_time or '时间暂无'} 的{peak_kind}峰值"
                f"（{value_text}）发生在同一分钟内，先后无法确定"
            )
        else:
            timing = f"话术结束后 {int(round(lag))} 秒观察到 {peak_time or '时间暂无'} 峰值 {value_text}"
        lines.append(
            f"[{excerpt.get('start') or '时间暂无'}] {body}\n{timing}"
        )
    return lines
