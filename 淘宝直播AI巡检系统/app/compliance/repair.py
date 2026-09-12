from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path
from pathlib import PurePosixPath
import subprocess
import tempfile
from typing import Callable
import wave

from app.compliance.audio import _read_pcm, _wav_duration_ms, plan_audio_job
from app.compliance.models import AudioJob, ClosedAudioChunk
from app.recorder.timeline import CoverageSlice, TimelineManifest


class RepairPlanningError(RuntimeError):
    pass


def _bound(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"repair interval {name} is invalid")
    return value


@dataclass(frozen=True, order=True)
class Interval:
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        start = _bound(self.start_ms, "start")
        end = _bound(self.end_ms, "end")
        if end <= start:
            raise ValueError("repair interval is empty")


@dataclass(frozen=True)
class RepairWindow:
    live_id: str
    recognition_start_ms: int
    commit_start_ms: int
    commit_end_ms: int
    wordlist_version_id: int
    slices: tuple[CoverageSlice, ...] = ()
    session_dir: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.live_id, str) or not self.live_id:
            raise ValueError("repair live identity is invalid")
        recognition = _bound(self.recognition_start_ms, "recognition start")
        commit_start = _bound(self.commit_start_ms, "commit start")
        commit_end = _bound(self.commit_end_ms, "commit end")
        if not recognition <= commit_start < commit_end:
            raise ValueError("repair window bounds are invalid")
        if (
            isinstance(self.wordlist_version_id, bool)
            or not isinstance(self.wordlist_version_id, int)
            or self.wordlist_version_id <= 0
        ):
            raise ValueError("repair wordlist version is invalid")
        if not isinstance(self.slices, tuple) or any(
            not isinstance(item, CoverageSlice) for item in self.slices
        ):
            raise ValueError("repair coverage slices are invalid")
        if self.session_dir is not None and not isinstance(self.session_dir, Path):
            raise ValueError("repair session directory is invalid")

    @property
    def chain_id(self) -> str:
        material = (
            f"main_repair\0{self.live_id}\0"
            f"{self.commit_start_ms}\0{self.commit_end_ms}"
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


def merge_intervals(
    intervals: tuple[Interval, ...],
) -> tuple[Interval, ...]:
    if not isinstance(intervals, tuple) or any(
        not isinstance(item, Interval) for item in intervals
    ):
        raise ValueError("repair intervals are invalid")
    if not intervals:
        return ()
    ordered = sorted(intervals)
    merged: list[Interval] = [ordered[0]]
    for current in ordered[1:]:
        previous = merged[-1]
        if current.start_ms <= previous.end_ms:
            merged[-1] = Interval(
                previous.start_ms, max(previous.end_ms, current.end_ms)
            )
        else:
            merged.append(current)
    return tuple(merged)


def subtract_intervals(
    authority: tuple[Interval, ...], owned: tuple[Interval, ...],
) -> tuple[Interval, ...]:
    authoritative = merge_intervals(authority)
    exclusions = merge_intervals(owned)
    result: list[Interval] = []
    for source in authoritative:
        cursor = source.start_ms
        for exclusion in exclusions:
            if exclusion.end_ms <= cursor:
                continue
            if exclusion.start_ms >= source.end_ms:
                break
            if exclusion.start_ms > cursor:
                result.append(Interval(
                    cursor, min(exclusion.start_ms, source.end_ms)
                ))
            cursor = max(cursor, exclusion.end_ms)
            if cursor >= source.end_ms:
                break
        if cursor < source.end_ms:
            result.append(Interval(cursor, source.end_ms))
    return tuple(result)


def _authority_intersections(
    gap: Interval, authority: tuple[Interval, ...],
) -> tuple[Interval, ...]:
    intersections: list[Interval] = []
    for source in authority:
        start = max(gap.start_ms, source.start_ms)
        end = min(gap.end_ms, source.end_ms)
        if end > start:
            intersections.append(Interval(start, end))
    return tuple(intersections)


def split_repair_windows(
    *,
    live_id: str,
    gaps: tuple[Interval, ...],
    authority: tuple[Interval, ...],
    wordlist_version_at: Callable[[int], int | None],
    max_commit_ms: int = 120_000,
    context_ms: int = 30_000,
) -> tuple[RepairWindow, ...]:
    if not isinstance(live_id, str) or not live_id:
        raise ValueError("repair live identity is invalid")
    if not callable(wordlist_version_at):
        raise ValueError("repair wordlist resolver is invalid")
    maximum = _bound(max_commit_ms, "maximum commit")
    context = _bound(context_ms, "context")
    if maximum <= 0 or context > 30_000:
        raise ValueError("repair window policy is invalid")
    authoritative = merge_intervals(authority)
    merged_gaps = merge_intervals(gaps)
    windows: list[RepairWindow] = []
    for gap in merged_gaps:
        for covered in _authority_intersections(gap, authoritative):
            cursor = covered.start_ms
            while cursor < covered.end_ms:
                end = min(cursor + maximum, covered.end_ms)
                containing = next(
                    item for item in authoritative
                    if item.start_ms <= cursor < item.end_ms
                )
                recognition_start = max(
                    containing.start_ms, cursor - context
                )
                version = wordlist_version_at(cursor)
                if (
                    isinstance(version, bool)
                    or not isinstance(version, int)
                    or version <= 0
                ):
                    raise RepairPlanningError(
                        "historical_wordlist_unavailable"
                    )
                windows.append(RepairWindow(
                    live_id=live_id,
                    recognition_start_ms=recognition_start,
                    commit_start_ms=cursor,
                    commit_end_ms=end,
                    wordlist_version_id=version,
                ))
                cursor = end
    return tuple(windows)


def _path_has_symlink_component(path: Path) -> bool:
    current = Path(path).absolute()
    while True:
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
        if current.parent == current:
            return False
        current = current.parent


def _is_descendant(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return path != directory


def read_authoritative_coverage(
    session_dir: Path, *, before_ms: int,
) -> tuple[CoverageSlice, ...]:
    """Read immutable main-recorder coverage without creating a manifest.

    Wall-clock coverage comes exclusively from complete timeline parts.  A
    part that crosses the listener-start cutoff contributes only its proven
    prefix, and overlapping parts are monotonically de-duplicated.
    """
    cutoff = _bound(before_ms, "coverage cutoff")
    directory = Path(session_dir)
    timeline_path = directory / "timeline.json"
    try:
        if (
            not directory.is_absolute()
            or _path_has_symlink_component(directory)
            or not directory.is_dir()
            or _path_has_symlink_component(timeline_path)
            or not timeline_path.is_file()
        ):
            raise ValueError
        resolved_directory = directory.resolve(strict=True)
        manifest = TimelineManifest.open_existing(resolved_directory)
        if manifest is None:
            raise ValueError

        slices: list[CoverageSlice] = []
        cursor_ms = 0
        complete_parts = sorted(
            (part for part in manifest.parts if part.state == "complete"),
            key=lambda part: (part.first_media_at_ms, part.relative_path),
        )
        for part in complete_parts:
            part_start = int(part.first_media_at_ms)
            part_end = part_start + int(part.duration_ms)
            if part_end <= 0 or part_start >= cutoff:
                continue
            source = resolved_directory / part.relative_path
            resolved_source = source.resolve(strict=True)
            if (
                _path_has_symlink_component(source)
                or not source.is_file()
                or not _is_descendant(resolved_source, resolved_directory)
                or source.stat().st_size != int(part.size_bytes)
            ):
                raise ValueError
            wall_start = max(part_start, cursor_ms)
            wall_end = min(part_end, cutoff)
            if wall_end <= wall_start:
                continue
            source_start = wall_start - part_start
            source_end = source_start + (wall_end - wall_start)
            slices.append(CoverageSlice(
                relative_path=part.relative_path,
                wall_start_ms=wall_start,
                wall_end_ms=wall_end,
                source_start_ms=source_start,
                source_end_ms=source_end,
            ))
            cursor_ms = wall_end

        if (
            _path_has_symlink_component(timeline_path)
            or timeline_path.resolve(strict=True).parent != resolved_directory
        ):
            raise ValueError
        return tuple(slices)
    except (OSError, TypeError, ValueError):
        raise RepairPlanningError("main_timeline_invalid") from None


def attach_window_coverage(
    window: RepairWindow,
    session_dir: Path,
    coverage: tuple[CoverageSlice, ...],
) -> RepairWindow:
    if not isinstance(window, RepairWindow):
        raise ValueError("repair window is invalid")
    directory = Path(session_dir)
    if not directory.is_absolute():
        raise RepairPlanningError("repair_media_source_invalid")
    if not isinstance(coverage, tuple) or any(
        not isinstance(item, CoverageSlice) for item in coverage
    ):
        raise ValueError("repair coverage is invalid")
    cursor = window.recognition_start_ms
    selected: list[CoverageSlice] = []
    for item in sorted(
        coverage, key=lambda value: (value.wall_start_ms, value.relative_path)
    ):
        values = (
            item.wall_start_ms,
            item.wall_end_ms,
            item.source_start_ms,
            item.source_end_ms,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in values
        ) or (
            item.wall_end_ms <= item.wall_start_ms
            or item.source_end_ms <= item.source_start_ms
            or item.wall_end_ms - item.wall_start_ms
            != item.source_end_ms - item.source_start_ms
        ):
            raise RepairPlanningError("repair_media_source_invalid")
        if item.wall_end_ms <= cursor:
            continue
        if item.wall_start_ms >= window.commit_end_ms:
            break
        if item.wall_start_ms > cursor:
            raise RepairPlanningError("repair_media_coverage_gap")
        wall_start = max(cursor, item.wall_start_ms)
        wall_end = min(window.commit_end_ms, item.wall_end_ms)
        if wall_end <= wall_start:
            continue
        source_start = item.source_start_ms + wall_start - item.wall_start_ms
        selected.append(CoverageSlice(
            relative_path=item.relative_path,
            wall_start_ms=wall_start,
            wall_end_ms=wall_end,
            source_start_ms=source_start,
            source_end_ms=source_start + wall_end - wall_start,
        ))
        cursor = wall_end
        if cursor == window.commit_end_ms:
            break
    if cursor != window.commit_end_ms:
        raise RepairPlanningError("repair_media_coverage_gap")
    return replace(window, slices=tuple(selected), session_dir=directory)


class RepairMediaBuilder:
    def __init__(
        self,
        *,
        ffmpeg: str,
        repair_dir: Path,
        run=subprocess.run,
    ):
        if not isinstance(ffmpeg, str) or not ffmpeg:
            raise ValueError("repair ffmpeg is invalid")
        directory = Path(repair_dir)
        if not directory.is_absolute() or not callable(run):
            raise ValueError("repair media builder is invalid")
        self.ffmpeg = ffmpeg
        self.repair_dir = directory
        self.run = run

    def _private_directory(self) -> Path:
        directory = self.repair_dir
        try:
            if _path_has_symlink_component(directory):
                raise ValueError
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if _path_has_symlink_component(directory) or not directory.is_dir():
                raise ValueError
            directory.chmod(0o700)
            return directory.resolve(strict=True)
        except (OSError, ValueError):
            raise RepairPlanningError(
                "repair_media_directory_invalid"
            ) from None

    @staticmethod
    def _source_path(window: RepairWindow, item: CoverageSlice) -> Path:
        session_dir = window.session_dir
        if session_dir is None:
            raise RepairPlanningError("repair_media_source_invalid")
        relative = PurePosixPath(item.relative_path)
        if (
            not item.relative_path
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise RepairPlanningError("repair_media_source_invalid")
        try:
            directory = Path(session_dir)
            if (
                not directory.is_absolute()
                or _path_has_symlink_component(directory)
                or not directory.is_dir()
            ):
                raise ValueError
            resolved_directory = directory.resolve(strict=True)
            source = directory / Path(relative.as_posix())
            if _path_has_symlink_component(source) or not source.is_file():
                raise ValueError
            resolved_source = source.resolve(strict=True)
            if not _is_descendant(resolved_source, resolved_directory):
                raise ValueError
            return resolved_source
        except (OSError, ValueError):
            raise RepairPlanningError("repair_media_source_invalid") from None

    @staticmethod
    def _chunk_key(window: RepairWindow) -> str:
        material = json.dumps(
            {
                "commit_end_ms": window.commit_end_ms,
                "commit_start_ms": window.commit_start_ms,
                "live_id": window.live_id,
                "recognition_start_ms": window.recognition_start_ms,
                "slices": [
                    {
                        "relative_path": item.relative_path,
                        "source_end_ms": item.source_end_ms,
                        "source_start_ms": item.source_start_ms,
                        "wall_end_ms": item.wall_end_ms,
                        "wall_start_ms": item.wall_start_ms,
                    }
                    for item in window.slices
                ],
                "wordlist_version_id": window.wordlist_version_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(
            ("repair_media\0" + material).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _new_temporary(directory: Path, suffix: str) -> Path:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=".repair-", suffix=suffix, dir=directory,
        )
        os.close(descriptor)
        path = Path(raw_path)
        path.chmod(0o600)
        return path

    def _extract(
        self,
        *,
        source: Path,
        item: CoverageSlice,
        directory: Path,
    ) -> Path:
        temporary = self._new_temporary(directory, ".wav")
        duration_ms = item.source_end_ms - item.source_start_ms
        command = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel", "error",
            "-nostdin",
            "-y",
            "-i", str(source),
            "-ss", f"{item.source_start_ms / 1000:.3f}",
            "-t", f"{duration_ms / 1000:.3f}",
            "-vn",
            "-ac", "1",
            "-ar", "16000",
            "-c:a", "pcm_s16le",
            str(temporary),
        ]
        try:
            completed = self.run(
                command,
                check=False,
                capture_output=True,
                shell=False,
                timeout=max(30.0, duration_ms / 1000 + 30.0),
            )
        except (OSError, subprocess.SubprocessError):
            temporary.unlink(missing_ok=True)
            raise RepairPlanningError("repair_media_extract_failed") from None
        if int(getattr(completed, "returncode", -1)) != 0:
            temporary.unlink(missing_ok=True)
            raise RepairPlanningError("repair_media_extract_failed")
        try:
            temporary.chmod(0o600)
            if _wav_duration_ms(temporary) != duration_ms:
                raise ValueError
        except (OSError, ValueError):
            temporary.unlink(missing_ok=True)
            raise RepairPlanningError("repair_media_duration_invalid") from None
        return temporary

    @staticmethod
    def _write_combined(
        directory: Path,
        extracted: tuple[Path, ...],
        expected_duration_ms: int,
    ) -> Path:
        temporary = RepairMediaBuilder._new_temporary(directory, ".combined.wav")
        try:
            payloads: list[bytes] = []
            frames = 0
            for path in extracted:
                part_frames, payload = _read_pcm(path)
                frames += part_frames
                payloads.append(payload)
            if frames != expected_duration_ms * 16:
                raise ValueError
            with wave.open(str(temporary), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(16_000)
                for payload in payloads:
                    handle.writeframes(payload)
            if _wav_duration_ms(temporary) != expected_duration_ms:
                raise ValueError
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            return temporary
        except (OSError, ValueError, wave.Error):
            temporary.unlink(missing_ok=True)
            raise RepairPlanningError("repair_media_write_failed") from None

    @staticmethod
    def _installed_is_valid(path: Path, expected_duration_ms: int) -> bool:
        try:
            return (
                not _path_has_symlink_component(path)
                and path.is_file()
                and _wav_duration_ms(path) == expected_duration_ms
            )
        except (OSError, ValueError):
            return False

    def build(
        self, window: RepairWindow,
    ) -> tuple[AudioJob, tuple[ClosedAudioChunk, ...]]:
        if (
            not isinstance(window, RepairWindow)
            or not window.slices
            or window.session_dir is None
        ):
            raise RepairPlanningError("repair_media_source_invalid")
        expected_duration_ms = (
            window.commit_end_ms - window.recognition_start_ms
        )
        cursor = window.recognition_start_ms
        for item in window.slices:
            if (
                type(item.wall_start_ms) is not int
                or type(item.wall_end_ms) is not int
                or type(item.source_start_ms) is not int
                or type(item.source_end_ms) is not int
                or item.wall_start_ms != cursor
                or item.wall_end_ms <= item.wall_start_ms
                or item.source_end_ms <= item.source_start_ms
                or item.wall_end_ms - item.wall_start_ms
                != item.source_end_ms - item.source_start_ms
                or item.wall_end_ms > window.commit_end_ms
            ):
                raise RepairPlanningError("repair_media_coverage_gap")
            cursor = item.wall_end_ms
        if cursor != window.commit_end_ms:
            raise RepairPlanningError("repair_media_coverage_gap")
        directory = self._private_directory()
        chunk_key = self._chunk_key(window)
        destination = directory / f"repair_{chunk_key}.wav"
        extracted: list[Path] = []
        combined: Path | None = None
        try:
            if not self._installed_is_valid(destination, expected_duration_ms):
                for item in window.slices:
                    extracted.append(self._extract(
                        source=self._source_path(window, item),
                        item=item,
                        directory=directory,
                    ))
                combined = self._write_combined(
                    directory, tuple(extracted), expected_duration_ms,
                )
                os.replace(combined, destination)
                combined = None
                destination.chmod(0o600)
                directory_descriptor = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
                if not self._installed_is_valid(
                    destination, expected_duration_ms
                ):
                    raise RepairPlanningError("repair_media_write_failed")
            else:
                destination.chmod(0o600)
        except RepairPlanningError:
            raise
        except OSError:
            raise RepairPlanningError("repair_media_write_failed") from None
        finally:
            for path in extracted:
                path.unlink(missing_ok=True)
            if combined is not None:
                combined.unlink(missing_ok=True)

        chunk = ClosedAudioChunk(
            chunk_key=chunk_key,
            live_id=window.live_id,
            path=destination,
            capture_start_ms=window.recognition_start_ms,
            capture_end_ms=window.commit_end_ms,
            media_duration_ms=expected_duration_ms,
            continuity="ok",
            delete_after_use=True,
        )
        job = plan_audio_job(
            None,
            chunk,
            window.commit_start_ms,
            window.wordlist_version_id,
            final=True,
            chain_id=window.chain_id,
            source_kind="main_repair",
        )
        return job, (chunk,)


class MainRecordingRepairService:
    def __init__(
        self,
        store: object,
        media_builder: object,
        *,
        coverage_reader=read_authoritative_coverage,
    ):
        if not callable(coverage_reader) or not callable(
            getattr(media_builder, "build", None)
        ):
            raise ValueError("repair service dependencies are invalid")
        self.store = store
        self.media_builder = media_builder
        self.coverage_reader = coverage_reader

    def _versioned_gap(self, gap: Interval) -> Interval | None:
        resolver = self.store.wordlist_version_at  # type: ignore[attr-defined]
        if resolver(gap.start_ms) is not None:
            return gap
        if resolver(gap.end_ms - 1) is None:
            return None
        low = gap.start_ms
        high = gap.end_ms - 1
        while low < high:
            middle = (low + high) // 2
            if resolver(middle) is None:
                low = middle + 1
            else:
                high = middle
        return Interval(low, gap.end_ms)

    @staticmethod
    def _validate_built_job(
        window: RepairWindow,
        built: object,
    ) -> tuple[AudioJob, tuple[ClosedAudioChunk, ...]]:
        if not isinstance(built, tuple) or len(built) != 2:
            raise RepairPlanningError("repair_job_invalid")
        job, raw_sources = built
        sources = tuple(raw_sources) if isinstance(raw_sources, tuple) else ()
        if (
            not isinstance(job, AudioJob)
            or not sources
            or any(not isinstance(item, ClosedAudioChunk) for item in sources)
            or job.live_id != window.live_id
            or job.recognition_origin_ms != window.recognition_start_ms
            or job.commit_start_ms != window.commit_start_ms
            or job.commit_end_ms != window.commit_end_ms
            or job.wordlist_version_id != window.wordlist_version_id
            or job.chain_id != window.chain_id
            or job.source_kind != "main_repair"
        ):
            raise RepairPlanningError("repair_job_invalid")
        return job, sources

    def scan_and_queue(
        self,
        now: float,
        *,
        listener_started_at_ms: int,
        limit: int = 1,
    ) -> int:
        if (
            isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(float(now))
            or float(now) < 0
            or isinstance(listener_started_at_ms, bool)
            or not isinstance(listener_started_at_ms, int)
            or listener_started_at_ms < 0
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 0
        ):
            raise ValueError("repair scan bounds are invalid")
        if limit == 0 or listener_started_at_ms == 0:
            return 0
        try:
            state = self.store.runtime_state()  # type: ignore[attr-defined]
            if str(state["listener_mode"]) != "shadow":
                return 0
            if self.store.has_unsettled_realtime_recovery():  # type: ignore[attr-defined]
                return 0
            if self.store.has_unsettled_main_repair():  # type: ignore[attr-defined]
                return 0
            sessions = tuple(
                self.store.active_recording_sessions()  # type: ignore[attr-defined]
            )
            if len(sessions) != 1:
                return 0
            live_id, session_dir = sessions[0]
            if not isinstance(live_id, str) or not live_id:
                return 0
            coverage = tuple(self.coverage_reader(
                Path(session_dir), before_ms=listener_started_at_ms,
            ))
            authority = merge_intervals(tuple(
                Interval(item.wall_start_ms, item.wall_end_ms)
                for item in coverage
            ))
            if not authority:
                return 0
            owned = self.store.owned_commit_intervals(  # type: ignore[attr-defined]
                live_id, before_ms=listener_started_at_ms,
            )
            attempted = self.store.repair_exclusion_intervals(  # type: ignore[attr-defined]
                live_id, before_ms=listener_started_at_ms,
            )
            gaps = subtract_intervals(
                authority, merge_intervals(tuple((*owned, *attempted))),
            )
            windows: list[RepairWindow] = []
            for gap in gaps:
                versioned = self._versioned_gap(gap)
                if versioned is None:
                    continue
                windows.extend(split_repair_windows(
                    live_id=live_id,
                    gaps=(versioned,),
                    authority=authority,
                    wordlist_version_at=(
                        self.store.wordlist_version_at  # type: ignore[attr-defined]
                    ),
                ))
            if not windows:
                return 0
            window = attach_window_coverage(
                windows[0], Path(session_dir), coverage,
            )
            built = self.media_builder.build(window)  # type: ignore[attr-defined]
            job, sources = self._validate_built_job(window, built)
            inserted = self.store.queue_audio_job(  # type: ignore[attr-defined]
                job,
                sources,
                created_at=float(now),
                creation_mode="shadow",
                target_hash="",
            )
            return int(inserted is True)
        except (OSError, TypeError, ValueError, RepairPlanningError):
            return 0


__all__ = [
    "Interval",
    "MainRecordingRepairService",
    "RepairMediaBuilder",
    "RepairPlanningError",
    "RepairWindow",
    "attach_window_coverage",
    "merge_intervals",
    "read_authoritative_coverage",
    "split_repair_windows",
    "subtract_intervals",
]
