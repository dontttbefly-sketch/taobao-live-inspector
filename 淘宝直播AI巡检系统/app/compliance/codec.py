from __future__ import annotations

import json
from pathlib import Path

from .models import ClosedAudioChunk


_AUDIO_SOURCE_KEYS = frozenset({
    "capture_end_ms",
    "capture_start_ms",
    "chunk_key",
    "continuity",
    "delete_after_use",
    "live_id",
    "media_duration_ms",
    "path",
})


def canonical_json(value: object, *, ensure_ascii: bool = False) -> str:
    if type(ensure_ascii) is not bool:
        raise TypeError("ensure_ascii must be a boolean")
    try:
        return json.dumps(
            value,
            ensure_ascii=ensure_ascii,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (OverflowError, TypeError, ValueError):
        raise ValueError("compliance canonical JSON invalid") from None


def _validate_audio_chunk(chunk: object) -> ClosedAudioChunk:
    if (
        not isinstance(chunk, ClosedAudioChunk)
        or not isinstance(chunk.chunk_key, str)
        or not chunk.chunk_key
        or not isinstance(chunk.live_id, str)
        or not chunk.live_id
        or not isinstance(chunk.path, Path)
        or not chunk.path.is_absolute()
        or type(chunk.capture_start_ms) is not int
        or type(chunk.capture_end_ms) is not int
        or type(chunk.media_duration_ms) is not int
        or type(chunk.delete_after_use) is not bool
        or chunk.capture_start_ms < 0
        or chunk.capture_end_ms <= chunk.capture_start_ms
        or chunk.capture_end_ms - chunk.capture_start_ms
        != chunk.media_duration_ms
        or chunk.continuity not in {"ok", "invalid"}
    ):
        raise ValueError("compliance audio sources invalid")
    return chunk


def encode_audio_sources(chunks: tuple[ClosedAudioChunk, ...]) -> str:
    if type(chunks) is not tuple or not chunks:
        raise ValueError("compliance audio sources invalid")
    validated = tuple(_validate_audio_chunk(chunk) for chunk in chunks)
    return canonical_json([
        {
            "capture_end_ms": chunk.capture_end_ms,
            "capture_start_ms": chunk.capture_start_ms,
            "chunk_key": chunk.chunk_key,
            "continuity": chunk.continuity,
            "delete_after_use": chunk.delete_after_use,
            "live_id": chunk.live_id,
            "media_duration_ms": chunk.media_duration_ms,
            "path": str(chunk.path),
        }
        for chunk in validated
    ])


def _reject_constant(_value: str) -> None:
    raise ValueError


def decode_audio_sources(encoded: str) -> tuple[ClosedAudioChunk, ...]:
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("compliance audio sources invalid")
    try:
        decoded = json.loads(encoded, parse_constant=_reject_constant)
        if type(decoded) is not list or not decoded:
            raise ValueError
        chunks: list[ClosedAudioChunk] = []
        for item in decoded:
            if type(item) is not dict or set(item) != _AUDIO_SOURCE_KEYS:
                raise ValueError
            path = item["path"]
            if not isinstance(path, str) or not path:
                raise ValueError
            chunks.append(_validate_audio_chunk(ClosedAudioChunk(
                chunk_key=item["chunk_key"],
                live_id=item["live_id"],
                path=Path(path),
                capture_start_ms=item["capture_start_ms"],
                capture_end_ms=item["capture_end_ms"],
                media_duration_ms=item["media_duration_ms"],
                continuity=item["continuity"],
                delete_after_use=item["delete_after_use"],
            )))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("compliance audio sources invalid") from None
    result = tuple(chunks)
    if encode_audio_sources(result) != encoded:
        raise ValueError("compliance audio sources are noncanonical")
    return result


__all__ = [
    "canonical_json",
    "decode_audio_sources",
    "encode_audio_sources",
]
