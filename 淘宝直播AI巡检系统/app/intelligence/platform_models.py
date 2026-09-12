"""Immutable models and formal-payload binding for platform intelligence."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .integrity import validated_result_hash
from .models import canonical_json


_RESULT_STATUSES = frozenset(("ready",))
_SOURCE_TYPES = frozenset((
    "hourly_analysis", "hourly_observation", "hourly_talktrack", "hourly_experiment",
    "official_metric",
))


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _array(value: object, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    return value


def _clone(value: object) -> Any:
    return json.loads(canonical_json(value))


@dataclass(frozen=True)
class PlatformSource:
    source_id: str
    source_type: str
    live_id: str
    anchor_id: int | None
    source_job_key: str
    payload: dict[str, Any]
    window_start_epoch_ms: int | None = None
    window_end_epoch_ms: int | None = None
    hour_bucket: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_type": self.source_type,
            "live_id": self.live_id,
            "anchor_id": self.anchor_id,
            "source_job_key": self.source_job_key,
            "payload": _clone(self.payload),
            "window_start_epoch_ms": self.window_start_epoch_ms,
            "window_end_epoch_ms": self.window_end_epoch_ms,
            "hour_bucket": self.hour_bucket,
        }

    @classmethod
    def from_dict(cls, value: object) -> "PlatformSource":
        data = _mapping(value, "PlatformSource")
        source_type = str(data.get("source_type") or "")
        if source_type not in _SOURCE_TYPES:
            raise ValueError("unknown platform source type")
        anchor_id = data.get("anchor_id")
        if anchor_id is not None and (not isinstance(anchor_id, int) or isinstance(anchor_id, bool)):
            raise ValueError("PlatformSource.anchor_id must be an integer or null")
        payload = _mapping(data.get("payload"), "PlatformSource.payload")
        chronological = {
            name: data.get(name) for name in (
                "window_start_epoch_ms", "window_end_epoch_ms", "hour_bucket")
        }
        if any(value is not None and (
                not isinstance(value, int) or isinstance(value, bool))
               for value in chronological.values()):
            raise ValueError("platform source chronological identity must use integers")
        source = cls(
            source_id=str(data.get("source_id") or ""),
            source_type=source_type,
            live_id=str(data.get("live_id") or ""),
            anchor_id=anchor_id,
            source_job_key=str(data.get("source_job_key") or ""),
            payload=_clone(payload),
            window_start_epoch_ms=chronological["window_start_epoch_ms"],
            window_end_epoch_ms=chronological["window_end_epoch_ms"],
            hour_bucket=chronological["hour_bucket"],
        )
        if not source.source_id or not source.live_id:
            raise ValueError("platform source identity is incomplete")
        if source.source_type.startswith("hourly_") and (
                source.window_start_epoch_ms is None
                or source.window_end_epoch_ms is None
                or source.hour_bucket is None
                or source.window_end_epoch_ms <= source.window_start_epoch_ms
                or source.hour_bucket != source.window_start_epoch_ms // 3_600_000):
            raise ValueError("hourly source chronological identity is invalid")
        return source


@dataclass(frozen=True)
class PlatformIntelligenceContext:
    live_id: str
    sources: list[PlatformSource] = field(default_factory=list)
    official_metrics: dict[str, Any] = field(default_factory=dict)
    rejected_inputs: list[dict[str, Any]] = field(default_factory=list)
    input_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "live_id": self.live_id,
            "sources": [item.to_dict() for item in self.sources],
            "official_metrics": _clone(self.official_metrics),
            "rejected_inputs": _clone(self.rejected_inputs),
            "input_hash": self.input_hash,
        }

    @classmethod
    def from_dict(cls, value: object) -> "PlatformIntelligenceContext":
        data = _mapping(value, "PlatformIntelligenceContext")
        official = _mapping(data.get("official_metrics", {}), "official_metrics")
        rejected = _array(data.get("rejected_inputs", []), "rejected_inputs")
        return cls(
            live_id=str(data.get("live_id") or ""),
            sources=[PlatformSource.from_dict(item) for item in _array(
                data.get("sources", []), "sources")],
            official_metrics=_clone(official),
            rejected_inputs=[_clone(_mapping(item, "rejected input")) for item in rejected],
            input_hash=str(data.get("input_hash") or ""),
        )


def platform_context_input_hash(context: PlatformIntelligenceContext) -> str:
    payload = {
        "live_id": context.live_id,
        "sources": [item.to_dict() for item in context.sources],
        "official_metrics": context.official_metrics,
        "rejected_inputs": context.rejected_inputs,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PlatformIntelligenceResult:
    job_key: str
    status: str
    # Phase-one daily report fields.  The full narrative is persisted without
    # a card-length cap; the two short lists are rendering summaries only.
    full_analysis: str = ""
    business_conclusions: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    rejected_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_key": self.job_key,
            "status": self.status,
            "full_analysis": self.full_analysis,
            "business_conclusions": list(self.business_conclusions),
            "next_actions": list(self.next_actions),
            "rejected_reasons": list(self.rejected_reasons),
        }

    @classmethod
    def from_dict(cls, value: object) -> "PlatformIntelligenceResult":
        data = _mapping(value, "PlatformIntelligenceResult")
        status = str(data.get("status") or "")
        if status not in _RESULT_STATUSES:
            raise ValueError("unknown platform intelligence result status")
        rejected = _array(data.get("rejected_reasons", []), "rejected_reasons")
        conclusions = _array(
            data.get("business_conclusions", []), "business_conclusions")
        next_actions = _array(data.get("next_actions", []), "next_actions")
        return cls(
            job_key=str(data.get("job_key") or ""), status=status,
            full_analysis=str(data.get("full_analysis") or ""),
            business_conclusions=[str(item) for item in conclusions],
            next_actions=[str(item) for item in next_actions],
            rejected_reasons=[str(item) for item in rejected],
        )


def sanitize_platform_intelligence_payload(value: object) -> dict[str, Any]:
    """Drop all model-owned fields outside the frozen platform contract."""
    if not isinstance(value, dict):
        return {}
    try:
        return PlatformIntelligenceResult.from_dict(value).to_dict()
    except (TypeError, ValueError):
        return {}


def build_platform_intelligence_binding(
        live_id: object, input_hash: object, value: object) -> dict[str, str]:
    """构造正式载荷中可独立核验的整场智能绑定。"""
    bound_live_id = str(live_id or "")
    bound_input_hash = str(input_hash or "")
    result = PlatformIntelligenceResult.from_dict(value)
    if (not bound_live_id
            or re.fullmatch(
                rf"platform:{re.escape(bound_live_id)}:prompt:[0-9a-f]{{16}}",
                result.job_key,
            ) is None
            or re.fullmatch(r"[0-9a-f]{64}", bound_input_hash) is None):
        raise ValueError("platform intelligence binding identity is invalid")
    return {
        "live_id": bound_live_id,
        "job_key": result.job_key,
        "input_hash": bound_input_hash,
        "result_hash": validated_result_hash(result.to_dict()),
    }


def bound_platform_intelligence_from_summary(
        summary: object,
) -> tuple[PlatformIntelligenceResult, dict[str, str]]:
    """Card/Markdown 共用的冻结载荷绑定门禁。"""
    data = _mapping(summary, "platform review summary")
    binding = _mapping(
        data.get("platform_intelligence_binding"),
        "platform_intelligence_binding",
    )
    if set(binding) != {"live_id", "job_key", "input_hash", "result_hash"}:
        raise ValueError("platform intelligence binding fields are invalid")
    result = PlatformIntelligenceResult.from_dict(data.get("platform_intelligence"))
    expected = build_platform_intelligence_binding(
        data.get("live_id"), data.get("platform_intelligence_input_hash"), result.to_dict())
    normalized = {key: str(binding.get(key) or "") for key in expected}
    if normalized != expected:
        raise ValueError("platform intelligence binding mismatch")
    return result, expected
