"""直播经营日报：按业务直播日合并多个 liveId 和主播技术碎片。"""
from __future__ import annotations

import json
import hashlib
import logging
import re
import time
from datetime import timedelta

from ..business_facts import business_operating_bounds
from ..config import local_epoch_ms, parse_local_datetime
from .report import generate_platform_report

log = logging.getLogger("review.platform")

RETRY_MINUTES = (0, 2, 5, 10, 20, 30)


def _decode(value, fallback):
    if isinstance(value, type(fallback)):
        return value
    if not value:
        return fallback
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if isinstance(parsed, type(fallback)) else fallback


def _normalise_text(value: str) -> str:
    return re.sub(r"[\s，。！？、；：,.!?;:]+", "", str(value or "")).lower()


def _same_text(left: str, right: str) -> bool:
    from ..asr.clean import numbers_equivalent

    a, b = _normalise_text(left), _normalise_text(right)
    if not a or not b:
        return False
    if not numbers_equivalent(left, right):
        return False
    if a == b or a in b or b in a:
        return True
    from rapidfuzz import fuzz
    return fuzz.ratio(a, b) >= 90


def _clock(started_at: str, offset_ms: int) -> tuple[str, str]:
    started = parse_local_datetime(started_at)
    if started is None:
        return "时间暂无", ""
    absolute = started + timedelta(milliseconds=max(0, int(offset_ms or 0)))
    return absolute.strftime("%H:%M:%S"), absolute.isoformat(timespec="milliseconds")


def _merge_highlights(cfg: dict, store, streams: list[dict], kind: str) -> list[dict]:
    from ..asr.clean import (OUTWARD_DISPLAY_REPAIRED, outward_record_text)
    from ..highlight.peak import sanitize_peak_meta_for_outward

    merged: list[dict] = []
    for stream in streams:
        for source in store.get_highlights(int(stream["id"])):
            if str(source["kind"] or "") != kind:
                continue
            peak = sanitize_peak_meta_for_outward(
                _decode(source["peak_meta"], {}), cfg)
            candidates = [outward_record_text(
                cfg, source, meta_field=("quality_meta" if kind == "quality"
                                        else "peak_meta"), max_chars=220)]
            if kind == "data_association" and peak.get("excerpts"):
                candidates = [str(item.get("text") or "").strip()
                              for item in peak["excerpts"]]
            for raw in (text for text in candidates if text):
                duplicate = next((item for item in merged
                                  if _same_text(item.get("transcript") or "", raw)), None)
                sid = int(stream["id"])
                if duplicate is not None:
                    if sid not in duplicate["source_stream_ids"]:
                        duplicate["source_stream_ids"].append(sid)
                    # 去重粒度是“原话”；同一句原话可保留多个指标观察。
                    if peak and peak not in duplicate["peak_observations"]:
                        duplicate["peak_observations"].append(peak)
                    continue
                clock, absolute = _clock(str(stream.get("started_at") or ""),
                                         int(source["start_ms"] or 0))
                merged.append({
                    "stream_id": sid,
                    "source_stream_ids": [sid],
                    "anchor_id": int(stream["anchor_id"]),
                    "start_ms": int(source["start_ms"] or 0),
                    "end_ms": int(source["end_ms"] or 0),
                    "clock": clock,
                    "absolute_time": absolute,
                    "time": clock,
                    "score": float(source["score"] or 0),
                    "reasons": _decode(source["reasons"], []),
                    "transcript": raw,
                    "text_provenance": OUTWARD_DISPLAY_REPAIRED,
                    "kind": kind,
                    "peak_meta": peak,
                    "peak_observations": [peak] if peak else [],
                    "quality_meta": _decode(source["quality_meta"], {}),
                })
    return sorted(merged, key=lambda item: (-item["score"], item["absolute_time"]))


def _anchor_metrics(store, live_id: str) -> dict[int, dict]:
    from ..metrics.daibo import get_platform_anchor_metrics
    result: dict[int, dict] = {}
    for row in get_platform_anchor_metrics(store, live_id):
        clean = dict(row)
        clean.pop("raw", None)
        result[int(clean["anchor_id"])] = clean
    return result


_FROZEN_REQUIRED = ("pay_amt", "viewer_uv", "buyer_cnt", "order_cnt", "item_qty")


def _frozen_metric_issues(row: dict) -> list[str]:
    labels = {
        "pay_amt": "整场成交金额", "viewer_uv": "整场观看人数",
        "buyer_cnt": "整场成交人数", "order_cnt": "整场成交订单",
        "item_qty": "整场成交件数",
    }
    return [f"缺少{labels[key]}" for key in _FROZEN_REQUIRED
            if row.get(key) is None]


def _metric_consistency_issues(summary: dict) -> list[str]:
    """只做官方总账与完整主播明细间的核对，不改写任何一侧原值。"""
    whole = summary.get("display_metrics") or {}
    anchors = summary.get("anchors") or []
    specs = (
        ("pay_amt", "pay_amt", "成交金额", 0.01, "元"),
        ("order_cnt", "pay_ord_cnt", "成交订单", 0, "单"),
        ("item_qty", "pay_itm_qty", "成交件数", 0, "件"),
    )
    issues: list[str] = []
    for whole_key, anchor_key, label, tolerance, unit in specs:
        values = [(row.get("metrics") or {}).get(anchor_key) for row in anchors]
        if whole.get(whole_key) is None or not anchors or any(v is None for v in values):
            continue
        anchor_total = sum(float(v) for v in values)
        whole_total = float(whole[whole_key])
        if abs(anchor_total - whole_total) > tolerance:
            issues.append(
                f"官方整场{label} {whole_total:g}{unit} 与主播明细合计 "
                f"{anchor_total:g}{unit} 不一致，保留双方官方值待平台核验")
    return issues


def build_platform_review_summary(cfg: dict, store, live_id: str) -> dict:
    """构造 liveId 级纯数据摘要；不请求网络、不调用 LLM、不发送消息。"""
    from ..asr.clean import OUTWARD_DISPLAY_REPAIRED, outward_display_text
    stream_rows = store.query(
        """SELECT s.*,a.name anchor_name FROM streams s
           LEFT JOIN anchors a ON a.id=s.anchor_id
           WHERE s.live_id=? AND s.file_path!=''
           ORDER BY s.started_at,s.id""",
        (str(live_id),),
    )
    streams = [dict(row) for row in stream_rows]
    metrics_by_anchor = _anchor_metrics(store, str(live_id))

    grouped: dict[int, list[dict]] = {}
    for stream in streams:
        grouped.setdefault(int(stream["anchor_id"]), []).append(stream)

    anchors: list[dict] = []
    for anchor_id, owned in grouped.items():
        ids = [int(row["id"]) for row in owned]
        transcripts: list[dict] = []
        for stream in owned:
            for row in store.get_transcripts(int(stream["id"])):
                clock, absolute = _clock(str(stream.get("started_at") or ""),
                                         int(row["start_ms"] or 0))
                source_hash = hashlib.sha256(
                    str(row["text"] or "").encode("utf-8")).hexdigest()
                if (str(row["display_state"] or "") != "repaired"
                        or str(row["text_provenance"] or "") != OUTWARD_DISPLAY_REPAIRED
                        or str(row["source_hash"] or "") != source_hash):
                    continue
                body = outward_display_text(
                    str(row["display_text"] or ""), cfg,
                    provenance=OUTWARD_DISPLAY_REPAIRED, max_chars=500)
                if not body:
                    continue
                transcripts.append({
                    "stream_id": int(stream["id"]),
                    "start_ms": int(row["start_ms"] or 0),
                    "end_ms": int(row["end_ms"] or 0),
                    "clock": clock,
                    "absolute_time": absolute,
                    "text": body,
                    "text_provenance": OUTWARD_DISPLAY_REPAIRED,
                })
        transcripts.sort(key=lambda item: item["absolute_time"])

        placeholders = ",".join("?" for _ in ids)
        occurrences = store.query(
            f"""SELECT * FROM talktrack_occurrences
                WHERE stream_id IN ({placeholders}) ORDER BY id""", tuple(ids)) if ids else []
        categories: dict[str, int] = {}
        talktracks: list[dict] = []
        seen_tracks: set[tuple[str, str]] = set()
        for row in occurrences:
            category = str(row["category"] or "其他")
            categories[category] = categories.get(category, 0) + 1
            marker = (category, str(row["norm_text"] or ""))
            if marker in seen_tracks:
                continue
            seen_tracks.add(marker)
            if str(row["text_provenance"] or "") != OUTWARD_DISPLAY_REPAIRED:
                continue
            body = outward_display_text(
                str(row["text"] or ""), cfg,
                provenance=OUTWARD_DISPLAY_REPAIRED, max_chars=500)
            if body:
                talktracks.append({
                    "category": category, "text": body,
                    "text_provenance": OUTWARD_DISPLAY_REPAIRED,
                    "stream_id": int(row["stream_id"]),
                })

        smart_minutes: list[dict] = []
        from ..transcription.models import TranscriptionResult
        for stream in owned:
            for job in store.get_stream_transcription_jobs(int(stream["id"])):
                result = TranscriptionResult.from_json(job["result_json"])
                artifact = result.smart if result else None
                if not artifact or not artifact.minute_url:
                    continue
                smart_minutes.append({
                    "stream_id": int(stream["id"]),
                    "window_start_ms": int(job["window_start_ms"] or 0),
                    "window_end_ms": int(job["window_end_ms"] or 0),
                    "provider": result.provider,
                    "minute_url": artifact.minute_url,
                    "minute_token": artifact.minute_token,
                    "note_id": artifact.note_id,
                    "note_doc_token": artifact.note_doc_token,
                    "summary": artifact.summary,
                    "chapters": [
                        {"start_ms": chapter.start_ms, "end_ms": chapter.end_ms,
                         "title": chapter.title, "summary": chapter.summary}
                        for chapter in artifact.chapters
                    ],
                    "golden_quotes": list(artifact.golden_quotes),
                })
        smart_minutes.sort(key=lambda item: (item["stream_id"], item["window_start_ms"]))

        anchors.append({
            "anchor_id": anchor_id,
            "anchor_name": str(owned[0].get("anchor_name") or f"主播#{anchor_id}"),
            "stream_ids": ids,
            "fragment_count": len(ids),
            "started_at": min(str(row.get("started_at") or "") for row in owned),
            "ended_at": max(str(row.get("ended_at") or "") for row in owned),
            "duration_sec": round(sum(float(row.get("duration_sec") or 0) for row in owned), 3),
            "transcripts": transcripts,
            "sentence_count": len(transcripts),
            "char_count": sum(len(row["text"]) for row in transcripts),
            "smart_minutes": smart_minutes,
            "data_highlights": _merge_highlights(
                cfg, store, owned, "data_association"),
            "quality_highlights": _merge_highlights(cfg, store, owned, "quality"),
            "categories": categories,
            "talktracks": talktracks,
            "metrics": metrics_by_anchor.get(anchor_id, {
                "live_id": str(live_id), "anchor_id": anchor_id,
                "data_state": "unavailable", "data_issues": "[]",
            }),
        })

    platform_rows = store.query(
        "SELECT * FROM platform_sessions WHERE live_id=?", (str(live_id),))
    platform_clean = dict(platform_rows[0]) if platform_rows else {}
    platform_clean.pop("raw", None)
    frozen_issues = _frozen_metric_issues(platform_clean) if platform_rows else []
    display_metrics = ({
        **platform_clean,
        "source": "platform_sessions.frozen",
        "source_scope": "平台完整场次（冻结总账）",
        "data_state": "partial" if frozen_issues else "ok",
        "data_issues": frozen_issues,
    } if platform_rows else {
        "live_id": str(live_id), "data_state": "unavailable",
        "source": "", "source_scope": "平台冻结总账暂未返回",
    })
    duration_sec = round(sum(float(row.get("duration_sec") or 0) for row in streams), 3)
    summary = {
        "live_id": str(live_id),
        "scope_stream_ids": [int(row["id"]) for row in streams],
        "scope_count": len(streams),
        "started_at": min((str(row.get("started_at") or "") for row in streams), default=""),
        "ended_at": max((str(row.get("ended_at") or "") for row in streams), default=""),
        "duration_sec": duration_sec,
        "anchor_count": len(anchors),
        "anchors": anchors,
        "display_metrics": display_metrics,
    }
    quality_issues = list(display_metrics.get("data_issues") or [])
    for anchor in anchors:
        anchor_issues = _decode((anchor.get("metrics") or {}).get("data_issues"), [])
        quality_issues.extend(
            f"{anchor['anchor_name']}：{item}" for item in anchor_issues)
    quality_issues.extend(_metric_consistency_issues(summary))
    states = [str(display_metrics.get("data_state") or "unavailable")]
    states.extend(str((anchor.get("metrics") or {}).get("data_state") or "unavailable")
                  for anchor in anchors)
    if streams and anchors and all(state == "ok" for state in states) and not quality_issues:
        summary["data_state"] = "complete"
    elif any(state in {"ok", "partial"} for state in states):
        summary["data_state"] = "partial"
    else:
        summary["data_state"] = "unavailable"
    summary["data_issues"] = list(dict.fromkeys(quality_issues))
    return summary


def _sum_numeric(rows: list[dict], key: str):
    values = [row.get(key) for row in rows if row.get(key) is not None]
    if not values:
        return None
    return sum(float(value) for value in values)


def _merge_business_anchor_metrics(items: list[dict]) -> dict:
    """合并同一主播跨技术 liveId 的官方明细，不把分段人数冒充去重人数。"""
    if not items:
        return {"data_state": "unavailable", "data_issues": "[]"}
    merged = dict(items[0])
    merged.pop("raw", None)
    for key in ("pay_amt", "pay_ord_cnt", "pay_itm_qty"):
        merged[key] = _sum_numeric(items, key)
    for key, segment_key in (
        ("look_uv", "look_uv_segment_sum"),
        ("pay_byr_cnt", "pay_byr_cnt_segment_sum"),
        ("ipv_uv", "ipv_uv_segment_sum"),
        ("cart_uv", "cart_uv_segment_sum"),
        ("atn_uv", "atn_uv_segment_sum"),
        ("cmt_uv", "cmt_uv_segment_sum"),
        ("shr_uv", "shr_uv_segment_sum"),
        ("fvr_uv", "fvr_uv_segment_sum"),
        ("sns_uv", "sns_uv_segment_sum"),
    ):
        merged[key] = None
        segments = []
        for item in items:
            value = item.get(segment_key)
            if value is None:
                value = item.get(key)
            if value is not None:
                segments.append(value)
        merged[segment_key] = sum(float(value) for value in segments) if segments else None
    # 转化率、客单价和环比在多个技术场次下没有可靠分母，必须显示暂无。
    for key in tuple(merged):
        if key.endswith("_rate") or key in {"cvr_pay", "atv", "on_air_duration_sec"}:
            merged[key] = None
    merged["on_air_duration_sec"] = sum(
        float(item.get("on_air_duration_sec") or 0) for item in items)
    merged["data_state"] = (
        "ok" if all(str(item.get("data_state") or "") == "ok" for item in items)
        else "partial")
    merged["source"] = "platform_anchor_metrics.business_session"
    merged["aggregation_note"] = "跨 liveId 合并；人数类为分段人次，非去重人数"
    merged["data_issues"] = json.dumps(
        list(dict.fromkeys(
            issue for item in items for issue in _decode(item.get("data_issues"), [])
        )), ensure_ascii=False)
    return merged


def build_business_review_summary(cfg: dict, store, business_session_key: str) -> dict:
    """合并一个业务直播日内所有技术 liveId，供唯一正式日报消费。"""
    key = str(business_session_key or "")
    streams = _stream_scope(store, key)
    live_ids = sorted({str(row.get("live_id") or "") for row in streams if row.get("live_id")})
    summaries = [build_platform_review_summary(cfg, store, live_id) for live_id in live_ids]
    hourly_artifacts, hourly_issues = load_complete_business_hours(store, key)
    hourly_trends: list[dict] = []
    for artifact in hourly_artifacts:
        identity = artifact.get("identity") or {}
        facts = artifact.get("core_metrics") or {}
        quality = artifact.get("quality") or {}
        hourly_trends.append({
            "shift_window_key": identity.get("shift_window_key"),
            "window_start_ms": identity.get("window_start_ms"),
            "window_end_ms": identity.get("window_end_ms"),
            "anchor_name": identity.get("anchor_name") or "",
            "pay_amt": (facts.get("成交金额") or {}).get("value"),
            "viewer_uv": (facts.get("本小时新增观看人数") or {}).get("value"),
            "visitor_total": (facts.get("本小时进入次数") or {}).get("value"),
            "avg_online": (facts.get("平均在线") or {}).get("value"),
            "max_online": (facts.get("最高在线") or {}).get("value"),
            "buyer_cnt": (facts.get("成交人数") or {}).get("value"),
            "media_coverage": quality.get("media_coverage") or {},
        })
    by_anchor: dict[int, list[dict]] = {}
    for item in summaries:
        for anchor in item.get("anchors") or []:
            by_anchor.setdefault(int(anchor.get("anchor_id") or 0), []).append(anchor)
    anchors: list[dict] = []
    for anchor_id, items in sorted(by_anchor.items()):
        first = items[0]
        merged = dict(first)
        merged["stream_ids"] = [sid for item in items for sid in item.get("stream_ids") or []]
        merged["fragment_count"] = len(merged["stream_ids"])
        merged["started_at"] = min(str(item.get("started_at") or "") for item in items)
        merged["ended_at"] = max(str(item.get("ended_at") or "") for item in items)
        merged["duration_sec"] = sum(float(item.get("duration_sec") or 0) for item in items)
        for field in ("transcripts", "smart_minutes", "data_highlights", "quality_highlights", "talktracks"):
            merged[field] = [entry for item in items for entry in item.get(field) or []]
        merged["sentence_count"] = len(merged["transcripts"])
        merged["char_count"] = sum(len(str(entry.get("text") or "")) for entry in merged["transcripts"])
        merged["categories"] = {}
        for item in items:
            for category, count in (item.get("categories") or {}).items():
                merged["categories"][category] = merged["categories"].get(category, 0) + int(count)
        merged["metrics"] = _merge_business_anchor_metrics(
            [item.get("metrics") or {} for item in items])
        anchors.append(merged)

    platform_rows = []
    for summary in summaries:
        metrics = summary.get("display_metrics") or {}
        if metrics:
            platform_rows.append(metrics)
    display = dict(platform_rows[0]) if platform_rows else {
        "data_state": "unavailable", "source_scope": "业务日平台冻结总账暂未返回",
    }
    for key_name in ("pay_amt", "buyer_cnt", "order_cnt", "item_qty"):
        display[key_name] = _sum_numeric(platform_rows, key_name)
    display["viewer_uv_segment_sum"] = _sum_numeric(
        [{"value": row.get("viewer_uv")} for row in platform_rows], "value")
    display["viewer_uv"] = None
    display["max_online_uv"] = max(
        (float(row["max_online_uv"]) for row in platform_rows
         if row.get("max_online_uv") is not None), default=None)
    display["source"] = "platform_sessions.business_session"
    display["source_scope"] = "业务直播日（跨 liveId 合并）"
    display["aggregation_note"] = "观看人数为分段人次，非跨 liveId 去重人数"
    summary = {
        # 日报的正式身份是业务经营日，不是其中任意一个
        # 技术 liveId。真实 liveId 完整保留在 live_ids 供审计。
        "live_id": key,
        "business_session_key": key,
        "live_ids": live_ids,
        "scope_stream_ids": [int(row["id"]) for row in streams],
        "scope_count": len(streams),
        "started_at": min((str(row.get("started_at") or "") for row in streams), default=""),
        "ended_at": max((str(row.get("ended_at") or "") for row in streams), default=""),
        "duration_sec": sum(float(row.get("duration_sec") or 0) for row in streams),
        "anchor_count": len(anchors), "anchors": anchors, "display_metrics": display,
        "hourly_artifacts": hourly_artifacts,
        "hourly_trends": hourly_trends,
    }
    issues = list(display.get("data_issues") or []) if isinstance(display.get("data_issues"), list) else []
    issues.extend(hourly_issues)
    issues.extend(_metric_consistency_issues(summary))
    states = [str(display.get("data_state") or "unavailable")]
    states.extend(str((anchor.get("metrics") or {}).get("data_state") or "unavailable")
                  for anchor in anchors)
    summary["data_state"] = "complete" if streams and anchors and all(state == "ok" for state in states) and not issues else (
        "partial" if any(state in {"ok", "partial"} for state in states) else "unavailable")
    summary["data_issues"] = list(dict.fromkeys(issues))
    return summary


def _next_retry_at(triggered_at: float, deadline_at: float, now: float) -> float:
    for minute in RETRY_MINUTES[1:]:
        candidate = min(float(deadline_at), float(triggered_at) + minute * 60)
        if candidate > now:
            return candidate
    return float(deadline_at)


def _stream_scope(store, live_id: str) -> list[dict]:
    """按正式任务键取范围：兼容单 liveId，也支持业务日跨 liveId 合并。"""
    key = str(live_id)
    # 旧数据或异常退出可能已经把业务日键写进 streams，却还没来得及建立
    # business_sessions 行；日报范围不能因此退化成按 liveId 查询。
    is_business = bool(store.query(
        """SELECT 1 FROM business_sessions WHERE business_session_key=?
           UNION ALL
           SELECT 1 FROM streams WHERE business_session_key=? LIMIT 1""",
        (key, key)))
    if is_business:
        rows = [dict(row) for row in store.query(
            """SELECT s.*,a.name anchor_name,a.taobao_user_id FROM streams s
               LEFT JOIN anchors a ON a.id=s.anchor_id
               WHERE s.business_session_key=? AND s.file_path!=''
               ORDER BY s.started_at,s.id""", (key,))]
        return _filter_business_streams(store, key, rows)
    return [dict(row) for row in store.query(
        """SELECT s.*,a.name anchor_name,a.taobao_user_id FROM streams s
           LEFT JOIN anchors a ON a.id=s.anchor_id
           WHERE s.live_id=? AND s.file_path!='' ORDER BY s.started_at,s.id""",
        (key,),
    )]


def _filter_business_streams(
        store, business_session_key: str, rows: list[dict]) -> list[dict]:
    """Exclude technically recorded streams outside the business envelope."""
    key = str(business_session_key or "").strip()
    session = store.get_business_session(key) if key else None
    bound_start_ms = local_epoch_ms(
        session["planned_start"] if session else None)
    bound_end_ms = local_epoch_ms(
        session["planned_end"] if session else None)
    if bound_start_ms is None or bound_end_ms is None:
        bound_start_ms, bound_end_ms = business_operating_bounds(key)
    scoped: list[dict] = []
    for raw in rows:
        row = dict(raw)
        start_ms = local_epoch_ms(row.get("started_at"))
        if start_ms is None:
            continue
        end_ms = local_epoch_ms(row.get("ended_at"))
        duration_end_ms = start_ms + max(
            1, int(float(row.get("duration_sec") or 0) * 1000))
        if end_ms is None or end_ms <= start_ms:
            end_ms = duration_end_ms
        if end_ms > int(bound_start_ms) and start_ms < int(bound_end_ms):
            scoped.append(row)
    return scoped


def _local_recording_rows(
        store, business_session_key: str, streams: list[dict]) -> list[dict]:
    key = str(business_session_key or "").strip()
    if not key:
        return list(streams)
    rows = [dict(row) for row in store.query(
        """SELECT * FROM streams WHERE business_session_key=?
           ORDER BY started_at,id""", (key,))]
    return _filter_business_streams(store, key, rows)


def _local_recordings_ready(
        store, business_session_key: str, streams: list[dict]) -> bool:
    """Require safely closed media, not the retired fragment-analysis status."""
    rows = _local_recording_rows(store, business_session_key, streams)
    return bool(rows) and all(
        str(row.get("status") or "") not in {
            "recording", "recovering", "interrupted", "failed"}
        and bool(str(row.get("file_path") or "").strip())
        and bool(str(row.get("ended_at") or "").strip())
        and float(row.get("duration_sec") or 0) > 0
        for row in rows
    )


def _hourly_materials_ready(store, streams: list[dict]) -> bool:
    """检查整场最终卡所依赖的小时转写/妙记是否已经冻结。

    旧数据可能没有统一转写任务，保持兼容；一旦存在小时任务，则不允许
    processing、空结果或仍在生成智能纪要的批次进入最终整场复盘。
    """
    from ..transcription.models import TranscriptionResult

    for stream in streams:
        jobs = [dict(row) for row in store.get_stream_transcription_jobs(
            int(stream["id"])) if str(row["purpose"] or "") == "hourly"]
        for job in jobs:
            status = str(job.get("status") or "")
            result_raw = str(job.get("result_json") or "")
            if status == "fallback_ready":
                if not int(job.get("review_fallback_approved") or 0):
                    return False
            elif status != "ready":
                return False
            if not result_raw:
                return False
            try:
                result = TranscriptionResult.from_json(result_raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                return False
            if result is None or not result.segments:
                return False
            if (result.provider == "feishu_minutes"
                    and (result.smart is None or not result.smart.minute_url)):
                return False
    return True


def load_complete_business_hours(
        store, business_session_key: str) -> tuple[list[dict], list[str]]:
    """Load only fully frozen phase-one hourly facts for one business day."""
    key = str(business_session_key or "").strip()
    if not key:
        return [], []
    session = store.get_business_session(key)
    planned_start_ms = local_epoch_ms(
        session["planned_start"] if session else None)
    planned_end_ms = local_epoch_ms(
        session["planned_end"] if session else None)
    if planned_start_ms is None or planned_end_ms is None:
        planned_start_ms, planned_end_ms = business_operating_bounds(key)
    rows = store.query(
        """SELECT w.shift_window_key,w.window_start_ms,w.window_end_ms,
                  w.actual_slices_json,a.quality_state,a.payload_json
           FROM shift_windows w LEFT JOIN hourly_artifacts a
             ON a.shift_window_key=w.shift_window_key
           WHERE w.business_session_key=? ORDER BY w.window_start_ms""",
        (key,),
    )
    if not rows:
        return [], ["经营日尚无已冻结的小时事实"]
    eligible_rows = [
        row for row in rows
        if (int(row["window_start_ms"]) >= int(planned_start_ms)
            and int(row["window_end_ms"]) <= int(planned_end_ms))
    ]
    if not eligible_rows:
        return [], ["经营日尚无已冻结的小时事实"]
    final_rows = [
        row for row in eligible_rows
        if str(row["shift_window_key"] or "").endswith(":final")]
    initial_rows = [
        row for row in eligible_rows
        if str(row["shift_window_key"] or "").endswith(":initial")]
    active_boundary_keys: set[str] = set()
    if final_rows:
        current_final = max(final_rows, key=lambda row: (
            int(row["window_end_ms"]), int(row["window_start_ms"]),
            str(row["quality_state"] or "") == "complete",
            str(row["shift_window_key"] or ""),
        ))
        active_boundary_keys.add(str(current_final["shift_window_key"]))
    if initial_rows:
        current_initial = min(initial_rows, key=lambda row: (
            int(row["window_start_ms"]), -int(row["window_end_ms"]),
            str(row["quality_state"] or "") != "complete",
            str(row["shift_window_key"] or ""),
        ))
        active_boundary_keys.add(str(current_initial["shift_window_key"]))
    complete: list[dict] = []
    issues: list[str] = []
    for row in eligible_rows:
        window_key = str(row["shift_window_key"] or "")
        if ((window_key.endswith(":initial") or window_key.endswith(":final"))
                and window_key not in active_boundary_keys):
            continue
        try:
            slices = json.loads(str(row["actual_slices_json"] or "[]"))
        except (TypeError, ValueError):
            slices = []
        valid_slices = bool(slices) and all(
            isinstance(item, dict)
            and int(item.get("actual_start_ms") or 0)
                < int(item.get("actual_end_ms") or 0)
            and int(row["window_start_ms"])
                <= int(item.get("actual_start_ms") or 0)
            and int(item.get("actual_end_ms") or 0)
                <= int(row["window_end_ms"])
            for item in slices
        )
        if not valid_slices:
            issues.append(f"{window_key}：实际直播切片未完整固化")
            continue
        if str(row["quality_state"] or "") != "complete":
            issues.append(f"{window_key}：小时产物尚未完整")
            continue
        try:
            artifact = json.loads(str(row["payload_json"] or "{}"))
        except (TypeError, ValueError):
            artifact = {}
        identity = artifact.get("identity") if isinstance(artifact, dict) else {}
        quality = artifact.get("quality") if isinstance(artifact, dict) else {}
        analysis = artifact.get("analysis") if isinstance(artifact, dict) else {}
        core_metrics = artifact.get("core_metrics") if isinstance(artifact, dict) else {}
        transcription = artifact.get("transcription") if isinstance(artifact, dict) else {}
        bound = (
            isinstance(identity, dict)
            and str(identity.get("business_session_key") or "") == key
            and str(identity.get("shift_window_key") or "") == window_key
            and int(identity.get("window_start_ms") or -1) == int(row["window_start_ms"])
            and int(identity.get("window_end_ms") or -1) == int(row["window_end_ms"])
        )
        is_boundary = window_key.endswith(":initial") or window_key.endswith(":final")
        core_metrics_complete = (
            is_boundary
            or (isinstance(core_metrics, dict) and len(core_metrics) == 10
                and all(isinstance(item, dict)
                        and item.get("quality_state") == "complete"
                        for item in core_metrics.values()))
        )
        complete_inputs = (
            isinstance(quality, dict) and quality.get("state") == "complete"
            and isinstance(transcription, dict) and bool(transcription)
            and isinstance(analysis, dict) and analysis.get("status") == "ready"
            and bool(str(analysis.get("full_analysis") or "").strip())
            and bool(analysis.get("business_conclusions"))
            and core_metrics_complete
        )
        if not bound or not complete_inputs:
            issues.append(f"{window_key}：小时事实、妙记或 DeepSeek 分析绑定不完整")
            continue
        complete.append(artifact)
    return complete, issues


def _try_official_metrics(cfg: dict, store, live_id: str,
                          streams: list[dict]) -> tuple[bool, set[int], list[str]]:
    """低频抓取官方数据；业务日任务按各技术 liveId 抓取后再合并。"""
    from ..config import local_epoch_ms
    from ..metrics import daibo as daibo_module
    from ..metrics import qianniu as qianniu_module

    issues: list[str] = []
    live_ids = sorted({str(row.get("live_id") or "") for row in streams if row.get("live_id")})
    if not live_ids:
        live_ids = [str(live_id)]
    all_anchor_ids: set[int] = set()
    all_frozen = True
    anchors = [dict(row) for row in store.query(
        "SELECT id,name,taobao_user_id FROM anchors")]
    name_map = {str(row.get("name") or "").strip(): int(row["id"])
                for row in anchors if str(row.get("name") or "").strip()}
    id_map = {str(row.get("taobao_user_id") or "").strip(): int(row["id"])
              for row in anchors if str(row.get("taobao_user_id") or "").strip()}
    for actual_live_id in live_ids:
        actual_streams = [row for row in streams
                          if str(row.get("live_id") or "") == actual_live_id]
        start = min((str(row.get("started_at") or "") for row in actual_streams), default="")
        end = max((str(row.get("ended_at") or "") for row in actual_streams), default="")
        stored_frozen = store.query(
            "SELECT * FROM platform_sessions WHERE live_id=? LIMIT 1", (actual_live_id,))
        frozen_ok = bool(stored_frozen and not _frozen_metric_issues(dict(stored_frozen[0])))
        try:
            frozen = qianniu_module.fetch_session_metrics(
                cfg, actual_live_id, start_ms=local_epoch_ms(start) if start else None,
                end_ms=local_epoch_ms(end) if end else None,
            )
            if frozen:
                fetched_issues = _frozen_metric_issues(frozen)
                if not fetched_issues or not frozen_ok:
                    qianniu_module.upsert_platform_session(store, actual_live_id, frozen)
                frozen_ok = frozen_ok or not fetched_issues
                if fetched_issues and not frozen_ok:
                    issues.extend(f"{actual_live_id}：{item}" for item in fetched_issues)
            elif not frozen_ok:
                issues.append(f"{actual_live_id}：平台整场冻结总账尚未返回")
        except Exception as exc:
            frozen_ok = False
            issues.append(f"{actual_live_id}：平台整场冻结总账抓取失败：{str(exc)[:160]}")
        all_frozen = all_frozen and frozen_ok

        metric_anchor_ids = {
            int(row["anchor_id"]) for row in store.query(
                """SELECT anchor_id FROM platform_anchor_metrics
                   WHERE live_id=? AND data_state='ok'""", (actual_live_id,))}
        try:
            rows = daibo_module.fetch_daibo_content(cfg, actual_live_id, start)
            aggregated = daibo_module.aggregate_daibo_content(
                rows, actual_live_id, name_map, id_map)
            if aggregated:
                save_rows = [row for row in aggregated
                             if row.get("data_state") == "ok"
                             or int(row["anchor_id"]) not in metric_anchor_ids]
                daibo_module.save_platform_anchor_metrics(store, save_rows)
                metric_anchor_ids.update(int(row["anchor_id"]) for row in aggregated
                                         if row.get("data_state") == "ok")
                for row in aggregated:
                    if (row.get("data_state") != "ok"
                            and int(row["anchor_id"]) not in metric_anchor_ids):
                        issues.extend(
                            f"{actual_live_id}/{row.get('daibo_name') or '未知主播'}：{item}"
                            for item in row.get("data_issues") or [])
            elif not metric_anchor_ids:
                issues.append(f"{actual_live_id}：平台主播上下钟经营数据尚未返回")
        except Exception as exc:
            issues.append(f"{actual_live_id}：平台主播上下钟经营数据抓取失败：{str(exc)[:160]}")
        all_anchor_ids.update(metric_anchor_ids)
    return all_frozen, all_anchor_ids, issues


def process_platform_review_job(
        cfg: dict, store, live_id: str, now: float | None = None, *,
        intelligence_provider=None,
        intelligence_now_fn=None,
) -> str:
    """执行一个业务日报任务；未齐全持续等待，30 分钟只提醒不发缩水日报。"""
    from ..notify.feishu import (freeze_platform_review_payload,
                                 notify_platform_review)

    now = float(time.time() if now is None else now)
    job = store.get_platform_review_job(live_id)
    if not job:
        return "missing"
    if str(job["status"]) in {"sent", "partial_sent"}:
        return str(job["status"])

    business_key = str(job["business_session_key"] or "").strip()
    scope_key = business_key or str(live_id)
    gate_open, gate_reason = store.business_review_gate(business_key)
    if not gate_open:
        store.update_platform_review_job(
            live_id, "parked", next_attempt_at=0,
            error=f"daily gate closed: {gate_reason}")
        return "parked"

    frozen_review = store.get_platform_review(live_id)
    if frozen_review and frozen_review.get("payload") \
            and frozen_review.get("data_state") == "complete":
        summary = frozen_review["payload"]
        try:
            if notify_platform_review(cfg, summary):
                store.update_platform_review_job(
                    live_id, "sent", report_path=frozen_review.get("report_path") or "")
                return "sent"
            store.update_platform_review_job(
                live_id, "failed",
                error="冻结复盘载荷未投递；为避免重跑 AI/改写卡片，已停止自动处理")
            return "failed"
        except Exception as exc:
            store.update_platform_review_job(
                live_id, "waiting", next_attempt_at=now + 300, error=str(exc))
            return "waiting"

    streams = _stream_scope(store, scope_key)
    all_ready = _local_recordings_ready(store, business_key, streams)
    hourly_ready = _hourly_materials_ready(store, streams)
    business_hours: list[dict] = []
    hourly_issues: list[str] = []
    if business_key:
        business_hours, hourly_issues = load_complete_business_hours(
            store, business_key)
        hourly_ready = hourly_ready and bool(business_hours) and not hourly_issues
    issues = list(hourly_issues)

    # 正式日报对妙记/小时分析不设超时；30 分钟只约束官方经营数据冻结等待。
    if not all_ready or not hourly_ready:
        if not all_ready:
            issues.append("本地录像碎片仍未安全封存")
        if not hourly_ready:
            issues.append("小时事实或智能纪要仍在生成")
        store.update_platform_review_job(
            live_id, "waiting_evidence", next_attempt_at=0,
            error="；".join(dict.fromkeys(issues)),
        )
        return "waiting_evidence"

    # 本地录像、小时事实、妙记和小时 DeepSeek 全部齐全后，才读取官方结算。
    # 这避免直播中或本地材料未完成时每分钟轮询已结束的旧 liveId。
    frozen_ok, metric_anchor_ids, official_issues = _try_official_metrics(
        cfg, store, live_id, streams)
    issues.extend(official_issues)
    anchor_ids = {int(row["anchor_id"]) for row in streams}
    official_complete = frozen_ok and bool(anchor_ids) and anchor_ids <= metric_anchor_ids
    deadline_reached = now >= float(job["deadline_at"])

    if not official_complete and not deadline_reached:
        next_at = _next_retry_at(float(job["triggered_at"]),
                                 float(job["deadline_at"]), now)
        store.update_platform_review_job(
            live_id, "waiting", next_attempt_at=next_at,
            error="；".join(dict.fromkeys(issues)),
        )
        return "waiting"

    if not official_complete and deadline_reached:
        # 04 终稿：30 分钟是提醒节点，不是缩水日报交付节点。官方结算晚到时
        # 只继续更新后台事实并重试；所有小时产物、妙记和全日智能分析必须
        # 在正式卡发送前一次性齐全，避免群里出现无法回收的临时结论。
        reminded_at = float(job["settlement_reminded_at"] or 0)
        reminder = (
            "官方结算仍缺失，已超过30分钟；仅提醒并继续等待，不发送不完整日报。"
            if not reminded_at else
            "官方结算仍缺失，继续等待，不发送不完整日报。"
        )
        store.update_platform_review_job(
            live_id, "waiting", next_attempt_at=now + 3600,
            error="；".join(dict.fromkeys([*issues, reminder])),
            settlement_reminded_at=(now if not reminded_at else None),
        )
        if not reminded_at:
            log.warning("业务日结算超过30分钟仍未齐全 identity=%s；仅提醒，不发送不完整日报",
                        scope_key)
        return "waiting"

    summary = (build_business_review_summary(cfg, store, business_key)
               if business_key else build_platform_review_summary(cfg, store, live_id))
    summary["data_state"] = "complete" if (all_ready and official_complete) else "partial"
    consistency_issues = _metric_consistency_issues(summary)
    if consistency_issues:
        summary["data_state"] = "partial"
        issues.extend(consistency_issues)
    if not all_ready:
        missing = [
            int(row["id"])
            for row in _local_recording_rows(store, business_key, streams)
            if (str(row.get("status") or "") in {
                    "recording", "recovering", "interrupted", "failed"}
                or not str(row.get("file_path") or "").strip()
                or not str(row.get("ended_at") or "").strip()
                or float(row.get("duration_sec") or 0) <= 0)
        ]
        issues.append(f"截止时仍有 {len(missing)} 个本地录像片段未安全封存")
        summary["missing_stream_ids"] = missing
    summary["data_issues"] = list(dict.fromkeys([
        *(summary.get("data_issues") or []), *issues,
    ]))
    # 全日智能只读受信的小时产物和官方快照。
    # 先于正式载荷冻结完成，因此投递失败或重启只会重放同一份结果。
    try:
        from ..intelligence.platform import (
            PlatformIntelligenceService,
            build_platform_intelligence_context,
        )
        context = build_platform_intelligence_context(store, summary, scope_key)
        intelligence = PlatformIntelligenceService(
            cfg, store, provider=intelligence_provider,
            now_fn=(intelligence_now_fn or time.time),
        ).analyze_platform(context, created_at=now)
        if str(intelligence.status or "") != "ready":
            # 正式日报需要完整 DeepSeek 分析，非 ready 产物不能投递。
            reason = "全日 DeepSeek 分析尚未 ready，暂不发送正式日报"
            store.update_platform_review_job(
                live_id, "waiting", next_attempt_at=now + 600, error=reason)
            log.warning("全日智能分析未 ready identity=%s status=%s；暂不发日报",
                        scope_key, intelligence.status)
            return "waiting"
        summary["platform_intelligence"] = intelligence.to_dict()
        summary["platform_intelligence_input_hash"] = context.input_hash
    except Exception as exc:
        reason = f"全日 DeepSeek 分析未完成，继续重试：{type(exc).__name__}"
        log.warning("全日智能产物绑定失败 identity=%s: %s", scope_key, exc)
        store.update_platform_review_job(
            live_id, "waiting", next_attempt_at=now + 600, error=reason)
        return "waiting"
    summary = freeze_platform_review_payload(summary)

    # 发送前冻结载荷；若飞书失败或进程重启，后续只重试同一份卡，不重复调用 AI。
    data_state = "complete" if summary["data_state"] == "complete" else "partial"
    try:
        report_path = str(generate_platform_report(cfg, store, scope_key, summary) or "")
    except Exception as exc:
        reason = f"完整飞书 Markdown 报告尚未生成，继续重试：{type(exc).__name__}"
        log.exception("直播经营日报 Markdown 生成失败 scope=%s", scope_key)
        store.update_platform_review_job(
            live_id, "waiting", next_attempt_at=now + 600, error=reason)
        return "waiting"
    if not report_path:
        reason = "完整飞书 Markdown 报告路径为空，继续重试"
        store.update_platform_review_job(
            live_id, "waiting", next_attempt_at=now + 600, error=reason)
        return "waiting"
    store.save_platform_review(
        live_id, summary, data_state=data_state, report_path=report_path)
    try:
        delivered = notify_platform_review(cfg, summary)
    except Exception as exc:
        store.update_platform_review_job(
            live_id, "waiting", next_attempt_at=now + 300, error=str(exc))
        return "waiting"
    if not delivered:
        store.update_platform_review_job(
            live_id, "failed",
            error="飞书正式日报未发送；冻结载荷已保留，需核验配置或投递状态",
        )
        return "failed"
    # 只要走到这里就是正式日报发送；data_state 可以记录质量异常，但不能
    # 再用 partial_sent 触发后续群发修正卡。
    final_status = "sent"
    store.update_platform_review_job(
        live_id, final_status, next_attempt_at=None, report_path=report_path)
    log.info("直播经营日报完成 identity=%s status=%s", scope_key, final_status)
    return final_status
