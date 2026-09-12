from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import TYPE_CHECKING

from app.config import SHANGHAI
from app.schedule import load_schedule, resolve_unique_scheduled_anchor

from .matcher import find_matches
from .codec import canonical_json
from .models import (
    AudioJob,
    ComplianceEventDraft,
    RecognizedSentence,
    RecognizedUnit,
    TranscriptBatch,
    WordEntry,
    WordlistSnapshot,
)

if TYPE_CHECKING:
    from .store import ComplianceStore


PENDING_ANCHOR = "待确认"
_TARGET_HASH = re.compile(r"[0-9a-f]{64}\Z")


@dataclasses.dataclass(frozen=True, order=True)
class EventProvenance:
    event_key: str
    delivery_key: str
    raw_term: str
    normalized_term: str
    sentence_text: str
    occurrence_index: int
    hit_start_ms: int
    hit_end_ms: int
    model_version: str


def _strict_nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    if value < 0:
        raise ValueError(f"{field} must be nonnegative")
    return value


def event_identity(
    live_id: str,
    hit_start_ms: int,
    hit_end_ms: int,
    normalized_term: str,
    occurrence_index: int,
) -> tuple[str, str]:
    if not isinstance(live_id, str) or not live_id.strip():
        raise ValueError("live_id must be nonempty")
    if not isinstance(normalized_term, str) or not normalized_term.strip():
        raise ValueError("normalized_term must be nonempty")
    start = _strict_nonnegative_int(hit_start_ms, "hit_start_ms")
    end = _strict_nonnegative_int(hit_end_ms, "hit_end_ms")
    occurrence = _strict_nonnegative_int(occurrence_index, "occurrence_index")
    if end <= start:
        raise ValueError("hit times must be ordered")
    if occurrence <= 0:
        raise ValueError("occurrence_index must be positive")
    material = "\0".join((
        live_id,
        str(start),
        str(end),
        normalized_term,
        str(occurrence),
    ))
    event_key = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return event_key, f"compliance:{event_key}"


class ScheduleAttributor:
    def __init__(
        self,
        schedule_loader: Callable[[datetime], object] | None = None,
    ) -> None:
        self._schedule_loader = schedule_loader

    def anchor_at(self, hit_ms: int) -> str:
        value = _strict_nonnegative_int(hit_ms, "hit_ms")
        hit_datetime = datetime.fromtimestamp(value / 1000, SHANGHAI)
        try:
            if self._schedule_loader is None:
                schedule = load_schedule(now=hit_datetime)
            else:
                schedule = self._schedule_loader(hit_datetime)
            if not isinstance(schedule, Mapping):
                return PENDING_ANCHOR
            anchor = resolve_unique_scheduled_anchor(dict(schedule), hit_datetime)
        except Exception:
            return PENDING_ANCHOR
        return anchor or PENDING_ANCHOR


def _validate_creation(mode: str, target_hash: str) -> None:
    if mode not in {"shadow", "live"}:
        raise ValueError("compliance event mode must be shadow or live")
    if not isinstance(target_hash, str):
        raise ValueError("compliance target hash is invalid")
    if mode == "shadow":
        if target_hash:
            raise ValueError("shadow compliance event cannot have a target hash")
    elif _TARGET_HASH.fullmatch(target_hash) is None:
        raise ValueError("live compliance event requires a SHA-256 target hash")


def _validate_transcript_batch(batch: TranscriptBatch) -> None:
    if (
        not isinstance(batch, TranscriptBatch)
        or not isinstance(batch.model_version, str)
        or not batch.model_version.strip()
        or type(batch.sentences) is not tuple
    ):
        raise ValueError("compliance transcript model version is required")
    previous_start = -1
    previous_end = -1
    for sentence in batch.sentences:
        if (
            not isinstance(sentence, RecognizedSentence)
            or not isinstance(sentence.text, str)
            or not sentence.text.strip()
            or type(sentence.units) is not tuple
            or not sentence.units
        ):
            raise ValueError("compliance transcript sentence is invalid")
        for unit in sentence.units:
            if (
                not isinstance(unit, RecognizedUnit)
                or not isinstance(unit.text, str)
                or not unit.text
                or type(unit.start_ms) is not int
                or type(unit.end_ms) is not int
                or unit.start_ms < 0
                or unit.end_ms < unit.start_ms
                or unit.start_ms < previous_start
                or unit.end_ms < previous_end
            ):
                raise ValueError("compliance transcript unit is invalid")
            previous_start = unit.start_ms
            previous_end = unit.end_ms


def serialize_transcript_batch(batch: TranscriptBatch) -> str:
    _validate_transcript_batch(batch)
    return canonical_json(dataclasses.asdict(batch))


def _reject_json_constant(_: str) -> None:
    raise ValueError("compliance transcript JSON contains a non-finite number")


def decode_transcript_batch(result_json: str) -> TranscriptBatch:
    if not isinstance(result_json, str) or not result_json:
        raise ValueError("compliance transcript result is required")
    try:
        payload = json.loads(result_json, parse_constant=_reject_json_constant)
        if type(payload) is not dict or set(payload) != {
            "model_version", "sentences"
        }:
            raise ValueError("compliance transcript root is invalid")
        model_version = payload["model_version"]
        raw_sentences = payload["sentences"]
        if not isinstance(model_version, str) or type(raw_sentences) is not list:
            raise ValueError("compliance transcript root is invalid")
        sentences: list[RecognizedSentence] = []
        for raw_sentence in raw_sentences:
            if type(raw_sentence) is not dict or set(raw_sentence) != {
                "text", "units"
            }:
                raise ValueError("compliance transcript sentence is invalid")
            sentence_text = raw_sentence["text"]
            raw_units = raw_sentence["units"]
            if not isinstance(sentence_text, str) or type(raw_units) is not list:
                raise ValueError("compliance transcript sentence is invalid")
            units: list[RecognizedUnit] = []
            for raw_unit in raw_units:
                if type(raw_unit) is not dict or set(raw_unit) != {
                    "end_ms", "start_ms", "text"
                }:
                    raise ValueError("compliance transcript unit is invalid")
                units.append(RecognizedUnit(
                    text=raw_unit["text"],
                    start_ms=raw_unit["start_ms"],
                    end_ms=raw_unit["end_ms"],
                ))
            sentences.append(RecognizedSentence(
                text=sentence_text,
                units=tuple(units),
            ))
        batch = TranscriptBatch(model_version, tuple(sentences))
        _validate_transcript_batch(batch)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("compliance transcript result is invalid") from exc
    if serialize_transcript_batch(batch) != result_json:
        raise ValueError("compliance transcript result is not canonical")
    return batch


def derive_event_provenance(
    *,
    live_id: str,
    recognition_origin_ms: int,
    commit_start_ms: int,
    commit_end_ms: int,
    batch: TranscriptBatch,
    entries: tuple[WordEntry, ...],
) -> tuple[EventProvenance, ...]:
    origin = _strict_nonnegative_int(
        recognition_origin_ms, "recognition_origin_ms"
    )
    commit_start = _strict_nonnegative_int(commit_start_ms, "commit_start_ms")
    commit_end = _strict_nonnegative_int(commit_end_ms, "commit_end_ms")
    if not commit_start < commit_end:
        raise ValueError("compliance commit window is invalid")
    _validate_transcript_batch(batch)
    if type(entries) is not tuple or any(
        not isinstance(entry, WordEntry) for entry in entries
    ):
        raise ValueError("compliance wordlist entries are invalid")
    results: list[EventProvenance] = []
    for sentence in batch.sentences:
        for match in find_matches(sentence, entries):
            absolute_start = origin + match.start_ms
            absolute_end = origin + match.end_ms
            if not commit_start <= absolute_start < commit_end:
                continue
            event_key, delivery_key = event_identity(
                live_id,
                absolute_start,
                absolute_end,
                match.normalized_term,
                match.occurrence_index,
            )
            results.append(EventProvenance(
                event_key=event_key,
                delivery_key=delivery_key,
                raw_term=match.raw_term,
                normalized_term=match.normalized_term,
                sentence_text=match.sentence_text,
                occurrence_index=match.occurrence_index,
                hit_start_ms=absolute_start,
                hit_end_ms=absolute_end,
                model_version=batch.model_version,
            ))
    return tuple(results)


class EventService:
    def __init__(
        self,
        store: ComplianceStore,
        attributor: ScheduleAttributor | None = None,
        *,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self.store = store
        self.attributor = attributor or ScheduleAttributor()
        self._now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)

    def commit(
        self,
        job: AudioJob,
        batch: TranscriptBatch,
        wordlist: WordlistSnapshot,
        mode: str,
        target_hash: str,
    ) -> tuple[str, ...]:
        committed = self.store.committed_audio_event_keys(job.job_key)
        if committed is not None:
            return committed
        if job.continuity != "ok":
            created_at_ms = _strict_nonnegative_int(self._now_ms(), "created_at_ms")
            self.store.block_audio_job_timeline(
                job, updated_at_ms=created_at_ms
            )
            return ()
        if (
            not isinstance(job.live_id, str)
            or not job.live_id.strip()
            or job.wordlist_version_id != wordlist.version_id
        ):
            raise ValueError("compliance job and wordlist identity mismatch")
        durable_wordlist = self.store.wordlist_version(wordlist.version_id)
        if durable_wordlist is None or durable_wordlist != wordlist:
            raise ValueError("compliance wordlist snapshot mismatch")
        _validate_creation(mode, target_hash)
        result_json = serialize_transcript_batch(batch)
        created_at_ms = _strict_nonnegative_int(self._now_ms(), "created_at_ms")

        drafts: list[ComplianceEventDraft] = []
        provenance = derive_event_provenance(
            live_id=job.live_id,
            recognition_origin_ms=job.recognition_origin_ms,
            commit_start_ms=job.commit_start_ms,
            commit_end_ms=job.commit_end_ms,
            batch=batch,
            entries=wordlist.entries,
        )
        for item in provenance:
            drafts.append(ComplianceEventDraft(
                event_key=item.event_key,
                delivery_key=item.delivery_key,
                job_key=job.job_key,
                live_id=job.live_id,
                wordlist_version_id=wordlist.version_id,
                raw_term=item.raw_term,
                normalized_term=item.normalized_term,
                sentence_text=item.sentence_text,
                occurrence_index=item.occurrence_index,
                hit_start_ms=item.hit_start_ms,
                hit_end_ms=item.hit_end_ms,
                anchor_name=self.attributor.anchor_at(item.hit_start_ms),
                model_version=item.model_version,
                creation_mode=mode,  # type: ignore[arg-type]
                target_hash=target_hash,
            ))
        return self.store.commit_audio_events(
            job.job_key,
            tuple(drafts),
            result_json=result_json,
            model_version=batch.model_version,
            commit_cursor_ms=job.commit_end_ms,
            created_at_ms=created_at_ms,
            expected_job=job,
        )


__all__ = [
    "EventService",
    "EventProvenance",
    "ScheduleAttributor",
    "decode_transcript_batch",
    "derive_event_provenance",
    "event_identity",
    "serialize_transcript_batch",
]
