from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
from typing import Callable, Iterator, Protocol
import wave

from .config import ComplianceSettings
from .models import RecognizedSentence, RecognizedUnit, TranscriptBatch
from .process_control import (
    WINDOWS_NEW_PROCESS_GROUP,
    coerce_process_identity,
    read_process_identity,
)
from .wordlist import normalize_term


MODEL_ALIAS = "paraformer-zh"
MODEL_REVISION = "v2.0.4"
WORKER_MODULE = "app.compliance.recognizer_worker"
# SeACo Paraformer emits timestamp bounds on a 20 ms frame grid.  A WAV can
# end between those frame boundaries, so only the final end bound may round
# forward by one model frame before the strict contract is enforced.
MAX_TERMINAL_TIMESTAMP_OVERSHOOT_MS = 20


class ModelWorkerOwner(Protocol):
    def mark_model_worker_started(self, pid: int, start_token: str) -> bool: ...

    def clear_model_worker(self, pid: int, start_token: str) -> bool: ...


def _is_worker_identity(identity: object, start_token: str) -> bool:
    try:
        candidate = coerce_process_identity(identity)
    except (TypeError, ValueError):
        return False
    if candidate is None or candidate.start_token != start_token:
        return False
    return any(
        candidate.argv[index:index + 2] == ("-m", WORKER_MODULE)
        for index in range(len(candidate.argv) - 1)
    )


def _kill_and_reap_worker(process) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=1.0)


def _clear_worker_after_confirmed_exit(
    process,
    *,
    start_token: str,
    worker_owner: ModelWorkerOwner,
    process_identity_reader: Callable[[int], object],
) -> None:
    try:
        current = process_identity_reader(int(process.pid))
    except Exception:
        return
    try:
        candidate = coerce_process_identity(current)
    except (TypeError, ValueError):
        return
    if candidate is not None and candidate.start_token == start_token:
        return
    try:
        worker_owner.clear_model_worker(int(process.pid), start_token)
    except Exception:
        pass


def _run_bounded_worker(
    payload: dict[str, object],
    *,
    timeout_seconds: float,
    popen=subprocess.Popen,
    worker_owner: ModelWorkerOwner,
    process_identity_reader: Callable[[int], object] | None = None,
    platform_name: str = os.name,
) -> TranscriptBatch:
    timeout = float(timeout_seconds)
    if timeout <= 0:
        raise TimeoutError("compliance recognition deadline exhausted")
    command = [sys.executable, "-m", WORKER_MODULE]
    spawn_options: dict[str, object] = {"close_fds": True}
    if platform_name == "nt":
        spawn_options["creationflags"] = WINDOWS_NEW_PROCESS_GROUP
    else:
        spawn_options["start_new_session"] = True
    process = popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        **spawn_options,
    )
    if process_identity_reader is None:
        process_identity_reader = read_process_identity
    try:
        identity = process_identity_reader(int(process.pid))
    except Exception:
        identity = None
    try:
        candidate = coerce_process_identity(identity)
    except (TypeError, ValueError):
        candidate = None
    start_token = "" if candidate is None else candidate.start_token
    if not start_token or not _is_worker_identity(candidate, start_token):
        _kill_and_reap_worker(process)
        raise RuntimeError("bounded worker ownership unavailable")
    registered = False
    try:
        registered = worker_owner.mark_model_worker_started(
            int(process.pid), start_token,
        ) is True
    except Exception:
        registered = False
    if not registered:
        _kill_and_reap_worker(process)
        _clear_worker_after_confirmed_exit(
            process,
            start_token=start_token,
            worker_owner=worker_owner,
            process_identity_reader=process_identity_reader,
        )
        raise RuntimeError("bounded worker ownership unavailable")
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ) + "\n"
    try:
        stdout, _stderr = process.communicate(encoded, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_and_reap_worker(process)
        raise TimeoutError("compliance recognition deadline exceeded") from None
    finally:
        if registered:
            _clear_worker_after_confirmed_exit(
                process,
                start_token=start_token,
                worker_owner=worker_owner,
                process_identity_reader=process_identity_reader,
            )
    if process.returncode != 0:
        raise RuntimeError("bounded compliance recognition failed")
    from .events import decode_transcript_batch

    return decode_transcript_batch(stdout.strip())


class TimestampContractError(ValueError):
    """FunASR output cannot be mapped to real, ordered millisecond bounds."""


@dataclass(frozen=True)
class GenerateAudit:
    call_count: int
    hotword_fingerprint: str


class _HotwordContentLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno != logging.INFO:
            return True
        pathname = record.pathname
        if (
            not isinstance(pathname, str)
            or not pathname.replace("\\", "/").endswith(
                "/funasr/models/seaco_paraformer/model.py")
            or record.funcName != "generate_hotwords_list"
        ):
            return True
        try:
            message = record.getMessage()
        except Exception:
            return True
        direct_content = (
            message.startswith("Hotword list: [") and message.endswith("].")
        )
        file_content = (
            message.startswith("Initialized hotword list from file: ")
            and ", hotword list: [" in message
            and message.endswith("].")
        )
        return not (direct_content or file_content)


@contextmanager
def _suppress_hotword_content_logs() -> Iterator[None]:
    root = logging.getLogger()
    existing_handlers = tuple(root.handlers)
    content_filter = _HotwordContentLogFilter()
    filter_owners: tuple[logging.Filterer, ...] = (root, *existing_handlers)
    for owner in filter_owners:
        owner.addFilter(content_filter)
    try:
        yield
    finally:
        for owner in reversed(filter_owners):
            owner.removeFilter(content_filter)


def wav_duration_ms(path: Path) -> int:
    with wave.open(str(path), "rb") as handle:
        frame_rate = handle.getframerate()
        frame_count = handle.getnframes()
    if frame_rate <= 0:
        raise ValueError("WAV frame rate must be positive")
    duration_ms = round(frame_count * 1000 / frame_rate)
    if duration_ms <= 0:
        raise ValueError("WAV duration must be positive")
    return duration_ms


def validate_transcript_batch(
    batch: TranscriptBatch, audio_duration_ms: int,
) -> None:
    if type(audio_duration_ms) is not int or audio_duration_ms <= 0:
        raise TimestampContractError("audio duration is invalid")
    if not isinstance(batch, TranscriptBatch):
        raise TimestampContractError("transcript batch is invalid")
    previous_start = -1
    previous_end = -1
    for sentence in batch.sentences:
        if (
            not isinstance(sentence, RecognizedSentence)
            or not isinstance(sentence.text, str)
            or not sentence.text.strip()
            or not sentence.units
        ):
            raise TimestampContractError("recognized sentence is invalid")
        for unit in sentence.units:
            if (
                not isinstance(unit, RecognizedUnit)
                or not isinstance(unit.text, str)
                or not unit.text
                or type(unit.start_ms) is not int
                or type(unit.end_ms) is not int
                or unit.start_ms < 0
                or unit.end_ms < unit.start_ms
                or unit.start_ms < previous_start
                or unit.end_ms < previous_end
                or unit.end_ms > audio_duration_ms
            ):
                raise TimestampContractError(
                    "timestamp order or range is invalid")
            previous_start = unit.start_ms
            previous_end = unit.end_ms


def _canonicalize_terminal_timestamp(
        batch: TranscriptBatch, audio_duration_ms: int) -> TranscriptBatch:
    """Clamp only SeACo's one-frame terminal rounding to physical audio.

    No start time, interior token, or larger overshoot is adjusted.  Strict
    validation below remains the authority for every other timestamp.
    """
    if type(audio_duration_ms) is not int or audio_duration_ms <= 0:
        return batch
    last_sentence = next((
        index for index in range(len(batch.sentences) - 1, -1, -1)
        if batch.sentences[index].units
    ), None)
    if last_sentence is None:
        return batch
    last_unit = len(batch.sentences[last_sentence].units) - 1
    terminal = batch.sentences[last_sentence].units[last_unit]
    overshoot = terminal.end_ms - audio_duration_ms
    if not (
        0 < overshoot <= MAX_TERMINAL_TIMESTAMP_OVERSHOOT_MS
        and terminal.start_ms <= audio_duration_ms
    ):
        return batch
    sentences = list(batch.sentences)
    units = list(sentences[last_sentence].units)
    units[last_unit] = RecognizedUnit(
        terminal.text, terminal.start_ms, audio_duration_ms)
    sentences[last_sentence] = RecognizedSentence(
        sentences[last_sentence].text, tuple(units))
    return TranscriptBatch(batch.model_version, tuple(sentences))


def parse_funasr_result(
    result: object, model_version: str, audio_duration_ms: int,
) -> TranscriptBatch:
    payloads = result if isinstance(result, list) else [result]
    sentences: list[RecognizedSentence] = []
    for payload in payloads:
        if not isinstance(payload, dict):
            raise TimestampContractError("recognizer payload is not an object")
        infos = payload.get("sentence_info")
        payload_text = payload.get("text")
        if payload_text is not None and not isinstance(payload_text, str):
            raise TimestampContractError("recognizer text is not a string")
        transcript_text = (payload_text or "").strip()
        if transcript_text and (not isinstance(infos, list) or not infos):
            raise TimestampContractError(
                "non-empty transcript has no sentence timestamps")
        if infos is not None and not isinstance(infos, list):
            raise TimestampContractError("sentence timestamps are invalid")
        outer_raw_text = payload.get("raw_text")
        outer_timestamps = payload.get("timestamp")
        if outer_raw_text is not None or outer_timestamps is not None:
            if (
                not isinstance(outer_raw_text, str)
                or not isinstance(outer_timestamps, list)
            ):
                raise TimestampContractError("outer timestamp surface is invalid")
            outer_tokens = outer_raw_text.split()
            if (
                not outer_tokens
                or len(outer_tokens) != len(outer_timestamps)
                or normalize_term("".join(outer_tokens))
                != normalize_term(transcript_text)
            ):
                raise TimestampContractError("outer timestamp surface is unaligned")
            cursor = 0
            for info in infos or []:
                if not isinstance(info, dict):
                    raise TimestampContractError(
                        "sentence timestamp item is invalid")
                display_value = info.get("text")
                raw_text_value = info.get("raw_text")
                timestamps = info.get("timestamp")
                if (
                    not isinstance(display_value, str)
                    or not display_value.strip()
                    or not isinstance(raw_text_value, str)
                    or not isinstance(timestamps, list)
                    or not timestamps
                ):
                    raise TimestampContractError("sentence timestamp item is invalid")
                end = cursor + len(timestamps)
                if end > len(outer_tokens) or timestamps != outer_timestamps[cursor:end]:
                    raise TimestampContractError("outer timestamp boundary is invalid")
                units: list[RecognizedUnit] = []
                for token, bounds in zip(outer_tokens[cursor:end], timestamps):
                    if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
                        raise TimestampContractError("timestamp bounds are invalid")
                    start_value, end_value = bounds
                    if type(start_value) is not int or type(end_value) is not int:
                        raise TimestampContractError(
                            "timestamp bounds are not integers")
                    units.append(RecognizedUnit(token, start_value, end_value))
                raw_sentence = "".join(outer_tokens[cursor:end])
                if not raw_sentence or not normalize_term(raw_sentence):
                    raise TimestampContractError("outer timestamp surface is invalid")
                display = display_value.strip()
                sentence_text = (
                    display
                    if normalize_term(display) == normalize_term(raw_sentence)
                    else raw_sentence
                )
                sentences.append(RecognizedSentence(sentence_text, tuple(units)))
                cursor = end
            if cursor != len(outer_tokens):
                raise TimestampContractError("outer timestamp boundary is incomplete")
            continue
        for info in infos or []:
            if not isinstance(info, dict):
                raise TimestampContractError(
                    "sentence timestamp item is invalid")
            display_value = info.get("text")
            raw_text_value = info.get("raw_text")
            if not isinstance(display_value, str):
                raise TimestampContractError("sentence text is not a string")
            if not isinstance(raw_text_value, str):
                raise TimestampContractError("raw text is not a string")
            display = display_value.strip()
            tokens = raw_text_value.split()
            timestamps = info.get("timestamp")
            if (
                not display
                or not tokens
                or not isinstance(timestamps, list)
                or len(tokens) != len(timestamps)
            ):
                raise TimestampContractError(
                    "token and timestamp counts differ")
            units: list[RecognizedUnit] = []
            for token, bounds in zip(tokens, timestamps):
                if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
                    raise TimestampContractError("timestamp bounds are invalid")
                start_value, end_value = bounds
                if type(start_value) is not int or type(end_value) is not int:
                    raise TimestampContractError(
                        "timestamp bounds are not integers")
                start_ms, end_ms = start_value, end_value
                units.append(RecognizedUnit(token, start_ms, end_ms))
            sentences.append(RecognizedSentence(display, tuple(units)))
    batch = _canonicalize_terminal_timestamp(
        TranscriptBatch(model_version, tuple(sentences)), audio_duration_ms)
    validate_transcript_batch(batch, audio_duration_ms)
    return batch


class SeacoParaformerRecognizer:
    def __init__(
        self,
        settings: ComplianceSettings,
        *,
        model_factory: Callable[..., object] | None = None,
        bounded_runner: Callable[..., TranscriptBatch] | None = None,
        worker_owner: ModelWorkerOwner | None = None,
    ) -> None:
        if (
            settings.recognizer_model != MODEL_ALIAS
            or settings.recognizer_revision != MODEL_REVISION
        ):
            raise ValueError(
                "compliance recognizer must use the pinned paraformer model")
        self.settings = settings
        self._factory = model_factory
        self._bounded_runner = bounded_runner
        self._worker_owner = worker_owner
        self._model: object | None = None
        self._lock = threading.RLock()
        self._generate_call_count = 0
        self._hotword_fingerprint = ""

    @property
    def model_version(self) -> str:
        funasr_version = importlib.metadata.version("funasr")
        return (
            f"{MODEL_ALIAS}@{MODEL_REVISION}/funasr-{funasr_version}"
        )

    @property
    def generate_audit(self) -> GenerateAudit:
        with self._lock:
            return GenerateAudit(
                self._generate_call_count, self._hotword_fingerprint)

    def can_complete_within(self, remaining_seconds: float) -> bool:
        try:
            return float(remaining_seconds) > 2.0
        except (TypeError, ValueError):
            return False

    def transcribe_bounded(
        self,
        path: Path,
        hotwords: tuple[str, ...],
        *,
        timeout_seconds: float,
    ) -> TranscriptBatch:
        if self._bounded_runner is not None:
            return self._bounded_runner(
                Path(path), tuple(hotwords), float(timeout_seconds)
            )
        if self._worker_owner is None:
            raise RuntimeError("bounded worker ownership unavailable")
        payload = {
            "audio_path": str(Path(path)),
            "hotwords": list(hotwords),
            "recognizer_device": self.settings.recognizer_device,
            "recognizer_model": self.settings.recognizer_model,
            "recognizer_revision": self.settings.recognizer_revision,
            "vad_model": self.settings.vad_model,
            "punc_model": self.settings.punc_model,
        }
        return _run_bounded_worker(
            payload,
            timeout_seconds=max(0.0, float(timeout_seconds) - 0.5),
            worker_owner=self._worker_owner,
        )

    def _load(self) -> object:
        with self._lock:
            if self._model is None:
                factory = self._factory
                if factory is None:
                    from funasr import AutoModel

                    factory = AutoModel
                self._model = factory(
                    model=MODEL_ALIAS,
                    model_revision=MODEL_REVISION,
                    vad_model=self.settings.vad_model,
                    vad_model_revision=MODEL_REVISION,
                    punc_model=self.settings.punc_model,
                    punc_model_revision=MODEL_REVISION,
                    device=self.settings.recognizer_device,
                    disable_update=True,
                )
            return self._model

    def ensure_ready(self) -> str:
        self._load()
        return self.model_version

    def transcribe(
        self, path: Path, hotwords: tuple[str, ...],
    ) -> TranscriptBatch:
        if not hotwords or not any(str(term).strip() for term in hotwords):
            raise ValueError("active hotword collection is empty")
        hotword = " ".join(hotwords)
        with self._lock:
            model = self._load()
            generate = getattr(model, "generate")
            self._generate_call_count += 1
            self._hotword_fingerprint = hashlib.sha256(
                hotword.encode("utf-8")).hexdigest()
            with _suppress_hotword_content_logs():
                result = generate(
                    input=str(path),
                    hotword=hotword,
                    batch_size_s=300,
                    sentence_timestamp=True,
                    return_raw_text=True,
                    disable_pbar=True,
                )
        return parse_funasr_result(
            result,
            self.model_version,
            audio_duration_ms=wav_duration_ms(path),
        )
