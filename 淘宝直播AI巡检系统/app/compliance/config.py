from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..config import resolve


@dataclass(frozen=True)
class ComplianceSettings:
    mode: Literal["disabled", "shadow", "live"]
    db_path: Path
    audio_dir: Path
    ffmpeg: str
    wordlist_spreadsheet_token: str
    wordlist_sheet_name: str
    wordlist_range: str
    wordlist_sync_seconds: int
    segment_seconds: int
    overlap_seconds: int
    holdback_seconds: int
    wallclock_tolerance_ms: int
    poll_seconds: float
    recognizer_model: str
    recognizer_revision: str
    vad_model: str
    punc_model: str
    recognizer_device: str
    recipient_chat_id: str

    @classmethod
    def from_config(cls, cfg: dict) -> "ComplianceSettings":
        raw = cfg.get("compliance") or {}
        mode = str(raw.get("mode") or "disabled").strip().lower()
        if mode not in {"disabled", "shadow", "live"}:
            raise ValueError("compliance.mode must be disabled, shadow, or live")
        wordlist = raw.get("wordlist") or {}
        audio = raw.get("audio") or {}
        recognizer = raw.get("recognizer") or {}
        delivery = raw.get("delivery") or {}
        token = str(wordlist.get("spreadsheet_token") or "").strip()
        recipient = str(delivery.get("recipient_chat_id") or "").strip()
        if mode != "disabled" and not token:
            raise ValueError("compliance wordlist spreadsheet token is required")
        if mode == "live" and not recipient:
            raise ValueError("compliance delivery recipient is required")
        overlap = int(audio.get("overlap_seconds", 30))
        segment = int(audio.get("segment_seconds", 120))
        holdback = int(audio.get("holdback_seconds", 15))
        sync_seconds = int(wordlist.get("sync_seconds", 300))
        wallclock_tolerance_ms = int(audio.get("wallclock_tolerance_ms", 3000))
        poll_seconds = float(raw.get("poll_seconds", 1))
        if (segment, overlap, holdback) != (120, 30, 15):
            raise ValueError(
                "compliance audio timing must be fixed at 120/30/15 seconds"
            )
        if sync_seconds != 300:
            raise ValueError("compliance wordlist sync interval must be 300 seconds")
        if wallclock_tolerance_ms <= 0 or not 0 < poll_seconds <= 5:
            raise ValueError(
                "compliance polling and wallclock tolerance must be positive"
            )
        model = str(recognizer.get("model") or "paraformer-zh").strip()
        if mode != "disabled" and model != "paraformer-zh":
            raise ValueError("compliance recognizer must be paraformer-zh")
        recorder = cfg.get("recorder") or {}
        ffmpeg = str(audio.get("ffmpeg") or recorder.get("ffmpeg") or "ffmpeg")
        return cls(
            mode=mode,
            db_path=resolve(str((cfg.get("paths") or {}).get(
                "db") or "data/inspection.db")),
            audio_dir=resolve(str(audio.get("out_dir") or "data/compliance/audio")),
            ffmpeg=ffmpeg,
            wordlist_spreadsheet_token=token,
            wordlist_sheet_name=str(
                wordlist.get("sheet_name") or "机器识别词库").strip(),
            wordlist_range=str(wordlist.get("range") or "A1:D5000").strip(),
            wordlist_sync_seconds=sync_seconds,
            segment_seconds=segment,
            overlap_seconds=overlap,
            holdback_seconds=holdback,
            wallclock_tolerance_ms=wallclock_tolerance_ms,
            poll_seconds=poll_seconds,
            recognizer_model=model,
            recognizer_revision=str(
                recognizer.get("revision") or "v2.0.4").strip(),
            vad_model=str(recognizer.get("vad_model") or "fsmn-vad").strip(),
            punc_model=str(recognizer.get("punc_model") or "ct-punc").strip(),
            recognizer_device=str(recognizer.get("device") or "cpu").strip(),
            recipient_chat_id=recipient,
        )
