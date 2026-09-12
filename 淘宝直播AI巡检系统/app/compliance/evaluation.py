from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from typing import Callable

from .models import AudioOffer, ClosedAudioChunk, WordEntry, WordlistSnapshot
from .wordlist import normalize_term


@dataclass(frozen=True)
class Label:
    sample_id: str
    term: str
    hit_ms: int


@dataclass(frozen=True)
class Prediction:
    sample_id: str
    term: str
    hit_ms: int
    ready_at_ms: int


@dataclass(frozen=True)
class ReplaySample:
    sample_id: str
    audio_path: Path
    origin_ms: int
    duration_ms: int
    live_id: str


@dataclass(frozen=True)
class ResourceSummary:
    sample_count: int = 0
    max_listener_cpu_percent: float = 0.0
    max_listener_rss_bytes: int = 0
    max_child_cpu_percent: float = 0.0
    max_child_rss_bytes: int = 0
    disk_read_bytes: int = 0
    disk_write_bytes: int = 0
    max_queued_job_age_seconds: float = 0.0
    sampling_complete: bool = True

    def valid(self) -> bool:
        floats = (
            self.max_listener_cpu_percent,
            self.max_child_cpu_percent,
            self.max_queued_job_age_seconds,
        )
        integers = (
            self.sample_count,
            self.max_listener_rss_bytes,
            self.max_child_rss_bytes,
            self.disk_read_bytes,
            self.disk_write_bytes,
        )
        return (
            type(self.sampling_complete) is bool
            and self.sampling_complete
            and all(type(value) is int and value >= 0 for value in integers)
            and self.sample_count > 0
            and all(
                not isinstance(value, bool)
                and isinstance(value, (int, float))
                and math.isfinite(float(value))
                and float(value) >= 0
                for value in floats
            )
        )


@dataclass(frozen=True)
class ResourceEvidence:
    window_start_ms: int
    window_end_ms: int
    baseline_main_gap_ms: int
    observed_main_gap_ms: int
    baseline_job_completions: int
    observed_job_completions: int
    observed_backlog_threshold_tripped: bool
    full_day_observed: bool = False
    observed_sample_count: int = 0
    observed_audio_ms: int = 0
    replay_job_completions: int = 0

    def _valid(self) -> bool:
        integer_values = (
            self.window_start_ms,
            self.window_end_ms,
            self.baseline_main_gap_ms,
            self.observed_main_gap_ms,
            self.baseline_job_completions,
            self.observed_job_completions,
            self.observed_sample_count,
            self.observed_audio_ms,
            self.replay_job_completions,
        )
        return (
            all(type(value) is int and value >= 0 for value in integer_values)
            and self.window_end_ms > self.window_start_ms
            and type(self.observed_backlog_threshold_tripped) is bool
            and type(self.full_day_observed) is bool
        )

    def matches_run(
        self,
        *,
        processed_sample_count: int,
        processed_audio_ms: int,
        processed_job_completions: int,
    ) -> bool:
        return (
            self._valid()
            and type(processed_sample_count) is int
            and type(processed_audio_ms) is int
            and type(processed_job_completions) is int
            and processed_sample_count >= 0
            and processed_audio_ms >= 0
            and processed_job_completions >= 0
            and self.observed_sample_count == processed_sample_count
            and self.observed_audio_ms == processed_audio_ms
            and self.replay_job_completions == processed_job_completions
        )

    def full_day_covered(
        self,
        samples: tuple[ReplaySample, ...],
        *,
        processed_sample_count: int,
        processed_audio_ms: int,
        processed_job_completions: int,
    ) -> bool:
        if not self._valid() or not self.full_day_observed or not samples:
            return False
        audio_ms = sum(sample.duration_ms for sample in samples)
        return (
            self.matches_run(
                processed_sample_count=processed_sample_count,
                processed_audio_ms=processed_audio_ms,
                processed_job_completions=processed_job_completions,
            )
            and processed_sample_count == len(samples)
            and processed_audio_ms == audio_ms
            and self.window_start_ms
            <= min(sample.origin_ms for sample in samples)
            and self.window_end_ms
            >= max(sample.origin_ms + sample.duration_ms for sample in samples)
            and self.window_end_ms - self.window_start_ms >= audio_ms
        )

    def resource_regression(self) -> bool:
        if not self._valid():
            return True
        return (
            self.observed_main_gap_ms > self.baseline_main_gap_ms
            or self.observed_job_completions
            < self.baseline_job_completions
            or self.observed_backlog_threshold_tripped
        )


@dataclass(frozen=True, order=True)
class PerTermCounts:
    normalized_term: str
    expected: int
    predicted: int
    true_positive: int
    false_positive: int
    false_negative: int

    @property
    def recall(self) -> float:
        return (
            self.true_positive / self.expected if self.expected else 0.0
        )

    @property
    def precision(self) -> float:
        return (
            self.true_positive / self.predicted if self.predicted else 0.0
        )


@dataclass(frozen=True)
class EvaluationReport:
    true_positive: int
    false_positive: int
    false_negative: int
    recall: float
    precision: float
    per_term: tuple[PerTermCounts, ...]
    p95_ready_latency_ms: float
    zero_hit_terms: tuple[str, ...]
    duplicate_events: int
    real_messages_sent: int = 0
    full_day: bool = False
    resource_regression: bool | None = None
    resource_observations: ResourceSummary | None = None

    def passes_shadow_gate(self) -> bool:
        return (
            math.isfinite(self.recall)
            and math.isfinite(self.precision)
            and math.isfinite(self.p95_ready_latency_ms)
            and self.recall >= 0.90
            and self.precision >= 0.95
            and self.p95_ready_latency_ms <= 285_000
            and not self.zero_hit_terms
            and self.duplicate_events == 0
            and self.real_messages_sent == 0
            and self.full_day is True
            and self.resource_regression is False
            and self.resource_observations is not None
            and self.resource_observations.valid()
            and self.resource_observations.max_queued_job_age_seconds <= 300.0
        )


def _strict_timestamp(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


def _identity(sample_id: object, term: object) -> tuple[str, str]:
    if not isinstance(sample_id, str) or not sample_id.strip():
        raise ValueError("sample_id must be nonempty")
    if not isinstance(term, str):
        raise ValueError("term must be text")
    normalized = normalize_term(term)
    if not normalized:
        raise ValueError("term must normalize to nonempty text")
    return sample_id, normalized


def evaluate(
    labels: tuple[Label, ...] | list[Label],
    predictions: tuple[Prediction, ...] | list[Prediction],
    *,
    tolerance_ms: int = 2_000,
) -> EvaluationReport:
    tolerance = _strict_timestamp(tolerance_ms, "tolerance_ms")
    grouped_labels: dict[tuple[str, str], list[Label]] = {}
    grouped_predictions: dict[tuple[str, str], list[Prediction]] = {}
    for label in labels:
        if not isinstance(label, Label):
            raise TypeError("labels must contain Label values")
        key = _identity(label.sample_id, label.term)
        _strict_timestamp(label.hit_ms, "label.hit_ms")
        grouped_labels.setdefault(key, []).append(label)
    for prediction in predictions:
        if not isinstance(prediction, Prediction):
            raise TypeError("predictions must contain Prediction values")
        key = _identity(prediction.sample_id, prediction.term)
        _strict_timestamp(prediction.hit_ms, "prediction.hit_ms")
        _strict_timestamp(prediction.ready_at_ms, "prediction.ready_at_ms")
        if prediction.ready_at_ms < prediction.hit_ms:
            raise ValueError("prediction.ready_at_ms precedes prediction.hit_ms")
        grouped_predictions.setdefault(key, []).append(prediction)

    per_term_values: dict[str, list[int]] = {}
    latencies: list[int] = []
    duplicate_events = 0
    keys = sorted(set(grouped_labels) | set(grouped_predictions))
    for key in keys:
        term = key[1]
        expected = sorted(grouped_labels.get(key, ()), key=lambda item: item.hit_ms)
        predicted = sorted(
            grouped_predictions.get(key, ()), key=lambda item: item.hit_ms
        )
        unmatched = set(range(len(predicted)))
        matched_labels = 0
        for label in expected:
            candidates = tuple(
                index for index in unmatched
                if abs(predicted[index].hit_ms - label.hit_ms) <= tolerance
            )
            if not candidates:
                continue
            chosen = min(candidates, key=lambda index: (
                abs(predicted[index].hit_ms - label.hit_ms),
                predicted[index].hit_ms,
                index,
            ))
            unmatched.remove(chosen)
            matched_labels += 1
            latency = predicted[chosen].ready_at_ms - label.hit_ms
            if latency < 0:
                raise ValueError("matched ready latency must be nonnegative")
            latencies.append(latency)
        for index in unmatched:
            if any(
                abs(predicted[index].hit_ms - label.hit_ms) <= tolerance
                for label in expected
            ):
                duplicate_events += 1
        counts = per_term_values.setdefault(term, [0, 0, 0])
        counts[0] += len(expected)
        counts[1] += len(predicted)
        counts[2] += matched_labels

    per_term = tuple(PerTermCounts(
        normalized_term=term,
        expected=counts[0],
        predicted=counts[1],
        true_positive=counts[2],
        false_positive=counts[1] - counts[2],
        false_negative=counts[0] - counts[2],
    ) for term, counts in sorted(per_term_values.items()))
    true_positive = sum(item.true_positive for item in per_term)
    false_positive = sum(item.false_positive for item in per_term)
    false_negative = sum(item.false_negative for item in per_term)
    label_count = true_positive + false_negative
    prediction_count = true_positive + false_positive
    ordered_latencies = sorted(latencies)
    p95 = (
        ordered_latencies[math.ceil(0.95 * len(ordered_latencies)) - 1]
        if ordered_latencies else math.inf
    )
    zero_hit_terms = tuple(
        item.normalized_term for item in per_term
        if item.expected >= 3 and item.true_positive == 0
    )
    return EvaluationReport(
        true_positive=true_positive,
        false_positive=false_positive,
        false_negative=false_negative,
        recall=true_positive / label_count if label_count else 0.0,
        precision=(
            true_positive / prediction_count if prediction_count else 0.0
        ),
        per_term=per_term,
        p95_ready_latency_ms=p95,
        zero_hit_terms=zero_hit_terms,
        duplicate_events=duplicate_events,
    )


class ReplayEvaluationIncomplete(RuntimeError):
    pass


def _reject_constant(_value: str) -> None:
    raise ValueError


WindowsAclVerifier = Callable[[Path], bool]


def _windows_current_user_sid() -> str:
    from app.windows_acl import current_user_sid

    return current_user_sid()


def _windows_acl_sddl(path: Path) -> str:
    from app.windows_acl import acl_sddl

    return acl_sddl(path)


def _windows_acl_is_private(path: Path) -> bool:
    from app.windows_acl import acl_is_private

    return acl_is_private(
        path,
        sddl_reader=_windows_acl_sddl,
        sid_reader=_windows_current_user_sid,
    )


def _windows_private_sddl(sid: str) -> str:
    from app.windows_acl import private_sddl

    return private_sddl(sid)


def _establish_windows_private_acl(path: Path) -> None:
    from app.windows_acl import establish_private_acl

    establish_private_acl(path, sid_reader=_windows_current_user_sid)


def _windows_private_path(
    path: Path,
    verifier: WindowsAclVerifier | None,
    *,
    establish: bool = False,
) -> bool:
    try:
        if verifier is not None:
            return bool(verifier(Path(path)))
        if establish:
            _establish_windows_private_acl(Path(path))
        return _windows_acl_is_private(Path(path))
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def _private_regular_file(
    path: Path,
    error_class: str,
    *,
    platform_name: str = os.name,
    windows_acl_verifier: WindowsAclVerifier | None = None,
) -> Path:
    candidate = Path(path)
    try:
        if (
            not candidate.is_absolute()
            or _path_has_symlink_component(candidate)
            or candidate.is_symlink()
            or not candidate.is_file()
            or (
                platform_name != "nt"
                and candidate.stat().st_mode & 0o077
            )
            or (
                platform_name == "nt"
                and not _windows_private_path(
                    candidate, windows_acl_verifier
                )
            )
        ):
            raise ValueError
    except (OSError, ValueError):
        raise ValueError(error_class) from None
    return candidate


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


def _jsonl_rows(
    path: Path,
    error_class: str,
    *,
    platform_name: str = os.name,
    windows_acl_verifier: WindowsAclVerifier | None = None,
) -> tuple[dict[str, object], ...]:
    source = _private_regular_file(
        path,
        error_class,
        platform_name=platform_name,
        windows_acl_verifier=windows_acl_verifier,
    )
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
        if not lines or any(not line.strip() for line in lines):
            raise ValueError
        rows = tuple(
            json.loads(line, parse_constant=_reject_constant) for line in lines
        )
        if any(type(row) is not dict for row in rows):
            raise ValueError
        return rows  # type: ignore[return-value]
    except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
        raise ValueError(error_class) from None


def load_replay_samples(
    path: Path,
    *,
    platform_name: str = os.name,
    windows_acl_verifier: WindowsAclVerifier | None = None,
) -> tuple[ReplaySample, ...]:
    from .audio import _wav_duration_ms

    rows = _jsonl_rows(
        Path(path),
        "evaluation_manifest_invalid",
        platform_name=platform_name,
        windows_acl_verifier=windows_acl_verifier,
    )
    samples: list[ReplaySample] = []
    seen: set[str] = set()
    seen_audio: set[Path] = set()
    last_global_end = -1
    for row in rows:
        try:
            if set(row) != {"sample_id", "audio_path", "origin_ms", "live_id"}:
                raise ValueError
            sample_id = row["sample_id"]
            audio_value = row["audio_path"]
            origin_ms = row["origin_ms"]
            live_id = row["live_id"]
            if (
                not isinstance(sample_id, str)
                or not sample_id.strip()
                or "\0" in sample_id
                or "\n" in sample_id
                or len(sample_id) > 200
                or sample_id in seen
                or not isinstance(audio_value, str)
                or type(origin_ms) is not int
                or origin_ms < 0
                or not isinstance(live_id, str)
                or not live_id.strip()
                or "\0" in live_id
                or "\n" in live_id
                or len(live_id) > 200
            ):
                raise ValueError
            audio = Path(audio_value)
            if (
                not audio.is_absolute()
                or _path_has_symlink_component(audio)
                or audio.is_symlink()
                or not audio.is_file()
                or (
                    platform_name != "nt"
                    and audio.stat().st_mode & 0o077
                )
                or (
                    platform_name == "nt"
                    and not _windows_private_path(
                        audio, windows_acl_verifier
                    )
                )
                or audio in seen_audio
            ):
                raise ValueError
            duration_ms = _wav_duration_ms(audio)
            if origin_ms < last_global_end:
                raise ValueError
            context = audio.with_name(f".{audio.stem}.context.wav")
            if context.exists() or context.is_symlink():
                raise ValueError
        except (OSError, TypeError, ValueError):
            raise ValueError("evaluation_manifest_invalid") from None
        seen.add(sample_id)
        seen_audio.add(audio)
        last_global_end = origin_ms + duration_ms
        samples.append(ReplaySample(
            sample_id=sample_id,
            audio_path=audio,
            origin_ms=origin_ms,
            duration_ms=duration_ms,
            live_id=live_id,
        ))
    return tuple(samples)


def load_labels(
    path: Path, samples: tuple[ReplaySample, ...],
    *,
    platform_name: str = os.name,
    windows_acl_verifier: WindowsAclVerifier | None = None,
) -> tuple[Label, ...]:
    rows = _jsonl_rows(
        Path(path),
        "evaluation_labels_invalid",
        platform_name=platform_name,
        windows_acl_verifier=windows_acl_verifier,
    )
    by_id = {sample.sample_id: sample for sample in samples}
    labels: list[Label] = []
    seen: set[tuple[str, str, int]] = set()
    for row in rows:
        try:
            if set(row) != {"sample_id", "term", "hit_offset_ms"}:
                raise ValueError
            sample_id = row["sample_id"]
            term = row["term"]
            offset = row["hit_offset_ms"]
            if (
                not isinstance(sample_id, str)
                or sample_id not in by_id
                or not isinstance(term, str)
                or not 1 <= len(term.strip()) <= 50
                or type(offset) is not int
                or offset < 0
                or offset >= by_id[sample_id].duration_ms
            ):
                raise ValueError
            normalized = normalize_term(term)
            if not normalized:
                raise ValueError
            hit_ms = by_id[sample_id].origin_ms + offset
            identity = sample_id, normalized, hit_ms
            if identity in seen:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ValueError("evaluation_labels_invalid") from None
        seen.add(identity)
        labels.append(Label(sample_id, term.strip(), hit_ms))
    if not labels:
        raise ValueError("evaluation_labels_invalid")
    return tuple(labels)


def _sync_directory(path: Path, *, platform_name: str = os.name) -> None:
    if platform_name == "nt":
        return
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_private(
    source: Path, destination: Path, *, platform_name: str = os.name,
) -> None:
    if platform_name != "nt":
        os.replace(source, destination)
        return
    try:
        import ctypes

        move_file = ctypes.windll.kernel32.MoveFileExW
        move_file.argtypes = (
            ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint,
        )
        move_file.restype = ctypes.c_int
        if not move_file(str(source), str(destination), 0x1 | 0x8):
            raise OSError(ctypes.get_last_error(), "MoveFileExW failed")
    except AttributeError:
        # Unit tests emulate Windows on POSIX. Production Windows has
        # MoveFileExW with write-through durability.
        os.replace(source, destination)


def _atomic_private_write(
    path: Path,
    payload: bytes,
    *,
    platform_name: str = os.name,
    windows_acl_verifier: WindowsAclVerifier | None = None,
) -> Path:
    path = Path(path)
    if _path_has_symlink_component(path.parent):
        raise ValueError("evaluation_private_output_invalid")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (
        _path_has_symlink_component(path.parent)
        or path.parent.is_symlink()
        or not path.parent.is_dir()
    ):
        raise ValueError("evaluation_private_output_invalid")
    if platform_name != "nt":
        path.parent.chmod(0o700)
    elif not _windows_private_path(
        path.parent, windows_acl_verifier, establish=True
    ):
        raise ValueError("evaluation_private_output_invalid")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        temporary.chmod(0o600)
        if platform_name != "nt":
            os.fchmod(descriptor, 0o600)
        elif not _windows_private_path(
            temporary, windows_acl_verifier, establish=True
        ):
            raise ValueError("evaluation_private_output_invalid")
        os.write(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        if path.is_symlink():
            raise ValueError("evaluation_private_output_invalid")
        _replace_private(temporary, path, platform_name=platform_name)
        if platform_name != "nt":
            path.chmod(0o600)
        elif not _windows_private_path(path, windows_acl_verifier):
            raise ValueError("evaluation_private_output_invalid")
        _sync_directory(path.parent, platform_name=platform_name)
        return path
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


def write_private_replay_manifest(sample: ReplaySample, directory: Path) -> Path:
    if not isinstance(sample, ReplaySample):
        raise TypeError("sample must be ReplaySample")
    material = json.dumps(
        {
            "audio_path": str(sample.audio_path),
            "duration_ms": sample.duration_ms,
            "live_id": sample.live_id,
            "origin_ms": sample.origin_ms,
            "sample_id": sample.sample_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    chunk_key = hashlib.sha256(material.encode("utf-8")).hexdigest()
    encoded = json.dumps(
        {
            "audio_path": str(sample.audio_path),
            "chunk_key": chunk_key,
            "duration_ms": sample.duration_ms,
            "live_id": sample.live_id,
            "origin_ms": sample.origin_ms,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ) + "\n"
    return _atomic_private_write(
        Path(directory) / "replay-manifest.jsonl", encoded.encode("utf-8")
    )


@dataclass(frozen=True)
class _ActiveStreamTruth:
    stream_id: int
    gap_ms: int
    last_growth_ms: int


@dataclass(frozen=True)
class _RuntimeTruth:
    streams: tuple[_ActiveStreamTruth, ...]
    issue_count: int
    backlog_threshold_tripped: bool
    job_completions: int


@dataclass(frozen=True)
class _MainTruth:
    session_key: str
    signature: str
    main_gap_ms: int
    job_completions: int
    baseline_gap_ms: int
    baseline_job_completions: int
    trusted_full_day: bool
    runtime: _RuntimeTruth


def _strict_json(value: object, expected_type: type) -> object:
    try:
        decoded = json.loads(str(value or ""), parse_constant=_reject_constant)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid persisted evaluation JSON") from exc
    if type(decoded) is not expected_type:
        raise ValueError("invalid persisted evaluation JSON shape")
    return decoded


class _ReadOnlyEvidenceStore:
    """Small read-only Store surface consumed by canonical validators."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def query(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self.connection.execute(sql, args).fetchall()

    def get_business_session(self, business_session_key: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM business_sessions WHERE business_session_key=?",
            (str(business_session_key),),
        ).fetchone()

    def get_intelligence_job(self, job_key: str) -> dict[str, object] | None:
        row = self.connection.execute(
            "SELECT * FROM intelligence_jobs WHERE job_key=?", (str(job_key),)
        ).fetchone()
        return None if row is None else dict(row)

    def get_intelligence_artifact(
        self, job_key: str,
    ) -> dict[str, object] | None:
        row = self.connection.execute(
            "SELECT * FROM intelligence_artifacts WHERE job_key=?", (str(job_key),)
        ).fetchone()
        if row is None:
            return None
        result: dict[str, object] = dict(row)
        for field, shape in (
            ("context_snapshot", dict),
            ("raw_evidence", dict),
            ("validated_evidence", dict),
            ("raw_actions", dict),
            ("validated_result", dict),
            ("rejected", list),
            ("usage", dict),
        ):
            result[field] = _strict_json(result.pop(f"{field}_json"), shape)
        return result


def _merged_intervals(
    values: list[tuple[int, int]], *, allow_contiguous: bool = True,
) -> tuple[tuple[int, int], ...]:
    ordered = sorted(values)
    merged: list[tuple[int, int]] = []
    for start, end in ordered:
        if start < 0 or end <= start:
            raise ValueError("evaluation_timeline_invalid")
        if not merged:
            merged.append((start, end))
            continue
        previous_start, previous_end = merged[-1]
        if start < previous_end:
            raise ValueError("evaluation_timeline_invalid")
        if allow_contiguous and start == previous_end:
            merged[-1] = previous_start, end
        else:
            merged.append((start, end))
    return tuple(merged)


class ProductionEvidenceProvider:
    """Read-only authority for the full-day and main-chain resource gates."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        path = self.db_path
        if (
            not path.is_absolute()
            or _path_has_symlink_component(path)
            or not path.is_file()
        ):
            raise ValueError("evaluation_production_evidence_unavailable")
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro", uri=True, timeout=5.0,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    @staticmethod
    def _sample_timeline(
        samples: tuple[ReplaySample, ...],
    ) -> dict[str, tuple[tuple[int, int], ...]]:
        by_live: dict[str, list[tuple[int, int]]] = {}
        for sample in samples:
            by_live.setdefault(sample.live_id, []).append((
                sample.origin_ms, sample.origin_ms + sample.duration_ms,
            ))
        return {
            live_id: _merged_intervals(intervals)
            for live_id, intervals in by_live.items()
        }

    @staticmethod
    def _session_timeline(
        connection: sqlite3.Connection, session_key: str,
    ) -> tuple[dict[str, tuple[tuple[int, int], ...]], int]:
        from app.recorder.timeline import TimelineManifest

        rows = tuple(connection.execute(
            "SELECT live_id,session_dir,status FROM streams "
            "WHERE business_session_key=? ORDER BY started_at,id",
            (session_key,),
        ))
        if not rows or any(
            str(row["status"] or "") in {"recording", "recovering"}
            for row in rows
        ):
            raise ValueError("evaluation_business_session_incomplete")
        by_live: dict[str, list[tuple[int, int]]] = {}
        all_intervals: list[tuple[int, int]] = []
        for row in rows:
            live_id = str(row["live_id"] or "").strip()
            directory = Path(str(row["session_dir"] or ""))
            if (
                not live_id
                or not directory.is_absolute()
                or _path_has_symlink_component(directory)
                or _path_has_symlink_component(directory / "timeline.json")
            ):
                raise ValueError("evaluation_business_timeline_invalid")
            timeline = TimelineManifest.open_existing(directory)
            if timeline is None:
                raise ValueError("evaluation_business_timeline_missing")
            for part in timeline.parts:
                if part.state == "failed":
                    continue
                if part.state != "complete":
                    raise ValueError("evaluation_business_timeline_incomplete")
                interval = (
                    int(part.first_media_at_ms),
                    int(part.first_media_at_ms) + int(part.duration_ms),
                )
                by_live.setdefault(live_id, []).append(interval)
                all_intervals.append(interval)
        canonical = {
            live_id: _merged_intervals(intervals)
            for live_id, intervals in by_live.items()
        }
        global_intervals = _merged_intervals(all_intervals)
        covered = sum(end - start for start, end in global_intervals)
        gap_ms = global_intervals[-1][1] - global_intervals[0][0] - covered
        return canonical, gap_ms

    @staticmethod
    def _job_completions(
        connection: sqlite3.Connection, session_key: str,
    ) -> int:
        transcription = int(connection.execute(
            "SELECT COUNT(*) FROM transcription_jobs "
            "WHERE business_session_key=? AND status IN ('ready','fallback_ready')",
            (session_key,),
        ).fetchone()[0])
        intelligence = int(connection.execute(
            "SELECT COUNT(*) FROM intelligence_jobs j "
            "JOIN streams s ON s.id=j.stream_id "
            "WHERE s.business_session_key=? AND j.status='ready'",
            (session_key,),
        ).fetchone()[0])
        daily = int(connection.execute(
            "SELECT COUNT(*) FROM platform_review_jobs "
            "WHERE business_session_key=? "
            "AND status IN ('sent','delivery_unknown')",
            (session_key,),
        ).fetchone()[0])
        return transcription + intelligence + daily

    @staticmethod
    def _local_timestamp(value: object) -> float:
        from app.config import parse_local_datetime

        try:
            parsed = parse_local_datetime(str(value or ""))
        except (TypeError, ValueError):
            parsed = None
        if parsed is None:
            raise ValueError("evaluation_full_day_authority_invalid")
        timestamp = float(parsed.timestamp())
        if not math.isfinite(timestamp):
            raise ValueError("evaluation_full_day_authority_invalid")
        return timestamp

    @staticmethod
    def _full_day_authority(
        connection: sqlite3.Connection,
        session: sqlite3.Row,
        timeline: dict[str, tuple[tuple[int, int], ...]],
    ) -> tuple[str, int]:
        from app.business_facts import (
            artifact_hash,
            normalize_media_coverage,
        )
        from app.config import local_epoch_ms
        from app.intelligence.context import (
            build_hourly_context,
            clip_qianniu_metrics_to_window,
            intelligence_context_input_hash,
            normalize_qianniu_metrics,
            offset_transcription_segments,
        )
        from app.intelligence.models import IntelligenceContext, canonical_json
        from app.intelligence.integrity import validated_result_from_artifact
        from app.intelligence.platform_context import (
            build_platform_intelligence_context,
        )
        from app.intelligence.platform_models import (
            bound_platform_intelligence_from_summary,
        )
        from app.intelligence.platform_service import (
            load_frozen_platform_intelligence,
        )
        from app.review.platform import load_complete_business_hours
        from app.transcription.models import TranscriptionResult

        key = str(session["business_session_key"] or "").strip()
        observation_at = float(session["observation_completed_at"] or 0)
        ended_at = float(session["ended_at"] or 0)
        if (
            not key
            or str(session["status"] or "") != "ended"
            or not math.isfinite(observation_at)
            or not math.isfinite(ended_at)
            or observation_at <= 0
            or ended_at <= 0
            or not timeline
        ):
            raise ValueError("evaluation_full_day_authority_invalid")

        store = _ReadOnlyEvidenceStore(connection)
        try:
            complete_hours, hour_issues = load_complete_business_hours(store, key)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("evaluation_full_day_authority_invalid") from exc
        if not complete_hours or hour_issues:
            raise ValueError("evaluation_full_day_authority_invalid")

        reviews = tuple(connection.execute(
            "SELECT j.live_id,r.data_state,r.payload,r.updated_at "
            "FROM platform_review_jobs j JOIN platform_reviews r "
            "ON r.live_id=j.live_id WHERE j.business_session_key=?",
            (key,),
        ))
        if len(reviews) != 1 or str(reviews[0]["data_state"] or "") != "complete":
            raise ValueError("evaluation_full_day_authority_invalid")
        review = reviews[0]

        def reject_constant(_value: str) -> None:
            raise ValueError("non-finite JSON")

        try:
            payload = json.loads(
                str(review["payload"] or ""), parse_constant=reject_constant
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("evaluation_full_day_authority_invalid") from exc
        intelligence = (
            payload.get("platform_intelligence")
            if isinstance(payload, dict) else None
        )
        job_key = (
            str(intelligence.get("job_key") or "").strip()
            if isinstance(intelligence, dict) else ""
        )
        if (
            not isinstance(payload, dict)
            or str(payload.get("business_session_key") or "") != key
            or str(payload.get("data_state") or "") != "complete"
            or not isinstance(intelligence, dict)
            or str(intelligence.get("status") or "") != "ready"
            or not job_key
        ):
            raise ValueError("evaluation_full_day_authority_invalid")

        artifacts = tuple(connection.execute(
            """SELECT w.shift_window_key,w.window_start_ms,w.window_end_ms,
                      w.actual_slices_json,a.artifact_hash,a.quality_state,
                      a.payload_json,a.updated_at
               FROM shift_windows w JOIN hourly_artifacts a
                 ON a.shift_window_key=w.shift_window_key
               WHERE w.business_session_key=? ORDER BY w.window_start_ms,
                      w.window_end_ms,w.shift_window_key""",
            (key,),
        ))
        complete_by_key: dict[str, dict[str, object]] = {}
        for item in complete_hours:
            if not isinstance(item, dict):
                raise ValueError("evaluation_full_day_authority_invalid")
            identity = item.get("identity")
            window_key = (
                str(identity.get("shift_window_key") or "")
                if isinstance(identity, dict) else ""
            )
            if not window_key or window_key in complete_by_key:
                raise ValueError("evaluation_full_day_authority_invalid")
            complete_by_key[window_key] = item
        artifact_rows = tuple(
            row for row in artifacts
            if str(row["shift_window_key"] or "") in complete_by_key
        )
        if len(artifact_rows) != len(complete_by_key):
            raise ValueError("evaluation_full_day_authority_invalid")

        frozen_hours = payload.get("hourly_artifacts")
        if not isinstance(frozen_hours, list) or len(frozen_hours) != len(complete_by_key):
            raise ValueError("evaluation_full_day_authority_invalid")
        frozen_by_key: dict[str, dict[str, object]] = {}
        for item in frozen_hours:
            if not isinstance(item, dict):
                raise ValueError("evaluation_full_day_authority_invalid")
            identity = item.get("identity")
            window_key = (
                str(identity.get("shift_window_key") or "")
                if isinstance(identity, dict) else ""
            )
            if not window_key or window_key in frozen_by_key:
                raise ValueError("evaluation_full_day_authority_invalid")
            frozen_by_key[window_key] = item
        if set(frozen_by_key) != set(complete_by_key) or any(
            canonical_json(frozen_by_key[window_key])
            != canonical_json(complete_by_key[window_key])
            for window_key in complete_by_key
        ):
            raise ValueError("evaluation_full_day_authority_invalid")

        expected_media: dict[str, list[tuple[int, int]]] = {}
        total_gap_ms = 0
        analysis_job_keys: set[str] = set()
        window_bounds: list[tuple[int, int]] = []
        for row in artifact_rows:
            window_key = str(row["shift_window_key"] or "")
            start = int(row["window_start_ms"])
            end = int(row["window_end_ms"])
            window_bounds.append((start, end))
            artifact = complete_by_key[window_key]
            stored_digest = str(row["artifact_hash"] or "")
            payload_digest = str(artifact.get("artifact_hash") or "")
            unhashed = dict(artifact)
            unhashed.pop("artifact_hash", None)
            if (
                str(row["quality_state"] or "") != "complete"
                or stored_digest != payload_digest
                or artifact_hash(unhashed) != payload_digest
            ):
                raise ValueError("evaluation_full_day_authority_invalid")

            quality = artifact.get("quality")
            try:
                coverage = normalize_media_coverage(
                    quality.get("media_coverage") if isinstance(quality, dict) else {},
                    window_start_ms=start,
                    window_end_ms=end,
                )
                slices = _strict_json(row["actual_slices_json"], list)
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("evaluation_full_day_authority_invalid") from exc
            total_gap_ms += int(coverage["missing_ms"])
            gap_intervals = tuple(
                (int(item["start_ms"]), int(item["end_ms"]))
                for item in coverage["gaps"]
            )
            media_complement: list[tuple[int, int]] = []
            cursor = start
            for gap_start, gap_end in gap_intervals:
                if cursor < gap_start:
                    media_complement.append((cursor, gap_start))
                cursor = gap_end
            if cursor < end:
                media_complement.append((cursor, end))

            actual_media: list[tuple[int, int]] = []
            actual_gaps: list[tuple[int, int]] = []
            slice_stream_ids: set[int] = set()
            for raw_slice in slices:
                if not isinstance(raw_slice, dict):
                    raise ValueError("evaluation_full_day_authority_invalid")
                slice_start = raw_slice.get("actual_start_ms")
                slice_end = raw_slice.get("actual_end_ms")
                if (
                    isinstance(slice_start, bool)
                    or isinstance(slice_end, bool)
                    or type(slice_start) is not int
                    or type(slice_end) is not int
                    or slice_start < start
                    or slice_end <= slice_start
                    or slice_end > end
                ):
                    raise ValueError("evaluation_full_day_authority_invalid")
                kind = str(raw_slice.get("kind") or "media")
                interval = (slice_start, slice_end)
                if kind == "gap":
                    actual_gaps.append(interval)
                    continue
                if kind != "media":
                    raise ValueError("evaluation_full_day_authority_invalid")
                stream_id = raw_slice.get("stream_id")
                live_id = str(raw_slice.get("live_id") or "").strip()
                if type(stream_id) is not int or stream_id <= 0 or not live_id:
                    raise ValueError("evaluation_full_day_authority_invalid")
                stream = connection.execute(
                    "SELECT live_id,business_session_key FROM streams WHERE id=?",
                    (stream_id,),
                ).fetchone()
                if (
                    stream is None
                    or str(stream["business_session_key"] or "") != key
                    or str(stream["live_id"] or "") != live_id
                ):
                    raise ValueError("evaluation_full_day_authority_invalid")
                actual_media.append(interval)
                slice_stream_ids.add(stream_id)
                expected_media.setdefault(live_id, []).append(interval)
            try:
                normalized_media = _merged_intervals(actual_media)
                normalized_gaps = _merged_intervals(
                    actual_gaps, allow_contiguous=False
                ) if actual_gaps else ()
            except ValueError as exc:
                raise ValueError("evaluation_full_day_authority_invalid") from exc
            if (
                normalized_media != tuple(media_complement)
                or normalized_gaps != gap_intervals
            ):
                raise ValueError("evaluation_full_day_authority_invalid")

            transcription = artifact.get("transcription")
            transcription_rows = tuple(connection.execute(
                """SELECT * FROM transcription_jobs
                   WHERE business_session_key=? AND shift_window_key=?
                     AND purpose='hourly' ORDER BY job_key""",
                (key, window_key),
            ))
            transcription_row = (
                transcription_rows[0] if len(transcription_rows) == 1 else None
            )
            if (
                not isinstance(transcription, dict)
                or transcription_row is None
                or str(transcription_row["business_session_key"] or "") != key
                or str(transcription_row["shift_window_key"] or "") != window_key
                or str(transcription_row["status"] or "")
                not in {"ready", "fallback_ready"}
                or (
                    str(transcription_row["status"] or "") == "fallback_ready"
                    and not int(transcription_row["review_fallback_approved"] or 0)
                )
            ):
                raise ValueError("evaluation_full_day_authority_invalid")
            try:
                transcript_result = TranscriptionResult.from_json(
                    str(transcription_row["result_json"] or "")
                )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError("evaluation_full_day_authority_invalid") from exc
            if (
                transcript_result is None
                or not transcript_result.segments
                or canonical_json(transcription)
                != canonical_json(json.loads(transcript_result.to_json()))
                or (
                    transcript_result.provider == "feishu_minutes"
                    and (
                        transcript_result.smart is None
                        or not transcript_result.smart.minute_url
                    )
                )
            ):
                raise ValueError("evaluation_full_day_authority_invalid")

            analysis = artifact.get("analysis")
            analysis_key = (
                str(analysis.get("job_key") or "")
                if isinstance(analysis, dict) else ""
            )
            analysis_row = store.get_intelligence_job(analysis_key)
            if (
                not analysis_row
                or str(analysis_row.get("task_type") or "") != "hourly"
                or str(analysis_row.get("status") or "") != "ready"
            ):
                raise ValueError("evaluation_full_day_authority_invalid")
            analysis_artifact = store.get_intelligence_artifact(analysis_key)
            try:
                analysis_result = validated_result_from_artifact(
                    analysis_artifact or {},
                    expected_job_key=analysis_key,
                    expected_status="ready",
                )
                frozen_context = IntelligenceContext.from_dict(
                    (analysis_artifact or {}).get("context_snapshot")
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("evaluation_full_day_authority_invalid") from exc
            if (
                not isinstance(analysis, dict)
                or canonical_json(analysis)
                != canonical_json(analysis_result.to_dict())
            ):
                raise ValueError("evaluation_full_day_authority_invalid")

            identity = artifact.get("identity")
            artifact_metrics = artifact.get("metrics")
            artifact_series = artifact.get("series")
            context_stream = connection.execute(
                "SELECT id,live_id,anchor_id,started_at,business_session_key "
                "FROM streams WHERE id=?",
                (frozen_context.stream_id,),
            ).fetchone()
            recording_start_ms = (
                local_epoch_ms(context_stream["started_at"])
                if context_stream is not None else None
            )
            if (
                not isinstance(identity, dict)
                or not isinstance(artifact_metrics, dict)
                or not isinstance(artifact_series, dict)
                or context_stream is None
                or recording_start_ms is None
                or frozen_context.stream_id not in slice_stream_ids
                or str(context_stream["business_session_key"] or "") != key
                or frozen_context.live_id
                != str(context_stream["live_id"] or "")
                or frozen_context.anchor_id
                != int(context_stream["anchor_id"] or 0)
                or frozen_context.live_id
                != str(identity.get("live_id") or "")
                or frozen_context.anchor_id
                != int(identity.get("anchor_id") or 0)
                or recording_start_ms + frozen_context.window_start_ms != start
                or recording_start_ms + frozen_context.window_end_ms != end
                or not frozen_context.input_hash
                or intelligence_context_input_hash(frozen_context)
                != frozen_context.input_hash
                or frozen_context.input_hash
                != str(analysis_row.get("input_hash") or "")
                or canonical_json(artifact_metrics.get("media_coverage"))
                != canonical_json(coverage)
                or canonical_json(artifact_series)
                != canonical_json(artifact_metrics.get("series"))
            ):
                raise ValueError("evaluation_full_day_authority_invalid")
            try:
                expected_metrics = clip_qianniu_metrics_to_window(
                    normalize_qianniu_metrics(
                        artifact_metrics,
                        recording_start_ms=recording_start_ms,
                    ),
                    window_start_ms=frozen_context.window_start_ms,
                    window_end_ms=frozen_context.window_end_ms,
                )
                expected_context = build_hourly_context(
                    stream_id=frozen_context.stream_id,
                    live_id=frozen_context.live_id,
                    anchor_id=frozen_context.anchor_id,
                    anchor_name=frozen_context.anchor_name,
                    window_start_ms=frozen_context.window_start_ms,
                    window_end_ms=frozen_context.window_end_ms,
                    segments=offset_transcription_segments(
                        transcript_result.segments,
                        frozen_context.window_start_ms,
                    ),
                    smart_minutes=transcript_result.smart,
                    metrics=expected_metrics,
                    peak_highlights=frozen_context.peak_context,
                    history=frozen_context.history,
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("evaluation_full_day_authority_invalid") from exc
            if any((
                frozen_context.transcripts != expected_context.transcripts,
                frozen_context.smart_minutes != expected_context.smart_minutes,
                canonical_json(frozen_context.metrics)
                != canonical_json(expected_context.metrics),
            )):
                raise ValueError("evaluation_full_day_authority_invalid")
            analysis_job_keys.add(analysis_key)

        try:
            _merged_intervals(window_bounds, allow_contiguous=False)
            canonical_expected_media = {
                live_id: _merged_intervals(intervals)
                for live_id, intervals in expected_media.items()
            }
        except ValueError as exc:
            raise ValueError("evaluation_full_day_authority_invalid") from exc
        if canonical_expected_media != timeline:
            raise ValueError("evaluation_full_day_authority_invalid")

        try:
            hourly_context = build_platform_intelligence_context(store, payload, key)
            source_jobs = {
                source.source_job_key for source in hourly_context.sources
                if source.source_job_key
            }
            result, binding = bound_platform_intelligence_from_summary(payload)
            platform_context, frozen_result, rejected = (
                load_frozen_platform_intelligence(
                    store,
                    key,
                    job_key=result.job_key,
                    expected_result=result.to_dict(),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("evaluation_full_day_authority_invalid") from exc
        if (
            hourly_context.rejected_inputs
            or source_jobs != analysis_job_keys
            or rejected
            or platform_context.live_id != key
            or frozen_result.job_key != result.job_key
            or binding["input_hash"] != platform_context.input_hash
            or canonical_json(hourly_context.to_dict())
            != canonical_json(platform_context.to_dict())
        ):
            raise ValueError("evaluation_full_day_authority_invalid")

        artifact_times = tuple(
            ProductionEvidenceProvider._local_timestamp(row["updated_at"])
            for row in artifact_rows
        )
        intelligence_rows = tuple(connection.execute(
            "SELECT status,updated_at FROM intelligence_jobs "
            "WHERE job_key=? AND task_type='platform' AND live_id=?",
            (job_key, key),
        ))
        if (
            len(intelligence_rows) != 1
            or str(intelligence_rows[0]["status"] or "") != "ready"
        ):
            raise ValueError("evaluation_full_day_authority_invalid")
        intelligence_at = ProductionEvidenceProvider._local_timestamp(
            intelligence_rows[0]["updated_at"]
        )
        frozen_at = ProductionEvidenceProvider._local_timestamp(
            review["updated_at"]
        )
        if frozen_at < max(observation_at, max(artifact_times), intelligence_at):
            raise ValueError("evaluation_full_day_authority_invalid")
        canonical = json.dumps(
            {
                "artifacts": [
                    {
                        "artifact_hash": str(row["artifact_hash"] or ""),
                        "actual_slices": _strict_json(
                            row["actual_slices_json"], list
                        ),
                        "end_ms": int(row["window_end_ms"]),
                        "key": str(row["shift_window_key"] or ""),
                        "start_ms": int(row["window_start_ms"]),
                    }
                    for row in artifact_rows
                ],
                "frozen_at": frozen_at,
                "intelligence_job": job_key,
                "payload_hash": hashlib.sha256(
                    str(review["payload"]).encode("utf-8")
                ).hexdigest(),
                "session": key,
                "timeline": timeline,
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest(), total_gap_ms

    @staticmethod
    def _active_issues(
        connection: sqlite3.Connection,
        *,
        active_since: str,
        checked_at: float,
        stale_running_seconds: int = 7_200,
    ) -> int:
        queries = (
            (
                "SELECT COUNT(*) FROM intelligence_jobs j "
                "JOIN streams s ON s.id=j.stream_id "
                "WHERE s.started_at>=? "
                "AND j.status NOT IN ('ready','blocked') AND j.error<>''",
                (active_since,),
            ),
            (
                "SELECT COUNT(*) FROM intelligence_jobs j "
                "JOIN streams s ON s.id=j.stream_id "
                "WHERE s.started_at>=? AND j.status='blocked'",
                (active_since,),
            ),
            (
                "SELECT COUNT(*) FROM platform_review_jobs WHERE status='failed'",
                (),
            ),
            (
                "SELECT COUNT(*) FROM maintenance_jobs "
                "WHERE status='failed' AND job_key LIKE 'cleanup:%'",
                (),
            ),
            (
                "SELECT COUNT(*) FROM maintenance_jobs "
                "WHERE status='running' AND job_key LIKE 'cleanup:%' "
                "AND last_attempt<?",
                (checked_at - max(60, int(stale_running_seconds)),),
            ),
            (
                "SELECT COUNT(*) FROM transcription_jobs WHERE status='blocked'",
                (),
            ),
            (
                "SELECT COUNT(*) FROM transcription_jobs "
                "WHERE status='processing' "
                "AND updated_at < datetime('now','localtime','-2 hours')",
                (),
            ),
            (
                "SELECT COUNT(*) FROM transcription_jobs "
                "WHERE cleanup_status='failed'",
                (),
            ),
        )
        return sum(
            int(connection.execute(query, parameters).fetchone()[0])
            for query, parameters in queries
        )

    @staticmethod
    def _active_runtime(connection: sqlite3.Connection) -> _RuntimeTruth:
        from datetime import datetime, timedelta

        from app.config import SHANGHAI

        from app.recorder.timeline import TimelineManifest

        rows = tuple(connection.execute(
            "SELECT id,business_session_key,session_dir FROM streams "
            "WHERE status IN ('recording','recovering') ORDER BY id"
        ))
        session_keys = {
            str(row["business_session_key"] or "").strip() for row in rows
        }
        if not rows or len(session_keys) != 1 or "" in session_keys:
            raise ValueError("evaluation_active_main_chain_unavailable")
        streams: list[_ActiveStreamTruth] = []
        for row in rows:
            directory = Path(str(row["session_dir"] or ""))
            if (
                not directory.is_absolute()
                or _path_has_symlink_component(directory)
                or _path_has_symlink_component(directory / "timeline.json")
            ):
                raise ValueError("evaluation_active_timeline_invalid")
            timeline = TimelineManifest.open_existing(directory)
            if timeline is None:
                raise ValueError("evaluation_active_timeline_missing")
            complete: list[tuple[int, int]] = []
            active_starts: list[int] = []
            growth: list[int] = []
            for part in timeline.parts:
                if part.state == "failed":
                    continue
                if part.state == "complete":
                    complete.append((
                        int(part.first_media_at_ms),
                        int(part.first_media_at_ms) + int(part.duration_ms),
                    ))
                    growth.append(int(part.last_growth_at_ms))
                elif part.state == "recording":
                    if (
                        int(part.first_media_at_ms) <= 0
                        or int(part.last_growth_at_ms) < int(part.first_media_at_ms)
                    ):
                        raise ValueError("evaluation_active_timeline_incomplete")
                    active_starts.append(int(part.first_media_at_ms))
                    growth.append(int(part.last_growth_at_ms))
                elif part.state != "started":
                    raise ValueError("evaluation_active_timeline_invalid")
            if not growth:
                raise ValueError("evaluation_active_timeline_incomplete")
            intervals = _merged_intervals(complete) if complete else ()
            gap_ms = 0
            if intervals:
                gap_ms = (
                    intervals[-1][1] - intervals[0][0]
                    - sum(end - start for start, end in intervals)
                )
                for start in active_starts:
                    if start > intervals[-1][1]:
                        gap_ms += start - intervals[-1][1]
            streams.append(_ActiveStreamTruth(
                stream_id=int(row["id"]),
                gap_ms=gap_ms,
                last_growth_ms=max(growth),
            ))
        session_key = next(iter(session_keys))
        checked_at = time.time()
        checked_local = datetime.fromtimestamp(checked_at, SHANGHAI)
        business_start = checked_local.replace(
            hour=5, minute=0, second=0, microsecond=0
        )
        if checked_local < business_start:
            business_start -= timedelta(days=1)
        issue_count = ProductionEvidenceProvider._active_issues(
            connection,
            active_since=business_start.strftime("%Y-%m-%d %H:%M:%S"),
            checked_at=checked_at,
        )
        return _RuntimeTruth(
            streams=tuple(streams),
            issue_count=issue_count,
            backlog_threshold_tripped=bool(issue_count),
            job_completions=ProductionEvidenceProvider._job_completions(
                connection, session_key
            ),
        )

    def _truth(self, samples: tuple[ReplaySample, ...]) -> _MainTruth:
        from datetime import datetime, timedelta

        requested = self._sample_timeline(samples)
        with self._connect() as connection:
            candidates = tuple(connection.execute(
                "SELECT business_session_key,status,observation_completed_at,"
                "ended_at FROM business_sessions "
                "WHERE status='ended' AND ended_at>0 "
                "ORDER BY ended_at,business_session_key"
            ))
            matched: list[tuple[
                sqlite3.Row, int, dict[str, tuple[tuple[int, int], ...]]
            ]] = []
            for candidate in candidates:
                key = str(candidate["business_session_key"])
                try:
                    timeline, gap_ms = self._session_timeline(connection, key)
                except (OSError, sqlite3.Error, ValueError):
                    continue
                if timeline == requested:
                    matched.append((candidate, gap_ms, timeline))
            if len(matched) != 1:
                raise ValueError("evaluation_business_session_ambiguous")
            selected, _raw_observed_gap, selected_timeline = matched[0]
            selected_key = str(selected["business_session_key"])
            try:
                selected_day = datetime.strptime(selected_key, "%Y%m%d")
            except ValueError as exc:
                raise ValueError("evaluation_business_session_ambiguous") from exc
            if selected_day.strftime("%Y%m%d") != selected_key:
                raise ValueError("evaluation_business_session_ambiguous")
            baseline_key = (selected_day - timedelta(days=1)).strftime("%Y%m%d")
            baseline_rows = tuple(connection.execute(
                "SELECT business_session_key,status,observation_completed_at,"
                "ended_at FROM business_sessions WHERE business_session_key=?",
                (baseline_key,),
            ))
            if len(baseline_rows) != 1:
                raise ValueError("evaluation_business_baseline_missing")
            baseline = baseline_rows[0]
            baseline_timeline, _raw_baseline_gap = self._session_timeline(
                connection, baseline_key
            )
            selected_authority, observed_gap = self._full_day_authority(
                connection, selected, selected_timeline
            )
            baseline_authority, baseline_gap = self._full_day_authority(
                connection, baseline, baseline_timeline
            )
            observed_jobs = self._job_completions(connection, selected_key)
            baseline_jobs = self._job_completions(connection, baseline_key)
            runtime = self._active_runtime(connection)
            signature_payload = json.dumps(
                {
                    "baseline_gap": baseline_gap,
                    "baseline_jobs": baseline_jobs,
                    "baseline_authority": baseline_authority,
                    "observed_gap": observed_gap,
                    "observed_jobs": observed_jobs,
                    "selected_authority": selected_authority,
                    "session": selected_key,
                    "timeline": requested,
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            return _MainTruth(
                session_key=selected_key,
                signature=hashlib.sha256(
                    signature_payload.encode("utf-8")
                ).hexdigest(),
                main_gap_ms=observed_gap,
                job_completions=observed_jobs,
                baseline_gap_ms=baseline_gap,
                baseline_job_completions=baseline_jobs,
                trusted_full_day=True,
                runtime=runtime,
            )

    def begin(self, samples: tuple[ReplaySample, ...]) -> _MainTruth:
        return self._truth(samples)

    def finish(
        self,
        token: object,
        samples: tuple[ReplaySample, ...],
        *,
        processed_sample_count: int,
        processed_audio_ms: int,
        processed_job_completions: int,
    ) -> ResourceEvidence:
        if not isinstance(token, _MainTruth):
            raise ValueError("evaluation_resource_token_invalid")
        current = self._truth(samples)
        unchanged = current.signature == token.signature
        before_streams = {
            stream.stream_id: stream for stream in token.runtime.streams
        }
        after_streams = {
            stream.stream_id: stream for stream in current.runtime.streams
        }
        runtime_healthy = (
            before_streams.keys() == after_streams.keys()
            and all(
                after_streams[key].gap_ms <= before_streams[key].gap_ms
                and after_streams[key].last_growth_ms
                > before_streams[key].last_growth_ms
                for key in before_streams
            )
            and current.runtime.issue_count <= token.runtime.issue_count
            and token.runtime.issue_count == 0
            and current.runtime.issue_count == 0
            and not token.runtime.backlog_threshold_tripped
            and not current.runtime.backlog_threshold_tripped
            and current.runtime.job_completions
            >= token.runtime.job_completions
        )
        return ResourceEvidence(
            window_start_ms=min(sample.origin_ms for sample in samples),
            window_end_ms=max(
                sample.origin_ms + sample.duration_ms for sample in samples
            ),
            baseline_main_gap_ms=current.baseline_gap_ms,
            observed_main_gap_ms=current.main_gap_ms,
            baseline_job_completions=current.baseline_job_completions,
            observed_job_completions=current.job_completions,
            observed_backlog_threshold_tripped=(
                not unchanged
                or not runtime_healthy
            ),
            full_day_observed=bool(
                unchanged
                and runtime_healthy
                and token.trusted_full_day
                and current.trusted_full_day
            ),
            observed_sample_count=processed_sample_count,
            observed_audio_ms=processed_audio_ms,
            replay_job_completions=processed_job_completions,
        )


class ProcessResourceSampler:
    def __init__(self, process: object) -> None:
        self.process = process
        self._sample_count = 0
        self._failed = False
        self._max_listener_cpu = 0.0
        self._max_listener_rss = 0
        self._max_child_cpu = 0.0
        self._max_child_rss = 0
        self._disk_read = 0
        self._disk_write = 0
        self._last_io: dict[object, tuple[int, int]] = {}
        self._max_queue_age = 0.0

    @classmethod
    def current(cls) -> "ProcessResourceSampler":
        import psutil

        return cls(psutil.Process())

    @staticmethod
    def _process_identity(process: object) -> object:
        pid = getattr(process, "pid", None)
        create_time = getattr(process, "create_time", None)
        try:
            started = create_time() if callable(create_time) else None
        except Exception:
            started = None
        return (pid, started) if pid is not None else id(process)

    def _io_delta(
        self, process: object, *, count_new_process: bool,
    ) -> tuple[int, int]:
        counters = process.io_counters()  # type: ignore[attr-defined]
        current = int(counters.read_bytes), int(counters.write_bytes)
        identity = self._process_identity(process)
        prior = self._last_io.get(identity)
        self._last_io[identity] = current
        if prior is None:
            return current if count_new_process else (0, 0)
        return max(0, current[0] - prior[0]), max(0, current[1] - prior[1])

    def sample(self, *, queued_job_age_seconds: float) -> None:
        try:
            if (
                isinstance(queued_job_age_seconds, bool)
                or not math.isfinite(float(queued_job_age_seconds))
                or float(queued_job_age_seconds) < 0
            ):
                raise ValueError
            listener_cpu = float(self.process.cpu_percent(interval=None))  # type: ignore[attr-defined]
            listener_rss = int(self.process.memory_info().rss)  # type: ignore[attr-defined]
            children = tuple(self.process.children(recursive=True))  # type: ignore[attr-defined]
            child_cpu = sum(
                float(child.cpu_percent(interval=None)) for child in children
            )
            child_rss = sum(int(child.memory_info().rss) for child in children)
            count_new = self._sample_count > 0
            read_delta, write_delta = self._io_delta(
                self.process, count_new_process=count_new
            )
            for child in children:
                read, written = self._io_delta(
                    child, count_new_process=count_new
                )
                read_delta += read
                write_delta += written
            values = (listener_cpu, child_cpu)
            if any(not math.isfinite(value) or value < 0 for value in values):
                raise ValueError
            if listener_rss < 0 or child_rss < 0:
                raise ValueError
        except Exception:
            self._failed = True
            return
        self._sample_count += 1
        self._max_listener_cpu = max(self._max_listener_cpu, listener_cpu)
        self._max_listener_rss = max(self._max_listener_rss, listener_rss)
        self._max_child_cpu = max(self._max_child_cpu, child_cpu)
        self._max_child_rss = max(self._max_child_rss, child_rss)
        self._disk_read += read_delta
        self._disk_write += write_delta
        self._max_queue_age = max(
            self._max_queue_age, float(queued_job_age_seconds)
        )

    def summary(self) -> ResourceSummary:
        return ResourceSummary(
            sample_count=self._sample_count,
            max_listener_cpu_percent=self._max_listener_cpu,
            max_listener_rss_bytes=self._max_listener_rss,
            max_child_cpu_percent=self._max_child_cpu,
            max_child_rss_bytes=self._max_child_rss,
            disk_read_bytes=self._disk_read,
            disk_write_bytes=self._disk_write,
            max_queued_job_age_seconds=self._max_queue_age,
            sampling_complete=not self._failed and self._sample_count > 0,
        )


class ResourceMonitor:
    def __init__(
        self,
        sampler: object,
        *,
        stop_event: object | None = None,
        queued_age_provider: Callable[[], float] = lambda: 0.0,
    ) -> None:
        self.sampler = sampler
        self.stop_event = stop_event or threading.Event()
        self.queued_age_provider = queued_age_provider

    def run(self) -> None:
        while True:
            try:
                age = float(self.queued_age_provider())
            except Exception:
                age = math.inf
            self.sampler.sample(queued_job_age_seconds=age)  # type: ignore[attr-defined]
            wait = getattr(self.stop_event, "wait")
            if bool(wait(30.0)):
                return

    def stop(self) -> None:
        setter = getattr(self.stop_event, "set")
        setter()

    def summary(self) -> ResourceSummary:
        return self.sampler.summary()  # type: ignore[attr-defined,no-any-return]


class _StaticWordlists:
    def __init__(self, snapshot: WordlistSnapshot) -> None:
        self.snapshot = snapshot

    def sync_if_due(self, _now: float) -> WordlistSnapshot:
        return self.snapshot


class _SequentialReplaySource:
    def __init__(
        self, samples: tuple[ReplaySample, ...], directory: Path,
    ) -> None:
        from .audio import ReplayAudioSource

        self._source_type = ReplayAudioSource
        self.samples = samples
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        if os.name != "nt":
            self.directory.chmod(0o700)
        self.mode_marker = self.directory / "sequence"
        self._index = 0
        self._prepared: tuple[int, float] | None = None
        self._offer: AudioOffer | None = None
        self._source: object | None = None
        self._returned = False

    def prepare_offer(
        self,
        *,
        wordlist_version_id: int,
        queued_at: float,
        final: bool,
        creation_mode: str,
        target_hash: str,
    ) -> None:
        del final
        if creation_mode != "shadow" or target_hash != "":
            raise ValueError("evaluation replay must remain shadow")
        if wordlist_version_id <= 0 or not math.isfinite(float(queued_at)):
            raise ValueError("evaluation replay authority is invalid")
        self._prepared = int(wordlist_version_id), float(queued_at)

    def poll(self, now_ms: int) -> AudioOffer | tuple[ClosedAudioChunk, ...]:
        del now_ms
        if self._offer is not None:
            self._returned = True
            return self._offer
        if self._index >= len(self.samples):
            return ()
        if self._prepared is None:
            raise ValueError("evaluation replay authority is missing")
        sample = self.samples[self._index]
        manifest = write_private_replay_manifest(
            sample, self.directory / f"sample-{self._index:06d}"
        )
        source = self._source_type(manifest)
        chunks = source.poll(0)
        if len(chunks) != 1 or chunks[0].live_id != sample.live_id:
            raise ValueError("evaluation replay boundary is ambiguous")
        current = chunks[0]
        following = (
            self.samples[self._index + 1]
            if self._index + 1 < len(self.samples) else None
        )
        final = not bool(
            following is not None
            and following.live_id == sample.live_id
            and following.origin_ms == sample.origin_ms + sample.duration_ms
        )
        material = current.chunk_key
        offer_id = hashlib.sha256(
            f"evaluation-offer\0{material}".encode("utf-8")
        ).hexdigest()
        boundary_id = hashlib.sha256(
            f"evaluation-boundary\0{material}".encode("utf-8")
        ).hexdigest()
        self._offer = AudioOffer(
            offer_id=offer_id,
            boundary_id=boundary_id,
            chunks=chunks,
            wordlist_version_id=self._prepared[0],
            queued_at=self._prepared[1],
            final=final,
            creation_mode="shadow",
            target_hash="",
            live_id=current.live_id,
            boundary_start_ms=current.capture_start_ms,
            boundary_end_ms=current.capture_end_ms,
        )
        self._source = source
        self._returned = True
        return self._offer

    def finish(
        self, now_ms: int, *, timeout_seconds: float | None = None,
    ) -> AudioOffer | tuple[ClosedAudioChunk, ...]:
        del timeout_seconds
        return self.poll(now_ms)

    def ack(self, offer_id: str) -> None:
        if self._offer is None or self._source is None or (
            offer_id != self._offer.offer_id or not self._returned
        ):
            raise ValueError("evaluation replay ACK is invalid")
        self._source.ack(self._offer.chunks)  # type: ignore[attr-defined]
        self._index += 1
        self._offer = None
        self._source = None
        self._returned = False

    def release_offer(self, offer_id: str) -> None:
        if self._offer is None or offer_id != self._offer.offer_id:
            raise ValueError("evaluation replay release is invalid")
        self._returned = False

    def quarantined_offers(self) -> tuple[AudioOffer, ...]:
        return ()

    @property
    def exhausted(self) -> bool:
        return self._index == len(self.samples) and self._offer is None

    def clock_seconds(self) -> float:
        index = min(self._index, len(self.samples) - 1)
        sample = self.samples[index]
        return (sample.origin_ms + sample.duration_ms) / 1000


class _SenderAudit:
    def __init__(self, sender: Callable[..., object] | None) -> None:
        self.sender = sender
        self.calls = 0

    def __call__(self, *args: object) -> bool:
        self.calls += 1
        if self.sender is None:
            return False
        return bool(self.sender(*args))


def _evaluation_config(config: dict[str, object], workspace: Path) -> dict:
    raw_compliance = config.get("compliance")
    raw_recognizer = (
        raw_compliance.get("recognizer")
        if isinstance(raw_compliance, dict) else None
    )
    recognizer = dict(raw_recognizer) if isinstance(raw_recognizer, dict) else {}
    allowed_recognizer = {
        key: recognizer[key]
        for key in (
            "model", "revision", "vad_model", "punc_model", "device"
        )
        if key in recognizer
    }
    return {
        "paths": {"db": str(workspace / "inspection.db")},
        "recorder": {"ffmpeg": "evaluation-unused"},
        "compliance": {
            "mode": "shadow",
            "wordlist": {
                "spreadsheet_token": "evaluation-shadow",
                "sync_seconds": 300,
            },
            "audio": {
                "out_dir": str(workspace / "audio"),
                "segment_seconds": 120,
                "overlap_seconds": 30,
                "holdback_seconds": 15,
            },
            "recognizer": allowed_recognizer,
            "delivery": {"recipient_chat_id": ""},
        },
    }


def _activate_evaluation_wordlist(
    store: object, labels: tuple[Label, ...], *, now: float,
) -> WordlistSnapshot:
    canonical: dict[str, str] = {}
    for label in labels:
        normalized = normalize_term(label.term)
        canonical.setdefault(normalized, label.term)
    entries = tuple(
        WordEntry(raw=canonical[term], normalized=term)
        for term in sorted(canonical)
    )
    encoded = json.dumps(
        [(entry.raw, entry.normalized) for entry in entries],
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    store.force_wordlist_sync()  # type: ignore[attr-defined]
    claim_token = store.claim_wordlist_sync(  # type: ignore[attr-defined]
        float(now), float(now) + 300
    )
    if claim_token is None:
        raise ReplayEvaluationIncomplete("durable replay wordlist claim failed")
    snapshot = store.activate_wordlist(  # type: ignore[attr-defined]
        source_hash=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        entries=entries,
        checked_at=float(now),
        next_sync_at=float(now) + 300,
        claim_token=claim_token,
    )
    return snapshot


@dataclass(frozen=True)
class _ReplayResult:
    predictions: tuple[Prediction, ...]
    event_keys: tuple[str, ...]
    delivery_keys: tuple[str, ...]
    job_completions: int
    max_queued_job_age_seconds: float


def _sample_for_hit(
    samples: tuple[ReplaySample, ...], live_id: str, hit_ms: int,
) -> ReplaySample:
    candidates = tuple(
        sample for sample in samples
        if sample.live_id == live_id
        and sample.origin_ms <= hit_ms < sample.origin_ms + sample.duration_ms
    )
    if len(candidates) != 1:
        raise ReplayEvaluationIncomplete(
            "durable event cannot be mapped to one replay sample"
        )
    return candidates[0]


def _run_replay_session(
    samples: tuple[ReplaySample, ...],
    *,
    labels: tuple[Label, ...],
    config: dict[str, object],
    workspace: Path,
    recognizer: object,
    sender_audit: _SenderAudit,
    monotonic: Callable[[], float],
    current_store: list[object | None],
    current_now: list[float],
) -> _ReplayResult:
    from .config import ComplianceSettings
    from .events import EventService
    from .listener import AudioJobPlanner, ComplianceListener
    from .notifier import ComplianceNotifier
    from .store import ComplianceStore

    safe_config = _evaluation_config(config, workspace)
    settings = ComplianceSettings.from_config(safe_config)
    store = ComplianceStore(settings.db_path)
    current_store[0] = store
    source = _SequentialReplaySource(samples, workspace / "replay")
    ready_at: dict[str, int] = {}
    job_ready_at: dict[str, int] = {}
    worker_available_ms = 0
    max_queued_age_ms = 0
    try:
        current_now[0] = source.clock_seconds()
        snapshot = _activate_evaluation_wordlist(
            store, labels, now=current_now[0]
        )
        notifier = ComplianceNotifier(
            store,
            sender=sender_audit,
            status_reader=lambda _key: "",
            clock=lambda: current_now[0],
            config=safe_config,
        )
        listener = ComplianceListener(
            config_loader=lambda: safe_config,
            store=store,
            notifier=notifier,
            wordlists=_StaticWordlists(snapshot),
            recognizer=recognizer,
            audio=source,
            jobs=AudioJobPlanner(store),
            events=EventService(
                store,
                now_ms=lambda: int(current_now[0] * 1000),
            ),
            replay_manifest=source.mode_marker,
            clock=lambda: current_now[0],
            monotonic=monotonic,
            sleep=lambda _seconds: None,
        )
        drained = False
        for _attempt in range(max(20, len(samples) * 20)):
            current_now[0] = source.clock_seconds()
            started = float(monotonic())
            listener.cycle(current_now[0])
            elapsed_ms = max(
                0, math.ceil((float(monotonic()) - started) * 1000)
            )
            for row in store.conn.execute(
                "SELECT job_key,capture_end_ms FROM compliance_audio_jobs "
                "WHERE status='committed' ORDER BY commit_end_ms,job_key"
            ):
                job_key = str(row["job_key"])
                if job_key in job_ready_at:
                    continue
                capture_end_ms = int(row["capture_end_ms"])
                max_queued_age_ms = max(
                    max_queued_age_ms,
                    max(0, worker_available_ms - capture_end_ms),
                )
                worker_available_ms = (
                    max(capture_end_ms, worker_available_ms) + elapsed_ms
                )
                job_ready_at[job_key] = worker_available_ms
            for row in store.conn.execute(
                "SELECT e.event_key,e.job_key "
                "FROM compliance_events e JOIN compliance_audio_jobs j "
                "ON j.job_key=e.job_key"
            ):
                event_job = str(row["job_key"])
                if event_job in job_ready_at:
                    ready_at.setdefault(
                        str(row["event_key"]), job_ready_at[event_job]
                    )
            rows = tuple(store.conn.execute(
                "SELECT status FROM compliance_audio_jobs ORDER BY job_key"
            ))
            statuses = tuple(str(row["status"]) for row in rows)
            if any(status in {
                "blocked_timeline", "needs_attention", "retry_wait"
            } for status in statuses):
                break
            if (
                source.exhausted
                and len(statuses) == len(samples)
                and all(status == "committed" for status in statuses)
            ):
                drained = True
                break
        if not drained:
            raise ReplayEvaluationIncomplete(
                "durable replay did not reach a committed terminal state"
            )
        listener.shutdown(current_now[0])
        job_rows = tuple(store.conn.execute(
            "SELECT status,result_json FROM compliance_audio_jobs"
        ))
        if (
            not job_rows
            or any(
                str(row["status"]) != "committed"
                or not str(row["result_json"])
                for row in job_rows
            )
        ):
            raise ReplayEvaluationIncomplete(
                "durable replay result is incomplete"
            )
        event_rows = tuple(store.conn.execute(
            "SELECT event_key,delivery_key,live_id,normalized_term,hit_start_ms,"
            "creation_mode,target_hash,delivery_status,delivery_attempts,"
            "sent_at_ms,payload_json,payload_hash "
            "FROM compliance_events ORDER BY hit_start_ms,event_key"
        ))
        if any(
            str(row["creation_mode"]) != "shadow"
            or str(row["target_hash"]) != ""
            or str(row["delivery_status"]) != "shadow"
            or int(row["delivery_attempts"]) != 0
            or int(row["sent_at_ms"]) != 0
            or not str(row["payload_json"])
            or not str(row["payload_hash"])
            for row in event_rows
        ):
            raise ReplayEvaluationIncomplete(
                "durable replay violated shadow delivery authority"
            )
        predictions: list[Prediction] = []
        for row in event_rows:
            event_key = str(row["event_key"])
            hit_ms = int(row["hit_start_ms"])
            sample = _sample_for_hit(
                samples, str(row["live_id"]), hit_ms
            )
            event_ready = ready_at.get(event_key)
            if event_ready is None:
                raise ReplayEvaluationIncomplete(
                    "durable event readiness was not observed"
                )
            predictions.append(Prediction(
                sample_id=sample.sample_id,
                term=str(row["normalized_term"]),
                hit_ms=hit_ms,
                ready_at_ms=event_ready,
            ))
        return _ReplayResult(
            predictions=tuple(predictions),
            event_keys=tuple(str(row["event_key"]) for row in event_rows),
            delivery_keys=tuple(
                str(row["delivery_key"]) for row in event_rows
            ),
            job_completions=len(job_rows),
            max_queued_job_age_seconds=max_queued_age_ms / 1000,
        )
    finally:
        current_store[0] = None
        store.close()


def run_replay_evaluation(
    manifest: Path,
    labels_path: Path,
    *,
    config: dict[str, object],
    workspace: Path,
    recognizer: object | None = None,
    sender: Callable[..., object] | None = None,
    tolerance_ms: int = 2_000,
    full_day_requested: bool = False,
    evidence_provider: object | None = None,
    resource_sampler: ProcessResourceSampler | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> EvaluationReport:
    if not isinstance(config, dict):
        raise TypeError("evaluation config must be a mapping")
    samples = load_replay_samples(Path(manifest))
    labels = load_labels(Path(labels_path), samples)
    root = Path(workspace)
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise ValueError("evaluation workspace is invalid")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        root.chmod(0o700)
    if any(root.iterdir()):
        raise ValueError("evaluation workspace must be empty")

    evidence_token: object | None = None
    evidence_start_failed = False
    if full_day_requested:
        if evidence_provider is None:
            evidence_start_failed = True
        else:
            try:
                evidence_token = evidence_provider.begin(samples)  # type: ignore[attr-defined]
            except Exception:
                evidence_start_failed = True

    if recognizer is None:
        from .config import ComplianceSettings
        from .recognizer import SeacoParaformerRecognizer

        default_config = _evaluation_config(config, root)
        recognizer = SeacoParaformerRecognizer(
            ComplianceSettings.from_config(default_config)
        )

    current_store: list[object | None] = [None]
    current_now = [0.0]

    def queued_age() -> float:
        store = current_store[0]
        if store is None:
            return 0.0
        age = store.oldest_actionable_audio_job_age(  # type: ignore[attr-defined]
            current_now[0]
        )
        return 0.0 if age is None else max(0.0, float(age))

    monitor: ResourceMonitor | None = None
    monitor_thread: threading.Thread | None = None
    if resource_sampler is not None:
        monitor = ResourceMonitor(
            resource_sampler, queued_age_provider=queued_age
        )
        monitor_thread = threading.Thread(
            target=monitor.run,
            name="compliance-evaluation-resources",
        )
        monitor_thread.start()

    sender_audit = _SenderAudit(sender)
    result: _ReplayResult | None = None
    try:
        result = _run_replay_session(
            samples,
            labels=labels,
            config=config,
            workspace=root,
            recognizer=recognizer,
            sender_audit=sender_audit,
            monotonic=monotonic,
            current_store=current_store,
            current_now=current_now,
        )
    finally:
        if monitor is not None and monitor_thread is not None:
            monitor.stop()
            monitor_thread.join(timeout=5.0)
            if monitor_thread.is_alive():
                raise ReplayEvaluationIncomplete(
                    "resource monitor did not stop"
                )

    if result is None:
        raise ReplayEvaluationIncomplete("durable replay result is missing")
    predictions = tuple(result.predictions)
    report = evaluate(labels, predictions, tolerance_ms=tolerance_ms)
    event_keys = result.event_keys
    delivery_keys = result.delivery_keys
    durable_duplicates = max(
        len(event_keys) - len(set(event_keys)),
        len(delivery_keys) - len(set(delivery_keys)),
    )
    observations = None if monitor is None else monitor.summary()
    if observations is not None:
        observations = replace(
            observations,
            max_queued_job_age_seconds=max(
                observations.max_queued_job_age_seconds,
                result.max_queued_job_age_seconds,
            ),
        )
    processed_sample_count = len(samples)
    processed_audio_ms = sum(sample.duration_ms for sample in samples)
    processed_job_completions = result.job_completions
    resource_evidence: ResourceEvidence | None = None
    evidence_finish_failed = evidence_start_failed
    if full_day_requested and not evidence_start_failed:
        try:
            resource_evidence = evidence_provider.finish(  # type: ignore[attr-defined]
                evidence_token,
                samples,
                processed_sample_count=processed_sample_count,
                processed_audio_ms=processed_audio_ms,
                processed_job_completions=processed_job_completions,
            )
            if not isinstance(resource_evidence, ResourceEvidence):
                raise TypeError
        except Exception:
            evidence_finish_failed = True
    evidence_matches = bool(
        resource_evidence is not None
        and resource_evidence.matches_run(
            processed_sample_count=processed_sample_count,
            processed_audio_ms=processed_audio_ms,
            processed_job_completions=processed_job_completions,
        )
    )
    regression = (
        True if evidence_finish_failed else (
            None if resource_evidence is None
            else (
                not evidence_matches
                or resource_evidence.resource_regression()
            )
        )
    )
    if observations is not None and not observations.sampling_complete:
        regression = True
    full_day = bool(
        full_day_requested
        and resource_evidence is not None
        and resource_evidence.full_day_covered(
            samples,
            processed_sample_count=processed_sample_count,
            processed_audio_ms=processed_audio_ms,
            processed_job_completions=processed_job_completions,
        )
    )
    return replace(
        report,
        duplicate_events=report.duplicate_events + durable_duplicates,
        real_messages_sent=sender_audit.calls,
        full_day=full_day,
        resource_regression=regression,
        resource_observations=observations,
    )


def public_report(report: EvaluationReport) -> dict[str, object]:
    resource = report.resource_observations
    return {
        "aggregate": {
            "true_positive": report.true_positive,
            "false_positive": report.false_positive,
            "false_negative": report.false_negative,
            "recall": report.recall,
            "precision": report.precision,
            "zero_hit_term_count": len(report.zero_hit_terms),
            "duplicate_events": report.duplicate_events,
            "real_messages_sent": report.real_messages_sent,
            "full_day": report.full_day,
            "gate_passed": report.passes_shadow_gate(),
        },
        "latency": {
            "p95_ready_latency_ms": (
                report.p95_ready_latency_ms
                if math.isfinite(report.p95_ready_latency_ms) else None
            ),
        },
        "resource": {
            "resource_regression": report.resource_regression,
            "sample_count": 0 if resource is None else resource.sample_count,
            "max_listener_cpu_percent": (
                0.0 if resource is None
                else resource.max_listener_cpu_percent
            ),
            "max_listener_rss_bytes": (
                0 if resource is None else resource.max_listener_rss_bytes
            ),
            "max_child_cpu_percent": (
                0.0 if resource is None else resource.max_child_cpu_percent
            ),
            "max_child_rss_bytes": (
                0 if resource is None else resource.max_child_rss_bytes
            ),
            "disk_read_bytes": (
                0 if resource is None else resource.disk_read_bytes
            ),
            "disk_write_bytes": (
                0 if resource is None else resource.disk_write_bytes
            ),
            "max_queued_job_age_seconds": (
                0.0 if resource is None
                else resource.max_queued_job_age_seconds
            ),
            "sampling_complete": (
                False if resource is None else resource.sampling_complete
            ),
        },
    }


def _private_term_row(item: PerTermCounts) -> dict[str, object]:
    return {
        "term": item.normalized_term,
        "expected": item.expected,
        "predicted": item.predicted,
        "true_positive": item.true_positive,
        "false_positive": item.false_positive,
        "false_negative": item.false_negative,
        "recall": item.recall,
        "precision": item.precision,
    }


def private_report_body(report: EvaluationReport) -> dict[str, object]:
    document = public_report(report)
    aggregate = document["aggregate"]
    if not isinstance(aggregate, dict):  # pragma: no cover - local invariant
        raise TypeError("evaluation aggregate is invalid")
    aggregate["zero_hit_terms"] = list(report.zero_hit_terms)
    document["per_term"] = [_private_term_row(item) for item in report.per_term]
    return document


def write_private_report(path: Path, report: EvaluationReport) -> Path:
    if not isinstance(report, EvaluationReport):
        raise TypeError("report must be EvaluationReport")
    encoded = json.dumps(
        private_report_body(report),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ) + "\n"
    return _atomic_private_write(Path(path), encoded.encode("utf-8"))


__all__ = [
    "EvaluationReport",
    "Label",
    "PerTermCounts",
    "Prediction",
    "ProcessResourceSampler",
    "ProductionEvidenceProvider",
    "ReplayEvaluationIncomplete",
    "ReplaySample",
    "ResourceEvidence",
    "ResourceMonitor",
    "ResourceSummary",
    "evaluate",
    "load_labels",
    "load_replay_samples",
    "private_report_body",
    "public_report",
    "run_replay_evaluation",
    "write_private_replay_manifest",
    "write_private_report",
]
