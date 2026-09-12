from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .models import TranscriptSegment


def canonical_media_layout(
        media_layout: list[dict[str, object]] | None, *,
        window_duration_ms: int | None = None) -> list[dict[str, object]]:
    """Validate a contiguous clock-hour layout and return stable JSON data."""
    if not media_layout:
        return []
    if window_duration_ms is None or int(window_duration_ms) <= 0:
        raise ValueError("media layout requires a positive window duration")
    expected = 0
    normalized: list[dict[str, object]] = []
    for raw in media_layout:
        if not isinstance(raw, dict):
            raise ValueError("media layout item must be an object")
        kind = str(raw.get("kind") or "")
        if kind not in {"media", "gap"}:
            raise ValueError("media layout kind must be media or gap")
        try:
            output_start = int(raw.get("output_start_ms"))
            duration = int(raw.get("duration_ms"))
        except (TypeError, ValueError) as exc:
            raise ValueError("media layout timings must be integers") from exc
        if (isinstance(raw.get("output_start_ms"), bool)
                or isinstance(raw.get("duration_ms"), bool)
                or output_start != expected or duration <= 0):
            raise ValueError("media layout must be positive and contiguous")
        item: dict[str, object] = {
            "kind": kind,
            "duration_ms": duration,
            "output_start_ms": output_start,
        }
        if kind == "media":
            path = str(raw.get("path") or "").strip()
            try:
                source_start = int(raw.get("source_start_ms", 0))
            except (TypeError, ValueError) as exc:
                raise ValueError("media source offset must be an integer") from exc
            if (not path or isinstance(raw.get("source_start_ms"), bool)
                    or source_start < 0):
                raise ValueError("media layout requires a path and source offset")
            item["path"] = path
            item["source_start_ms"] = source_start
        normalized.append(item)
        expected += duration
    if expected != int(window_duration_ms):
        raise ValueError("media layout does not fill the requested window")
    return normalized


def filter_segments_to_coverage(
        segments: list[TranscriptSegment], coverage: dict[str, object], *,
        overlap_tolerance_ms: int = 1_000,
) -> tuple[list[TranscriptSegment], int]:
    """Drop timestamp evidence that materially overlaps an explicit gap.

    Timestamps stay unchanged.  In particular, post-gap speech is never moved
    earlier to compress the missing interval.
    """
    if not coverage:
        return list(segments), 0
    try:
        window_start = int(coverage["window_start_ms"])
        raw_gaps = coverage.get("gaps") or []
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("media coverage is invalid") from exc
    if not isinstance(raw_gaps, list):
        raise ValueError("media coverage gaps are invalid")
    relative_gaps: list[tuple[int, int]] = []
    for raw in raw_gaps:
        if not isinstance(raw, dict):
            raise ValueError("media coverage gap is invalid")
        try:
            start = int(raw["start_ms"]) - window_start
            end = int(raw["end_ms"]) - window_start
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("media coverage gap is invalid") from exc
        if end <= start:
            raise ValueError("media coverage gap is invalid")
        relative_gaps.append((start, end))
    tolerance = max(0, int(overlap_tolerance_ms))
    kept: list[TranscriptSegment] = []
    removed = 0
    for segment in segments:
        overlap = max((
            max(0, min(int(segment.end_ms), gap_end)
                - max(int(segment.start_ms), gap_start))
            for gap_start, gap_end in relative_gaps
        ), default=0)
        if overlap > tolerance:
            removed += 1
        else:
            kept.append(segment)
    return kept, removed


def media_manifest_hash(
        parts: list[Path],
        media_layout: list[dict[str, object]] | None = None) -> str:
    digest = hashlib.sha256()
    for path in parts:
        stat = path.stat()
        digest.update(str(path.resolve()).encode())
        digest.update(f":{stat.st_size}:{stat.st_mtime_ns}".encode())
        with path.open("rb") as handle:
            digest.update(handle.read(64 * 1024))
    if media_layout:
        digest.update(json.dumps(
            media_layout, ensure_ascii=False, allow_nan=False,
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8"))
    return digest.hexdigest()


def _run(command: list[str], *, timeout: int = 900) -> None:
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError("小时音频构建失败")


# 整个时间线只编码一次后，AAC-LC 在 16kHz 下的单帧为 64ms。
# 只容忍一个编码帧加容器舍入；更大的差异说明媒体源没有覆盖
# timeline 声称的区间，必须失败，不能再用 apad 把缺音伪装成完整。
MAX_LAYOUT_ENCODE_DRIFT_MS = 80

# Historical manifests measured the MPEG-TS container instead of its audio
# stream.  The resulting per-part tail difference is metadata rounding, not
# evidence that later media happened earlier.  Reconcile only a tightly
# bounded tail; anything larger remains a hard timeline failure.
MAX_RECONCILABLE_SOURCE_SHORTFALL_MS = 1_000
MAX_FORMAL_WINDOW_GAP_MS = 20 * 60 * 1_000


def _ffprobe_for(ffmpeg: str) -> str:
    executable = Path(ffmpeg)
    if executable.name == "ffmpeg":
        candidate = executable.with_name("ffprobe")
        if candidate.exists():
            return str(candidate)
    return "ffprobe"


def _probe_output_duration_ms(ffmpeg: str, output: Path) -> int:
    result = subprocess.run(
        [_ffprobe_for(ffmpeg), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(output)],
        capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError("小时音频时长校验失败")
    try:
        seconds = float(result.stdout.strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError("小时音频时长校验失败") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise RuntimeError("小时音频时长校验失败")
    return int(round(seconds * 1_000))


def _probe_source_audio_duration_ms(ffmpeg: str, source: Path) -> int:
    result = subprocess.run(
        [_ffprobe_for(ffmpeg), "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=duration", "-of", "json", str(source)],
        capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError("小时音频源时长校验失败")
    try:
        payload = json.loads(result.stdout)
        streams = payload["streams"]
        seconds = float(streams[0]["duration"])
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("小时音频源时长校验失败") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise RuntimeError("小时音频源时长校验失败")
    return int(round(seconds * 1_000))


def _merge_gap_ranges(
        ranges: list[tuple[int, int]], *, window_start_ms: int,
        window_end_ms: int) -> list[dict[str, int]]:
    ordered = sorted(ranges)
    merged: list[list[int]] = []
    for start, end in ordered:
        if start < window_start_ms or end > window_end_ms or end <= start:
            raise ValueError("media coverage gap is outside the window")
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [
        {"start_ms": start, "end_ms": end}
        for start, end in merged
    ]


def reconcile_media_layout_to_audio(
        ffmpeg: str, media_layout: list[dict[str, object]],
        media_coverage: dict[str, object], *, window_duration_ms: int,
        duration_probe=None,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Persist small audio-tail differences as explicit wall-clock gaps.

    Later atoms retain their original output offsets.  This function never
    pads an unreported source shortfall or compresses the clock-hour timeline.
    """
    duration = int(window_duration_ms)
    layout = canonical_media_layout(
        media_layout, window_duration_ms=duration)
    if not isinstance(media_coverage, dict):
        raise ValueError("media coverage must be an object")
    try:
        window_start = int(media_coverage["window_start_ms"])
        window_end = int(media_coverage["window_end_ms"])
        raw_gaps = media_coverage.get("gaps") or []
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("media coverage is invalid") from exc
    if window_end - window_start != duration or not isinstance(raw_gaps, list):
        raise ValueError("media coverage window is invalid")
    persisted_gap_ranges: list[tuple[int, int]] = []
    for raw in raw_gaps:
        if not isinstance(raw, dict):
            raise ValueError("media coverage gap is invalid")
        try:
            persisted_gap_ranges.append((
                int(raw["start_ms"]), int(raw["end_ms"])))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("media coverage gap is invalid") from exc
    layout_gap_ranges = [
        (
            window_start + int(item["output_start_ms"]),
            window_start + int(item["output_start_ms"])
            + int(item["duration_ms"]),
        )
        for item in layout if item["kind"] == "gap"
    ]
    if _merge_gap_ranges(
            persisted_gap_ranges, window_start_ms=window_start,
            window_end_ms=window_end) != _merge_gap_ranges(
                layout_gap_ranges, window_start_ms=window_start,
                window_end_ms=window_end):
        raise ValueError("小时音频布局与覆盖缺口不一致")
    gap_ranges = list(layout_gap_ranges)

    probe = duration_probe or _probe_source_audio_duration_ms
    reconciled: list[dict[str, object]] = []
    for item in layout:
        output_start = int(item["output_start_ms"])
        requested = int(item["duration_ms"])
        if item["kind"] == "gap":
            reconciled.append(dict(item))
            continue
        source = Path(str(item["path"]))
        if not source.is_file():
            raise ValueError("media layout source does not exist")
        source_start = int(item["source_start_ms"])
        source_duration = int(probe(ffmpeg, source))
        if source_duration <= 0:
            raise RuntimeError("小时音频源时长校验失败")
        available = max(0, source_duration - source_start)
        shortfall = max(0, requested - available)
        if shortfall > MAX_RECONCILABLE_SOURCE_SHORTFALL_MS:
            raise RuntimeError(
                "小时音频源音轨短缺超过可核验的元数据舍入范围")
        media_duration = requested - shortfall
        if media_duration > 0:
            reconciled.append({
                **item,
                "duration_ms": media_duration,
                "output_start_ms": output_start,
            })
        if shortfall:
            gap_start = output_start + media_duration
            reconciled.append({
                "kind": "gap",
                "duration_ms": shortfall,
                "output_start_ms": gap_start,
            })
            gap_ranges.append((
                window_start + gap_start,
                window_start + output_start + requested,
            ))

    reconciled = canonical_media_layout(
        reconciled, window_duration_ms=duration)
    merged_gaps = _merge_gap_ranges(
        gap_ranges, window_start_ms=window_start, window_end_ms=window_end)
    missing = sum(item["end_ms"] - item["start_ms"] for item in merged_gaps)
    if missing > MAX_FORMAL_WINDOW_GAP_MS:
        raise RuntimeError("小时音频实际缺口超过正式窗口上限")
    covered = duration - missing
    exact_coverage = {
        **media_coverage,
        "window_start_ms": window_start,
        "window_end_ms": window_end,
        "covered_ms": covered,
        "missing_ms": missing,
        "coverage_ratio": round(covered / duration, 6),
        "gaps": merged_gaps,
    }
    return reconciled, exact_coverage


def _build_layout_audio(
        ffmpeg: str, output: Path, layout: list[dict[str, object]],
        window_duration_ms: int) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".hour-media-", dir=output.parent))
    try:
        command = [ffmpeg, "-y", "-v", "error"]
        filters: list[str] = []
        labels: list[str] = []
        input_index = 0
        for index, item in enumerate(layout):
            duration = int(item["duration_ms"])
            duration_sec = duration / 1000
            label = f"layout_{index}"
            if item["kind"] == "gap":
                filters.append(
                    "anullsrc=channel_layout=mono:sample_rate=16000:"
                    f"d={duration_sec:.3f},asetpts=PTS-STARTPTS[{label}]"
                )
            else:
                source = Path(str(item["path"]))
                if not source.is_file():
                    raise ValueError("media layout source does not exist")
                command += ["-i", str(source)]
                source_start_sec = int(item["source_start_ms"]) / 1000
                filters.append(
                    f"[{input_index}:a]aresample=16000,"
                    "aformat=sample_rates=16000:channel_layouts=mono,"
                    "asetpts=PTS-STARTPTS,"
                    f"atrim=start={source_start_sec:.3f}:"
                    f"duration={duration_sec:.3f},"
                    f"asetpts=PTS-STARTPTS[{label}]"
                )
                input_index += 1
            labels.append(f"[{label}]")
        filters.append(
            "".join(labels)
            + f"concat=n={len(labels)}:v=0:a=1[hour_audio]"
        )
        candidate = work / "hour.m4a"
        command += [
            "-filter_complex", ";".join(filters), "-map", "[hour_audio]",
            "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart",
            str(candidate),
        ]
        _run(command)
        if not candidate.is_file() or candidate.stat().st_size <= 0:
            raise RuntimeError("小时音频构建失败")
        expected_ms = int(window_duration_ms)
        measured_ms = _probe_output_duration_ms(ffmpeg, candidate)
        drift_ms = int(round(measured_ms - expected_ms))
        if abs(drift_ms) > MAX_LAYOUT_ENCODE_DRIFT_MS:
            raise RuntimeError(
                f"小时音频时长与墙钟窗口不一致：实测 {measured_ms}ms，"
                f"窗口 {expected_ms}ms，偏差 {drift_ms:+d}ms")
        os.replace(candidate, output)
        return output
    finally:
        shutil.rmtree(work, ignore_errors=True)


def build_hourly_audio(
        ffmpeg: str, parts: list[Path], output: Path, *,
        trim_start_ms: int = 0, duration_ms: int | None = None,
        media_layout: list[dict[str, object]] | None = None,
        window_duration_ms: int | None = None) -> Path:
    """按持久化时间线一次编码 AAC；旧调用则合并稳定分片。"""
    if media_layout:
        layout = canonical_media_layout(
            media_layout, window_duration_ms=window_duration_ms)
        return _build_layout_audio(
            ffmpeg, output, layout, int(window_duration_ms or 0))
    if not parts:
        raise ValueError("音频窗口没有媒体分片")
    output.parent.mkdir(parents=True, exist_ok=True)
    concat = output.with_suffix(".ffconcat")
    lines = ["ffconcat version 1.0"]
    for path in parts:
        escaped = str(path.resolve()).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    concat.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp = output.with_name(output.stem + ".tmp" + output.suffix)
    try:
        command = [
            ffmpeg, "-y", "-v", "error", "-f", "concat", "-safe", "0",
            "-i", str(concat),
        ]
        if int(trim_start_ms or 0) > 0:
            command += ["-ss", f"{int(trim_start_ms) / 1000:.3f}"]
        if duration_ms is not None and int(duration_ms) > 0:
            command += ["-t", f"{int(duration_ms) / 1000:.3f}"]
        command += [
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "aac",
            "-b:a", "64k", "-movflags", "+faststart", str(tmp),
        ]
        proc = subprocess.run(
            command,
            capture_output=True, text=True, timeout=900,
        )
        if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            raise RuntimeError("小时音频构建失败")
        tmp.replace(output)
        return output
    finally:
        concat.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)
