from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class WordEntry:
    raw: str
    normalized: str
    replacement: str = ""
    note: str = ""


@dataclass(frozen=True)
class WordlistSnapshot:
    version_id: int
    source_hash: str
    entries: tuple[WordEntry, ...]


@dataclass(frozen=True)
class ClosedAudioChunk:
    chunk_key: str
    live_id: str
    path: Path
    capture_start_ms: int
    capture_end_ms: int
    media_duration_ms: int
    continuity: Literal["ok", "invalid"]
    delete_after_use: bool = True


@dataclass(frozen=True)
class AudioOffer:
    offer_id: str
    boundary_id: str
    chunks: tuple[ClosedAudioChunk, ...]
    wordlist_version_id: int
    queued_at: float
    final: bool
    creation_mode: Literal["shadow", "live"]
    target_hash: str
    legacy_quarantine: bool = False
    live_id: str = ""
    boundary_start_ms: int = 0
    boundary_end_ms: int = 0

    def __iter__(self):
        return iter(self.chunks)

    def __len__(self) -> int:
        return len(self.chunks)

    def __getitem__(self, index):
        return self.chunks[index]


@dataclass(frozen=True)
class AudioJob:
    job_key: str
    live_id: str
    context_path: Path
    recognition_origin_ms: int
    commit_start_ms: int
    commit_end_ms: int
    wordlist_version_id: int
    continuity: Literal["ok", "invalid"]
    chain_id: str = ""
    source_kind: Literal["realtime", "main_repair"] = "realtime"


@dataclass(frozen=True)
class RecognizedUnit:
    text: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class RecognizedSentence:
    text: str
    units: tuple[RecognizedUnit, ...]


@dataclass(frozen=True)
class TranscriptBatch:
    model_version: str
    sentences: tuple[RecognizedSentence, ...]


@dataclass(frozen=True)
class Match:
    raw_term: str
    normalized_term: str
    sentence_text: str
    occurrence_index: int
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class ComplianceEventDraft:
    event_key: str
    delivery_key: str
    job_key: str
    live_id: str
    wordlist_version_id: int
    raw_term: str
    normalized_term: str
    sentence_text: str
    occurrence_index: int
    hit_start_ms: int
    hit_end_ms: int
    anchor_name: str
    model_version: str
    creation_mode: Literal["shadow", "live"]
    target_hash: str
