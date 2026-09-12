"""经营复盘中心的纯数据层。

这里不访问网络、不写飞书，专门把 SQLite 的原始事实整理成稳定业务键和展示字段。
这样日/周环比与缺失值规则既可测试，也不会因同步重试而改变口径。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from ..config import now_shanghai


DAY_METRICS = (
    ("pay_amt", "成交金额"),
    ("look_uv", "观看人数"),
    ("pay_byr_cnt", "成交人数"),
    ("pay_ord_cnt", "成交订单"),
    ("pay_itm_qty", "成交件数"),
)


def _number(value: Any) -> float | int | None:
    """数值为空或 NaN 时一律保持缺失，绝不静默变成 0。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def _iso_day(compact_day: str) -> str:
    raw = str(compact_day or "")
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return raw


def _parse_day(compact_day: str) -> date | None:
    try:
        return datetime.strptime(str(compact_day), "%Y%m%d").date()
    except (TypeError, ValueError):
        return None


def _state_for_day(day: str) -> str:
    parsed = _parse_day(day)
    if parsed is None:
        return "部分数据"
    return "进行中" if parsed == now_shanghai().date() else "已冻结"


def _sum_present(rows: list[dict], field: str) -> float | int | None:
    values = [_number(row.get(field)) for row in rows]
    present = [value for value in values if value is not None]
    if not present:
        return None
    total = sum(present)
    return int(total) if float(total).is_integer() else total


def compare_metric(current: float | None, previous: float | None) -> dict:
    """统一比较口径：缺失不算、昨日为零只标识新增。"""
    current, previous = _number(current), _number(previous)
    if current is None or previous is None:
        return {"current": current, "previous": previous, "delta": None,
                "rate": None, "label": "暂无"}
    if previous == 0:
        return {"current": current, "previous": previous, "delta": current,
                "rate": None, "label": "新增" if current > 0 else "0.0%"}
    delta = current - previous
    return {"current": current, "previous": previous, "delta": delta,
            "rate": delta / previous, "label": f"{delta / previous:+.1%}"}


def _daibo_rows(store) -> list[dict]:
    try:
        rows = store.query("SELECT * FROM daibo_daily ORDER BY date, daibo_id")
    except Exception:
        return []
    return [dict(row) for row in rows]


def build_anchor_day_rows(store) -> list[dict]:
    """每位主播每天一行，严格取前一自然日作为日环比对照。"""
    source = _daibo_rows(store)
    indexed = {(str(row.get("date") or ""), str(row.get("daibo_id") or "")): row
               for row in source}
    output: list[dict] = []
    for item in source:
        raw_day = str(item.get("date") or "")
        daibo_id = str(item.get("daibo_id") or "")
        parsed = _parse_day(raw_day)
        if not raw_day or not daibo_id or parsed is None:
            continue
        yesterday = (parsed - timedelta(days=1)).strftime("%Y%m%d")
        prior = indexed.get((yesterday, daibo_id), {})
        pay = compare_metric(item.get("pay_amt"), prior.get("pay_amt"))
        row: dict[str, Any] = {
            "主播": str(item.get("daibo_name") or "未知主播"),
            "日期": _iso_day(raw_day),
            "成交金额": _number(item.get("pay_amt")),
            "昨日成交金额": pay["previous"],
            "成交金额增减": pay["delta"],
            "成交金额日环比": pay["label"],
            "观看人数": _number(item.get("look_uv")),
            "观看次数": _number(item.get("look_pv")),
            "成交人数": _number(item.get("pay_byr_cnt")),
            "成交订单": _number(item.get("pay_ord_cnt")),
            "成交件数": _number(item.get("pay_itm_qty")),
            "成交转化率": _number(item.get("cvr_pay")),
            "客单价": _number(item.get("atv")),
            "商品点击人数": _number(item.get("ipv_uv")),
            "商品点击次数": _number(item.get("ipv")),
            "商品点击率": _number(item.get("ctr_itm")),
            "加购人数": _number(item.get("cart_uv")),
            "加购次数": _number(item.get("cart_pv")),
            "加购件数": _number(item.get("cart_itm_qty")),
            "新增粉丝": _number(item.get("atn_uv")),
            "转粉率": _number(item.get("atn_uv_rate")),
            "评论人数": _number(item.get("cmt_uv")),
            "分享人数": _number(item.get("shr_uv")),
            "点赞人数": _number(item.get("fvr_uv")),
            "上播时长秒": _number(item.get("look_time_sec")),
            "数据状态": _state_for_day(raw_day),
            "业务键": f"{raw_day}:{daibo_id}",
        }
        output.append(row)
    return output


def _week_row(anchor_rows: list[dict], week_start: date, as_of: date) -> list[dict]:
    return [row for row in anchor_rows
            if (parsed := _parse_day(str(row.get("date") or ""))) is not None
            and week_start <= parsed <= as_of]


def _effective_offsets(rows: list[dict], week_start: date) -> set[int]:
    return {
        (_parse_day(str(row.get("date"))).__sub__(week_start).days)
        for row in rows
        if _number(row.get("pay_amt")) is not None and _parse_day(str(row.get("date")))
    }


def build_anchor_week_rows(store, today: date | None = None) -> list[dict]:
    """自然周累计；仅相同有效日集合才产生周环比，避免缺日补零。"""
    today = today or now_shanghai().date()
    by_anchor: dict[str, list[dict]] = defaultdict(list)
    for row in _daibo_rows(store):
        if _parse_day(str(row.get("date") or "")) and str(row.get("daibo_id") or ""):
            by_anchor[str(row["daibo_id"])].append(row)

    result: list[dict] = []
    today_monday = today - timedelta(days=today.weekday())
    for daibo_id, rows in by_anchor.items():
        names = [str(row.get("daibo_name") or "") for row in rows if row.get("daibo_name")]
        anchor_name = names[-1] if names else "未知主播"
        starts = sorted({
            parsed - timedelta(days=parsed.weekday())
            for row in rows if (parsed := _parse_day(str(row.get("date") or ""))) is not None
        })
        for week_start in starts:
            if week_start > today_monday:
                continue
            end = today if week_start == today_monday else week_start + timedelta(days=6)
            current_rows = _week_row(rows, week_start, end)
            previous_start = week_start - timedelta(days=7)
            previous_end = previous_start + (end - week_start)
            previous_rows = _week_row(rows, previous_start, previous_end)
            offsets, prior_offsets = (_effective_offsets(current_rows, week_start),
                                      _effective_offsets(previous_rows, previous_start))
            current_pay, previous_pay = (_sum_present(current_rows, "pay_amt"),
                                         _sum_present(previous_rows, "pay_amt"))
            comparison = (compare_metric(current_pay, previous_pay)
                          if offsets and offsets == prior_offsets else {
                              "current": current_pay, "previous": previous_pay,
                              "delta": None, "rate": None, "label": "暂无"})
            week_number = week_start.isocalendar().week
            state = "进行中" if week_start == today_monday else "已冻结"
            result.append({
                "主播": anchor_name,
                "自然周": f"{week_start.year}-W{week_number:02d}",
                "周开始": week_start.isoformat(),
                "周截止": end.isoformat(),
                "本周成交金额": current_pay,
                "上周同期成交金额": previous_pay,
                "成交金额增减": comparison["delta"],
                "成交金额周环比": comparison["label"],
                "本周有效天数": len(offsets),
                "上周同期有效天数": len(prior_offsets),
                "本周观看人数": _sum_present(current_rows, "look_uv"),
                "本周成交人数": _sum_present(current_rows, "pay_byr_cnt"),
                "本周成交订单": _sum_present(current_rows, "pay_ord_cnt"),
                "本周成交件数": _sum_present(current_rows, "pay_itm_qty"),
                "数据状态": state,
                "业务键": f"{week_start.strftime('%Y%m%d')}:{daibo_id}",
            })
    return sorted(result, key=lambda row: (row["周开始"], row["主播"]))


def _decode(value: Any, fallback: Any) -> Any:
    if isinstance(value, type(fallback)):
        return value
    try:
        decoded = json.loads(str(value or ""))
    except (TypeError, ValueError):
        return fallback
    return decoded if isinstance(decoded, type(fallback)) else fallback


def build_platform_rows(store) -> list[dict]:
    """一条业务直播日对应一条可读复盘记录，不泄露本地技术碎片 ID。"""
    sessions = store.query("SELECT * FROM platform_sessions ORDER BY started_at, live_id")
    reviews = {
        str(row["live_id"]): dict(row)
        for row in store.query("SELECT * FROM platform_reviews")
    }
    jobs = {
        str(row["live_id"]): dict(row)
        for row in store.query("SELECT * FROM platform_review_jobs")
    }
    counts = {
        str(row["live_id"]): int(row["count"] or 0)
        for row in store.query(
            "SELECT live_id,COUNT(*) AS count FROM platform_anchor_metrics GROUP BY live_id")
    }
    result: list[dict] = []
    covered_live_ids: set[str] = set()
    # 新终稿把多个技术 liveId 冻结在同一份业务日载荷中；Base 只展示一行。
    business_reviews: dict[str, tuple[dict, dict, dict]] = {}
    for review in reviews.values():
        payload = _decode(review.get("payload"), {})
        business_key = str(payload.get("business_session_key") or "").strip()
        if not business_key:
            continue
        for actual_live_id in payload.get("live_ids") or []:
            covered_live_ids.add(str(actual_live_id))
        job = jobs.get(str(review.get("live_id") or ""), {})
        business_reviews[business_key] = (review, job, payload)
    for business_key, (review, job, payload) in sorted(business_reviews.items()):
        metrics = payload.get("display_metrics") or {}
        started = str(payload.get("started_at") or "")
        review_state = str(review.get("data_state") or "")
        state = {"complete": "完整", "partial": "部分"}.get(review_state, "已冻结")
        job_state = {"sent": "已复盘", "partial_sent": "部分复盘",
                     "waiting": "待复盘", "failed": "投递待核验"}.get(
                         str(job.get("status") or ""), "待复盘")
        result.append({
            "直播名称": f"{started[:10] or '日期暂无'} · 业务直播日",
            "直播日期": started[:10] or None,
            "开始时间": started or None,
            "结束时间": str(payload.get("ended_at") or "") or None,
            "整场成交金额": _number(metrics.get("pay_amt")),
            "观看人数": _number(metrics.get("viewer_uv")),
            "观看次数": _number(metrics.get("viewer_pv")),
            "成交人数": _number(metrics.get("buyer_cnt")),
            "成交订单": _number(metrics.get("order_cnt")),
            "成交件数": _number(metrics.get("item_qty")),
            "最高在线": _number(metrics.get("max_online_uv")),
            "主播数": len(payload.get("anchors") or []) or None,
            "数据状态": state,
            "复盘状态": job_state,
            "报告链接": "已生成（本机）" if review.get("report_path") else "暂无",
            "数据限制": "；".join(str(item) for item in payload.get("data_issues") or []),
            "业务键": business_key,
        })
    for source in sessions:
        row = dict(source)
        live_id = str(row.get("live_id") or "")
        if live_id in covered_live_ids:
            continue
        review, job = reviews.get(live_id, {}), jobs.get(live_id, {})
        review_payload = _decode(review.get("payload"), {})
        review_state = str(review.get("data_state") or "")
        state = {"complete": "完整", "partial": "部分"}.get(review_state, "已冻结")
        job_state = {"sent": "已复盘", "partial_sent": "部分复盘",
                     "waiting": "待复盘", "failed": "投递待核验"}.get(
                         str(job.get("status") or ""), "待复盘")
        started = str(row.get("started_at") or "")
        result.append({
            "直播名称": f"{started[:10] or '日期暂无'} · 整场直播",
            "直播日期": started[:10] or None,
            "开始时间": started or None,
            "结束时间": str(row.get("ended_at") or "") or None,
            "整场成交金额": _number(row.get("pay_amt")),
            "观看人数": _number(row.get("viewer_uv")),
            "观看次数": _number(row.get("viewer_pv")),
            "成交人数": _number(row.get("buyer_cnt")),
            "成交订单": _number(row.get("order_cnt")),
            "成交件数": _number(row.get("item_qty")),
            "最高在线": _number(row.get("max_online_uv")),
            "主播数": counts.get(live_id, 0) or None,
            "数据状态": state,
            "复盘状态": job_state,
            "报告链接": "已生成（本机）" if review.get("report_path") else "暂无",
            "数据限制": "；".join(str(item) for item in review_payload.get(
                "data_issues", []) if item),
            "业务键": live_id,
        })
    return result


def _text_hash(text: str) -> str:
    normalized = re.sub(r"[\s，。！？、；：,.!?;:]+", "", text).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def build_talktrack_rows(cfg: dict, store) -> list[dict]:
    """同步数据关联与高质复用两类高亮，并带入已有人工评价。"""
    from ..asr.clean import outward_record_text
    try:
        feedback_by_hash = store.get_highlight_feedback()
    except Exception:
        feedback_by_hash = {}
    rows = store.query(
        """SELECT h.*,s.live_id,s.started_at,a.name AS anchor_name
           FROM highlights h JOIN streams s ON s.id=h.stream_id
           LEFT JOIN anchors a ON a.id=h.anchor_id
           WHERE h.kind IN ('quality','data_association')
           ORDER BY s.started_at,h.start_ms,h.id""")
    result: list[dict] = []
    for raw in rows:
        item = dict(raw)
        text = outward_record_text(
            cfg, item,
            meta_field=("quality_meta" if item.get("kind") == "quality"
                        else "peak_meta"),
            max_chars=220,
        )
        if not text:
            continue
        kind = "高质复用" if item.get("kind") == "quality" else "数据关联"
        meta = _decode(item.get("quality_meta") if kind == "高质复用"
                       else item.get("peak_meta"), {})
        text_hash = _text_hash(text)
        feedback = feedback_by_hash.get(text_hash) or {}
        started = str(item.get("started_at") or "")
        absolute = ""
        if started:
            try:
                absolute = (datetime.strptime(started, "%Y-%m-%d %H:%M:%S")
                            + timedelta(milliseconds=int(item.get("start_ms") or 0))).strftime(
                                "%Y-%m-%d %H:%M:%S")
            except ValueError:
                absolute = started
        if kind == "高质复用":
            category = str(meta.get("category") or "高质话术")
            score = _number(meta.get("quality_score") or item.get("score"))
            rationale = "、".join(str(x) for x in meta.get("rationale") or [])
            observation = ""
        else:
            category = str(meta.get("label") or meta.get("type") or "数据峰值")
            score = None
            rationale = "数据变化附近的原话；关联不代表因果"
            observation = "；".join(
                str(value) for value in (meta.get("observations") or []) if value)
        live_id = str(item.get("live_id") or "")
        result.append({
            "原话": text,
            "主播": str(item.get("anchor_name") or "未知主播"),
            "直播": f"{started[:10] or '日期暂无'} · 整场直播" if live_id else "本地片段（待归并）",
            "发生时间": absolute or None,
            "类型": kind,
            "类别": category,
            "质量分": score,
            "入选理由": rationale or "暂无",
            "数据观察": observation or "暂无",
            "人工评价": str(feedback.get("rating") or "待评"),
            "反馈备注": str(feedback.get("note") or ""),
            "业务键": f"{live_id or 'local'}:{kind}:{started}:{item.get('start_ms') or 0}:{text_hash}",
        })
    return result


def build_action_rows(store) -> list[dict]:
    """Export the current frozen daily next-actions without legacy outcomes."""
    from ..intelligence.platform import load_frozen_platform_intelligence

    output: list[dict] = []
    reviews = store.query(
        "SELECT live_id,payload FROM platform_reviews ORDER BY updated_at,live_id")
    for row in reviews:
        live_id = str(row["live_id"] or "")
        payload = _decode(row["payload"], {})
        if not live_id or not isinstance(payload, dict):
            continue
        try:
            _context, result, _rejected = load_frozen_platform_intelligence(
                store, live_id,
                expected_result=payload.get("platform_intelligence"),
            )
        except Exception:
            # 任何绑定错误都只关闭该场自动行动同步。
            continue
        for index, action in enumerate(result.next_actions, 1):
            business_key = f"{live_id}:daily-action:{index}"
            output.append({
                "行动": action,
                "直播": f"{str(payload.get('started_at') or '')[:10] or '日期暂无'} · 整场直播",
                "动作": action,
                "证据": f"DeepSeek 日报任务 {result.job_key}",
                "状态": "待确认",
                "业务键": business_key,
            })
    return list({str(row["业务键"]): row for row in reversed(output)}.values())[::-1]
