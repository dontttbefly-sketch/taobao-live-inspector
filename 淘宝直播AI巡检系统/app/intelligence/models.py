from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from app.transcription.models import SmartMinutesArtifact


EvidenceSourceType = Literal["transcript", "metric_window", "peak"]
ResultStatus = Literal["ready"]
ObservationRelation = Literal[
    "temporal_association", "comparison", "pattern", "counterexample", "causal",
]
_EVIDENCE_SOURCE_TYPES = frozenset(("transcript", "metric_window", "peak"))
_RESULT_STATUSES = frozenset(("ready",))
_OBSERVATION_RELATIONS = frozenset((
    "temporal_association", "comparison", "pattern", "counterexample", "causal",
))


def _require_mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _require_list(value: object, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    return value


def _enum(value: object, allowed: frozenset[str], name: str) -> str:
    item = str(value or "")
    if item not in allowed:
        raise ValueError(f"unknown {name}: {item!r}")
    return item


def _json_value(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON does not allow NaN or infinity")
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError(f"value is not JSON serializable: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


@dataclass(frozen=True)
class EvidenceRef:
    source_type: EvidenceSourceType
    source_id: str

    def to_dict(self) -> dict[str, str]:
        return {"source_type": self.source_type, "source_id": self.source_id}

    @classmethod
    def from_dict(cls, value: object) -> "EvidenceRef":
        data = _require_mapping(value, "EvidenceRef")
        return cls(
            source_type=_enum(data.get("source_type"), _EVIDENCE_SOURCE_TYPES, "EvidenceRef.source_type"),  # type: ignore[arg-type]
            source_id=str(data.get("source_id") or ""),
        )


@dataclass(frozen=True)
class TranscriptEvidence:
    segment_id: str
    start_ms: int
    end_ms: int
    text: str
    speaker: str | None = None
    source: str = "feishu_minutes"

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "text": self.text,
            "speaker": self.speaker,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, value: object) -> "TranscriptEvidence":
        data = _require_mapping(value, "TranscriptEvidence")
        return cls(
            segment_id=str(data.get("segment_id") or ""),
            start_ms=int(data.get("start_ms") or 0),
            end_ms=int(data.get("end_ms") or 0),
            text=str(data.get("text") or ""),
            speaker=None if data.get("speaker") is None else str(data["speaker"]),
            source=str(data.get("source") or "feishu_minutes"),
        )


@dataclass(frozen=True)
class Observation:
    statement: str
    relation: ObservationRelation
    evidence: list[EvidenceRef] = field(default_factory=list)
    scene: str = ""
    live_id: str = ""
    source_id: str = ""
    # Populated by the evidence validator, never trusted from model output.
    # Older persisted observations remain readable with empty values.
    phenomenon_key: str = ""
    phenomenon_statement: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _json_value({
            "statement": self.statement,
            "relation": self.relation,
            "evidence": [item.to_dict() for item in self.evidence],
            "scene": self.scene,
            "live_id": self.live_id,
            "source_id": self.source_id,
            "phenomenon_key": self.phenomenon_key,
            "phenomenon_statement": self.phenomenon_statement,
        })

    @classmethod
    def from_dict(cls, value: object) -> "Observation":
        data = _require_mapping(value, "Observation")
        evidence = _require_list(data.get("evidence", []), "Observation.evidence")
        return cls(
            statement=str(data.get("statement") or ""),
            relation=_enum(data.get("relation"), _OBSERVATION_RELATIONS, "Observation.relation"),  # type: ignore[arg-type]
            evidence=[EvidenceRef.from_dict(item) for item in evidence],
            scene=str(data.get("scene") or ""),
            live_id=str(data.get("live_id") or ""),
            source_id=str(data.get("source_id") or ""),
            phenomenon_key=str(data.get("phenomenon_key") or ""),
            phenomenon_statement=str(data.get("phenomenon_statement") or ""),
        )


@dataclass(frozen=True)
class ReusableTalktrack:
    original_text: str
    reusable_script: str
    scene: str
    evidence: list[EvidenceRef] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _json_value({
            "original_text": self.original_text,
            "reusable_script": self.reusable_script,
            "scene": self.scene,
            "evidence": [item.to_dict() for item in self.evidence],
        })

    @classmethod
    def from_dict(cls, value: object) -> "ReusableTalktrack":
        data = _require_mapping(value, "ReusableTalktrack")
        evidence = _require_list(data.get("evidence", []), "ReusableTalktrack.evidence")
        return cls(
            original_text=str(data.get("original_text") or ""),
            reusable_script=str(data.get("reusable_script") or ""),
            scene=str(data.get("scene") or ""),
            evidence=[EvidenceRef.from_dict(item) for item in evidence],
        )


@dataclass(frozen=True)
class ActionExperiment:
    experiment_id: str
    issue: str
    action: str
    script: str
    trigger: str
    duration: str
    metric_name: str
    comparison: str
    # 2026-08-08 改版：分析型建议可不配实验窗口（duration/comparison 为空），
    # evaluation_window_seconds 允许 None（复验按无窗口处理）。
    evaluation_window_seconds: int | None = None
    evidence: list[EvidenceRef] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _json_value({
            "experiment_id": self.experiment_id,
            "issue": self.issue,
            "action": self.action,
            "script": self.script,
            "trigger": self.trigger,
            "duration": self.duration,
            "metric_name": self.metric_name,
            "comparison": self.comparison,
            "evaluation_window_seconds": self.evaluation_window_seconds,
            "evidence": [item.to_dict() for item in self.evidence],
        })

    @classmethod
    def from_dict(cls, value: object) -> "ActionExperiment":
        data = _require_mapping(value, "ActionExperiment")
        evidence = _require_list(data.get("evidence", []), "ActionExperiment.evidence")
        raw_window = data.get("evaluation_window_seconds")
        if raw_window is None:
            evaluation_window_seconds: int | None = None
        elif (not isinstance(raw_window, (int, float))
                or isinstance(raw_window, bool)
                or int(raw_window) != raw_window
                or not 1 <= int(raw_window) <= 3_600):
            raise ValueError(
                "ActionExperiment.evaluation_window_seconds must be 1..3600 or null")
        else:
            evaluation_window_seconds = int(raw_window)
        return cls(
            experiment_id=str(data.get("experiment_id") or ""),
            issue=str(data.get("issue") or ""),
            action=str(data.get("action") or ""),
            script=str(data.get("script") or ""),
            trigger=str(data.get("trigger") or ""),
            duration=str(data.get("duration") or ""),
            metric_name=str(data.get("metric_name") or ""),
            comparison=str(data.get("comparison") or ""),
            evaluation_window_seconds=evaluation_window_seconds,
            evidence=[EvidenceRef.from_dict(item) for item in evidence],
        )


@dataclass(frozen=True)
class IntelligenceContext:
    stream_id: int
    live_id: str
    anchor_id: int
    anchor_name: str
    window_start_ms: int
    window_end_ms: int
    transcripts: list[TranscriptEvidence] = field(default_factory=list)
    smart_minutes: SmartMinutesArtifact | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    peak_context: list[dict[str, Any]] = field(default_factory=list)
    history: dict[str, Any] = field(default_factory=dict)
    input_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        smart_minutes = asdict(self.smart_minutes) if self.smart_minutes is not None else None
        return _json_value({
            "stream_id": self.stream_id,
            "live_id": self.live_id,
            "anchor_id": self.anchor_id,
            "anchor_name": self.anchor_name,
            "window_start_ms": self.window_start_ms,
            "window_end_ms": self.window_end_ms,
            "transcripts": [segment.to_dict() for segment in self.transcripts],
            "smart_minutes": smart_minutes,
            "metrics": self.metrics,
            "peak_context": self.peak_context,
            "history": self.history,
            "input_hash": self.input_hash,
        })

    @classmethod
    def from_dict(cls, value: object) -> "IntelligenceContext":
        data = _require_mapping(value, "IntelligenceContext")
        transcripts = _require_list(data.get("transcripts", []), "IntelligenceContext.transcripts")
        metrics = data.get("metrics", {})
        if not isinstance(metrics, dict):
            raise ValueError("IntelligenceContext.metrics must be an object")
        peak_context = _require_list(data.get("peak_context", []), "IntelligenceContext.peak_context")
        smart_minutes_data = data.get("smart_minutes")
        if smart_minutes_data is not None and not isinstance(smart_minutes_data, dict):
            raise ValueError("IntelligenceContext.smart_minutes must be an object or null")
        history = data.get("history", {})
        if not isinstance(history, dict):
            raise ValueError("IntelligenceContext.history must be an object")
        return cls(
            stream_id=int(data.get("stream_id") or 0),
            live_id=str(data.get("live_id") or ""),
            anchor_id=int(data.get("anchor_id") or 0),
            anchor_name=str(data.get("anchor_name") or ""),
            window_start_ms=int(data.get("window_start_ms") or 0),
            window_end_ms=int(data.get("window_end_ms") or 0),
            transcripts=[TranscriptEvidence.from_dict(item) for item in transcripts],
            smart_minutes=SmartMinutesArtifact.from_dict(smart_minutes_data),
            metrics=_json_value(metrics),
            peak_context=_json_value(peak_context),
            history=_json_value(history),
            input_hash=str(data.get("input_hash") or ""),
        )

    def transcript_chunks(self, max_chars: int) -> list[list[TranscriptEvidence]]:
        if max_chars <= 0:
            raise ValueError("max_chars must be positive")
        chunks: list[list[TranscriptEvidence]] = []
        current: list[TranscriptEvidence] = []
        size = 0
        for segment in self.transcripts:
            width = len(segment.text)
            if current and size + width > max_chars:
                chunks.append(current)
                current, size = [], 0
            current.append(segment)
            size += width
        if current:
            chunks.append(current)
        return chunks


@dataclass(frozen=True)
class HourlyIntelligenceResult:
    job_key: str
    status: ResultStatus
    # DeepSeek's complete analyst narrative.  Cards may show only a bounded
    # selection, but the frozen artifact keeps the full analysis verbatim.
    full_analysis: str = ""
    business_conclusions: list[str] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    reusable_talktracks: list[ReusableTalktrack] = field(default_factory=list)
    action_experiments: list[ActionExperiment] = field(default_factory=list)
    rejected_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _json_value({
            "job_key": self.job_key,
            "status": self.status,
            "full_analysis": self.full_analysis,
            "business_conclusions": self.business_conclusions,
            "observations": [item.to_dict() for item in self.observations],
            "reusable_talktracks": [item.to_dict() for item in self.reusable_talktracks],
            "action_experiments": [item.to_dict() for item in self.action_experiments],
            "rejected_reasons": self.rejected_reasons,
        })

    @classmethod
    def from_dict(cls, value: object) -> "HourlyIntelligenceResult":
        data = _require_mapping(value, "HourlyIntelligenceResult")
        observations = _require_list(data.get("observations", []), "HourlyIntelligenceResult.observations")
        talktracks = _require_list(
            data.get("reusable_talktracks", []), "HourlyIntelligenceResult.reusable_talktracks",
        )
        actions = _require_list(
            data.get("action_experiments", []), "HourlyIntelligenceResult.action_experiments",
        )
        rejections = _require_list(data.get("rejected_reasons", []), "HourlyIntelligenceResult.rejected_reasons")
        conclusions = _require_list(
            data.get("business_conclusions", []),
            "HourlyIntelligenceResult.business_conclusions",
        )
        return cls(
            job_key=str(data.get("job_key") or ""),
            status=_enum(data.get("status"), _RESULT_STATUSES, "HourlyIntelligenceResult.status"),  # type: ignore[arg-type]
            full_analysis=str(data.get("full_analysis") or ""),
            business_conclusions=[str(item) for item in conclusions],
            observations=[Observation.from_dict(item) for item in observations],
            reusable_talktracks=[ReusableTalktrack.from_dict(item) for item in talktracks],
            action_experiments=[ActionExperiment.from_dict(item) for item in actions],
            rejected_reasons=[str(item) for item in rejections],
        )
