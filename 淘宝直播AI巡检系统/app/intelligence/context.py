from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta
from dataclasses import asdict, replace
from typing import Any
from zoneinfo import ZoneInfo

from app.config import local_epoch_ms, parse_local_datetime
from app.transcription.models import SmartMinutesArtifact, TranscriptSegment

from .models import IntelligenceContext, TranscriptEvidence, canonical_json


_ALLOWED_TOKEN_KEYS = frozenset(("minutetoken", "notedoctoken"))


def _normalized_key(value: object) -> str:
    return "".join(character for character in str(value).casefold() if character.isalnum())


def _is_sensitive_key(value: object) -> bool:
    normalized = _normalized_key(value)
    if normalized in _ALLOWED_TOKEN_KEYS:
        return False
    return normalized.endswith(("apikey", "cookie", "chatid", "authorization", "token"))


def _reject_sensitive_keys(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if _is_sensitive_key(key):
                raise ValueError(f"sensitive key not allowed: {key}")
            _reject_sensitive_keys(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_sensitive_keys(item)


def _json_snapshot(value: Any) -> Any:
    _reject_sensitive_keys(value)
    return json.loads(canonical_json(value))


def intelligence_context_input_hash(context: IntelligenceContext) -> str:
    """Hash the immutable facts supplied to one DeepSeek analysis."""
    payload = {
        "stream_id": int(context.stream_id),
        "live_id": str(context.live_id),
        "anchor_id": int(context.anchor_id),
        "anchor_name": str(context.anchor_name),
        "window_start_ms": int(context.window_start_ms),
        "window_end_ms": int(context.window_end_ms),
        "transcripts": [segment.to_dict() for segment in context.transcripts],
        "smart_minutes": (
            None if context.smart_minutes is None else asdict(context.smart_minutes)),
        "metrics": context.metrics,
        "peak_context": context.peak_context,
        "history": context.history,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def offset_transcription_segments(
    segments: list[TranscriptSegment], window_start_ms: int,
) -> list[TranscriptSegment]:
    """Convert a transcription window's local timestamps to recording-relative time."""
    offset = int(window_start_ms)
    return [TranscriptSegment(
        int(segment.start_ms) + offset,
        int(segment.end_ms) + offset,
        segment.text,
        speaker=segment.speaker,
        source=segment.source,
    ) for segment in segments]


def normalize_qianniu_metrics(
    metrics: dict[str, Any], *, recording_start_ms: int | None,
) -> dict[str, Any]:
    """Normalize Qianniu rows to the evidence contract used by every caller.

    The resulting series has only the four supported metric names and stable
    ``ts``/``value`` fields.  Qianniu wall-clock timestamps become offsets from
    the recording start; without that anchor they are omitted rather than
    accidentally mixed with recording-relative timestamps.
    """
    snapshot = dict(metrics or {})
    raw_series = metrics.get("series") if isinstance(metrics, dict) else None
    fields = {
        "uv": "online",
        "itemClick": "value",
        "deal": "amount",
        "heatScore": "value",
    }
    normalized: dict[str, list[dict[str, float | int]]] = {}
    if isinstance(raw_series, dict):
        for metric_name, value_field in fields.items():
            points: list[dict[str, float | int]] = []
            rows = raw_series.get(metric_name)
            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    try:
                        timestamp = float(row.get("ts", row.get("time")))
                        value = float(row.get("value", row.get(value_field)))
                    except (TypeError, ValueError):
                        continue
                    if not math.isfinite(timestamp) or not math.isfinite(value):
                        continue
                    ts = int(timestamp)
                    if ts >= 100_000_000_000:
                        if recording_start_ms is None:
                            continue
                        ts -= int(recording_start_ms)
                    points.append({"ts": ts, "value": value})
            points.sort(key=lambda item: int(item["ts"]))
            normalized[metric_name] = points
    snapshot["series"] = normalized
    return snapshot


def clip_qianniu_metrics_to_window(
    metrics: dict[str, Any], *, window_start_ms: int, window_end_ms: int,
) -> dict[str, Any]:
    """Keep normalized minute points inside the half-open window ``[start,end)``.

    A point at an hourly boundary belongs to the newer window, so it cannot
    become evidence for two adjacent intelligence jobs.  This function accepts
    the canonical output of :func:`normalize_qianniu_metrics`; it deliberately
    does not re-parse Qianniu row fields.
    """
    start, end = int(window_start_ms), int(window_end_ms)
    snapshot = dict(metrics or {})
    series = metrics.get("series") if isinstance(metrics, dict) else None
    clipped: dict[str, list[dict[str, float | int]]] = {}
    if isinstance(series, dict) and end > start:
        for metric_name, points in series.items():
            kept: list[dict[str, float | int]] = []
            if isinstance(points, list):
                for point in points:
                    if not isinstance(point, dict):
                        continue
                    ts, value = point.get("ts"), point.get("value")
                    if (isinstance(ts, bool) or not isinstance(ts, (int, float))
                            or isinstance(value, bool) or not isinstance(value, (int, float))):
                        continue
                    if start <= int(ts) < end:
                        kept.append({"ts": int(ts), "value": float(value)})
            clipped[str(metric_name)] = kept
    snapshot["series"] = clipped
    return snapshot


def resolve_context_window_end(
    segments: list[TranscriptSegment], *, window_start_ms: int,
    requested_window_end_ms: int | None, media_window_ms: int,
) -> int:
    """Keep the persisted media boundary authoritative when it is valid."""
    start = int(window_start_ms)
    if requested_window_end_ms is not None and int(requested_window_end_ms) >= start:
        return int(requested_window_end_ms)
    spoken_end = max((int(segment.end_ms) for segment in segments), default=start)
    return max(spoken_end, start + max(0, int(media_window_ms)))


def build_hourly_context(
    *,
    stream_id: int,
    live_id: str,
    anchor_id: int,
    anchor_name: str,
    window_start_ms: int,
    window_end_ms: int,
    segments: list[TranscriptSegment],
    smart_minutes: SmartMinutesArtifact | None,
    metrics: dict[str, Any],
    peak_highlights: list[dict[str, Any]],
    history: dict[str, Any] | None = None,
) -> IntelligenceContext:
    ordered_segments = sorted(
        segments,
        key=lambda item: (item.start_ms, item.end_ms, item.text, item.speaker or "", item.source),
    )
    transcripts = [
        TranscriptEvidence(
            segment_id=f"S{index:04d}",
            start_ms=segment.start_ms,
            end_ms=segment.end_ms,
            text=segment.text,
            speaker=segment.speaker,
            source=segment.source,
        )
        for index, segment in enumerate(ordered_segments, 1)
    ]
    smart_minutes_snapshot = (
        None
        if smart_minutes is None
        else SmartMinutesArtifact.from_dict(_json_snapshot(asdict(smart_minutes)))
    )
    metrics_snapshot = _json_snapshot(metrics)
    peak_context_snapshot = _json_snapshot(peak_highlights)
    history_snapshot = _json_snapshot(history or {
        "previous_hour": None,
        "yesterday_same_window": None,
        "recent_anchor_shifts": [],
        "seven_day_trend": [],
        "target": {"state": "no_target"},
    })
    context = IntelligenceContext(
        stream_id=int(stream_id),
        live_id=str(live_id),
        anchor_id=int(anchor_id),
        anchor_name=str(anchor_name),
        window_start_ms=int(window_start_ms),
        window_end_ms=int(window_end_ms),
        transcripts=transcripts,
        smart_minutes=smart_minutes_snapshot,
        metrics=metrics_snapshot,
        peak_context=peak_context_snapshot,
        history=history_snapshot,
        input_hash="",
    )
    return replace(context, input_hash=intelligence_context_input_hash(context))


def build_analysis_history(
        store: Any, *, anchor_id: int, stream_id: int, live_id: str,
        context_start_ms: int) -> dict[str, Any]:
    """Read comparable persisted history for one analyst invocation.

    Missing history is explicit and remains missing; this helper never fills
    absent business values with zero.
    """
    history: dict[str, Any] = {
        "previous_hour": None,
        "yesterday_same_window": None,
        "recent_anchor_shifts": [],
        "seven_day_trend": [],
        "target": {"state": "no_target"},
    }
    try:
        current_stream = store.query(
            "SELECT started_at FROM streams WHERE id=? LIMIT 1", (int(stream_id),))
        current_started_text = str(current_stream[0]["started_at"] or "") if current_stream else ""
        current_started = parse_local_datetime(current_started_text) if current_stream else None
        context_epoch_ms = (
            int(local_epoch_ms(current_started_text) or 0) + int(context_start_ms)
            if current_started is not None else None
        )
        context_dt = (
            datetime.fromtimestamp(context_epoch_ms / 1000, ZoneInfo("Asia/Shanghai"))
            if context_epoch_ms is not None else None
        )
        recent = store.query(
            """SELECT id,started_at,ended_at,duration_sec,status,live_id
               FROM streams WHERE anchor_id=? AND id<>?
               ORDER BY COALESCE(started_at,'') DESC,id DESC LIMIT 5""",
            (int(anchor_id), int(stream_id)),
        )
        history["recent_anchor_shifts"] = [
            {key: row[key] for key in (
                "id", "started_at", "ended_at", "duration_sec", "status", "live_id")}
            for row in recent
        ]
        snapshots = store.query(
            """SELECT ts,source,data_state,pay_amt,buyer_cnt,item_qty,
                      online_uv,max_online_uv,viewer_uv,viewer_pv
               FROM brief_snapshots WHERE stream_id=? AND snapshot_kind='brief'
               ORDER BY id DESC LIMIT 2""",
            (int(stream_id),),
        )
        if snapshots:
            history["previous_hour"] = dict(snapshots[0])
        if context_dt is not None:
            yesterday = (context_dt - timedelta(days=1)).strftime("%Y-%m-%d")
            same_window = store.query(
                """SELECT b.ts,b.source,b.data_state,b.pay_amt,b.buyer_cnt,b.item_qty,
                          b.online_uv,b.max_online_uv,b.viewer_uv,b.viewer_pv
                   FROM brief_snapshots b JOIN streams s ON s.id=b.stream_id
                   WHERE s.anchor_id=? AND s.id<>? AND s.started_at LIKE ?
                         AND b.snapshot_kind='brief'
                   ORDER BY b.id DESC LIMIT 1""",
                (int(anchor_id), int(stream_id), f"{yesterday} {context_dt:%H}:%"),
            )
            if same_window:
                history["yesterday_same_window"] = dict(same_window[0])
            trend_rows = store.query(
                """SELECT substr(s.started_at,1,10) AS day,
                          AVG(b.pay_amt) AS pay_amt, AVG(b.buyer_cnt) AS buyer_cnt,
                          AVG(b.viewer_uv) AS viewer_uv, AVG(b.max_online_uv) AS max_online_uv
                   FROM brief_snapshots b JOIN streams s ON s.id=b.stream_id
                   WHERE s.anchor_id=? AND s.started_at>=? AND b.snapshot_kind='brief'
                   GROUP BY substr(s.started_at,1,10) ORDER BY day DESC LIMIT 7""",
                (int(anchor_id), (context_dt - timedelta(days=6)).strftime(
                    "%Y-%m-%d %H:%M:%S")),
            )
            history["seven_day_trend"] = [dict(row) for row in trend_rows]
    except Exception:
        return history
    return history


def intelligence_peak_context(highlights: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach stable evidence IDs without changing an audited peak payload."""
    result: list[dict[str, Any]] = []
    for index, highlight in enumerate(highlights or [], 1):
        if not isinstance(highlight, dict):
            continue
        item = _json_snapshot(highlight)
        item["peak_id"] = str(
            item.get("peak_id") or item.get("source_id") or f"P{index:04d}")
        result.append(item)
    return result


__all__ = [
    "build_analysis_history", "build_hourly_context", "intelligence_context_input_hash",
    "intelligence_peak_context",
    "clip_qianniu_metrics_to_window", "normalize_qianniu_metrics", "offset_transcription_segments",
    "resolve_context_window_end",
]
