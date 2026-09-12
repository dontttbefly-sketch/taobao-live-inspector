from __future__ import annotations

import re
import json
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..asr.clean import numbers_equivalent
from ..lark_cli import LarkCliError, find_value, run_lark_cli
from ..transcript_segments import split_timed_text
from .media import build_hourly_audio
from .models import SmartChapter, SmartMinutesArtifact, TranscriptSegment


_STAMP_RE = re.compile(
    r"^(?P<speaker>.+?)\s+(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})(?:\.(?P<ms>\d{1,3}))?\s*$"
)
_HAS_NUMBER_RE = re.compile(r"[0-9零一二两三四五六七八九十百千万亿]")


def _stamp_ms(match: re.Match[str]) -> int:
    fraction = (match.group("ms") or "0").ljust(3, "0")[:3]
    return ((int(match.group("h")) * 3600 + int(match.group("m")) * 60
             + int(match.group("s"))) * 1000 + int(fraction))


def _split_complete_turn(start_ms: int, end_ms: int, text: str,
                         speaker: str) -> list[TranscriptSegment]:
    """把妙记的说话人长段拆成完整句，并按字符比例分配时间。

    妙记的时间戳是说话人轮次级，不是句子级。只在明确的句末标点
    处切分，不改字、不补写原话；这样下游的峰值关联、话术精选和
    建议证据共用同一套句子粒度。
    """
    return [
        TranscriptSegment(start, end, body, speaker or None, "feishu_minutes")
        for start, end, body in split_timed_text(start_ms, end_ms, text)
    ]


def parse_minutes_transcript(raw: str, *, duration_ms: int = 0) -> list[TranscriptSegment]:
    """解析妙记导出的逐字稿文本，不依赖界面语言或关键词段。"""
    pending: list[tuple[int, str, str]] = []
    speaker = ""
    start_ms: int | None = None
    body: list[str] = []

    def flush() -> None:
        nonlocal body
        text = " ".join(line.strip() for line in body if line.strip()).strip()
        if start_ms is not None and text:
            pending.append((start_ms, speaker, text))
        body = []

    for line in str(raw or "").splitlines():
        match = _STAMP_RE.match(line.strip())
        if match:
            flush()
            start_ms = _stamp_ms(match)
            speaker = match.group("speaker").strip()
        elif start_ms is not None:
            body.append(line)
    flush()

    segments: list[TranscriptSegment] = []
    for index, (start, who, text) in enumerate(pending):
        next_start = pending[index + 1][0] if index + 1 < len(pending) else 0
        end = next_start if next_start > start else max(start + 1, int(duration_ms or 0))
        segments.extend(_split_complete_turn(start, end, text, who))
    return segments


def chapter_deep_link(minute_url: str, start_ms: int) -> str:
    parts = urlsplit(str(minute_url or ""))
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["t"] = str(max(0, int(start_ms)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def note_doc_link(minute_url: str, note_doc_token: str) -> str:
    """用妙记所属租户域名构造智能会议纪要文档链接。"""
    parts = urlsplit(str(minute_url or ""))
    token = str(note_doc_token or "").strip()
    if not parts.scheme or not parts.netloc or not token:
        return ""
    return urlunsplit((parts.scheme, parts.netloc, f"/docx/{token}", "", ""))


def _sentences(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"(?<=[。！？!?；;])|\n+", text or "")
            if item.strip()]


def _grounded(text: str, segments: list[TranscriptSegment]) -> bool:
    if not _HAS_NUMBER_RE.search(text or ""):
        return True
    if any(numbers_equivalent(segment.text, text) for segment in segments):
        return True

    # 妙记说话人轮次拆句后，一条智能纪要可能合并相邻两句
    # 的数字（例如“1米伸缩线＋45瓦快充”）。只允许联合连续的同一
    # 说话人句子，不跨说话人、不跨时间空档，仍要求数值完全等价。
    for start in range(len(segments)):
        combined = ""
        speaker = segments[start].speaker
        previous_end = segments[start].start_ms
        for segment in segments[start:start + 4]:
            if segment.speaker != speaker:
                break
            if combined and int(segment.start_ms) - int(previous_end) > 1_500:
                break
            if len(combined) + len(segment.text) > 1_000:
                break
            combined += segment.text
            previous_end = segment.end_ms
            if numbers_equivalent(combined, text):
                return True
    return False


def sanitize_smart_artifact(artifact: SmartMinutesArtifact,
                            segments: list[TranscriptSegment]) -> SmartMinutesArtifact:
    """过滤智能纪要里无法由逐字稿数值等价核验的数字断言。"""
    summary = "".join(item for item in _sentences(artifact.summary)
                      if _grounded(item, segments))
    chapters = [
        chapter for chapter in artifact.chapters
        if _grounded(f"{chapter.title}。{chapter.summary}", segments)
    ]
    quotes = [quote for quote in artifact.golden_quotes if _grounded(quote, segments)]
    return replace(artifact, summary=summary, chapters=chapters, golden_quotes=quotes)


def _chapter_rows(value) -> list[SmartChapter]:
    found: list[SmartChapter] = []
    if isinstance(value, dict):
        start = value.get("start_ms", value.get("start_time"))
        title = value.get("title") or value.get("chapter_title")
        if start is not None and title:
            found.append(SmartChapter(
                int(start or 0), int(value.get("stop_ms") or value.get("end_ms") or start or 0),
                str(title), str(value.get("summary_content") or value.get("summary") or ""),
            ))
        else:
            for child in value.values():
                found.extend(_chapter_rows(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_chapter_rows(child))
    unique = {(item.start_ms, item.title): item for item in found}
    return sorted(unique.values(), key=lambda item: item.start_ms)


def _summary_text(payload: dict) -> str:
    value = find_value(payload, ("summary_content", "summary"))
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("content") or value.get("text") or "").strip()
    return ""


def _artifact_field_available(value, keys: tuple[str, ...], *,
                              excluded_ancestors: tuple[str, ...] = ()) -> bool:
    """字段明确返回即算产物已生成；空字符串/空列表也是合法终态。"""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys and item is not None:
                return True
            if key not in excluded_ancestors and _artifact_field_available(
                    item, keys, excluded_ancestors=excluded_ancestors):
                return True
        return False
    if isinstance(value, list):
        return any(_artifact_field_available(
            item, keys, excluded_ancestors=excluded_ancestors) for item in value)
    return False


def _golden_quotes_from_doc(payload: dict) -> list[str]:
    raw = find_value(payload, ("content", "markdown", "text"))
    if not isinstance(raw, str):
        return []
    text = re.sub(r"<[^>]+>", "\n", raw)
    match = re.search(r"金句时刻\s*\n+(.*?)(?=\n\s*#{1,6}\s|\Z)", text, re.S)
    if not match:
        return []
    quotes = []
    for line in match.group(1).splitlines():
        line = re.sub(r"^[\s>*\-\d.、]+", "", line).strip()
        if line and line not in quotes:
            quotes.append(line)
    return quotes[:5]


class FeishuMinutesProvider:
    """飞书妙记 Provider；每次 ``advance`` 最多执行一个远端步骤。"""

    def __init__(self, cfg: dict, *, project_root: Path | None = None, runner=None):
        self.cfg = cfg
        self.project_root = (project_root or Path(__file__).resolve().parents[2]).resolve()
        self.runner = runner or run_lark_cli

    def _run(self, args: list[str], timeout: int = 120, *, cwd: Path | None = None) -> dict:
        return self.runner(args, cwd=cwd or self.project_root, timeout=timeout)

    def _relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.project_root).as_posix()
        except ValueError as exc:
            raise RuntimeError("妙记媒体必须位于项目目录内") from exc

    def advance(self, job: dict, now: float) -> dict:
        try:
            return self._advance(job, now)
        except LarkCliError as exc:
            if not exc.retryable:
                return {"status": "blocked", "error_class": exc.error_class, "error": str(exc)}
            if str(job.get("remote_status") or job.get("status")) in {
                    "processing", "fallback_ready"}:
                return {
                    "status": "processing", "next_poll_at": now + 30,
                    "error_class": exc.error_class, "error": str(exc),
                }
            raise

    def _advance(self, job: dict, now: float) -> dict:
        status = str(job.get("remote_status") or job.get("status") or "queued")
        if status == "queued":
            parts = [Path(item) for item in json.loads(job.get("media_manifest") or "[]")]
            media_layout = json.loads(job.get("media_layout_json") or "[]")
            output = self.project_root / "data" / "transcription_media" / (
                str(job["job_key"]).replace(":", "_") + ".m4a")
            duration_ms = max(
                1, int(job.get("window_end_ms") or 0)
                - int(job.get("window_start_ms") or 0))
            if media_layout:
                build_hourly_audio(
                    (self.cfg.get("recorder", {}) or {}).get("ffmpeg", "ffmpeg"),
                    [], output, media_layout=media_layout,
                    window_duration_ms=duration_ms)
            else:
                build_hourly_audio(
                    (self.cfg.get("recorder", {}) or {}).get("ffmpeg", "ffmpeg"),
                    parts, output,
                    trim_start_ms=max(
                        0, int(job.get("window_start_ms") or 0)
                        - int(job.get("media_origin_ms") or 0)),
                    duration_ms=duration_ms,
                )
            return {"status": "media_ready", "media_path": str(output), "next_poll_at": now}

        if status == "media_ready":
            media = Path(str(job.get("media_path") or "")).resolve()
            self._relative(media)
            payload = self._run([
                "drive", "+upload", "--file", f"./{media.name}",
                "--as", "user", "--format", "json",
            ], timeout=900, cwd=media.parent)
            token = str(find_value(payload, ("file_token", "token")) or "")
            if not token:
                raise LarkCliError("上传成功但未返回 file_token", error_class="protocol")
            return {"status": "uploaded", "drive_file_token": token, "next_poll_at": now}

        if status == "uploaded":
            payload = self._run([
                "minutes", "+upload", "--file-token", str(job.get("drive_file_token") or ""),
                "--as", "user", "--format", "json",
            ], timeout=180)
            minute_token = str(find_value(payload, ("minute_token", "token")) or "")
            minute_url = str(find_value(payload, ("minute_url", "url")) or "")
            if not minute_token:
                raise LarkCliError("妙记创建成功但未返回 minute_token", error_class="protocol")
            return {
                "status": "processing", "minute_token": minute_token,
                "minute_url": minute_url, "next_poll_at": now + 10,
            }

        if status in {"processing", "fallback_ready"}:
            token = str(job.get("minute_token") or "")
            output_dir = self.project_root / "data" / "minutes" / token
            output_dir.mkdir(parents=True, exist_ok=True)
            payload = self._run([
                "minutes", "+detail", "--minute-tokens", token,
                "--summary", "--chapter", "--keyword", "--transcript",
                "--output-dir", self._relative(output_dir), "--overwrite",
                "--as", "user", "--format", "json",
            ], timeout=180)
            transcript_path = find_value(payload, ("transcript_path", "output_path", "file_path"))
            candidates: list[Path] = []
            if transcript_path:
                candidate = Path(str(transcript_path))
                candidates.append(candidate if candidate.is_absolute() else self.project_root / candidate)
            candidates.extend(output_dir.glob("*.txt"))
            candidates.extend(output_dir.rglob("*transcript*.txt"))
            transcript = next((path for path in candidates if path.is_file()), None)
            if transcript is None:
                return {"status": "processing", "next_poll_at": now + 30}
            raw = transcript.read_text(encoding="utf-8", errors="replace")
            duration = max(1, int(job.get("window_end_ms") or 0)
                           - int(job.get("window_start_ms") or 0))
            segments = parse_minutes_transcript(raw, duration_ms=duration)
            if not segments:
                return {"status": "processing", "next_poll_at": now + 30}
            minute_url = str(job.get("minute_url") or find_value(payload, ("minute_url", "url")) or "")
            note_id = str(find_value(payload, ("note_id",)) or job.get("note_id") or "")
            note_doc_token = str(job.get("note_doc_token") or "")
            quotes: list[str] = []
            if note_id:
                try:
                    note = self._run([
                        "note", "+detail", "--note-id", note_id,
                        "--as", "user", "--format", "json",
                    ])
                    note_doc_token = str(find_value(
                        note, ("note_doc_token", "doc_token", "document_token")) or "")
                    if note_doc_token:
                        doc = self._run([
                            "docs", "+fetch", "--doc", note_doc_token,
                            "--scope", "keyword", "--keyword", "金句时刻",
                            "--context-after", "8", "--doc-format", "markdown",
                            "--as", "user", "--format", "json",
                        ])
                        quotes = _golden_quotes_from_doc(doc)
                except LarkCliError:
                    # 智能纪要附加产物失败不阻止逐字稿按时交付；后续轮询可再补。
                    pass
            raw_summary = _summary_text(payload)
            raw_chapters = _chapter_rows(payload)
            artifact = sanitize_smart_artifact(SmartMinutesArtifact(
                minute_token=token, minute_url=minute_url,
                summary=raw_summary, chapters=raw_chapters,
                golden_quotes=quotes, note_id=note_id, note_doc_token=note_doc_token,
            ), segments)
            from .models import TranscriptionResult
            fields_returned = (
                _artifact_field_available(
                    payload, ("summary", "summary_content"),
                    excluded_ancestors=("chapter", "chapters"))
                and _artifact_field_available(payload, ("chapter", "chapters"))
            )
            grace_polls = max(6, int(
                (self.cfg.get("transcription", {}) or {}).get(
                    "smart_empty_grace_polls", 20)))
            # 真实接口会先返回空摘要/空章节，随后才生成智能纪要文档。
            # 只有文档与实质产物齐备才提前完成；稳定空返回超过宽限轮数
            # 才终止，避免确实无内容的任务永久 processing。
            substantive_smart = bool(
                note_doc_token and (raw_summary or raw_chapters or quotes))
            empty_terminal = bool(
                fields_returned and int(job.get("attempts") or 0) >= grace_polls)
            smart_complete = substantive_smart or empty_terminal
            quality = "complete" if smart_complete else "transcript_ready"
            return {
                "status": "ready" if smart_complete else "processing",
                "next_poll_at": now + 30,
                "note_id": note_id, "note_doc_token": note_doc_token,
                "result": TranscriptionResult("feishu_minutes", segments, artifact, quality),
            }
        raise RuntimeError(f"未知妙记远端状态: {status}")

    def cleanup_source(self, drive_file_token: str) -> None:
        if not drive_file_token:
            return
        self._run([
            "drive", "+delete", "--file-token", drive_file_token, "--type", "file",
            "--yes", "--as", "user", "--format", "json",
        ])
