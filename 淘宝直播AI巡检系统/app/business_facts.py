"""Stable business-day and hourly-fact identities.

These identities deliberately sit above Taobao technical live IDs and local
recording stream IDs.  They are safe to persist and contain no credentials.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SHANGHAI = ZoneInfo("Asia/Shanghai")
# 00:00–04:59 remains part of the previous 06:00 business live day.
BUSINESS_DAY_CUTOFF_HOUR = 5
DEFAULT_BUSINESS_EARLIEST_START = "05:30"
DEFAULT_BUSINESS_LATEST_END = "01:30"

CORE_METRIC_DEFINITIONS: tuple[tuple[str, str, str], ...] = (
    ("成交金额", "元", "screen.totalStats.boundary_delta"),
    ("本小时新增观看人数", "人", "screen.totalStats.boundary_delta"),
    ("本小时进入次数", "次", "tblive.portal.minuteSeries"),
    ("平均在线", "人", "tblive.portal.minuteSeries"),
    ("最高在线", "人", "tblive.portal.minuteSeries"),
    ("成交人数", "人", "screen.totalStats.boundary_delta"),
    ("本小时转化", "%", "derived.hourly_buyers_per_new_viewer"),
    ("新增粉丝", "人", "screen.totalStats.boundary_delta"),
    ("平均停留", "秒", "screen.totalStats.weighted_boundary_delta"),
    ("退款金额", "元", "screen.totalStats.boundary_delta"),
)


def _local(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=SHANGHAI)
    return value.astimezone(SHANGHAI)


def business_session_key(
        value: datetime, *, cutoff_hour: int = BUSINESS_DAY_CUTOFF_HOUR) -> str:
    """Return the business live-day key used by the 06:00-to-01:00 flow."""
    local = _local(value)
    if local.hour < int(cutoff_hour):
        local = local - timedelta(days=1)
    return local.strftime("%Y%m%d")


def _clock_minutes(value: str) -> int:
    try:
        hour_text, minute_text = str(value).strip().split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (TypeError, ValueError) as exc:
        raise ValueError("business clock must use HH:MM") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("business clock must use HH:MM")
    return hour * 60 + minute


def business_operating_bounds(
        session_key: str, *,
        earliest_start: str = DEFAULT_BUSINESS_EARLIEST_START,
        latest_end: str = DEFAULT_BUSINESS_LATEST_END,
) -> tuple[int, int]:
    """Return the configurable wall-clock envelope for one business live day."""
    try:
        day = datetime.strptime(str(session_key), "%Y%m%d").replace(tzinfo=SHANGHAI)
    except ValueError as exc:
        raise ValueError("business session key must use YYYYMMDD") from exc
    start_minutes = _clock_minutes(earliest_start)
    end_minutes = _clock_minutes(latest_end)
    start = day.replace(
        hour=start_minutes // 60, minute=start_minutes % 60,
        second=0, microsecond=0)
    end_day = day + timedelta(days=1) if end_minutes <= start_minutes else day
    end = end_day.replace(
        hour=end_minutes // 60, minute=end_minutes % 60,
        second=0, microsecond=0)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def business_interval_overlaps(
        session_key: str, interval_start_ms: int, interval_end_ms: int, *,
        earliest_start: str = DEFAULT_BUSINESS_EARLIEST_START,
        latest_end: str = DEFAULT_BUSINESS_LATEST_END,
) -> bool:
    """Return whether a real media interval belongs to this business live."""
    start_ms = int(interval_start_ms)
    end_ms = int(interval_end_ms)
    if end_ms <= start_ms:
        return False
    bound_start_ms, bound_end_ms = business_operating_bounds(
        session_key, earliest_start=earliest_start, latest_end=latest_end)
    return end_ms > bound_start_ms and start_ms < bound_end_ms


def plan_business_windows(
        session_key: str, *, observed_start_ms: int, observed_end_ms: int,
        earliest_start: str = DEFAULT_BUSINESS_EARLIEST_START,
        latest_end: str = DEFAULT_BUSINESS_LATEST_END,
) -> list[dict[str, int | str]]:
    """Plan one real opening fragment, full hours, and one closing fragment.

    Media outside the configurable business envelope is never turned into a
    missing recording window.  Boundary fragments are daily-only materials;
    only full wall-clock hours are formal hourly briefing windows.
    """
    bound_start_ms, bound_end_ms = business_operating_bounds(
        session_key, earliest_start=earliest_start, latest_end=latest_end)
    start_ms = max(int(observed_start_ms), bound_start_ms)
    end_ms = min(int(observed_end_ms), bound_end_ms)
    if end_ms <= start_ms:
        return []

    windows: list[dict[str, int | str]] = []
    point = datetime.fromtimestamp(start_ms / 1000, SHANGHAI)
    cursor = int(point.replace(
        minute=0, second=0, microsecond=0).timestamp() * 1000)
    hour_ms = 3_600_000

    # The first formal window remains the real wall-clock hour even when
    # recording starts shortly after its boundary.  The later coverage gate
    # records that leading gap and decides whether the hour may be sent.  Only
    # the pre-business portion, or an opening fragment that never reaches a
    # completed clock hour, stays daily-only.
    if cursor < bound_start_ms:
        initial_end_ms = min(end_ms, cursor + hour_ms)
        if start_ms < initial_end_ms:
            windows.append({
                "kind": "initial",
                "window_start_ms": start_ms,
                "window_end_ms": initial_end_ms,
            })
        cursor += hour_ms
        if cursor >= end_ms:
            return windows
    elif cursor + hour_ms > end_ms:
        windows.append({
            "kind": "final" if start_ms == cursor else "initial",
            "window_start_ms": start_ms,
            "window_end_ms": end_ms,
        })
        return windows

    while cursor + hour_ms <= end_ms:
        windows.append({
            "kind": "hourly",
            "window_start_ms": cursor,
            "window_end_ms": cursor + hour_ms,
        })
        cursor += hour_ms

    if cursor < end_ms:
        windows.append({
            "kind": "final",
            "window_start_ms": max(cursor, start_ms),
            "window_end_ms": end_ms,
        })
    return windows


def shift_window_key(
        session_key: str, window_start: datetime,
        anchor_names: Iterable[str] = ()) -> str:
    """Return an immutable key for one absolute one-hour schedule window."""
    start = _local(window_start).replace(minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=1)
    names = tuple(sorted({str(name).strip() for name in anchor_names if str(name).strip()}))
    owner = "+".join(names) or "窗口级"
    return f"{str(session_key)}:{start:%Y%m%d%H%M}:{end:%Y%m%d%H%M}:{owner}"


def final_partial_window_key(
        session_key: str, window_start: datetime, window_end: datetime,
        anchor_names: Iterable[str] = ()) -> str:
    """Return the immutable identity for the last, not-full clock-hour slice."""
    start = _local(window_start).replace(microsecond=0)
    end = _local(window_end).replace(microsecond=0)
    if end <= start:
        raise ValueError("final partial window must have positive duration")
    names = tuple(sorted({str(name).strip() for name in anchor_names if str(name).strip()}))
    owner = "+".join(names) or "窗口级"
    return f"{str(session_key)}:{start:%Y%m%d%H%M%S}:{end:%Y%m%d%H%M%S}:{owner}:final"


def initial_partial_window_key(
        session_key: str, window_start: datetime, window_end: datetime,
        anchor_names: Iterable[str] = ()) -> str:
    """Return the immutable identity for the opening, partial clock hour."""
    start = _local(window_start).replace(microsecond=0)
    end = _local(window_end).replace(microsecond=0)
    if end <= start:
        raise ValueError("initial partial window must have positive duration")
    names = tuple(sorted({str(name).strip() for name in anchor_names if str(name).strip()}))
    owner = "+".join(names) or "窗口级"
    return f"{str(session_key)}:{start:%Y%m%d%H%M%S}:{end:%Y%m%d%H%M%S}:{owner}:initial"


def absolute_hour_window(
        recording_start_ms: int, media_offset_ms: int = 0) -> tuple[datetime, datetime]:
    """Map a media offset to the absolute local schedule hour it belongs to."""
    point = datetime.fromtimestamp(
        (int(recording_start_ms) + int(media_offset_ms)) / 1000,
        SHANGHAI,
    )
    start = point.replace(minute=0, second=0, microsecond=0)
    return start, start + timedelta(hours=1)


def completed_absolute_hour_windows(
        recording_start_ms: int, available_media_end_ms: int) -> list[dict[str, int]]:
    """Return formal clock-hour windows fully covered by the available media.

    The formal boundary is always a Shanghai wall-clock hour.  ``media_*``
    values are offsets from the local recording start and describe only the
    actual slice available for that hour.  A recorder that starts at 09:03
    therefore produces the 09:00–10:00 window with a 00:00–57:00 media slice;
    it never silently turns the business window into 09:03–10:03.
    """
    recording_start_ms = int(recording_start_ms)
    available_media_end_ms = max(0, int(available_media_end_ms))
    available_absolute_end = recording_start_ms + available_media_end_ms
    first_start, _ = absolute_hour_window(recording_start_ms, 0)
    cursor = first_start
    windows: list[dict[str, int]] = []
    while True:
        end = cursor + timedelta(hours=1)
        absolute_start_ms = int(cursor.timestamp() * 1000)
        absolute_end_ms = int(end.timestamp() * 1000)
        if absolute_end_ms > available_absolute_end:
            break
        media_start_ms = max(0, absolute_start_ms - recording_start_ms)
        media_end_ms = absolute_end_ms - recording_start_ms
        if media_end_ms > media_start_ms:
            windows.append({
                "absolute_start_ms": absolute_start_ms,
                "absolute_end_ms": absolute_end_ms,
                "media_start_ms": media_start_ms,
                "media_end_ms": media_end_ms,
            })
        cursor = end
    return windows


def build_core_metric_facts(
        metrics: dict[str, Any], *, window_start_ms: int,
        window_end_ms: int) -> dict[str, dict[str, Any]]:
    """Normalize the ten card metrics into auditable hourly fact records."""
    metrics = metrics if isinstance(metrics, dict) else {}
    values = metrics.get("core_metrics") or {}
    observed_at = str(metrics.get("fetched_at") or "")
    batch_state = str(metrics.get("data_state") or "unavailable")
    raw_issues = metrics.get("data_issues") or []
    if isinstance(raw_issues, str):
        raw_issues = [raw_issues]
    batch_issue = "；".join(str(item) for item in raw_issues if str(item).strip())
    facts: dict[str, dict[str, Any]] = {}
    for label, unit, source in CORE_METRIC_DEFINITIONS:
        value = values.get(label)
        if value is None:
            quality_state = "missing"
            issue = batch_issue or f"{label}在该小时无可核验值"
        elif batch_state == "ok":
            quality_state = "complete"
            issue = ""
        else:
            quality_state = "partial"
            issue = batch_issue or f"数据批次状态为 {batch_state}"
        facts[label] = {
            "value": value,
            "unit": unit,
            "source": source,
            "observed_at": observed_at,
            "window_start_ms": int(window_start_ms),
            "window_end_ms": int(window_end_ms),
            "quality_state": quality_state,
            "issue": issue,
        }
    return facts


def formal_hourly_metrics_ready(artifact: dict[str, Any] | None) -> bool:
    """Return whether a formal hour has all ten frozen, auditable metrics."""
    if not isinstance(artifact, dict):
        return False
    metrics = artifact.get("metrics")
    core_metrics = artifact.get("core_metrics")
    if (not isinstance(metrics, dict)
            or metrics.get("data_state") != "ok"
            or metrics.get("frozen_boundary") is not True
            or not isinstance(core_metrics, dict)):
        return False
    identity = artifact.get("identity") or {}
    provenance = metrics.get("fact_provenance")
    if not isinstance(provenance, dict):
        return False
    try:
        window_start = int(identity.get("window_start_ms"))
        window_end = int(identity.get("window_end_ms"))
        provenance_start = int(provenance.get("window_start_ms"))
        provenance_end = int(provenance.get("window_end_ms"))
        tolerance_ms = int(provenance.get("truth_tolerance_ms"))
    except (TypeError, ValueError):
        return False
    if (provenance.get("schema_version") != 2
            or provenance_start != window_start
            or provenance_end != window_end
            or window_end <= window_start
            or not 0 <= tolerance_ms <= 300_000):
        return False
    required = provenance.get("required_source_intervals")
    covered = provenance.get("covered_source_intervals")
    unresolved = provenance.get("unresolved_source_intervals")
    boundaries = provenance.get("boundaries")
    minute_series = provenance.get("minute_series")
    if (not isinstance(required, list) or not required
            or covered != required
            or unresolved != []
            or not isinstance(boundaries, list)
            or not isinstance(minute_series, list)
            or len(minute_series) != len(required)):
        return False
    proof_roles: set[tuple[str, str, int]] = set()
    for proof in boundaries:
        if not isinstance(proof, dict):
            return False
        try:
            nominal_ms = int(proof.get("nominal_boundary_ms"))
            distance_ms = int(proof.get("distance_ms"))
        except (TypeError, ValueError):
            return False
        live_id = str(proof.get("live_id") or "")
        role = str(proof.get("role") or "")
        if (not live_id or role not in {"start", "end"}
                or not 0 <= distance_ms <= tolerance_ms
                or proof.get("evidence_kind") not in {
                    "persisted_screen_boundary", "legacy_screen_boundary",
                }):
            return False
        proof_roles.add((live_id, role, nominal_ms))
    minute_proofs: dict[tuple[str, int, int], dict] = {}
    for proof in minute_series:
        if not isinstance(proof, dict):
            return False
        try:
            proof_start = int(proof.get("start_ms"))
            proof_end = int(proof.get("end_ms"))
        except (TypeError, ValueError):
            return False
        proof_identity = (
            str(proof.get("live_id") or ""), proof_start, proof_end)
        if not proof_identity[0] or proof_identity in minute_proofs:
            return False
        minute_proofs[proof_identity] = proof
    expected_markers_by_live: dict[str, set[int]] = {}
    for source in required:
        if not isinstance(source, dict):
            return False
        try:
            source_start = int(source.get("start_ms"))
            source_end = int(source.get("end_ms"))
        except (TypeError, ValueError):
            return False
        live_id = str(source.get("live_id") or "")
        if (not live_id or source_end <= source_start
                or source_start < window_start or source_end > window_end
                or (live_id, "start", source_start) not in proof_roles
                or (live_id, "end", source_end) not in proof_roles):
            return False
        minute_proof = minute_proofs.get((live_id, source_start, source_end))
        first_expected = (
            (source_start + 60_000 - 1) // 60_000 * 60_000)
        expected_count = max(0, (source_end - first_expected + 59_999) // 60_000)
        expected_last = (
            first_expected + (expected_count - 1) * 60_000
            if expected_count else None)
        expected_markers_by_live.setdefault(live_id, set()).update(
            first_expected + index * 60_000
            for index in range(expected_count))
        if (not isinstance(minute_proof, dict)
                or minute_proof.get("complete") is not True
                or minute_proof.get("expected_first_ms") != (
                    first_expected if expected_count else None)
                or minute_proof.get("expected_last_ms") != expected_last
                or minute_proof.get("expected_bucket_count") != expected_count):
            return False
        series_states = minute_proof.get("series")
        if (not isinstance(series_states, dict)
                or set(series_states) != {"uv", "itemClick", "deal"}):
            return False
        for state in series_states.values():
            if (not isinstance(state, dict)
                    or state.get("complete") is not True
                    or state.get("bucket_count") != expected_count
                    or state.get("first_ms") != first_expected
                    or state.get("last_ms") != expected_last
                    or state.get("missing_bucket_count") != 0):
                return False
    actual_series = metrics.get("series")
    required_fields = {
        "uv": ("online", "visitorEnter"),
        "itemClick": ("value",),
        "deal": ("amount",),
    }
    if not isinstance(actual_series, dict):
        return False
    sole_live_id = (
        next(iter(expected_markers_by_live))
        if len(expected_markers_by_live) == 1 else "")
    for series_kind, value_fields in required_fields.items():
        rows = actual_series.get(series_kind)
        if not isinstance(rows, list):
            return False
        observed_by_live = {
            live_id: set() for live_id in expected_markers_by_live
        }
        for row in rows:
            if not isinstance(row, dict):
                return False
            row_live_id = str(row.get("fact_live_id") or sole_live_id)
            expected_markers = expected_markers_by_live.get(row_live_id)
            if expected_markers is None:
                return False
            try:
                marker = int(row.get("time"))
                values = [float(row.get(field)) for field in value_fields]
            except (TypeError, ValueError):
                return False
            if (marker not in expected_markers
                    or marker in observed_by_live[row_live_id]
                    or any(not math.isfinite(value) for value in values)):
                return False
            observed_by_live[row_live_id].add(marker)
        if observed_by_live != expected_markers_by_live:
            return False
    expected = {label for label, _unit, _source in CORE_METRIC_DEFINITIONS}
    return set(core_metrics) == expected and all(
        isinstance(core_metrics[label], dict)
        and core_metrics[label].get("quality_state") == "complete"
        for label in expected
    )


def artifact_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def normalize_media_coverage(
        value: dict[str, Any], *, window_start_ms: int,
        window_end_ms: int) -> dict[str, Any]:
    """Validate and normalize the auditable recording-coverage fact."""
    if not isinstance(value, dict):
        raise ValueError("media coverage must be an object")

    def integer(name: str) -> int:
        raw = value.get(name)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"media coverage {name} must be numeric")
        number = float(raw)
        if not number.is_integer() or number < 0:
            raise ValueError(f"media coverage {name} must be a non-negative integer")
        return int(number)

    start = integer("window_start_ms")
    end = integer("window_end_ms")
    covered = integer("covered_ms")
    missing = integer("missing_ms")
    expected_start = int(window_start_ms)
    expected_end = int(window_end_ms)
    if start != expected_start or end != expected_end or end <= start:
        raise ValueError("media coverage window does not match hourly identity")

    raw_gaps = value.get("gaps")
    if not isinstance(raw_gaps, list):
        raise ValueError("media coverage gaps must be a list")
    gaps: list[dict[str, int]] = []
    cursor = start
    for raw in raw_gaps:
        if not isinstance(raw, dict):
            raise ValueError("media coverage gap must be an object")
        try:
            gap_start = int(raw["start_ms"])
            gap_end = int(raw["end_ms"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("media coverage gap bounds must be integers") from exc
        if (isinstance(raw.get("start_ms"), bool)
                or isinstance(raw.get("end_ms"), bool)
                or gap_start < cursor or gap_end <= gap_start or gap_end > end):
            raise ValueError("media coverage gaps must be ordered inside the window")
        gaps.append({"start_ms": gap_start, "end_ms": gap_end})
        cursor = gap_end
    gap_total = sum(item["end_ms"] - item["start_ms"] for item in gaps)
    duration = end - start
    if missing != gap_total or covered != duration - missing:
        raise ValueError("media coverage totals do not match its gaps")
    ratio = round(covered / duration, 6)
    try:
        supplied_ratio = float(value.get("coverage_ratio"))
    except (TypeError, ValueError) as exc:
        raise ValueError("media coverage ratio must be numeric") from exc
    if abs(supplied_ratio - ratio) > 0.000001:
        raise ValueError("media coverage ratio does not match its totals")
    state = str(value.get("timeline_state") or "")
    if state not in {"complete", "incomplete", "empty", "missing_manifest"}:
        raise ValueError("media coverage timeline state is invalid")
    return {
        "window_start_ms": start,
        "window_end_ms": end,
        "covered_ms": covered,
        "missing_ms": missing,
        "coverage_ratio": ratio,
        "gaps": gaps,
        "timeline_state": state,
    }


def format_media_coverage(
        coverage: dict[str, Any], *,
        timezone_name: str = "Asia/Shanghai") -> str:
    """Render the same complete recording-coverage disclosure everywhere."""
    if not isinstance(coverage, dict):
        return ""
    try:
        normalized = normalize_media_coverage(
            coverage,
            window_start_ms=int(coverage.get("window_start_ms")),
            window_end_ms=int(coverage.get("window_end_ms")),
        )
        timezone = ZoneInfo(str(timezone_name))
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return ""
    duration = normalized["window_end_ms"] - normalized["window_start_ms"]
    lines = [
        f"实际录音覆盖 {normalized['covered_ms'] / 60_000:.1f}/"
        f"{duration / 60_000:.1f} 分钟",
    ]
    gaps = []
    for item in normalized["gaps"]:
        start = datetime.fromtimestamp(item["start_ms"] / 1000, timezone)
        end = datetime.fromtimestamp(item["end_ms"] / 1000, timezone)
        gaps.append(f"{start:%H:%M:%S}–{end:%H:%M:%S}（无录音）")
    if gaps:
        lines.append("缺失时段：" + "；".join(gaps))
    return "\n".join(lines)


def build_hourly_artifact(
        *,
        business_session: str,
        shift_window: str,
        window_start_ms: int,
        window_end_ms: int,
        identity: dict[str, Any] | None = None,
        metrics: dict[str, Any] | None = None,
        series: dict[str, Any] | None = None,
        transcription: dict[str, Any] | None = None,
        analysis: dict[str, Any] | None = None,
        presentation: dict[str, Any] | None = None,
        quality: dict[str, Any] | None = None,
        sources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the shared serializable payload consumed by card/report/Base."""
    quality_payload = dict(quality or {"state": "partial"})
    if "media_coverage" in quality_payload:
        quality_payload["media_coverage"] = normalize_media_coverage(
            quality_payload["media_coverage"],
            window_start_ms=int(window_start_ms),
            window_end_ms=int(window_end_ms),
        )
    payload: dict[str, Any] = {
        "identity": {
            "business_session_key": str(business_session),
            "shift_window_key": str(shift_window),
            "window_start_ms": int(window_start_ms),
            "window_end_ms": int(window_end_ms),
            **(identity or {}),
        },
        "metrics": metrics or {},
        "core_metrics": build_core_metric_facts(
            metrics or {}, window_start_ms=int(window_start_ms),
            window_end_ms=int(window_end_ms)),
        "series": series or {},
        "transcription": transcription or {},
        "analysis": analysis or {},
        "presentation": presentation or {},
        "quality": quality_payload,
        "sources": sources or [],
    }
    payload["artifact_hash"] = artifact_hash(payload)
    return payload


__all__ = [
    "BUSINESS_DAY_CUTOFF_HOUR", "DEFAULT_BUSINESS_EARLIEST_START",
    "DEFAULT_BUSINESS_LATEST_END", "SHANGHAI", "artifact_hash",
    "absolute_hour_window", "build_core_metric_facts", "build_hourly_artifact",
    "business_interval_overlaps", "business_operating_bounds",
    "business_session_key",
    "completed_absolute_hour_windows",
    "final_partial_window_key", "format_media_coverage",
    "initial_partial_window_key",
    "formal_hourly_metrics_ready", "normalize_media_coverage",
    "plan_business_windows", "shift_window_key",
]
