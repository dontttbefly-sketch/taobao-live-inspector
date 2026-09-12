from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class TranscriptSegment:
    start_ms: int
    end_ms: int
    text: str
    speaker: str | None = None
    source: str = "feishu_minutes"

    @classmethod
    def from_dict(cls, value: dict) -> "TranscriptSegment":
        return cls(
            int(value.get("start_ms") or 0), int(value.get("end_ms") or 0),
            str(value.get("text") or ""), value.get("speaker"),
            str(value.get("source") or "feishu_minutes"),
        )


@dataclass(frozen=True)
class SmartChapter:
    start_ms: int
    end_ms: int
    title: str
    summary: str = ""

    @classmethod
    def from_dict(cls, value: dict) -> "SmartChapter":
        return cls(
            int(value.get("start_ms") or 0), int(value.get("end_ms") or 0),
            str(value.get("title") or ""), str(value.get("summary") or ""),
        )


@dataclass(frozen=True)
class SmartMinutesArtifact:
    minute_token: str = ""
    minute_url: str = ""
    summary: str = ""
    chapters: list[SmartChapter] = field(default_factory=list)
    golden_quotes: list[str] = field(default_factory=list)
    note_id: str = ""
    note_doc_token: str = ""

    @classmethod
    def from_dict(cls, value: dict | None) -> "SmartMinutesArtifact | None":
        if not value:
            return None
        return cls(
            minute_token=str(value.get("minute_token") or ""),
            minute_url=str(value.get("minute_url") or ""),
            summary=str(value.get("summary") or ""),
            chapters=[SmartChapter.from_dict(item) for item in value.get("chapters") or []],
            golden_quotes=[str(item) for item in value.get("golden_quotes") or [] if str(item).strip()],
            note_id=str(value.get("note_id") or ""),
            note_doc_token=str(value.get("note_doc_token") or ""),
        )


@dataclass(frozen=True)
class TranscriptionResult:
    provider: str
    segments: list[TranscriptSegment] = field(default_factory=list)
    smart: SmartMinutesArtifact | None = None
    quality_status: str = "complete"
    quality_issues: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str | dict | None) -> "TranscriptionResult | None":
        if not raw:
            return None
        value = json.loads(raw) if isinstance(raw, str) else raw
        return cls(
            provider=str(value.get("provider") or ""),
            segments=[TranscriptSegment.from_dict(item) for item in value.get("segments") or []],
            smart=SmartMinutesArtifact.from_dict(value.get("smart")),
            quality_status=str(value.get("quality_status") or "complete"),
            quality_issues=[str(item) for item in value.get("quality_issues") or []],
        )
