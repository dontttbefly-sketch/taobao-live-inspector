from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile

from .codec import canonical_json, decode_audio_sources
from .models import AudioOffer, ClosedAudioChunk


def chunk_metadata(chunk: ClosedAudioChunk) -> dict[str, object]:
    return {
        "capture_end_ms": chunk.capture_end_ms,
        "capture_start_ms": chunk.capture_start_ms,
        "chunk_key": chunk.chunk_key,
        "continuity": chunk.continuity,
        "delete_after_use": chunk.delete_after_use,
        "live_id": chunk.live_id,
        "media_duration_ms": chunk.media_duration_ms,
        "path": str(chunk.path),
    }


def offer_metadata(offer: AudioOffer) -> dict[str, object]:
    live_id = offer.live_id or (
        offer.chunks[0].live_id if offer.chunks else ""
    )
    boundary_start_ms = offer.boundary_start_ms or (
        offer.chunks[0].capture_start_ms if offer.chunks else 0
    )
    boundary_end_ms = offer.boundary_end_ms or (
        offer.chunks[-1].capture_end_ms if offer.chunks else 0
    )
    return {
        "boundary_id": offer.boundary_id,
        "boundary_end_ms": boundary_end_ms,
        "boundary_start_ms": boundary_start_ms,
        "chunks": [chunk_metadata(chunk) for chunk in offer.chunks],
        "creation_mode": offer.creation_mode,
        "final": offer.final,
        "legacy_quarantine": offer.legacy_quarantine,
        "live_id": live_id,
        "offer_id": offer.offer_id,
        "queued_at": offer.queued_at,
        "target_hash": offer.target_hash,
        "wordlist_version_id": offer.wordlist_version_id,
    }


def _validate_authority(mode: object, target_hash: object) -> None:
    valid = (
        mode == "shadow" and target_hash == ""
    ) or (
        mode == "live"
        and isinstance(target_hash, str)
        and len(target_hash) == 64
        and all(character in "0123456789abcdef" for character in target_hash)
    )
    if not valid:
        raise ValueError


def _journal_chunks(
    raw_chunks: object, capture_dir: Path,
) -> tuple[ClosedAudioChunk, ...]:
    if type(raw_chunks) is not list:
        raise ValueError
    chunks = decode_audio_sources(canonical_json(raw_chunks)) if raw_chunks else ()
    for chunk in chunks:
        path = chunk.path
        material = (
            f"{chunk.live_id}\0{path.name}\0"
            f"{chunk.capture_start_ms}\0{chunk.media_duration_ms}"
        )
        if (
            chunk.chunk_key
            != hashlib.sha256(material.encode("utf-8")).hexdigest()
            or path.parent != capture_dir
            or not path.name.startswith("segment_")
            or not path.name.endswith(".wav")
            or chunk.delete_after_use is not True
        ):
            raise ValueError
    if len({chunk.chunk_key for chunk in chunks}) != len(chunks):
        raise ValueError
    return chunks


def decode_legacy_journal(
    document: dict[str, object], encoded: str, capture_dir: Path,
) -> tuple[AudioOffer, ...]:
    if (
        set(document) != {"chunks", "version"}
        or document.get("version") != 1
        or not isinstance(document.get("chunks"), list)
        or not document["chunks"]
        or canonical_json(document) != encoded
    ):
        raise ValueError
    chunks = _journal_chunks(document["chunks"], capture_dir)
    material = f"legacy-offer\0{capture_dir}\0{encoded}"
    offer_id = hashlib.sha256(material.encode("utf-8")).hexdigest()
    boundary_id = hashlib.sha256(
        f"legacy-boundary\0{offer_id}".encode("utf-8")
    ).hexdigest()
    return (AudioOffer(
        offer_id=offer_id,
        boundary_id=boundary_id,
        chunks=chunks,
        wordlist_version_id=0,
        queued_at=0.0,
        final=False,
        creation_mode="shadow",
        target_hash="",
        legacy_quarantine=True,
    ),)


def _reject_constant(_value: str) -> None:
    raise ValueError


def decode_observation_journal(
    journal: Path, capture_dir: Path,
) -> tuple[AudioOffer, ...]:
    try:
        encoded = journal.read_text(encoding="utf-8")
        document = json.loads(encoded, parse_constant=_reject_constant)
        if isinstance(document, dict) and document.get("version") == 1:
            return decode_legacy_journal(document, encoded, capture_dir)
        if (
            not isinstance(document, dict)
            or set(document) != {"offers", "version"}
            or document["version"] not in {2, 3}
            or not isinstance(document["offers"], list)
            or canonical_json(document) != encoded
        ):
            raise ValueError
        offers: list[AudioOffer] = []
        legacy_offer_keys = {
            "boundary_id", "chunks", "creation_mode", "final",
            "legacy_quarantine", "offer_id", "queued_at", "target_hash",
            "wordlist_version_id",
        }
        current_offer_keys = legacy_offer_keys | {
            "boundary_end_ms", "boundary_start_ms", "live_id",
        }
        for raw_offer in document["offers"]:
            if (
                not isinstance(raw_offer, dict)
                or set(raw_offer) != (
                    legacy_offer_keys
                    if document["version"] == 2
                    else current_offer_keys
                )
                or not isinstance(raw_offer["chunks"], list)
                or (not raw_offer["chunks"] and document["version"] == 2)
            ):
                raise ValueError
            chunks = _journal_chunks(raw_offer["chunks"], capture_dir)
            live_id = (
                chunks[0].live_id
                if document["version"] == 2
                else raw_offer["live_id"]
            )
            boundary_start_ms = (
                chunks[0].capture_start_ms
                if document["version"] == 2
                else raw_offer["boundary_start_ms"]
            )
            boundary_end_ms = (
                chunks[-1].capture_end_ms
                if document["version"] == 2
                else raw_offer["boundary_end_ms"]
            )
            if (
                (chunks and len({chunk.live_id for chunk in chunks}) != 1)
                or type(raw_offer["wordlist_version_id"]) is not int
                or raw_offer["wordlist_version_id"] <= 0
                or type(raw_offer["queued_at"]) not in {int, float}
                or raw_offer["queued_at"] < 0
                or type(raw_offer["final"]) is not bool
                or raw_offer["legacy_quarantine"] is not False
                or not isinstance(raw_offer["boundary_id"], str)
                or not isinstance(raw_offer["offer_id"], str)
                or not isinstance(live_id, str)
                or not live_id
                or type(boundary_start_ms) is not int
                or type(boundary_end_ms) is not int
                or boundary_start_ms < 0
                or boundary_end_ms < boundary_start_ms
                or (
                    not chunks
                    and (
                        raw_offer["final"] is not True
                        or boundary_end_ms < boundary_start_ms
                    )
                )
                or (
                    chunks
                    and (
                        live_id != chunks[0].live_id
                        or boundary_start_ms != chunks[0].capture_start_ms
                        or boundary_end_ms != chunks[-1].capture_end_ms
                    )
                )
            ):
                raise ValueError
            _validate_authority(
                raw_offer["creation_mode"], raw_offer["target_hash"]
            )
            expected_boundary = hashlib.sha256(
                f"{live_id}\0{boundary_start_ms}\0{boundary_end_ms}".encode(
                    "utf-8"
                )
            ).hexdigest()
            expected_offer = hashlib.sha256(canonical_json({
                key: raw_offer[key] for key in sorted(raw_offer)
                if key != "offer_id"
            }).encode("utf-8")).hexdigest()
            if (
                raw_offer["boundary_id"] != expected_boundary
                or raw_offer["offer_id"] != expected_offer
            ):
                raise ValueError
            offers.append(AudioOffer(
                offer_id=raw_offer["offer_id"],
                boundary_id=raw_offer["boundary_id"],
                chunks=chunks,
                wordlist_version_id=raw_offer["wordlist_version_id"],
                queued_at=float(raw_offer["queued_at"]),
                final=raw_offer["final"],
                creation_mode=raw_offer["creation_mode"],
                target_hash=raw_offer["target_hash"],
                live_id=live_id,
                boundary_start_ms=boundary_start_ms,
                boundary_end_ms=boundary_end_ms,
            ))
        if len({offer.offer_id for offer in offers}) != len(offers):
            raise ValueError
        return tuple(offers)
    except (
        KeyError, OSError, TypeError, ValueError, json.JSONDecodeError,
    ):
        raise ValueError("audio_observation_journal_invalid") from None


def _write_atomic(
    path: Path,
    document: dict[str, object],
    *,
    platform_name: str,
    replace,
    sync_directory,
    error_class: str,
) -> None:
    encoded = canonical_json(document)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        replace(temporary, path, platform_name=platform_name)
        path.chmod(0o600)
        sync_directory(path.parent, platform_name=platform_name)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise RuntimeError(error_class) from None


def write_observation_journal(
    journal: Path,
    offers: tuple[AudioOffer, ...],
    *,
    platform_name: str,
    replace,
    sync_directory,
) -> None:
    _write_atomic(
        journal,
        {"version": 3, "offers": [offer_metadata(offer) for offer in offers]},
        platform_name=platform_name,
        replace=replace,
        sync_directory=sync_directory,
        error_class="audio_observation_journal_failed",
    )


def write_final_intent(
    path: Path,
    document: dict[str, object],
    *,
    platform_name: str,
    replace,
    sync_directory,
) -> None:
    _write_atomic(
        path,
        document,
        platform_name=platform_name,
        replace=replace,
        sync_directory=sync_directory,
        error_class="audio_final_intent_failed",
    )


__all__ = [
    "chunk_metadata",
    "decode_legacy_journal",
    "decode_observation_journal",
    "offer_metadata",
    "write_final_intent",
    "write_observation_journal",
]
