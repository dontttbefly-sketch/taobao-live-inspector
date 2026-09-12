from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from ..asr.transcribe import transcribe_wav
from ..asr.clean import can_show_raw_excerpt
from ..recorder.recorder import extract_audio
from .media import build_hourly_audio
from .models import TranscriptSegment, TranscriptionResult


class FunASRProvider:
    """严格备胎：只返回模型原文，不调用 DeepSeek 语义修复。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def transcribe(self, job: dict) -> TranscriptionResult:
        media_path = Path(str(job.get("media_path") or ""))
        if not media_path.exists():
            parts = [Path(item) for item in json.loads(job.get("media_manifest") or "[]")]
            media_layout = json.loads(job.get("media_layout_json") or "[]")
            media_path = Path(self.cfg.get("paths", {}).get("data", "data")) / "transcription_media" / (
                str(job["job_key"]).replace(":", "_") + ".m4a")
            duration_ms = max(
                1, int(job.get("window_end_ms") or 0)
                - int(job.get("window_start_ms") or 0))
            if media_layout:
                build_hourly_audio(
                    (self.cfg.get("recorder", {}) or {}).get("ffmpeg", "ffmpeg"),
                    [], media_path, media_layout=media_layout,
                    window_duration_ms=duration_ms)
            else:
                build_hourly_audio(
                    (self.cfg.get("recorder", {}) or {}).get("ffmpeg", "ffmpeg"),
                    parts, media_path,
                    trim_start_ms=max(
                        0, int(job.get("window_start_ms") or 0)
                        - int(job.get("media_origin_ms") or 0)),
                    duration_ms=duration_ms,
                )
        wav_path = media_path
        temporary_wav: Path | None = None
        with media_path.open("rb") as handle:
            is_wav = handle.read(4) == b"RIFF"
        try:
            if not is_wav:
                fd, temp_name = tempfile.mkstemp(
                    prefix=f"{media_path.stem}.funasr.", suffix=".wav",
                    dir=media_path.parent,
                )
                os.close(fd)
                temporary_wav = Path(temp_name)
                extract_audio(
                    (self.cfg.get("recorder", {}) or {}).get("ffmpeg", "ffmpeg"),
                    media_path, temporary_wav,
                )
                wav_path = temporary_wav
            rows = transcribe_wav(wav_path, self.cfg)
        finally:
            if temporary_wav is not None:
                temporary_wav.unlink(missing_ok=True)
        segments = [TranscriptSegment(int(s), int(e), str(text).strip(), None, "funasr")
                    for s, e, text in rows if can_show_raw_excerpt(str(text).strip())]
        status = "fallback" if segments else "failed"
        issues = [] if segments else ["FunASR 未产出有效逐字稿"]
        return TranscriptionResult("funasr", segments, quality_status=status,
                                   quality_issues=issues)
