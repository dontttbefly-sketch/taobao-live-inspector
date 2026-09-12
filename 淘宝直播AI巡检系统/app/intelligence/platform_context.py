"""Build the minimal integrity-bound liveId platform context."""
from __future__ import annotations

from typing import Any

from app.config import local_epoch_ms
from app.db import Store

from .context import intelligence_context_input_hash
from .integrity import validated_result_from_artifact
from .models import HourlyIntelligenceResult, IntelligenceContext
from .platform_models import (
    PlatformIntelligenceContext,
    PlatformSource,
    _clone,
    platform_context_input_hash,
)


def _hourly_sources(context: IntelligenceContext) -> dict[tuple[str, str], object]:
    sources: dict[tuple[str, str], object] = {
        ("transcript", item.segment_id): item.to_dict() for item in context.transcripts
    }
    for item in context.peak_context:
        if isinstance(item, dict):
            source_id = str(item.get("peak_id") or item.get("source_id") or "")
            if source_id:
                sources[("peak", source_id)] = item
    windows = context.metrics.get("metric_windows") if isinstance(context.metrics, dict) else None
    if isinstance(windows, list):
        for item in windows:
            if isinstance(item, dict):
                source_id = str(item.get("source_id") or item.get("window_id") or item.get("id") or "")
                if source_id:
                    sources[("metric_window", source_id)] = item
    series = context.metrics.get("series") if isinstance(context.metrics, dict) else None
    if isinstance(series, dict):
        for name, values in series.items():
            payload = {"metric_name": str(name), "series": values,
                       "window_start_ms": context.window_start_ms,
                       "window_end_ms": context.window_end_ms}
            sources.setdefault(("metric_window", str(name)), payload)
            sources.setdefault(("metric_window", f"M:{name}"), payload)
    return sources


def _result_refs(result: HourlyIntelligenceResult) -> list[tuple[str, str]]:
    return [
        (str(ref.source_type), str(ref.source_id))
        for group in (result.observations, result.reusable_talktracks,
                      result.action_experiments)
        for item in group for ref in item.evidence
    ]


def _official_snapshot(summary: dict) -> dict[str, Any]:
    platform = _clone(summary.get("display_metrics") or {})
    platform.pop("raw", None)
    anchors: dict[str, Any] = {}
    for anchor in summary.get("anchors") or []:
        if not isinstance(anchor, dict):
            continue
        anchor_id = int(anchor.get("anchor_id") or 0)
        if anchor_id <= 0:
            continue
        metrics = _clone(anchor.get("metrics") or {})
        metrics.pop("raw", None)
        anchors[str(anchor_id)] = {
            "anchor_id": anchor_id,
            "anchor_name": str(anchor.get("anchor_name") or ""),
            "metrics": metrics,
        }
    return {"platform": platform, "anchors": anchors}


def _reject(rejections: list[dict[str, Any]], code: str, source_id: str, detail: str = "") -> None:
    rejections.append({"code": code, "source_id": str(source_id), "detail": str(detail)[:240]})


def build_platform_intelligence_context(
        store: Store, summary: dict, live_id: str) -> PlatformIntelligenceContext:
    """Build a minimal trusted snapshot and quarantine every invalid source."""
    live_id = str(live_id)
    business_scope = bool(store.query(
        "SELECT 1 FROM business_sessions WHERE business_session_key=? LIMIT 1",
        (live_id,)))
    sources: list[PlatformSource] = []
    rejected: list[dict[str, Any]] = []
    rows = store.query(
        """SELECT j.*,s.live_id AS stream_live_id,s.anchor_id AS stream_anchor_id,
                  s.started_at AS stream_started_at
           FROM intelligence_jobs j LEFT JOIN streams s ON s.id=j.stream_id
           WHERE j.task_type='hourly' AND
                 (CASE WHEN ? THEN s.business_session_key=?
                       ELSE (j.live_id=? OR s.live_id=?) END)
           ORDER BY j.window_start_ms,j.window_end_ms,j.job_key""",
        (int(business_scope), live_id, live_id, live_id),
    )
    for raw in rows:
        job = dict(raw)
        job_key = str(job.get("job_key") or "")
        try:
            if (not business_scope and (
                    str(job.get("live_id") or "") != live_id
                    or str(job.get("stream_live_id") or "") != live_id)):
                raise ValueError("cross_live_hourly_job")
            if business_scope and not str(job.get("stream_live_id") or ""):
                raise ValueError("missing_stream_live_id")
            status = str(job.get("status") or "")
            if status != "ready":
                raise ValueError("hourly_job_not_terminal")
            if int(job.get("anchor_id") or 0) != int(job.get("stream_anchor_id") or 0):
                raise ValueError("hourly_anchor_mismatch")
            artifact = store.get_intelligence_artifact(job_key)
            result = validated_result_from_artifact(
                artifact or {}, expected_job_key=job_key, expected_status=status)
            frozen_raw = (artifact or {}).get("context_snapshot")
            if not frozen_raw:
                raise ValueError("missing_hourly_context_snapshot")
            frozen = IntelligenceContext.from_dict(frozen_raw)
            if (not frozen.input_hash
                    or intelligence_context_input_hash(frozen) != frozen.input_hash
                    or frozen.input_hash != str(job.get("input_hash") or "")
                    or (not business_scope and frozen.live_id != live_id)
                    or frozen.stream_id != int(job.get("stream_id") or 0)
                    or frozen.anchor_id != int(job.get("anchor_id") or 0)
                    or frozen.window_start_ms != int(job.get("window_start_ms") or 0)
                    or frozen.window_end_ms != int(job.get("window_end_ms") or 0)):
                raise ValueError("hourly_context_binding_mismatch")
            evidence = _hourly_sources(frozen)
            if any(ref not in evidence for ref in _result_refs(result)):
                raise ValueError("hourly_result_evidence_missing")
            stream_start_epoch_ms = local_epoch_ms(
                str(job.get("stream_started_at") or ""))
            if stream_start_epoch_ms is None:
                raise ValueError("missing_stream_started_at")
            absolute_start_ms = stream_start_epoch_ms + frozen.window_start_ms
            absolute_end_ms = stream_start_epoch_ms + frozen.window_end_ms
            hour_bucket = absolute_start_ms // 3_600_000
            if (result.status == "ready"
                    and (result.full_analysis.strip()
                         or result.business_conclusions)):
                sources.append(PlatformSource(
                    source_id=f"{job_key}:analysis",
                    source_type="hourly_analysis",
                    live_id=live_id,
                    anchor_id=frozen.anchor_id,
                    source_job_key=job_key,
                    payload={
                        "full_analysis": result.full_analysis,
                        "business_conclusions": list(
                            result.business_conclusions),
                        "window_start_ms": frozen.window_start_ms,
                        "window_end_ms": frozen.window_end_ms,
                    },
                    window_start_epoch_ms=absolute_start_ms,
                    window_end_epoch_ms=absolute_end_ms,
                    hour_bucket=hour_bucket,
                ))
            groups: tuple[tuple[str, str, list[Any]], ...] = (
                ("observation", "hourly_observation", result.observations),
                ("talktrack", "hourly_talktrack", result.reusable_talktracks),
                ("experiment", "hourly_experiment", result.action_experiments),
            )
            for label, source_type, items in groups:
                for index, item in enumerate(items):
                    item_payload = item.to_dict()
                    refs = [{"source_type": ref.source_type, "source_id": ref.source_id,
                             "payload": _clone(evidence[(str(ref.source_type), str(ref.source_id))])}
                            for ref in item.evidence]
                    suffix = (item.experiment_id if label == "experiment"
                              else str(index))
                    source_id = f"{job_key}:{label}:{suffix}"
                    sources.append(PlatformSource(
                        source_id=source_id, source_type=source_type, live_id=live_id,
                        anchor_id=frozen.anchor_id, source_job_key=job_key,
                        payload={"item": item_payload, "evidence": refs,
                                 "window_start_ms": frozen.window_start_ms,
                                 "window_end_ms": frozen.window_end_ms},
                        window_start_epoch_ms=absolute_start_ms,
                        window_end_epoch_ms=absolute_end_ms,
                        hour_bucket=hour_bucket,
                    ))
        except (KeyError, TypeError, ValueError) as exc:
            _reject(rejected, "rejected_hourly_artifact", job_key, str(exc))

    official = _official_snapshot(summary)
    for key, value in sorted((official.get("platform") or {}).items()):
        if value is not None and isinstance(value, (str, int, float, bool)):
            sources.append(PlatformSource(
                source_id=f"official:platform:{key}", source_type="official_metric",
                live_id=live_id, anchor_id=None, source_job_key="",
                payload={"metric_name": key, "value": value, "scope": "platform"},
            ))
    for anchor_key, anchor in sorted((official.get("anchors") or {}).items()):
        for key, value in sorted((anchor.get("metrics") or {}).items()):
            if value is not None and isinstance(value, (str, int, float, bool)):
                sources.append(PlatformSource(
                    source_id=f"official:anchor:{anchor_key}:{key}",
                    source_type="official_metric", live_id=live_id,
                    anchor_id=int(anchor_key), source_job_key="",
                    payload={"metric_name": key, "value": value, "scope": "anchor"},
                ))

    sources.sort(key=lambda item: item.source_id)
    context = PlatformIntelligenceContext(
        live_id=live_id, sources=sources, official_metrics=official,
        rejected_inputs=rejected,
    )
    return PlatformIntelligenceContext(
        live_id=context.live_id, sources=context.sources,
        official_metrics=context.official_metrics,
        rejected_inputs=context.rejected_inputs,
        input_hash=platform_context_input_hash(context),
    )
