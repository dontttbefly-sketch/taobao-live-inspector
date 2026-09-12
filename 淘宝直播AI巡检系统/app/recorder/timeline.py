"""Persistent wall-clock evidence for recording parts.

The manifest records only local relative paths and timing/size metadata.  It
never contains stream URLs or credentials.  Formal hourly consumers must use
this manifest instead of compressing every media part onto one synthetic
duration axis.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Sequence


SCHEMA_VERSION = 1
MANIFEST_NAME = "timeline.json"
MAX_FORMAL_MISSING_MS = 1_200_000
_PART_STATES = {"started", "recording", "complete", "failed"}


@dataclass(frozen=True)
class PartTimeline:
    relative_path: str
    first_media_at_ms: int
    last_growth_at_ms: int
    duration_ms: int
    size_bytes: int
    state: str


@dataclass(frozen=True)
class CoverageSlice:
    relative_path: str
    wall_start_ms: int
    wall_end_ms: int
    source_start_ms: int
    source_end_ms: int


@dataclass(frozen=True)
class WindowCoverage:
    window_start_ms: int
    window_end_ms: int
    slices: Sequence[CoverageSlice]
    gaps: Sequence[tuple[int, int]]
    covered_ms: int
    missing_ms: int
    timeline_state: str

    def to_fact(self) -> dict[str, object]:
        """Return the stable subset persisted in hourly business artifacts."""
        duration_ms = max(0, self.window_end_ms - self.window_start_ms)
        ratio = self.covered_ms / duration_ms if duration_ms else 0.0
        return {
            "window_start_ms": self.window_start_ms,
            "window_end_ms": self.window_end_ms,
            "covered_ms": self.covered_ms,
            "missing_ms": self.missing_ms,
            "coverage_ratio": round(ratio, 6),
            "gaps": [
                {"start_ms": start, "end_ms": end}
                for start, end in self.gaps
            ],
            "timeline_state": self.timeline_state,
        }


def _milliseconds(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite non-negative number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field} must be a finite non-negative number")
    return int(round(number))


def _default_clock_ms() -> int:
    return time.time_ns() // 1_000_000


def probe_duration_ms(path: Path) -> int:
    """Read transcribable audio duration without estimating from file mtime."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, timeout=15, check=False,
    )
    if result.returncode != 0:
        raise ValueError("ffprobe could not read recording part duration")
    try:
        payload = json.loads(result.stdout)
        streams = payload["streams"]
        seconds = float(streams[0]["duration"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("ffprobe returned an invalid recording duration") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("recording part duration must be positive")
    return int(round(seconds * 1_000))


class TimelineManifest:
    """Atomic per-session recording timeline.

    Constructing a new recorder creates an empty manifest immediately.  No
    method derives wall-clock coverage from file names, mtimes, or cumulative
    duration, so legacy directories cannot accidentally become formal hours.
    """

    def __init__(
            self, session_dir: Path, *,
            clock_ms: Callable[[], int] = _default_clock_ms,
            probe_duration_ms: Callable[[Path], int] = probe_duration_ms,
    ):
        self.session_dir = Path(session_dir).resolve(strict=False)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.session_dir / MANIFEST_NAME
        self._clock_ms = clock_ms
        self._probe_duration_ms = probe_duration_ms
        self._lock = threading.RLock()
        self._parts: dict[str, dict[str, object]] = {}
        self._created_at_ms = _milliseconds(self._clock_ms(), "clock_ms")
        self._updated_at_ms = self._created_at_ms
        if self.path.exists():
            self._load()
        else:
            self._persist()

    @classmethod
    def open_existing(
            cls, session_dir: Path, *,
            clock_ms: Callable[[], int] = _default_clock_ms,
            probe_duration_ms: Callable[[Path], int] = probe_duration_ms,
    ) -> "TimelineManifest | None":
        """Open a manifest only when real persisted timeline evidence exists."""
        directory = Path(session_dir)
        if not (directory / MANIFEST_NAME).is_file():
            return None
        return cls(
            directory, clock_ms=clock_ms,
            probe_duration_ms=probe_duration_ms)

    @property
    def parts(self) -> tuple[PartTimeline, ...]:
        with self._lock:
            return tuple(self._public_part(item) for item in self._parts.values())

    @property
    def next_part_index(self) -> int:
        indexes: list[int] = []
        for name in self._parts:
            stem = Path(name).stem
            if stem.startswith("part_") and stem[5:].isdigit():
                indexes.append(int(stem[5:]))
        return max(indexes, default=0) + 1

    def _relative_path(self, path: Path) -> str:
        candidate = Path(path).resolve(strict=False)
        try:
            relative = candidate.relative_to(self.session_dir)
        except ValueError as exc:
            raise ValueError("recording part must stay inside session directory") from exc
        pure = PurePosixPath(relative.as_posix())
        if (pure.is_absolute() or not pure.parts
                or any(part in {"", ".", ".."} for part in pure.parts)):
            raise ValueError("recording part must use a safe relative path")
        return pure.as_posix()

    def _now(self) -> int:
        return _milliseconds(self._clock_ms(), "clock_ms")

    def _public_part(self, item: dict[str, object]) -> PartTimeline:
        return PartTimeline(
            relative_path=str(item["relative_path"]),
            first_media_at_ms=int(item.get("first_media_at_ms") or 0),
            last_growth_at_ms=int(item.get("last_growth_at_ms") or 0),
            duration_ms=int(item.get("duration_ms") or 0),
            size_bytes=int(item.get("size_bytes") or 0),
            state=str(item.get("state") or "failed"),
        )

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("recording timeline manifest is unreadable") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported recording timeline schema")
        raw_parts = payload.get("parts")
        if not isinstance(raw_parts, list):
            raise ValueError("recording timeline parts must be a list")
        loaded: dict[str, dict[str, object]] = {}
        previous_started = -1
        for raw in raw_parts:
            item = self._validate_item(raw)
            relative = str(item["relative_path"])
            if relative in loaded:
                raise ValueError("recording timeline contains duplicate parts")
            started = int(item["started_at_ms"])
            if started < previous_started:
                raise ValueError("recording timeline contains a wall-clock regression")
            previous_started = started
            loaded[relative] = item
        self._created_at_ms = _milliseconds(
            payload.get("created_at_ms", 0), "created_at_ms")
        self._updated_at_ms = _milliseconds(
            payload.get("updated_at_ms", self._created_at_ms), "updated_at_ms")
        self._parts = loaded

    def _validate_item(self, raw: object) -> dict[str, object]:
        if not isinstance(raw, dict):
            raise ValueError("recording timeline part must be an object")
        relative = str(raw.get("relative_path") or "")
        pure = PurePosixPath(relative)
        if (not relative or pure.is_absolute()
                or any(part in {"", ".", ".."} for part in pure.parts)):
            raise ValueError("recording timeline contains an unsafe relative path")
        state = str(raw.get("state") or "")
        if state not in _PART_STATES:
            raise ValueError("recording timeline contains an invalid part state")
        started = _milliseconds(raw.get("started_at_ms", 0), "started_at_ms")
        first_raw = raw.get("first_media_at_ms")
        last_raw = raw.get("last_growth_at_ms")
        first = None if first_raw is None else _milliseconds(first_raw, "first_media_at_ms")
        last = None if last_raw is None else _milliseconds(last_raw, "last_growth_at_ms")
        duration = _milliseconds(raw.get("duration_ms", 0), "duration_ms")
        size = _milliseconds(raw.get("size_bytes", 0), "size_bytes")
        if first is not None and first < started:
            raise ValueError("recording timeline contains a wall-clock regression")
        if first is not None and last is not None and last < first:
            raise ValueError("recording timeline contains a wall-clock regression")
        if state == "complete" and (first is None or last is None or duration <= 0 or size <= 0):
            raise ValueError("complete recording part lacks timing evidence")
        return {
            "relative_path": pure.as_posix(),
            "started_at_ms": started,
            "first_media_at_ms": first,
            "last_growth_at_ms": last,
            "duration_ms": duration,
            "size_bytes": size,
            "state": state,
        }

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "created_at_ms": self._created_at_ms,
            "updated_at_ms": self._updated_at_ms,
            "parts": list(self._parts.values()),
        }

    def _persist(self) -> None:
        encoded = json.dumps(
            self._payload(), ensure_ascii=False, allow_nan=False,
            sort_keys=True, separators=(",", ":"),
        )
        descriptor, temporary = tempfile.mkstemp(
            prefix=".timeline-", suffix=".tmp", dir=self.session_dir)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.session_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def mark_started(self, path: Path) -> PartTimeline:
        relative = self._relative_path(path)
        with self._lock:
            existing = self._parts.get(relative)
            if existing is not None:
                return self._public_part(existing)
            now = self._now()
            if self._parts:
                last_started = max(
                    int(item["started_at_ms"]) for item in self._parts.values())
                if now < last_started:
                    raise ValueError("recording timeline wall-clock regression")
            item: dict[str, object] = {
                "relative_path": relative,
                "started_at_ms": now,
                "first_media_at_ms": None,
                "last_growth_at_ms": None,
                "duration_ms": 0,
                "size_bytes": 0,
                "state": "started",
            }
            self._parts[relative] = item
            self._updated_at_ms = now
            self._persist()
            return self._public_part(item)

    def observe_growth(self, path: Path, *, size_bytes: int | None = None) -> bool:
        relative = self._relative_path(path)
        with self._lock:
            if relative not in self._parts:
                raise ValueError("recording part must be marked started before growth")
            item = self._parts[relative]
            size = (Path(path).stat().st_size if size_bytes is None
                    else _milliseconds(size_bytes, "size_bytes"))
            old_size = int(item.get("size_bytes") or 0)
            if size < old_size:
                raise ValueError("recording part size regression")
            if size == old_size:
                return False
            if str(item.get("state")) == "complete":
                raise ValueError("complete recording part cannot grow")
            now = self._now()
            started = int(item["started_at_ms"])
            last_raw = item.get("last_growth_at_ms")
            if now < started or (last_raw is not None and now < int(last_raw)):
                raise ValueError("recording timeline wall-clock regression")
            if item.get("first_media_at_ms") is None:
                item["first_media_at_ms"] = now
            item["last_growth_at_ms"] = now
            item["size_bytes"] = size
            item["state"] = "recording"
            self._updated_at_ms = now
            self._persist()
            return True

    def finalize_part(self, path: Path) -> PartTimeline:
        relative = self._relative_path(path)
        with self._lock:
            if relative not in self._parts:
                raise ValueError("recording part must be marked started before finalizing")
            item = self._parts[relative]
            if str(item.get("state")) == "complete":
                return self._public_part(item)
            try:
                actual_size = Path(path).stat().st_size
            except OSError:
                actual_size = int(item.get("size_bytes") or 0)
            if actual_size > int(item.get("size_bytes") or 0):
                self.observe_growth(path, size_bytes=actual_size)
            duration = 0
            if item.get("first_media_at_ms") is not None and actual_size > 0:
                try:
                    duration = _milliseconds(
                        self._probe_duration_ms(Path(path)), "duration_ms")
                except (OSError, subprocess.SubprocessError, ValueError):
                    duration = 0
            item["duration_ms"] = duration
            item["size_bytes"] = max(actual_size, int(item.get("size_bytes") or 0))
            item["state"] = "complete" if duration > 0 else "failed"
            self._updated_at_ms = self._now()
            self._persist()
            return self._public_part(item)

    def finalize_unfinished_parts(self) -> tuple[PartTimeline, ...]:
        """Close manifest entries left active by a stopped or killed process."""
        with self._lock:
            pending = [
                str(item["relative_path"])
                for item in self._parts.values()
                if str(item.get("state") or "") in {"started", "recording"}
            ]
        return tuple(
            self.finalize_part(self.session_dir / relative)
            for relative in pending
        )

    def window_coverage(
            self, window_start_ms: int, window_end_ms: int) -> WindowCoverage:
        start = _milliseconds(window_start_ms, "window_start_ms")
        end = _milliseconds(window_end_ms, "window_end_ms")
        if end <= start:
            raise ValueError("coverage window must have positive duration")
        with self._lock:
            raw_slices: list[tuple[str, int, int]] = []
            incomplete = False
            any_terminal = False
            for item in self._parts.values():
                state = str(item.get("state") or "")
                started = int(item.get("started_at_ms") or 0)
                if state in {"started", "recording"} and started < end:
                    incomplete = True
                if state in {"complete", "failed"}:
                    any_terminal = True
                if state != "complete":
                    continue
                first_raw = item.get("first_media_at_ms")
                duration = int(item.get("duration_ms") or 0)
                if first_raw is None or duration <= 0:
                    continue
                wall_start = int(first_raw)
                wall_end = wall_start + duration
                if wall_end > start and wall_start < end:
                    raw_slices.append((str(item["relative_path"]), wall_start, wall_end))
            raw_slices.sort(key=lambda value: (value[1], value[2], value[0]))

            slices: list[CoverageSlice] = []
            gaps: list[tuple[int, int]] = []
            cursor = start
            for relative, raw_start, raw_end in raw_slices:
                clipped_start = max(start, raw_start, cursor)
                clipped_end = min(end, raw_end)
                if clipped_end <= clipped_start:
                    continue
                if clipped_start > cursor:
                    gaps.append((cursor, clipped_start))
                source_start = clipped_start - raw_start
                source_end = clipped_end - raw_start
                slices.append(CoverageSlice(
                    relative_path=relative,
                    wall_start_ms=clipped_start,
                    wall_end_ms=clipped_end,
                    source_start_ms=source_start,
                    source_end_ms=source_end,
                ))
                cursor = clipped_end
            if cursor < end:
                gaps.append((cursor, end))
            covered = sum(item.wall_end_ms - item.wall_start_ms for item in slices)
            missing = (end - start) - covered
            state = ("incomplete" if incomplete else
                     "complete" if any_terminal else "empty")
            return WindowCoverage(
                window_start_ms=start,
                window_end_ms=end,
                slices=tuple(slices),
                gaps=tuple(gaps),
                covered_ms=covered,
                missing_ms=missing,
                timeline_state=state,
            )


def formal_brief_allowed(
        coverage: WindowCoverage, *,
        max_missing_ms: int = MAX_FORMAL_MISSING_MS) -> bool:
    threshold = _milliseconds(max_missing_ms, "max_missing_ms")
    return bool(
        coverage.timeline_state == "complete"
        and coverage.window_end_ms > coverage.window_start_ms
        and coverage.covered_ms > 0
        and 0 <= coverage.missing_ms <= threshold
    )


__all__ = [
    "CoverageSlice", "MANIFEST_NAME", "MAX_FORMAL_MISSING_MS",
    "PartTimeline", "TimelineManifest", "WindowCoverage",
    "formal_brief_allowed", "probe_duration_ms",
]
