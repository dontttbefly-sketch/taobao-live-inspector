"""绝对整点小时简报：从持久化录像时间线取材，冻结事实后推送飞书。

主播轮换、进程重启和分片换片都不改变小时边界；同一整点窗口可以
跨多个技术分片聚合，缺口必须原位保留并按正式门禁处理。
"""
from __future__ import annotations

import copy
import json
import logging
import re
import traceback
import time
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from .config import local_epoch_ms, resolve
from .transcription.feishu import note_doc_link
from .transcription.models import SmartMinutesArtifact, TranscriptSegment, TranscriptionResult
from .intelligence.context import (
    build_analysis_history, build_hourly_context,
    clip_qianniu_metrics_to_window, normalize_qianniu_metrics,
    intelligence_peak_context, offset_transcription_segments, resolve_context_window_end,
)
from .intelligence.integrity import validated_result_from_artifact
from .intelligence.models import HourlyIntelligenceResult
from .intelligence.service import IntelligenceService
from .metrics.qianniu import save_brief_snapshot
from .recorder.recorder import MIN_VALID_PART_BYTES
from .recorder.mtop import MtopAuthError
from .notify.feishu import (_serialize_card, delivery_status,
                            frozen_delivery_card, safe_delivery_error_fields,
                            send_card, send_card_once)

log = logging.getLogger("briefing")

# 分片"已闭合"判定：文件最后修改时间距今超过该秒数（正在写的分片 mtime 会持续更新）
DEFAULT_PART_MIN_AGE = 120
BRIEF_CARD_MAX_COMPONENTS = 180
BRIEF_CARD_MAX_BYTES = 28_000
BRIEF_LINK_MAX_CHARS = 2_048


def _safe_brief_url(value: object) -> str:
    """Accept only bounded public HTTP(S) links from persisted artifacts."""
    url = str(value or "").strip()
    if (not url or len(url) > BRIEF_LINK_MAX_CHARS
            or any(character.isspace() or ord(character) < 32 for character in url)):
        return ""
    try:
        parsed = urlsplit(url)
        hostname = str(parsed.hostname or "").casefold()
        port = parsed.port
    except ValueError:
        return ""
    if (parsed.scheme.casefold() not in {"http", "https"}
            or not hostname or parsed.username or parsed.password
            or not re.fullmatch(r"[a-z0-9.-]+", hostname)):
        return ""
    netloc = hostname + (f":{port}" if port is not None else "")
    normalized = urlunsplit((
        parsed.scheme.casefold(),
        netloc,
        quote(parsed.path, safe="/-._~%"),
        quote(parsed.query, safe="-._~%=&"),
        quote(parsed.fragment, safe="-._~%"),
    ))
    return normalized if len(normalized) <= BRIEF_LINK_MAX_CHARS else ""


def _media_end_epoch_ms(started_at: str | None, media_sec: float,
                        now_ms: int | None = None) -> int:
    """用开播时刻加媒体时长推导时间轴末尾，避免 ASR 处理延迟污染墙钟。"""
    start_ms = local_epoch_ms(started_at)
    if start_ms is not None and media_sec > 0:
        return start_ms + int(media_sec * 1000)
    return now_ms if now_ms is not None else int(time.time() * 1000)


def closed_parts(session_dir: Path, min_age: int = DEFAULT_PART_MIN_AGE,
                 now: float | None = None) -> list[Path]:
    """返回已闭合（写完）的分片文件，按序号排序"""
    now = now if now is not None else time.time()
    parts = []
    for p in sorted(session_dir.glob("part_*.ts")):
        stat = p.stat()
        if stat.st_size >= MIN_VALID_PART_BYTES and now - stat.st_mtime > min_age:
            parts.append(p)
    return parts


def _duration_of(part: Path) -> float | None:
    """分片时长（秒），用 ffprobe；失败返回 None（不缓存，下次重测）。"""
    import subprocess
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(part)],
            capture_output=True, text=True, timeout=10,
        )
        value = float(out.stdout.strip())
        return value if value > 0 else None
    except Exception:
        return None


def _estimate_part_duration(part: Path, durs: dict[str, float],
                            sizes: dict[str, int]) -> float:
    """ffprobe 瞬失败时按文件大小比例从最近成功分片估算时长。

    固定码率 TS 分片时长与大小近似成正比；只做进程内兜底，不写 sizes 缓存，
    下次大小变化或仍失败时会重新尝试 ffprobe（自愈）。
    """
    try:
        size = part.stat().st_size
    except OSError:
        return 0.0
    for name in sorted(durs, reverse=True):
        base_dur = durs.get(name) or 0.0
        base_size = sizes.get(name) or 0
        if name < part.name and base_dur > 0 and base_size > 0:
            return base_dur * size / base_size
    return 0.0


# 分片时长缓存：{(session_dir): {part_name: duration_sec}}，进程内常驻避免反复 ffprobe
_part_dur_cache: dict[str, dict[str, float]] = {}
_part_size_cache: dict[str, dict[str, int]] = {}


def _session_part_durations(session_dir: Path, through: str | None = None) -> dict[str, float]:
    """返回该会话所有分片 {part_NN.ts: 时长秒}（带进程内缓存）"""
    key = str(session_dir)
    durs = _part_dur_cache.setdefault(key, {})
    sizes = _part_size_cache.setdefault(key, {})
    # 会话持续产生新分片，缓存不能在第一期简报后冻结；只补测新出现的文件。
    for p in sorted(session_dir.glob("part_*.ts")):
        if through is not None and p.name > through:
            break
        size = p.stat().st_size
        if size < MIN_VALID_PART_BYTES:
            continue
        if p.name not in durs or sizes.get(p.name) != size:
            measured = _duration_of(p)
            if measured is not None:
                durs[p.name] = measured
                sizes[p.name] = size
            else:
                # ffprobe 瞬失败：估算兜底且不写 sizes，避免 0.0 永久错位；
                # 下次仍失败会继续尝试真实测量（自愈）。
                durs[p.name] = _estimate_part_duration(p, durs, sizes)
    return durs


def _part_offset(session_dir: Path, part: Path) -> float:
    """分片相对全场开始的时间偏移（秒）= 之前所有分片时长之和。
    断流重连/分片时长不均也能对齐（逐片累计实测时长）"""
    # 只测当前分片及它之前的文件。旧逻辑会把仍在写入/反复失败的后续 part 也逐个
    # ffprobe，单次简报可因此被拖慢几十分钟。
    durs = _session_part_durations(session_dir, through=part.name)
    off = 0.0
    for name in sorted(durs):
        if name == part.name:
            break
        off += durs[name]
    return off


def _trend_bar(values: list[float], width: int = 30) -> str:
    """在线人数分钟序列 → 文字趋势条（最近 width 分钟）"""
    vals = [v for v in values if v is not None]
    if not vals:
        return ""
    vals = vals[-width:]
    mx = max(vals) or 1
    chars = "▁▂▃▄▅▆▇█"
    return "".join(chars[min(len(chars) - 1, int(v / mx * (len(chars) - 1)))]
                   for v in vals)


def _delta(cur: float | None, prev: float | None, unit: str = "") -> str:
    """环比文本：+12 / -3 / --"""
    if cur is None or prev is None or cur < prev:
        return "--"
    d = cur - prev
    if d > 0:
        return f"+{d:,.0f}{unit}"
    if d < 0:
        return f"{d:,.0f}{unit}"
    return "持平"


def _fetch_brief_metrics(cfg: dict, store, stream_id: int, live_id: str,
                         period_sec: float = 3600,
                         end_ms: int | None = None,
                         include_current_totals: bool = True,
                         totals_override: dict | None = None,
                         previous_snapshot_override: dict | None = None,
                         ) -> tuple[dict, str, str, str]:
    """简报实时数据增强：
    返回 (metrics, 数据区文本, 趋势条, 时段对比文本)
    - totalStats：当前在线、平台最高在线、累计观看/成交人数/成交件数/成交金额
    - minuteSeries：本时段在线走势、点击与成交增量
    - 在线趋势条（最近 30 分钟）
    - 时段对比（vs 上一期简报快照）"""
    from .metrics.qianniu import (
        fetch_metric_series, fetch_screen_totals, latest_brief_snapshot,
        safe_delta, weighted_interval_average,
    )
    # 补发/重试简报不能用“处理当下”作时间窗末端，否则媒体和分钟趋势会
    # 错位而拒绝峰值关联。直播中实时简报不传 end_ms，仍以当前时间为准。
    now_ms = int(end_ms) if end_ms is not None else int(time.time() * 1000)
    start_ms = now_ms - max(60, int(period_sec)) * 1000
    errors: list[str] = []
    totals: dict = {}
    if include_current_totals:
        if totals_override is not None:
            totals = dict(totals_override)
        else:
            try:
                # 正式小时简报只读大屏 totalStats。这里不能调用
                # fetch_live_totals，因为它会回退到旧 iliad 口径并混入不可审计值。
                totals = fetch_screen_totals(cfg, live_id)
            except MtopAuthError:
                raise
            except Exception as exc:
                errors.append(f"累计快照不可用：{exc}")
    else:
        errors.append("历史补抓仅恢复分钟趋势；累计增量不拆分到单小时")
    try:
        series = fetch_metric_series(cfg, live_id, search_type="2", time_type=1,
                                     start_ms=start_ms, end_ms=now_ms)
    except MtopAuthError:
        raise
    except Exception as exc:
        series = {}
        errors.append(f"分钟趋势不可用：{exc}")
    m = dict(totals)
    m["data_state"] = "ok" if not errors else ("partial" if totals or series else "unavailable")
    m["data_issues"] = errors
    m["period"] = {k: series.get(k) for k in (
        "online_uv", "max_online_uv", "uv_avg", "visitor_total", "ipv_total",
        "pay_amt", "heat_score")}
    raw_series = (series.get("raw") or {}) if isinstance(series, dict) else {}
    minute_times: list[int] = []
    for rows in raw_series.values():
        for row in rows or []:
            try:
                minute_times.append(int(row.get("time")))
            except (TypeError, ValueError):
                continue
    m["series"] = raw_series
    m["interval"] = {
        "timezone": "Asia/Shanghai",
        "requested_start_ms": start_ms,
        "requested_end_ms": now_ms,
        "actual_start_ms": min(minute_times) if minute_times else None,
        "actual_end_ms": max(minute_times) if minute_times else None,
        "requested_minutes": max(1, round(period_sec / 60)),
    }

    # 在线峰值 + 趋势条（从分钟序列 raw 计算）
    uv_seq = []
    for b in raw_series.get("uv", []) or []:
        v = b.get("online")
        if v not in (None, "", "null"):
            uv_seq.append(float(v))
    trend = _trend_bar(uv_seq)

    # 时段对比只对真正的累计字段做差；首期优先使用开录基线。
    prev = (dict(previous_snapshot_override)
            if previous_snapshot_override is not None else
            (latest_brief_snapshot(store, stream_id) if store else {}))
    if not prev and store:
        from .metrics.qianniu import baseline_snapshot
        prev = baseline_snapshot(store, stream_id)
    # 快照保存移到简报推送成功后（run_briefing 成功分支）：失败的构建/重试
    # 不得写入快照链，否则补发时 prev 被失败重试污染，差值算成 0
    # （2026-08-08 实测：5 次失败重试把快照推到 15559，补发时 15559-15559=0）。
    hour_pay = safe_delta(totals.get("pay_amt"), prev.get("pay_amt") if prev else None)
    hour_buyers = safe_delta(totals.get("buyer_cnt"), prev.get("buyer_cnt") if prev else None)
    hour_items = safe_delta(totals.get("item_qty"), prev.get("item_qty") if prev else None)
    hour_viewers = safe_delta(totals.get("viewer_uv"), prev.get("viewer_uv") if prev else None)
    hour_followers = safe_delta(totals.get("atn_uv"), prev.get("atn_uv") if prev else None)
    hour_refund = safe_delta(totals.get("refund_amt"), prev.get("refund_amt") if prev else None)
    hour_stay = weighted_interval_average(
        totals.get("stay_time_pu"), totals.get("viewer_uv"),
        prev.get("stay_time_pu") if prev else None,
        prev.get("viewer_uv") if prev else None,
    )
    m["delta"] = {
        "pay_amt": round(hour_pay, 2) if hour_pay is not None else None,
        "buyer_cnt": int(round(hour_buyers)) if hour_buyers is not None else None,
        "item_qty": int(round(hour_items)) if hour_items is not None else None,
        "viewer_uv": int(round(hour_viewers)) if hour_viewers is not None else None,
        "atn_uv": int(round(hour_followers)) if hour_followers is not None else None,
        "refund_amt": round(hour_refund, 2) if hour_refund is not None else None,
        "baseline_kind": (prev.get("snapshot_kind") if prev else None),
        "available": bool(prev),
    }
    hour_conversion = (
        float(hour_buyers) / float(hour_viewers) * 100
        if hour_buyers is not None and hour_viewers is not None and hour_viewers > 0
        else None
    )
    period = m.get("period") or {}
    m["core_metrics"] = {
        "成交金额": m["delta"]["pay_amt"],
        "本小时新增观看人数": m["delta"]["viewer_uv"],
        "本小时进入次数": period.get("visitor_total"),
        "平均在线": period.get("uv_avg"),
        "最高在线": period.get("max_online_uv") or m.get("max_online_uv"),
        "成交人数": m["delta"]["buyer_cnt"],
        "本小时转化": hour_conversion,
        "新增粉丝": m["delta"]["atn_uv"],
        # 大屏只给“整场累计观看人数 + 整场累计人均时长”。
        # 用相邻两个整点边界做加权差，才是这一小时的平均停留。
        "平均停留": round(hour_stay, 2) if hour_stay is not None else None,
        "退款金额": m["delta"]["refund_amt"],
    }

    def _s(v, decimals: int = 0):
        if v is None:
            return "暂无"
        return f"{float(v):,.{decimals}f}"
    def _u(v, unit: str, decimals: int = 0):
        return _s(v, decimals) + (unit if v is not None else "")
    minutes = max(1, round(period_sec / 60))
    if not include_current_totals:
        blocks = [
            f"历史时段补抓：平均在线 {_u(period.get('uv_avg'), ' 人', 1)} ｜ "
            f"商品点击 {_u(period.get('ipv_total'), ' 次')}",
            f"分钟趋势成交 **{_u(period.get('pay_amt'), ' 元', 2)}**",
            "累计观看、成交人数和成交件数：缺少该小时边界快照，不从恢复时快照倒推",
            "数据来源：千牛历史分钟趋势",
        ]
    elif hour_pay is None:
        blocks = [
            f"当前在线 **{_u(m.get('online_uv'), ' 人')}** ｜ 平台最高在线 {_u(m.get('max_online_uv'), ' 人')} ｜ 累计观看 {_u(m.get('viewer_uv'), ' 人')}",
            f"本小时成交 **暂无**（开播首小时无上小时基准；平台累计 {_u(m.get('pay_amt'), ' 元', 2)}）",
            f"近 {minutes} 分钟：平均在线 {_u(period.get('uv_avg'), ' 人', 1)} ｜ 商品点击 {_u(period.get('ipv_total'), ' 次')}",
            "成交订单数：暂无（实时接口不返回，下播冻结后补齐）",
            "数据来源：千牛实时累计差值（本小时 = 当前累计 − 上小时快照）",
        ]
    else:
        blocks = [
            f"当前在线 **{_u(m.get('online_uv'), ' 人')}** ｜ 平台最高在线 {_u(m.get('max_online_uv'), ' 人')} ｜ 本小时新增观看 {_u(hour_viewers, ' 人')}",
            f"本小时成交 **{_u(hour_pay, ' 元', 2)}** ｜ 成交人数 {_u(hour_buyers, ' 人')} ｜ 成交件数 {_u(hour_items, ' 件')}",
            f"近 {minutes} 分钟：平均在线 {_u(period.get('uv_avg'), ' 人', 1)} ｜ 商品点击 {_u(period.get('ipv_total'), ' 次')}",
            "成交订单数：暂无（实时接口不返回，下播冻结后补齐）",
            "数据来源：千牛实时累计差值（本小时 = 当前累计 − 上小时快照）",
        ]
    # 低在线时段实时接口可信度低：在线 0 时明确提示，避免把
    # 接口数据问题误读成「直播间没人」。
    if m.get("online_uv") is not None and float(m.get("online_uv")) == 0:
        blocks.append("⚠ 实时在线显示为 0，可能为接口数据延迟，请以直播间实际为准")
    if errors:
        blocks.append("数据限制：" + "；".join(errors))
    text = "\n".join(blocks)
    if trend:
        text += f"\n在线趋势（近 {minutes} 分钟）：`{trend}`"
    # 接口可能成功返回空载荷，此时 errors 为空但不能冒充
    # “数据完整”。只检查经营文本，不让有效转写掩盖指标缺失。
    has_metric = any(m.get(key) is not None for key in (
        "pay_amt", "online_uv", "max_online_uv", "viewer_uv", "viewer_pv",
    )) or any(
        isinstance(points, list) and bool(points)
        for points in (m.get("series") or {}).values()
    )
    if not has_metric:
        m["data_state"] = "unavailable"
    return m, text, trend, ""


def _intelligence_peak_context(highlights: list[dict]) -> list[dict]:
    """Backward-compatible local seam for tests and legacy callers."""
    return intelligence_peak_context(highlights)


def _chart_points(metrics: dict) -> tuple[list[dict], bool, bool, bool, bool]:
    """分钟序列去重、排序并补分钟；缺失值保留 None 供 JSON 输出 null。

    返回长格式数据（每行一条序列点，『指标』字段区分序列），
    seriesField 使用『指标』才能让图例正确显示中文系列名与颜色。
    返回 (rows, 有在线, 有进入, 有点击, 有成交)。
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    # 保险丝：单图超过 120 个分钟点（约 4 小时）即抽稀，防超长班次
    # 的轮换最终简报图表数据撑爆飞书 30KB 卡片上限而丢卡。
    MAX_CHART_MINUTES = 120
    labels = (
        ("uv", "在线人数"),
        ("enter", "进入次数"),
        ("itemClick", "商品点击"),
        ("deal", "成交金额"),
    )
    fields = {
        "uv": "online", "enter": "visitorEnter",
        "itemClick": "value", "deal": "amount",
    }
    series = metrics.get("series") or {}
    maps: dict[str, dict[int, float]] = {
        "uv": {}, "enter": {}, "itemClick": {}, "deal": {},
    }
    for typ, _label in labels:
        source_type = "uv" if typ == "enter" else typ
        for row in series.get(source_type) or []:
            try:
                timestamp = int(row.get("time"))
                value = float(row.get(fields[typ]))
            except (TypeError, ValueError):
                continue
            maps[typ][timestamp - timestamp % 60_000] = value
    all_times = sorted(
        set(maps["uv"]) | set(maps["enter"])
        | set(maps["itemClick"]) | set(maps["deal"]))
    if not all_times:
        return [], False, False, False, False
    rows: list[dict] = []
    timezone = ZoneInfo("Asia/Shanghai")
    cursor = all_times[0]
    while cursor <= all_times[-1]:
        hm = datetime.fromtimestamp(cursor / 1000, timezone).strftime("%H:%M")
        for typ, label in labels:
            rows.append({"time": hm, "指标": label, "值": maps[typ].get(cursor)})
        cursor += 60_000
    if len(rows) > MAX_CHART_MINUTES * len(labels):
        # 均匀抽稀并保留尾部三个序列点，避免图表尾端截断。
        step = max(1, len(rows) // (MAX_CHART_MINUTES * len(labels)))
        sampled = rows[::step]
        tail = rows[-len(labels):]
        if sampled[-len(labels):] != tail:
            sampled.extend(tail)
        rows = sampled
    return (
        rows,
        len(maps["uv"]) >= 2,
        len(maps["enter"]) >= 2,
        len(maps["itemClick"]) >= 2,
        len(maps["deal"]) >= 2,
    )


def _brief_chart(metrics: dict) -> list[dict]:
    """四项核心分钟趋势合并为两张移动端友好组合图，缺哪条只降级哪条。"""
    rows, has_uv, has_enter, has_click, has_deal = _chart_points(metrics)
    charts: list[dict] = []
    line_color = "#3B82F6"
    bar_color = "#F472B6"
    series_captions = {
        "在线人数": "在线人数（人）",
        "进入次数": "进入次数（次）",
        "商品点击": "商品点击（次）",
        "成交金额": "成交金额（元）",
    }

    def _spec(title: str, series_defs: list[tuple[str, str, str]]) -> dict:
        data: list[dict] = []
        series: list[dict] = []
        for label, chart_type, color in series_defs:
            data_id = {
                "在线人数": "online",
                "进入次数": "enter",
                "商品点击": "click",
                "成交金额": "deal",
            }[label]
            data.append({
                "id": data_id,
                "values": [row for row in rows if row["指标"] == label],
            })
            item = {
                "type": chart_type,
                "dataId": data_id,
                "xField": "time",
                "yField": "值",
                "seriesField": "指标",
                "color": [color],
            }
            if chart_type == "line":
                item["line"] = {"style": {"stroke": color, "lineWidth": 3}}
                item["point"] = {"visible": False}
            else:
                item["bar"] = {
                    "style": {
                        "fill": color,
                        "fillOpacity": .72,
                        "stroke": color,
                        "lineWidth": 1,
                    },
                }
            series.append(item)

        subtitle = " · ".join(
            series_captions[label]
            for label, _chart_type, _color in series_defs
        )
        spec = {
            "type": "common",
            "title": {"text": title, "subtext": subtitle},
            "data": data,
            "series": series,
            "axes": [
                {
                    "orient": "bottom",
                    "type": "band",
                    "label": {
                        "visible": True,
                        "autoHide": True,
                        "autoHideMethod": "greedy",
                        "autoRotate": False,
                        "style": {"fill": "#6B7280", "fontSize": 10},
                    },
                    "tick": {"visible": False},
                    "domainLine": {
                        "visible": True,
                        "style": {"stroke": "#9CA3AF"},
                    },
                },
                {
                    "orient": "left",
                    "type": "linear",
                    "seriesIndex": [0],
                    "zero": True,
                    "label": {
                        "visible": True,
                        "style": {"fill": "#6B7280", "fontSize": 10},
                    },
                    "grid": {
                        "visible": True,
                        "style": {"stroke": "#E5E7EB", "lineDash": [4, 4]},
                    },
                },
            ],
            "legends": {"visible": False},
            "tooltip": {"visible": True},
            "crosshair": {"xField": {"visible": True}, "yField": {"visible": False}},
        }
        if len(series_defs) > 1:
            spec["axes"].append({
                "orient": "right",
                "type": "linear",
                "seriesIndex": [1],
                "zero": True,
                "label": {
                    "visible": True,
                    "style": {"fill": "#6B7280", "fontSize": 10},
                },
                "grid": {"visible": False},
            })
        return {
            "tag": "chart",
            "chart_spec": spec,
            "height": "240px",
            "preview": True,
            "color_theme": "primary",
        }

    traffic = []
    if has_uv:
        traffic.append(("在线人数", "line", line_color))
    if has_enter:
        traffic.append(("进入次数", "bar", bar_color))
    if traffic:
        charts.append(_spec("流量趋势", traffic))

    conversion = []
    if has_click:
        conversion.append(("商品点击", "line", line_color))
    if has_deal:
        conversion.append(("成交金额", "bar", bar_color))
    if conversion:
        charts.append(_spec("转化趋势", conversion))
    return charts


def _coverage_text(metrics: dict) -> str:
    interval = metrics.get("interval") or {}
    start = interval.get("actual_start_ms")
    end = interval.get("actual_end_ms")
    if start is None or end is None:
        return "分钟图表覆盖：暂无可用分钟序列"
    from datetime import datetime
    from zoneinfo import ZoneInfo
    timezone = ZoneInfo("Asia/Shanghai")
    start_text = datetime.fromtimestamp(start / 1000, timezone).strftime("%H:%M")
    end_text = datetime.fromtimestamp(end / 1000, timezone).strftime("%H:%M")
    return f"分钟图表覆盖：{start_text} ~ {end_text}（Asia/Shanghai；断档显示为空）"


def _validated_formal_media_coverage(
        coverage: dict | None) -> dict | None:
    if not coverage:
        return None
    from .business_facts import normalize_media_coverage
    from .recorder.timeline import MAX_FORMAL_MISSING_MS

    normalized = normalize_media_coverage(
        coverage,
        window_start_ms=int(coverage.get("window_start_ms")),
        window_end_ms=int(coverage.get("window_end_ms")),
    )
    if (normalized["timeline_state"] != "complete"
            or int(normalized["covered_ms"]) <= 0
            or int(normalized["missing_ms"]) > MAX_FORMAL_MISSING_MS):
        raise ValueError("formal brief recording coverage gate closed")
    return normalized


def _brief_snapshot(metrics: dict, peak_count: int) -> str:
    """首屏五秒摘要：只陈述本期可核验事实，不把峰值关联写成因果。"""
    delta = metrics.get("delta") or {}
    pay_amt = delta.get("pay_amt")
    online_uv = metrics.get("online_uv")
    parts: list[str] = []
    if pay_amt is not None:
        parts.append(f"本期成交 **{float(pay_amt):,.2f} 元**")
    else:
        parts.append("本期成交 **暂无**")
    if online_uv is not None:
        parts.append(f"当前在线 **{float(online_uv):,.0f} 人**")
    else:
        parts.append("当前在线 **暂无**")
    if peak_count > 0:
        parts.append(f"已识别 **{int(peak_count)} 组**高亮话术关联")
    return (
        " ｜ ".join(parts)
        + "\n<font color='grey'>先看结论，再看证据；时间关联不代表因果。</font>"
    )


def _smart_minutes_preview_points(
        smart_minutes: SmartMinutesArtifact, *, max_points: int = 3,
        max_chars: int = 100) -> list[str]:
    """从完整智能纪要中选取卡片预览，不改写也不截断原文。

    飞书纪要可能返回无换行的 Markdown 树。这里只拆列表和完整句，过滤纯标题；
    超过字数且无法按句号等完整收束的单句直接跳过，完整产物仍留在纪要链接中。
    """
    sources = [smart_minutes.summary]
    sources.extend(chapter.summary for chapter in smart_minutes.chapters)
    sources.extend(smart_minutes.golden_quotes)
    points: list[str] = []
    seen: set[str] = set()

    def clean(value: str) -> str:
        value = re.sub(r"\[([^\]]+)]\([^)]+\)", r"\1", value)
        value = re.sub(r"^\s*(?:[-*•]|\d+[.、])\s+", "", value)
        value = re.sub(r"^\s*#{1,6}\s*", "", value)
        value = value.replace("**", "").replace("__", "").replace("`", "")
        return re.sub(r"\s+", " ", value).strip()

    def is_heading(raw: str, value: str) -> bool:
        stripped = re.sub(r"^\s*(?:[-*•]|\d+[.、])\s+", "", raw).strip()
        if re.fullmatch(r"(?:#{1,6}\s*)?(?:\*\*|__)[^*_]+(?:\*\*|__)",
                        stripped):
            return True
        return (len(value) <= 18
                and not any(mark in value for mark in "。！？!?；;：:")
                and not value.endswith(("。", "！", "？", "!", "?", ";")))

    def add(value: str) -> None:
        value = clean(value)
        if (not value or len(value) > max_chars
                or value.endswith(("，", ",", "、", "：", ":"))
                or value in seen):
            return
        seen.add(value)
        points.append(value)

    for source in sources:
        if len(points) >= max_points or not str(source or "").strip():
            continue
        # 妙记常把树形条目压在同一行（例如 **标题**- 正文），先恢复列表边界。
        marked = re.sub(r"(?<!\n)([-*•])\s+", r"\n\1 ",
                        str(source).replace("\r", "\n"))
        for raw_item in marked.splitlines():
            if len(points) >= max_points:
                break
            item = clean(raw_item)
            if not item or is_heading(raw_item, item):
                continue
            if len(item) <= max_chars:
                add(item)
                continue
            # 只按完整句拆分；没有完整收束符的超长句不进入卡片。
            sentences = [part.strip() for part in
                         re.split(r"(?<=[。！？!?；;])\s*", item)
                         if part.strip()]
            for sentence in sentences:
                if len(points) >= max_points:
                    break
                if sentence.endswith(tuple("。！？!?；;")):
                    add(sentence)
    return points


def _card_component_count(node: object) -> int:
    if isinstance(node, dict):
        return (1 if node.get("tag") else 0) + sum(
            _card_component_count(value) for value in node.values())
    if isinstance(node, list):
        return sum(_card_component_count(value) for value in node)
    return 0


def _brief_card_within_limits(card: dict) -> bool:
    # Capacity must be measured from the exact compact payload sent to Feishu.
    # Measuring Python's default spaced JSON falsely rejects otherwise valid
    # cards and causes complete DeepSeek items to be removed unnecessarily.
    encoded = _serialize_card(card).encode("utf-8")
    return (_card_component_count(card) <= BRIEF_CARD_MAX_COMPONENTS
            and len(encoded) <= BRIEF_CARD_MAX_BYTES)


def _thin_brief_chart_points(card: dict) -> bool:
    """Halve chart samples in place while retaining both timeline endpoints."""
    changed = False

    def visit(node: object) -> None:
        nonlocal changed
        if isinstance(node, dict):
            if node.get("tag") == "chart":
                spec = node.get("chart_spec")
                data = spec.get("data") if isinstance(spec, dict) else None
                datasets = data if isinstance(data, list) else [data]
                for dataset in datasets:
                    values = (dataset.get("values")
                              if isinstance(dataset, dict) else None)
                    if isinstance(values, list) and len(values) > 2:
                        sampled = values[::2]
                        if sampled[-1] != values[-1]:
                            sampled.append(values[-1])
                        values[:] = sampled
                        changed = True
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(card)
    return changed


def _set_section_panel_content(panel: dict, content: str) -> None:
    """Update a section or collapsible body without changing whole sentences."""
    if panel.get("tag") == "collapsible_panel":
        panel_elements = panel.get("elements")
        if (not isinstance(panel_elements, list) or len(panel_elements) != 1
                or not isinstance(panel_elements[0], dict)
                or panel_elements[0].get("tag") != "markdown"):
            raise ValueError("invalid collapsible panel elements")
        panel_elements[0]["content"] = content
        return
    columns = panel.get("columns")
    if not isinstance(columns, list) or len(columns) != 1:
        raise ValueError("invalid section panel columns")
    column = columns[0]
    panel_elements = column.get("elements") if isinstance(column, dict) else None
    if not isinstance(panel_elements, list) or len(panel_elements) < 2:
        raise ValueError("invalid section panel elements")
    body = panel_elements[1]
    if not isinstance(body, dict) or body.get("tag") != "markdown":
        raise ValueError("invalid section panel body")
    body["content"] = content


def _fit_brief_card(
        card: dict,
        *,
        elements: list[dict],
        model_sections: list[tuple[dict, list[str]]],
        fallback_sections: list[tuple[list[dict], dict]],
        minimal_elements: list[dict],
) -> dict:
    """Fit Card 2.0 by dropping complete items, never slicing model text."""

    def refresh(panel: dict, blocks: list[str]) -> None:
        if blocks:
            _set_section_panel_content(panel, "\n\n".join(blocks))
            title = (((panel.get("header") or {}).get("title") or {})
                     if isinstance(panel.get("header"), dict) else {})
            if isinstance(title, dict):
                content = str(title.get("content") or "")
                title["content"] = re.sub(
                    r"（\d+条）$", f"（{len(blocks)}条）", content)
        elif panel in elements:
            elements.remove(panel)

    # Minute charts dominate payload size and remain legible after deterministic
    # endpoint-preserving downsampling.  Preserve the three complete DeepSeek
    # items shown to operators before considering removal of any model text.
    while (not _brief_card_within_limits(card)
           and _thin_brief_chart_points(card)):
        pass

    # If a pathological payload is still too large, remove complete model items
    # only.  The visible item count is refreshed together with the body so the
    # card can never claim that hidden items are present.
    while not _brief_card_within_limits(card):
        candidates = [entry for entry in model_sections if len(entry[1]) > 1]
        if not candidates:
            candidates = [entry for entry in model_sections if entry[1]]
        if not candidates:
            break
        panel, blocks = candidates[0]
        blocks.pop()
        refresh(panel, blocks)

    # A legacy deterministic body can itself exceed the limit.  Its full text
    # remains in Markdown, so remove complete low-priority panels only after all
    # model analysis has gone.  Overview, peak evidence and smart-minutes links
    # are never shed.
    for container, panel in fallback_sections:
        if _brief_card_within_limits(card):
            break
        if panel in container:
            container.remove(panel)

    if _brief_card_within_limits(card):
        return card

    # Hard fuse: rebuild a bounded deterministic card from independently
    # validated essentials.  Each optional fact block is kept whole only when
    # it fits; no model sentence is sliced.  Charts are reduced to endpoints.
    minimal = copy.deepcopy(card)
    minimal["header"].pop("subtitle", None)
    minimal["body"]["elements"] = []
    for element in minimal_elements:
        candidate = copy.deepcopy(minimal)
        candidate["body"]["elements"].append(copy.deepcopy(element))
        while _thin_brief_chart_points(candidate):
            pass
        if _brief_card_within_limits(candidate):
            minimal = candidate
    if _brief_card_within_limits(minimal):
        log.warning("简报卡已启用容量硬熔断，仅保留可容纳的确定性关键块")
        return minimal

    # This constant payload is intentionally independent of all persisted
    # strings, so callers can never receive an oversized card.
    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "width_mode": "default",
            "summary": {"content": "直播经营简报"},
        },
        "header": {
            "title": {"tag": "plain_text", "content": "直播经营简报"},
            "template": "turquoise",
        },
        "body": {
            "direction": "vertical",
            "elements": [{
                "tag": "markdown",
                "content": "本小时完整产物已保留至 Markdown，卡片因容量限制降级。",
            }],
        },
    }


def build_brief_card(
        cfg: dict, anchor_name: str, stream_id: int, duration_sec: float, *,
        metrics: dict | None = None,
        peak_highlights: list[dict] | None = None,
        smart_minutes: SmartMinutesArtifact | None = None,
        intelligence: HourlyIntelligenceResult) -> dict:
    """Build the single production brief card from ready DeepSeek output."""
    if not isinstance(intelligence, HourlyIntelligenceResult) or intelligence.status != "ready":
        raise ValueError("ready DeepSeek intelligence required")
    from .notify.feishu import (card_v2, collapsible_text, metric_grid, section_panel,
                                _brief_peak_blocks,
                                _brief_intelligence_action_blocks,
                                _brief_intelligence_conclusion_blocks,
                                _brief_intelligence_talktrack_blocks,
                                _data_state, _metric)

    metrics = metrics or {}
    delta = metrics.get("delta") or {}
    period = metrics.get("period") or {}
    status, status_color = _data_state(metrics)
    evidence = _brief_peak_blocks(
        cfg, peak_highlights or [],
        max_items=8, content_is_repaired=True)
    evidence_count = evidence.count("**入选理由**")
    core = metrics.get("core_metrics") or {
        "成交金额": delta.get("pay_amt"),
        "本小时新增观看人数": delta.get("viewer_uv"),
        "本小时进入次数": period.get("visitor_total"),
        "平均在线": period.get("uv_avg"),
        "最高在线": period.get("max_online_uv") or metrics.get("max_online_uv"),
        "成交人数": delta.get("buyer_cnt"),
        "本小时转化": None,
        "新增粉丝": delta.get("atn_uv"),
        "平均停留": period.get("stay_time_pu"),
        "退款金额": delta.get("refund_amt"),
    }
    metric_items = [
        ("成交金额", _metric(core.get("成交金额"), " 元", 2)),
        ("本小时新增观看人数", _metric(core.get("本小时新增观看人数"), " 人")),
        ("本小时进入次数", _metric(core.get("本小时进入次数"), " 次")),
        ("平均在线", _metric(core.get("平均在线"), " 人", 1)),
        ("最高在线", _metric(core.get("最高在线"), " 人")),
        ("成交人数", _metric(core.get("成交人数"), " 人")),
        ("本小时转化", _metric(core.get("本小时转化"), "%", 2)),
        ("新增粉丝", _metric(core.get("新增粉丝"), " 人")),
        ("平均停留", _metric(core.get("平均停留"), " 秒", 1)),
        ("退款金额", _metric(core.get("退款金额"), " 元", 2)),
    ]
    metric_rows: list[dict] = []
    for start in range(0, len(metric_items), 2):
        row = metric_grid(
            metric_items[start:start + 2],
            focus_index=0 if start == 0 else None,
            color="turquoise",
        )
        # 五列指标在手机端会被压成单字竖排。固定拆为 2 列等宽行；
        # none + weighted 使两列在电脑端铺满整行，手机端仍各占一半。
        # 成交金额仅通过底色/字色强调，不再占双倍宽度。
        row["flex_mode"] = "none"
        for column in row.get("columns") or []:
            column["width"] = "weighted"
            column["weight"] = 1
        metric_rows.append(row)
    period_label = str(metrics.get("period_label") or "当前时段")[:80]
    overview_elements: list[dict] = [
        {"tag": "markdown", "content":
         "<font color='turquoise'>LIVE OPERATIONS BRIEF</font>\n"
         f"**{anchor_name}**  ·  {period_label}  ·  "
         f"本期 {int(duration_sec // 60)} 分 {int(duration_sec % 60)} 秒"},
        section_panel(
            "本期速览",
            _brief_snapshot(metrics, evidence_count),
            color="turquoise", kicker="AT A GLANCE",
        ),
        *metric_rows,
    ]
    elements: list[dict] = []
    quality_panel: dict | None = None
    advice_panel: dict | None = None

    charts = _brief_chart(metrics)
    chart_panel: dict | None = None
    if charts:
        # 每张图上方用 markdown 文字标签标明数据（VChart 的 title 在飞书客户端
        # 不渲染，不能依赖它做标注）
        chart_elements: list[dict] = []
        for chart in charts:
            title = (chart.get("chart_spec") or {}).get("title", {})
            label = title.get("text") or "分钟数据"
            subtitle = title.get("subtext") or ""
            chart_heading = f"**{label}**"
            if subtitle:
                chart_heading += f"\n<font color='grey'>{subtitle}</font>"
            chart_elements.append({"tag": "markdown", "content": chart_heading})
            chart_elements.append(chart)
        chart_elements.append({"tag": "markdown", "content": _coverage_text(metrics),
                               "text_size": "notation"})
        chart_panel = {
            "tag": "column_set", "flex_mode": "none",
            "columns": [{
                "tag": "column", "width": "weighted", "weight": 1,
                "padding": "0px",
                "vertical_spacing": "8px",
                "elements": chart_elements,
            }],
        }
        overview_elements.append(chart_panel)
    elif metrics:
        # 抓了分钟数据但不足 2 个有效点：显式说明缺图原因，而不是默默消失
        overview_elements.append({
            "tag": "column_set", "flex_mode": "none",
            "columns": [{
                "tag": "column", "width": "weighted", "weight": 1,
                "background_style": "turquoise-50", "padding": "8px",
                "elements": [
                    {"tag": "markdown", "content": "**分钟走势**"},
                    {"tag": "markdown", "content": "分钟图表覆盖：暂无可用分钟序列",
                     "text_size": "notation"},
                ],
            }],
        })

    elements.append({
        "tag": "column_set", "flex_mode": "none",
        "columns": [{
            "tag": "column", "width": "weighted", "weight": 1,
            "direction": "vertical", "vertical_spacing": "8px",
            "elements": overview_elements,
        }],
    })
    # 图表后的业务阅读顺序固定为：智能纪要 → 下一班次建议 → 经营结论
    # → 峰值原话 → 可复用话术；完整分析正文留在持久化产物和日报上下文。
    safe_minute_url = _safe_brief_url(
        smart_minutes.minute_url if smart_minutes else "")
    smart_link_element: dict | None = None
    if smart_minutes and safe_minute_url:
        note_url = _safe_brief_url(note_doc_link(
            safe_minute_url, smart_minutes.note_doc_token))
        has_smart_content = bool(
            smart_minutes.summary or smart_minutes.chapters
                or smart_minutes.golden_quotes or note_url)
        if has_smart_content:
            smart_lines = [
                "<font color='grey'>AI基于主播原话生成，不代表平台经营数据。</font>",
            ]
            preview_points = _smart_minutes_preview_points(smart_minutes)
            if preview_points:
                smart_lines.append(
                    "**本小时重点**\n" + "\n".join(
                        f"- {point}" for point in preview_points))
            if note_url:
                smart_lines.append(f"[打开智能会议纪要]({note_url})")
            smart_lines.append(f"[查看完整逐字稿]({safe_minute_url})")
            elements.append(section_panel(
                "本小时智能纪要", "\n\n".join(smart_lines),
                color="turquoise", kicker="SMART MINUTES",
            ))
        else:
            elements.append({
                "tag": "markdown",
                "content": f"[打开本小时妙记逐字稿]({safe_minute_url})",
            })
        link_lines = ([f"[打开智能会议纪要]({note_url})"] if note_url else [])
        link_lines.append(f"[查看完整逐字稿]({safe_minute_url})")
        smart_link_element = {
            "tag": "markdown",
            "content": "\n".join(link_lines),
        }

    intelligence_actions = _brief_intelligence_action_blocks(
        intelligence, max_items=3)
    intelligence_conclusions = _brief_intelligence_conclusion_blocks(
        intelligence, max_items=3)
    intelligence_talktracks = _brief_intelligence_talktrack_blocks(
        intelligence, max_items=8)
    advice_text = "\n\n".join(intelligence_actions)
    if advice_text:
        advice_panel = collapsible_text(
            f"DeepSeek 下一班次建议（{len(intelligence_actions)}条）",
            advice_text, expanded=False)
        elements.append(advice_panel)

    conclusion_panel: dict | None = None
    if intelligence_conclusions:
        conclusion_panel = collapsible_text(
            f"DeepSeek 本小时经营结论（{len(intelligence_conclusions)}条）",
            "\n\n".join(intelligence_conclusions), expanded=False)
        elements.append(conclusion_panel)

    evidence_panel: dict | None = None
    if evidence:
        evidence_panel = collapsible_text(
            f"数据峰值与完整原话（{evidence_count}条）",
            evidence, expanded=False)
        elements.append(evidence_panel)

    quality_text = "\n\n".join(intelligence_talktracks)
    if quality_text:
        quality_panel = collapsible_text(
            f"值得复用的完整话术（{len(intelligence_talktracks)}条）",
            quality_text, expanded=False)
        elements.append(quality_panel)

    subtitle = (
        f"{anchor_name} · 本期 {int(duration_sec // 60)} 分 "
        f"{int(duration_sec % 60)} 秒"
    )
    if status == "数据完整":
        status_color = "turquoise"
    card = card_v2(
        "直播经营简报", "turquoise", elements, subtitle=subtitle,
        status=status, status_color=status_color, icon="chart_colorful",
    )
    model_sections = []
    if advice_panel is not None:
        model_sections.append((advice_panel, intelligence_actions))
    if conclusion_panel is not None:
        model_sections.append((conclusion_panel, intelligence_conclusions))
    if evidence_panel is not None:
        # Peak blocks are deterministic whole items plus one disclaimer.  They
        # are not model text, so capacity fitting keeps the section intact.
        pass
    if quality_panel is not None:
        model_sections.append((quality_panel, intelligence_talktracks))
    minimal_elements = [section_panel(
        "本期速览",
        _brief_snapshot(metrics, evidence_count),
        color="turquoise",
        kicker="AT A GLANCE",
    )]
    if chart_panel is not None:
        minimal_elements.append(chart_panel)
    if smart_link_element is not None:
        minimal_elements.append(smart_link_element)
    if evidence_panel is not None:
        minimal_elements.append(evidence_panel)
    fitted = _fit_brief_card(
        card,
        elements=elements,
        model_sections=model_sections,
        fallback_sections=[],
        minimal_elements=minimal_elements,
    )
    if not _brief_card_within_limits(fitted):
        raise ValueError("brief card capacity fuse failed")
    return fitted


def run_briefing(cfg: dict, store, anchor_id: int, stream_id: int,
                 parts: list[Path], *, transcription_result: TranscriptionResult,
                 delivery_key: str | None = None,
                 window_start_ms: int = 0, window_end_ms: int | None = None,
                 job_key: str = "",
                 metric_end_ms: int | None = None,
                 historical_recovery: bool = False,
                 business_session_key: str = "",
                 shift_window_key: str = "",
                 send_notification: bool = True,
                 media_coverage: dict | None = None) -> bool:
    """对一批已闭合分片做转写、DeepSeek 分析与小时事实固化。"""
    nf = (cfg.get("notify", {}) or {}).get("feishu", {}) or {}
    if send_notification and not nf.get("enabled"):
        # 通知关闭是人为配置，不是处理失败。返回成功让 watcher 消费本批分片，
        # 避免每五分钟重复提交同一份简报任务。
        log.info("飞书通知已关闭，跳过直播中简报")
        return True
    chat_id = nf.get("chat_id", "")
    if send_notification and not chat_id:
        log.info("未配置飞书推送群，跳过直播中简报")
        return False
    try:
        media_coverage = _validated_formal_media_coverage(media_coverage)
    except (TypeError, ValueError):
        log.warning("录像时间线未达到正式简报门禁，已停止分析和投递")
        return False
    if send_notification and delivery_key:
        status = delivery_status(delivery_key)
        if status == "sent":
            log.info("相同轮换最终简报已投递，跳过重复生成：%s", delivery_key)
            return True
        if status in {"sending", "delivery_unknown"}:
            log.warning("轮换最终简报投递结果待核验，不自动重发：%s", delivery_key)
            return False
        if status == "failed":
            frozen = frozen_delivery_card(delivery_key)
            if frozen is not None:
                if not _brief_card_within_limits(frozen):
                    log.error("冻结简报卡超出 180 组件/28KB，拒绝重试发送")
                    return False
                log.info("轮换最终简报复用首次冻结载荷重试：%s", delivery_key)
                return send_card_once(cfg, chat_id, frozen, delivery_key)

    anchor = store.get_anchor(anchor_id)
    anchor_name = anchor["name"] if anchor else f"主播#{anchor_id}"
    try:
        # 先抓经营数据（快照与简报区间对齐）：转写含 ASR 冷加载可能耗时 20 分钟，
        # 转写后再抓会把区间外的成交/在线算进本小时（2026-08-04 实测 20:04 触发
        # 20:24 推送，快照错位 24 分钟）。period_sec 用本批分片实测时长估计。
        metrics_text = "（未抓取）"
        diff_text = ""
        stream = store.get_stream(stream_id) if store else None
        live_id = ((stream["live_id"] if stream else "") or
                   (cfg.get("taobao", {}) or {}).get("live_id", ""))
        # 无 liveId 意味着本轮不可能取得经营数据，应在卡片
        # 构建前明确标为 unavailable，不留给展示层猜测 unknown。
        metrics: dict = {"data_state": "unavailable", "data_issues": []}
        part_durs = (_session_part_durations(parts[0].parent) if parts else {})
        frozen_hourly = (
            store.get_hourly_artifact(shift_window_key)
            if store and shift_window_key else None
        )
        frozen_metrics = (
            copy.deepcopy(frozen_hourly.get("metrics") or {})
            if isinstance(frozen_hourly, dict) else {}
        )
        if frozen_metrics.get("frozen_boundary"):
            metrics = frozen_metrics
            metrics_text = str(
                ((frozen_hourly.get("presentation") or {}).get("metrics_text")
                 if isinstance(frozen_hourly, dict) else "")
                or "已读取冻结小时数据")
        elif live_id:
            period_sec = sum(part_durs.get(p.name, 0.0) for p in parts) or 3600.0
            resolved_metric_end_ms = metric_end_ms
            if resolved_metric_end_ms is None and stream and parts:
                last_part = max(parts, key=lambda p: _part_offset(p.parent, p))
                session_media_sec = (_part_offset(last_part.parent, last_part)
                                     + (part_durs.get(last_part.name) or 0.0))
                if session_media_sec > 0:
                    resolved_metric_end_ms = _media_end_epoch_ms(
                        stream["started_at"], session_media_sec)
            metrics, metrics_text, _, diff_text = _fetch_brief_metrics(
                cfg, store, stream_id, live_id, period_sec=period_sec,
                end_ms=resolved_metric_end_ms,
                include_current_totals=not historical_recovery)
        if media_coverage:
            metrics["media_coverage"] = copy.deepcopy(media_coverage)

        all_sentences: list[tuple[int, int, str]] = []
        total_dur = 0.0
        all_sentences = [
            (int(segment.start_ms) + int(window_start_ms),
             int(segment.end_ms) + int(window_start_ms), segment.text)
            for segment in transcription_result.segments if segment.text.strip()
        ]
        total_dur = max(0.0, max(
            (segment.end_ms for segment in transcription_result.segments), default=0) / 1000)
        if store:
            store.save_brief_transcripts(
                stream_id, job_key or f"window_{int(window_start_ms)}", all_sentences)
        # 妙记原文直接面向业务；FunASR 备胎也只过规则质量门禁，不送 AI 补写。
        display_sentences = list(all_sentences)

        # 直播中时间关联证据：与下播复盘同一结构化函数，临时计算不写高亮表；
        # 覆盖校验按分钟序列实际窗口（简报只抓近期），时间轴原点仍是开播时刻。
        # 媒体末尾用「本批最后一个分片的全程偏移+实测时长」（含尾静音），
        # 不能用句子最大时间戳——尾静音/长场次下偏差超 60s 会误拒时间轴门禁。
        peak_highlights: list[dict] = []
        if metrics.get("series"):
            from .highlight.peak import detect_data_peaks
            interval = metrics.get("interval") or {}
            if media_coverage:
                session_media_sec = max(0.0, int(window_end_ms or 0) / 1000)
            else:
                last_part = max(parts, key=lambda p: _part_offset(p.parent, p))
                session_media_sec = (_part_offset(last_part.parent, last_part)
                                     + (part_durs.get(last_part.name) or 0.0))
            recording_start_ms = local_epoch_ms(stream["started_at"] if stream else None)
            # 简报通常在录制结束很久以后才完成 ASR。用处理时刻作为墙钟终点会
            # 把正常媒体误判为时间轴断裂；媒体末尾应由开播时刻+已测媒体时长推导。
            recording_end_ms = (
                int(media_coverage.get("window_end_ms"))
                if media_coverage and media_coverage.get("window_end_ms") is not None
                else _media_end_epoch_ms(
                    stream["started_at"] if stream else None, session_media_sec))
            peak_highlights = detect_data_peaks(
                {"raw": {"series": metrics.get("series") or {}}},
                display_sentences,
                recording_start_ms=recording_start_ms,
                recording_end_ms=recording_end_ms,
                duration_sec=session_media_sec if session_media_sec > 0 else None,
                categories=(cfg.get("highlight", {}) or {}).get("categories", {}),
                coverage_start_ms=interval.get("actual_start_ms"),
                coverage_end_ms=interval.get("actual_end_ms"),
                text_provenance="display_repaired",
            )

        context_segments = offset_transcription_segments(
            transcription_result.segments, int(window_start_ms))
        media_window_ms = int(round(sum(
            float(part_durs.get(part.name) or 0.0) for part in parts
        ) * 1000))
        context_end_ms = resolve_context_window_end(
            context_segments,
            window_start_ms=int(window_start_ms),
            requested_window_end_ms=window_end_ms,
            media_window_ms=media_window_ms,
        )
        context_metrics = clip_qianniu_metrics_to_window(
            normalize_qianniu_metrics(
                metrics,
                recording_start_ms=local_epoch_ms(
                    stream["started_at"] if stream else None),
            ),
            window_start_ms=int(window_start_ms),
            window_end_ms=context_end_ms,
        )
        context_peaks = _intelligence_peak_context(peak_highlights)
        intelligence_context = build_hourly_context(
            stream_id=stream_id,
            live_id=str(live_id or ""),
            anchor_id=anchor_id,
            anchor_name=anchor_name,
            window_start_ms=int(window_start_ms),
            window_end_ms=context_end_ms,
            segments=context_segments,
            smart_minutes=transcription_result.smart,
            metrics=context_metrics,
            peak_highlights=context_peaks,
            history=build_analysis_history(
                store,
                anchor_id=anchor_id,
                stream_id=stream_id,
                live_id=str(live_id or ""),
                context_start_ms=int(window_start_ms),
            ),
        )
        # DeepSeek 是正式简报的分析者；异常或非 ready 终态只重试，不发降级卡。
        try:
            intelligence = IntelligenceService(cfg, store).analyze_hourly(
                intelligence_context)
        except Exception as exc:
            log.warning("小时智能分析失败，正式简报暂停等待重试: %s",
                        str(exc)[:160])
            return False
        if intelligence.status != "ready":
            log.warning(
                "小时智能分析未达到正式交付状态，简报暂停等待重试：job=%s status=%s",
                intelligence.job_key, intelligence.status,
            )
            return False
        try:
            intelligence = validated_result_from_artifact(
                store.get_intelligence_artifact(intelligence.job_key) or {},
                expected_job_key=intelligence.job_key,
                expected_status=intelligence.status,
            )
        except (TypeError, ValueError):
            log.warning("小时智能产物完整性校验失败，正式简报暂停：job=%s",
                        intelligence.job_key)
            return False

        # 妙记负责纪要；不再构造第二份 AI 摘要正文。
        smart_complete = transcription_result.quality_status == "complete"
        smart_for_card = transcription_result.smart if smart_complete else (
            SmartMinutesArtifact(
                minute_token=transcription_result.smart.minute_token,
                minute_url=transcription_result.smart.minute_url,
            ) if transcription_result.smart else None)
        if store and business_session_key and shift_window_key:
            from .business_facts import absolute_hour_window, build_hourly_artifact
            recording_start_ms = local_epoch_ms(
                stream["started_at"] if stream else None)
            if media_coverage:
                artifact_window_start_ms = int(media_coverage["window_start_ms"])
                artifact_window_end_ms = int(media_coverage["window_end_ms"])
            elif recording_start_ms is not None:
                absolute_start, absolute_end = absolute_hour_window(
                    recording_start_ms, int(window_start_ms))
                artifact_window_start_ms = int(absolute_start.timestamp() * 1000)
                artifact_window_end_ms = int(absolute_end.timestamp() * 1000)
            else:
                artifact_window_start_ms = int(window_start_ms)
                artifact_window_end_ms = int(window_end_ms or context_end_ms)
            transcript_state = transcription_result.quality_status
            from .business_facts import build_core_metric_facts
            core_facts = build_core_metric_facts(
                metrics,
                window_start_ms=artifact_window_start_ms,
                window_end_ms=artifact_window_end_ms,
            )
            metrics_ready = (
                metrics.get("data_state") == "ok"
                and bool(metrics.get("frozen_boundary"))
                and all(
                    item.get("quality_state") == "complete"
                    for item in core_facts.values())
            )
            is_daily_boundary = (
                not send_notification
                and (shift_window_key.endswith(":initial")
                     or shift_window_key.endswith(":final"))
            )
            delivery_ready = (
                (metrics_ready or is_daily_boundary)
                and transcript_state in {"complete", "fallback"}
                and intelligence is not None
                and str(getattr(intelligence, "status", "")) == "ready"
                and bool(str(getattr(intelligence, "full_analysis", "")).strip())
                and bool(getattr(intelligence, "business_conclusions", None))
            )
            artifact_state = "complete" if delivery_ready else "partial"
            previous_sources = (
                list(frozen_hourly.get("sources") or [])
                if isinstance(frozen_hourly, dict) else [])
            artifact = build_hourly_artifact(
                business_session=business_session_key,
                shift_window=shift_window_key,
                window_start_ms=artifact_window_start_ms,
                window_end_ms=artifact_window_end_ms,
                identity={"anchor_id": anchor_id, "anchor_name": anchor_name,
                          "live_id": str(live_id or "")},
                metrics=metrics,
                series=metrics.get("series") or {},
                transcription=json.loads(transcription_result.to_json()),
                analysis=(intelligence.to_dict()
                          if hasattr(intelligence, "to_dict") else {}),
                presentation={"metrics_text": metrics_text},
                quality={"state": artifact_state,
                         "stage": (
                             "ready_for_daily" if delivery_ready and is_daily_boundary
                             else "ready_for_delivery" if delivery_ready
                             else "waiting_complete_inputs"),
                         "metrics_frozen": bool(metrics.get("frozen_boundary")),
                         "transcription_state": transcript_state,
                         "intelligence_state": (
                             str(getattr(intelligence, "status", ""))
                             if intelligence is not None else "missing"),
                         "data_state": metrics.get("data_state", ""),
                         "brief_delivery_allowed": not is_daily_boundary,
                         **({"media_coverage": copy.deepcopy(media_coverage)}
                            if media_coverage else {})},
                sources=previous_sources,
            )
            store.save_hourly_artifact(shift_window_key, artifact)
            if not delivery_ready:
                missing = [
                    label for label, item in core_facts.items()
                    if item.get("quality_state") != "complete"
                ]
                log.warning(
                    "排班小时产物未完整，正式简报继续等待："
                    "window=%s metrics=%s transcript=%s intelligence=%s",
                    shift_window_key,
                    ",".join(missing) or metrics.get("data_state", "unknown"),
                    transcript_state,
                    getattr(intelligence, "status", "missing") if intelligence else "missing",
                )
                return False
        if not send_notification:
            log.info("小时事实已固化，不发额外简报：window=%s",
                     shift_window_key or job_key)
            return True
        # Re-check the persisted fact immediately before freezing/sending.  A
        # stale or mutated retry cannot bypass the original 20-minute gate.
        try:
            _validated_formal_media_coverage(
                metrics.get("media_coverage") if media_coverage else None)
        except (TypeError, ValueError):
            log.warning("正式简报投递前录像覆盖复核失败，已停止投递")
            return False
        card = build_brief_card(
            cfg, anchor_name, stream_id, total_dur,
            metrics=metrics, peak_highlights=peak_highlights,
            smart_minutes=smart_for_card,
            intelligence=intelligence,
        )
        if not _brief_card_within_limits(card):
            log.error("简报卡超出 180 组件/28KB，已在传输前拒绝")
            return False
        if delivery_key:
            if not send_card_once(cfg, chat_id, card, delivery_key):
                return False
        else:
            send_card(cfg, chat_id, card)
        # 简报推送成功才落快照（下一期差值的基线）；失败重试不得污染快照链。
        if (store and metrics.get("pay_amt") is not None
                and not metrics.get("frozen_boundary")):
            save_brief_snapshot(store, stream_id, metrics, snapshot_kind="brief")
        log.info("直播中简报已推送：场次 #%d，%d 个分片，%d 句，数据：%s",
                 stream_id, len(parts), len(all_sentences),
                 metrics_text[:40])
        return True
    except MtopAuthError:
        raise
    except Exception as e:
        stage, error_type = safe_delivery_error_fields(e)
        log.warning(
            "直播中简报失败（不影响录制与最终复盘）: "
            "stage=%s error_type=%s\n%s",
            stage, error_type, traceback.format_exc())
        return False
