#!/usr/bin/env python3
"""Preflight a warmed 149.5–150.5 second contextual recognition window."""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.compliance.config import ComplianceSettings
from app.compliance.matcher import find_matches
from app.compliance.models import WordEntry
from app.compliance.recognizer import (
    SeacoParaformerRecognizer,
    validate_transcript_batch,
    wav_duration_ms,
)
from app.compliance.wordlist import normalize_term
from app.config import load_config


MIN_CONTEXT_AUDIO_MS = 149_500
MAX_CONTEXT_AUDIO_MS = 150_500


def _result(
    *,
    model_version: str = "",
    audio_duration_ms: int = 0,
    elapsed_ms: int = 0,
    real_time_factor: float = 0.0,
    timestamp_contract_ok: bool = False,
    sentence_count: int = 0,
    match_count: int = 0,
) -> dict[str, object]:
    return {
        "model_version": model_version,
        "audio_duration_ms": audio_duration_ms,
        "elapsed_ms": elapsed_ms,
        "real_time_factor": real_time_factor,
        "timestamp_contract_ok": timestamp_contract_ok,
        "sentence_count": sentence_count,
        "match_count": match_count,
    }


def _read_terms(path: Path) -> tuple[tuple[str, ...], tuple[WordEntry, ...]]:
    raw_terms = tuple(
        line.strip() for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    entries: list[WordEntry] = []
    hotwords: list[str] = []
    seen: set[str] = set()
    for raw in raw_terms:
        normalized = normalize_term(raw)
        if not normalized:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        hotwords.append(raw)
        entries.append(WordEntry(raw=raw, normalized=normalized))
    if not hotwords:
        raise ValueError("terms file has no active terms")
    return tuple(hotwords), tuple(entries)


def _audit_values(recognizer: object) -> tuple[int, str]:
    audit = getattr(recognizer, "generate_audit")
    call_count = getattr(audit, "call_count")
    fingerprint = getattr(audit, "hotword_fingerprint")
    if (
        type(call_count) is not int
        or call_count < 0
        or not isinstance(fingerprint, str)
    ):
        raise ValueError("recognizer generate audit is invalid")
    return call_count, fingerprint


def _verify_generate_call(
    before: tuple[int, str], after: tuple[int, str], expected_fingerprint: str,
) -> None:
    if (
        after[0] != before[0] + 1
        or after[1] != expected_fingerprint
    ):
        raise ValueError("dynamic hotword generate call was not verified")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Warmed contextual compliance recognizer preflight; WAV duration "
            "must be 149.5 to 150.5 seconds"
        ))
    parser.add_argument(
        "--audio", required=True,
        help="149.5 to 150.5 second PCM WAV used for warm and timed calls",
    )
    parser.add_argument("--terms-file", required=True)
    args = parser.parse_args(argv)

    report = _result()
    try:
        audio = Path(args.audio)
        terms_file = Path(args.terms_file)
        if not audio.is_file() or not terms_file.is_file():
            raise ValueError("preflight input is unavailable")
        duration_ms = wav_duration_ms(audio)
        report["audio_duration_ms"] = duration_ms
        if not MIN_CONTEXT_AUDIO_MS <= duration_ms <= MAX_CONTEXT_AUDIO_MS:
            raise ValueError("preflight WAV duration is outside contract")
        hotwords, entries = _read_terms(terms_file)
        expected_fingerprint = hashlib.sha256(
            " ".join(hotwords).encode("utf-8")).hexdigest()
        settings = ComplianceSettings.from_config(load_config())
        with open(os.devnull, "w", encoding="utf-8") as sink:
            with redirect_stdout(sink), redirect_stderr(sink):
                recognizer = SeacoParaformerRecognizer(settings)
                model_version = recognizer.ensure_ready()
                report["model_version"] = model_version

                initial_audit = _audit_values(recognizer)
                warm_batch = recognizer.transcribe(audio, hotwords)
                validate_transcript_batch(warm_batch, duration_ms)
                if warm_batch.model_version != model_version:
                    raise ValueError("recognizer model identity changed")
                warm_audit = _audit_values(recognizer)
                _verify_generate_call(
                    initial_audit, warm_audit, expected_fingerprint)

                started = time.perf_counter()
                batch = recognizer.transcribe(audio, hotwords)
                elapsed = time.perf_counter() - started
                timed_audit = _audit_values(recognizer)
                _verify_generate_call(
                    warm_audit, timed_audit, expected_fingerprint)
        elapsed_ms = round(elapsed * 1000)
        real_time_factor = elapsed * 1000 / duration_ms
        report["elapsed_ms"] = elapsed_ms
        report["real_time_factor"] = round(real_time_factor, 6)

        validate_transcript_batch(batch, duration_ms)
        if batch.model_version != model_version:
            raise ValueError("recognizer model identity changed")
        report["timestamp_contract_ok"] = True
        report["sentence_count"] = len(batch.sentences)
        report["match_count"] = sum(
            len(find_matches(sentence, entries)) for sentence in batch.sentences)
        exit_code = 1 if real_time_factor > 0.8 else 0
    except Exception:
        exit_code = 1
    print(json.dumps(report, ensure_ascii=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
