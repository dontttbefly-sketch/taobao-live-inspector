"""Build one hourly fact batch from every live source intersecting the window."""

from __future__ import annotations

import json
import math
from typing import Any

from .config import local_epoch_ms
from .hourly_fact_capture import BOUNDARY_TOLERANCE_MS


PROVENANCE_SCHEMA_VERSION = 2
MINUTE_MS = 60_000
REQUIRED_MINUTE_SERIES = {
    "uv": ("online", "visitorEnter"),
    "itemClick": ("value",),
    "deal": ("amount",),
}


def _source_intervals(
        store, *, business_session_key: str, shift_window_key: str,
        window_start_ms: int, window_end_ms: int,
        fallback_live_id: str, fallback_stream_id: int) -> list[dict]:
    start_ms = int(window_start_ms)
    end_ms = int(window_end_ms)
    rows = store.business_fact_sources_for_window(
        str(business_session_key),
        window_start_ms=start_ms,
        window_end_ms=end_ms,
    )
    intervals: list[dict] = []
    for row in rows:
        source_start = max(start_ms, int(row["started_at_ms"]))
        raw_end = int(row.get("ended_at_ms") or 0)
        source_end = min(end_ms, raw_end if raw_end > 0 else end_ms)
        if source_end > source_start:
            intervals.append({
                "live_id": str(row.get("live_id") or ""),
                "stream_id": int(row.get("stream_id") or fallback_stream_id),
                "start_ms": source_start,
                "end_ms": source_end,
                "evidence": "business_fact_source_intervals",
            })
    # Media can prove an additional live source existed even when a crash lost
    # its lifecycle observation. It may only add/extend required evidence;
    # absence of media never removes a lifecycle source.
    shift = store.query(
        "SELECT actual_slices_json FROM shift_windows WHERE shift_window_key=?",
        (str(shift_window_key),),
    )
    grouped: dict[str, dict] = {}
    if shift:
        try:
            slices = json.loads(str(shift[0]["actual_slices_json"] or "[]"))
        except (TypeError, ValueError):
            slices = []
        for item in slices if isinstance(slices, list) else []:
            if not isinstance(item, dict) or item.get("kind") not in (None, "media"):
                continue
            live_id = str(item.get("live_id") or "")
            if not live_id:
                continue
            item_start = max(start_ms, int(item.get("actual_start_ms") or start_ms))
            item_end = min(end_ms, int(item.get("actual_end_ms") or end_ms))
            if item_end <= item_start:
                continue
            existing = grouped.setdefault(live_id, {
                "live_id": live_id,
                "stream_id": int(item.get("stream_id") or fallback_stream_id),
                "start_ms": item_start,
                "end_ms": item_end,
                "evidence": "legacy_media_slice",
            })
            existing["start_ms"] = min(int(existing["start_ms"]), item_start)
            existing["end_ms"] = max(int(existing["end_ms"]), item_end)
    combined = intervals + list(grouped.values())
    if combined:
        by_live: dict[str, dict] = {}
        for item in combined:
            live_id = str(item["live_id"])
            existing = by_live.setdefault(live_id, dict(item))
            existing["start_ms"] = min(
                int(existing["start_ms"]), int(item["start_ms"]))
            existing["end_ms"] = max(
                int(existing["end_ms"]), int(item["end_ms"]))
            if existing.get("evidence") != item.get("evidence"):
                existing["evidence"] = "lifecycle_plus_media"
        result = sorted(by_live.values(), key=lambda item: (
            int(item["start_ms"]), int(item["end_ms"]), str(item["live_id"])))
        if len(result) == 1 and len(combined) > 1:
            result[0]["evidence"] = "merged_same_live_intervals"
        return result
    return [{
        "live_id": str(fallback_live_id),
        "stream_id": int(fallback_stream_id),
        "start_ms": start_ms,
        "end_ms": end_ms,
        "evidence": "legacy_single_source",
    }]


def _legacy_boundary(snapshot: dict | None, *, nominal_ms: int,
                     role: str) -> dict | None:
    if not isinstance(snapshot, dict):
        return None
    if (snapshot.get("source") != "screen.totalStats"
            or snapshot.get("data_state") != "ok"):
        return None
    captured_ms = local_epoch_ms(snapshot.get("fetched_at") or snapshot.get("ts"))
    if captured_ms is None:
        return None
    distance = abs(int(captured_ms) - int(nominal_ms))
    if distance > BOUNDARY_TOLERANCE_MS:
        return None
    return {
        "job_key": "legacy",
        "boundary_kind": role,
        "nominal_boundary_ms": int(nominal_ms),
        "captured_at_ms": int(captured_ms),
        "distance_ms": distance,
        "source": "screen.totalStats",
        "data_state": "ok",
        "snapshot": dict(snapshot),
        "evidence_kind": "legacy_screen_boundary",
    }


def _boundary(
        store, *, business_session_key: str, live_id: str,
        nominal_ms: int, role: str,
        legacy_snapshot: dict | None = None) -> dict | None:
    kinds = (("clock", "live_start") if role == "start"
             else ("clock", "live_end"))
    result = store.find_hourly_fact_boundary(
        business_session_key=str(business_session_key),
        live_id=str(live_id),
        nominal_boundary_ms=int(nominal_ms),
        boundary_kinds=kinds,
    )
    if result is not None:
        result = dict(result)
        result["evidence_kind"] = "persisted_screen_boundary"
        return result
    return _legacy_boundary(
        legacy_snapshot, nominal_ms=int(nominal_ms), role=role)


def _all_sum(values: list[Any], *, decimals: int | None = None):
    if not values or any(value is None for value in values):
        return None
    total = sum(float(value) for value in values)
    if decimals is None:
        return int(round(total))
    return round(total, decimals)


def _boundary_proof(boundary: dict, *, live_id: str, role: str) -> dict:
    return {
        "live_id": str(live_id),
        "role": str(role),
        "job_key": str(boundary.get("job_key") or ""),
        "boundary_kind": str(boundary.get("boundary_kind") or ""),
        "nominal_boundary_ms": int(boundary.get("nominal_boundary_ms") or 0),
        "captured_at_ms": int(boundary.get("captured_at_ms") or 0),
        "distance_ms": int(boundary.get("distance_ms") or 0),
        "evidence_kind": str(boundary.get("evidence_kind") or ""),
    }


def _minute_series_proof(
        series: dict, *, live_id: str, start_ms: int, end_ms: int) -> dict:
    interval_start = int(start_ms)
    interval_end = int(end_ms)
    first_expected = (
        (interval_start + MINUTE_MS - 1) // MINUTE_MS * MINUTE_MS)
    expected = tuple(range(first_expected, interval_end, MINUTE_MS))
    expected_set = set(expected)
    states: dict[str, dict] = {}
    for series_kind, value_fields in REQUIRED_MINUTE_SERIES.items():
        markers: set[int] = set()
        for row in (series or {}).get(series_kind) or []:
            if not isinstance(row, dict):
                continue
            try:
                marker = int(row.get("time"))
                values = [float(row.get(field)) for field in value_fields]
            except (TypeError, ValueError):
                continue
            if (marker not in expected_set
                    or any(not math.isfinite(value) for value in values)):
                continue
            markers.add(marker)
        ordered = sorted(markers)
        missing_count = len(expected_set - markers)
        states[series_kind] = {
            "bucket_count": len(ordered),
            "first_ms": ordered[0] if ordered else None,
            "last_ms": ordered[-1] if ordered else None,
            "missing_bucket_count": missing_count,
            "complete": bool(expected) and missing_count == 0,
        }
    complete = bool(expected) and all(
        state["complete"] for state in states.values())
    return {
        "live_id": str(live_id),
        "start_ms": interval_start,
        "end_ms": interval_end,
        "expected_first_ms": expected[0] if expected else None,
        "expected_last_ms": expected[-1] if expected else None,
        "expected_bucket_count": len(expected),
        "series": states,
        "complete": complete,
    }


def build_persisted_hourly_metrics(
        cfg: dict, store, *, business_session_key: str,
        shift_window_key: str, window_start_ms: int, window_end_ms: int,
        fallback_live_id: str, fallback_stream_id: int,
        legacy_boundaries: tuple[dict, dict] | None = None,
) -> tuple[dict, str]:
    """Aggregate auditable per-live facts without reading current totals."""
    from .briefing import _fetch_brief_metrics

    start_ms = int(window_start_ms)
    end_ms = int(window_end_ms)
    intervals = _source_intervals(
        store,
        business_session_key=str(business_session_key),
        shift_window_key=str(shift_window_key),
        window_start_ms=start_ms,
        window_end_ms=end_ms,
        fallback_live_id=str(fallback_live_id),
        fallback_stream_id=int(fallback_stream_id),
    )
    required = [dict(item) for item in intervals]
    covered: list[dict] = []
    proofs: list[dict] = []
    unresolved: list[str] = []
    observed: list[str] = []
    local_batches: list[tuple[dict, dict]] = []
    minute_proofs: list[dict] = []
    single_interval = len(intervals) == 1
    for item in intervals:
        live_id = str(item["live_id"])
        interval_start = int(item["start_ms"])
        interval_end = int(item["end_ms"])
        legacy_start = legacy_boundaries[0] if single_interval and legacy_boundaries else None
        legacy_end = legacy_boundaries[1] if single_interval and legacy_boundaries else None
        start_boundary = _boundary(
            store,
            business_session_key=str(business_session_key), live_id=live_id,
            nominal_ms=interval_start, role="start",
            legacy_snapshot=legacy_start,
        )
        end_boundary = _boundary(
            store,
            business_session_key=str(business_session_key), live_id=live_id,
            nominal_ms=interval_end, role="end",
            legacy_snapshot=legacy_end,
        )
        if start_boundary is not None:
            proofs.append(_boundary_proof(start_boundary, live_id=live_id, role="start"))
        else:
            unresolved.append(f"missing_start_boundary:{live_id}:{interval_start}")
        if end_boundary is not None:
            proofs.append(_boundary_proof(end_boundary, live_id=live_id, role="end"))
            observed_at = str(
                (end_boundary.get("snapshot") or {}).get("fetched_at")
                or (end_boundary.get("snapshot") or {}).get("ts") or "")
            if observed_at:
                observed.append(observed_at)
        else:
            unresolved.append(f"missing_end_boundary:{live_id}:{interval_end}")
        boundaries_ready = start_boundary is not None and end_boundary is not None
        metrics, _text, _trend, _diff = _fetch_brief_metrics(
            cfg, store, int(item["stream_id"]), live_id,
            period_sec=max(60.0, (interval_end - interval_start) / 1000.0),
            end_ms=interval_end,
            include_current_totals=boundaries_ready,
            totals_override=(end_boundary["snapshot"] if end_boundary else None),
            previous_snapshot_override=(
                start_boundary["snapshot"] if start_boundary else None),
        )
        local_batches.append((item, metrics))
        minute_proof = _minute_series_proof(
            metrics.get("series") or {},
            live_id=live_id, start_ms=interval_start, end_ms=interval_end)
        minute_proofs.append(minute_proof)
        if not minute_proof["complete"]:
            incomplete = [
                series_kind for series_kind, state
                in minute_proof["series"].items()
                if not state["complete"]
            ]
            unresolved.append(
                f"incomplete_minute_series:{live_id}:{interval_start}:"
                + ",".join(incomplete))
        if boundaries_ready and minute_proof["complete"]:
            covered.append(dict(item))

    live_ids = {str(item["live_id"]) for item in intervals}
    cross_live = len(live_ids) > 1
    if cross_live:
        unresolved.append("cross_live_unique_people")
    local_core = [batch.get("core_metrics") or {} for _item, batch in local_batches]
    pay = _all_sum([core.get("成交金额") for core in local_core], decimals=2)
    visitors = _all_sum([core.get("本小时进入次数") for core in local_core])
    followers = _all_sum([core.get("新增粉丝") for core in local_core])
    refunds = _all_sum([core.get("退款金额") for core in local_core], decimals=2)
    clicks = _all_sum([
        (batch.get("period") or {}).get("ipv_total")
        for _item, batch in local_batches
    ])

    merged_series: dict[str, list[dict]] = {}
    seen_series: set[tuple[str, str, str]] = set()
    online_values: list[float] = []
    for item, batch in local_batches:
        live_id = str(item["live_id"])
        for series_kind, rows in (batch.get("series") or {}).items():
            for raw in rows or []:
                if not isinstance(raw, dict):
                    continue
                marker = str(raw.get("time") or "")
                identity = (live_id, str(series_kind), marker)
                if identity in seen_series:
                    continue
                seen_series.add(identity)
                row = dict(raw)
                row["fact_live_id"] = live_id
                merged_series.setdefault(str(series_kind), []).append(row)
                if str(series_kind) == "uv":
                    try:
                        online_values.append(float(row.get("online")))
                    except (TypeError, ValueError):
                        pass
    def series_time(row: dict) -> int:
        try:
            return int(row.get("time") or 0)
        except (TypeError, ValueError):
            return 0

    for rows in merged_series.values():
        rows.sort(key=series_time)
    average_online = (
        round(sum(online_values) / len(online_values), 1)
        if online_values else None)
    highest_online = max(online_values) if online_values else None

    if not cross_live and len(local_core) == 1:
        viewers = local_core[0].get("本小时新增观看人数")
        buyers = local_core[0].get("成交人数")
        stay = local_core[0].get("平均停留")
    else:
        viewers = buyers = stay = None
    conversion = (
        round(float(buyers) / float(viewers) * 100, 6)
        if buyers is not None and viewers not in (None, 0) else None)
    core_metrics = {
        "成交金额": pay,
        "本小时新增观看人数": viewers,
        "本小时进入次数": visitors,
        "平均在线": average_online,
        "最高在线": highest_online,
        "成交人数": buyers,
        "本小时转化": conversion,
        "新增粉丝": followers,
        "平均停留": stay,
        "退款金额": refunds,
    }
    all_core_ready = all(value is not None for value in core_metrics.values())
    local_states_ready = all(
        batch.get("data_state") == "ok" for _item, batch in local_batches)
    data_state = (
        "ok" if all_core_ready and local_states_ready and not unresolved
        else ("partial" if local_batches else "unavailable"))
    provenance = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "window_start_ms": start_ms,
        "window_end_ms": end_ms,
        "required_source_intervals": required,
        "covered_source_intervals": covered,
        "unresolved_source_intervals": unresolved,
        "boundaries": proofs,
        "minute_series": minute_proofs,
        "truth_tolerance_ms": BOUNDARY_TOLERANCE_MS,
        "cross_live_id": cross_live,
    }
    metrics = {
        "source": "hourly.fact_aggregation.v1",
        "data_state": data_state,
        "data_issues": list(unresolved),
        "fetched_at": max(observed, default=""),
        "frozen_boundary": len(covered) == len(required) and bool(required),
        "window_start_ms": start_ms,
        "window_end_ms": end_ms,
        "period_label": "",
        "period": {
            "uv_avg": average_online,
            "max_online_uv": highest_online,
            "visitor_total": visitors,
            "ipv_total": clicks,
            "pay_amt": pay,
        },
        "delta": {
            "pay_amt": pay,
            "viewer_uv": viewers,
            "buyer_cnt": buyers,
            "atn_uv": followers,
            "refund_amt": refunds,
            "available": len(covered) == len(required) and bool(required),
        },
        "online_uv": online_values[-1] if online_values else None,
        "max_online_uv": highest_online,
        "series": merged_series,
        "core_metrics": core_metrics,
        "fact_provenance": provenance,
    }
    text = (
        f"本小时经营事实已覆盖 {len(covered)}/{len(required)} 个直播来源；"
        + ("跨场次去重人数无官方口径，未冒充完整数据"
           if cross_live else "整点累计与分钟趋势已按同一窗口冻结")
    )
    return metrics, text


__all__ = ["PROVENANCE_SCHEMA_VERSION", "build_persisted_hourly_metrics"]
