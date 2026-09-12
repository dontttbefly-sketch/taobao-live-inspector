from __future__ import annotations

import hashlib
import json
import math
import ntpath
import os
import struct
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path
from typing import Protocol

from .config import ComplianceSettings
from .codec import canonical_json
from .audio_journal import (
    chunk_metadata,
    decode_legacy_journal,
    decode_observation_journal,
    offer_metadata,
    write_final_intent,
    write_observation_journal,
)
from .models import AudioJob, AudioOffer, ClosedAudioChunk
from .process_control import (
    GUARD_OWNER_NAME,
    ProcessIdentity,
    WINDOWS_NEW_PROCESS_GROUP,
    coerce_process_identity as _coerce_process_identity,
    read_process_identity,
    stop_owned_process as _stop_owned_process,
    terminate_owned_ffmpeg,
)


DEFAULT_USER_AGENT = "Mozilla/5.0"
DEFAULT_REFERER = "https://h5.m.taobao.com/"


class AudioTargetError(RuntimeError):
    """A fixed, privacy-safe live-target failure."""


class _AudioStartPreparationError(RuntimeError):
    """A retryable failure before any capture process owns the session."""


class CurrentLiveTargetProvider:
    def __init__(self, store, *, config_loader=None, client_factory=None) -> None:
        if config_loader is None:
            from ..config import load_config
            config_loader = load_config
        if client_factory is None:
            from ..recorder.mtop import shared_client
            client_factory = shared_client
        self.store = store
        self.config_loader = config_loader
        self.client_factory = client_factory

    def active_live_ids(self) -> tuple[str, ...]:
        return tuple(self.store.active_live_ids())

    def get(self):
        live_ids = self.active_live_ids()
        if not live_ids:
            return None
        if len(live_ids) != 1:
            raise AudioTargetError("audio_source_ambiguous")
        live_id = live_ids[0]
        persisted_reader = getattr(
            self.store, "active_recording_media_urls", None)
        if callable(persisted_reader):
            try:
                persisted = tuple(persisted_reader(live_id))
            except Exception:
                raise AudioTargetError("audio_source_state_failed") from None
            persisted_urls = tuple(
                url for url in persisted if isinstance(url, str) and url)
            if persisted_urls:
                return live_id, persisted_urls
        try:
            cfg = self.config_loader()
            result = self.client_factory(cfg).probe(live_id)
        except Exception:
            raise AudioTargetError("audio_probe_failed") from None
        if not isinstance(result, dict) or result.get("is_live") is not True:
            return None
        raw_urls = result.get("stream_urls")
        if not isinstance(raw_urls, (list, tuple)):
            return None
        urls = tuple(url for url in raw_urls if isinstance(url, str) and url)
        if not urls:
            return None
        return live_id, urls


class AudioSource(Protocol):
    def prepare_offer(
        self, *, wordlist_version_id: int, queued_at: float, final: bool,
        creation_mode: str, target_hash: str,
    ) -> None: ...

    def poll(self, now_ms: int) -> AudioOffer | tuple[ClosedAudioChunk, ...]: ...

    def finish(
        self, now_ms: int, *, timeout_seconds: float | None = None,
    ) -> AudioOffer | tuple[ClosedAudioChunk, ...]: ...

    def ack(self, offer_id: str) -> None: ...

    def release_offer(self, offer_id: str) -> None: ...


def select_audio_url(urls: tuple[str, ...]) -> str:
    ordered = tuple(dict.fromkeys(str(url) for url in urls if str(url)))
    hls = tuple(url for url in ordered if ".m3u8" in url.casefold())
    if hls:
        return hls[0]
    if ordered:
        return ordered[0]
    raise ValueError("live target has no stream URL")


def _wav_duration_ms(path: Path) -> int:
    try:
        with wave.open(str(path), "rb") as handle:
            if (
                handle.getnchannels() != 1
                or handle.getsampwidth() != 2
                or handle.getframerate() != 16_000
            ):
                raise ValueError("audio_chunk_format_invalid")
            frames = handle.getnframes()
            decoded = handle.readframes(frames)
    except (EOFError, OSError, wave.Error) as exc:
        raise ValueError("audio_chunk_invalid") from exc
    duration_ms = frames * 1000 // 16_000
    if duration_ms <= 0 or len(decoded) != frames * 2:
        raise ValueError("audio_chunk_empty")
    return duration_ms


def _observed_pcm_duration_ms(path: Path) -> int:
    """Read currently available PCM frames without trusting mutable sizes."""
    try:
        payload = Path(path).read_bytes()
    except OSError as exc:
        raise ValueError("audio_chunk_observation_failed") from exc
    if len(payload) < 12 or payload[:4] != b"RIFF" or payload[8:12] != b"WAVE":
        raise ValueError("audio_chunk_observation_incomplete")
    offset = 12
    pcm_format: tuple[int, int, int] | None = None
    while offset + 8 <= len(payload):
        chunk_id = payload[offset:offset + 4]
        declared_size = struct.unpack_from("<I", payload, offset + 4)[0]
        chunk_start = offset + 8
        if chunk_id == b"fmt ":
            if declared_size < 16 or chunk_start + 16 > len(payload):
                raise ValueError("audio_chunk_observation_incomplete")
            audio_format, channels, rate, _, block_align, bits = struct.unpack_from(
                "<HHIIHH", payload, chunk_start
            )
            if (
                audio_format != 1 or channels != 1 or rate != 16_000
                or block_align != 2 or bits != 16
            ):
                raise ValueError("audio_chunk_format_invalid")
            pcm_format = (rate, block_align, bits)
        elif chunk_id == b"data":
            if pcm_format is None:
                raise ValueError("audio_chunk_observation_incomplete")
            rate, block_align, _ = pcm_format
            available_bytes = len(payload) - chunk_start
            frames = available_bytes // block_align
            if frames <= 0:
                raise ValueError("audio_chunk_observation_incomplete")
            return frames * 1000 // rate
        if declared_size == 0xFFFFFFFF:
            raise ValueError("audio_chunk_observation_incomplete")
        next_offset = chunk_start + declared_size + (declared_size & 1)
        if next_offset > len(payload):
            raise ValueError("audio_chunk_observation_incomplete")
        offset = next_offset
    raise ValueError("audio_chunk_observation_incomplete")


class LiveAudioSource:
    _JOURNAL_NAME = ".compliance-observations.json"
    _FINAL_INTENT_NAME = ".compliance-final-intent.json"
    _GUARD_OWNER_NAME = GUARD_OWNER_NAME

    @staticmethod
    def _valid_segment_name(name: str) -> bool:
        return (
            isinstance(name, str)
            and name.startswith("segment_")
            and name.endswith(".wav")
            and len(name[8:-4]) >= 6
            and name[8:-4].isdigit()
        )

    @classmethod
    def _capture_segment_paths(cls, capture: Path) -> tuple[Path, ...]:
        if capture.is_symlink() or not capture.is_dir():
            raise ValueError("audio_capture_directory_invalid")
        paths: list[Path] = []
        for path in capture.iterdir():
            private_context = (
                path.name.startswith(".segment_")
                and path.name.endswith(".context.wav")
            )
            if private_context:
                if path.parent != capture or path.is_symlink() or not path.is_file():
                    raise ValueError("audio_segment_path_invalid")
                continue
            looks_like_segment = (
                path.name.startswith("segment_")
                or (
                    "segment_" in path.name
                    and path.name.endswith(".wav")
                )
            )
            if not looks_like_segment:
                continue
            if (
                not cls._valid_segment_name(path.name)
                or path.parent != capture
                or path.is_symlink()
                or not path.is_file()
            ):
                raise ValueError("audio_segment_path_invalid")
            paths.append(path)
        return tuple(sorted(paths, key=lambda item: item.name))

    def __init__(
        self,
        settings: ComplianceSettings,
        *,
        target_provider,
        popen=subprocess.Popen,
        store=None,
        user_agent: str = DEFAULT_USER_AGENT,
        referer: str = DEFAULT_REFERER,
        platform_name: str = os.name,
        process_stopper=None,
        command_observer=None,
        process_identity_reader=None,
        use_guard: bool | None = None,
        monotonic=time.monotonic,
        sleep=time.sleep,
    ) -> None:
        self.settings = settings
        self.target_provider = target_provider
        self._popen = popen
        self.store = store
        self.user_agent = user_agent
        self.referer = referer
        self.platform_name = platform_name
        self._command_observer = command_observer
        self._process_identity_reader = process_identity_reader
        self._use_guard = (
            popen is subprocess.Popen if use_guard is None else bool(use_guard)
        )
        self._monotonic = monotonic
        self._sleep = sleep
        self._process_stopper = process_stopper or (
            self._stop_guard_process if self._use_guard else
            lambda process: _stop_owned_process(
                process, platform_name=self.platform_name
            )
        )
        self.capture_dir = settings.audio_dir / "capture-pending"
        self.process = None
        self.live_id = ""
        self._emitted: set[str] = set()
        self._origin_ms: int | None = None
        self._known_durations: dict[str, int] = {}
        self._last_checked_closed_media_ms: int | None = None
        self._last_observed_at_ms: int | None = None
        self._last_observed_total_media_ms: int | None = None
        self._unsettled_wallclock_drift_ms = 0
        self._finalized_segment_names: set[str] = set()
        self._observed_continuity = "ok"
        self.health_issue = ""
        self._session_retired = True
        self._journals = self._load_observation_journals()
        self._remove_acknowledged_journals()
        self._recovered_offers = tuple(sorted(
            (
                offer for offers in self._journals.values()
                for offer in offers
            ),
            key=lambda item: (item.queued_at, item.offer_id),
        ))
        self._returned_offer_ids: set[str] = set()
        self._acked_offer_ids: set[str] = set()
        # Legacy injected sources remain convenient for hermetic unit tests;
        # the production guard path refuses capture until the listener freezes
        # a real durable wordlist/delivery authority.
        self._prepared_offer: tuple[int, float, bool, str, str] | None = (
            None if self._use_guard else (1, 0.0, False, "shadow", "")
        )
        self._session_authority: tuple[int, str, str] | None = None
        self._session_started_ms: int | None = None
        self._authority_change_pending = False
        self._final_intents_recovered = False
        self._guard_start_unconfirmed = False
        self._inherited_guard_owner: tuple[Path, int, str] | None = None
        self._discover_guard_owner()

    @staticmethod
    def _validate_authority(
        creation_mode: str, target_hash: str,
    ) -> None:
        valid = (
            creation_mode == "shadow" and target_hash == ""
        ) or (
            creation_mode == "live"
            and isinstance(target_hash, str)
            and len(target_hash) == 64
            and all(character in "0123456789abcdef" for character in target_hash)
        )
        if not valid:
            raise ValueError("audio offer authority invalid")

    def prepare_offer(
        self,
        *,
        wordlist_version_id: int,
        queued_at: float,
        final: bool,
        creation_mode: str,
        target_hash: str,
    ) -> None:
        if (
            isinstance(wordlist_version_id, bool)
            or not isinstance(wordlist_version_id, int)
            or wordlist_version_id <= 0
            or isinstance(queued_at, bool)
            or not isinstance(queued_at, (int, float))
            or not math.isfinite(float(queued_at))
            or float(queued_at) < 0
            or type(final) is not bool
        ):
            raise ValueError("audio offer boundary invalid")
        self._validate_authority(creation_mode, target_hash)
        requested = (
            wordlist_version_id, float(queued_at), final,
            creation_mode, target_hash,
        )
        requested_authority = (
            wordlist_version_id, creation_mode, target_hash,
        )
        if (
            not self._session_retired
            and self._session_authority is not None
            and requested_authority != self._session_authority
        ):
            version_id, old_mode, old_target = self._session_authority
            self._prepared_offer = (
                version_id, float(queued_at), True, old_mode, old_target,
            )
            self._authority_change_pending = True
            return
        self._prepared_offer = requested

    _chunk_metadata = staticmethod(chunk_metadata)
    _offer_metadata = staticmethod(offer_metadata)

    _decode_legacy_journal = staticmethod(decode_legacy_journal)

    _decode_journal = staticmethod(decode_observation_journal)

    def _load_observation_journals(
        self,
    ) -> dict[Path, tuple[AudioOffer, ...]]:
        root = self.settings.audio_dir
        if not root.exists():
            return {}
        try:
            if root.is_symlink() or not root.is_dir():
                raise ValueError
            result: dict[Path, tuple[AudioOffer, ...]] = {}
            for directory in sorted(root.iterdir()):
                if directory.is_symlink():
                    raise ValueError
                if not directory.is_dir():
                    continue
                journal = directory / self._JOURNAL_NAME
                if not journal.exists() and not journal.is_symlink():
                    continue
                if journal.is_symlink() or not journal.is_file():
                    raise ValueError
                result[journal] = self._decode_journal(journal, directory)
            return result
        except (OSError, ValueError):
            raise ValueError("audio_observation_journal_invalid") from None

    def _remove_acknowledged_journals(self) -> None:
        for journal, offers in tuple(self._journals.items()):
            if offers:
                continue
            try:
                journal.unlink(missing_ok=True)
                self._sync_directory(
                    journal.parent, platform_name=self.platform_name
                )
            except OSError:
                raise ValueError(
                    "audio_observation_journal_invalid"
                ) from None
            self._journals.pop(journal, None)

    @staticmethod
    def _sync_directory(directory: Path, *, platform_name: str = os.name) -> None:
        if platform_name == "nt":
            return
        descriptor = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def _decode_guard_owner(
        cls, owner: Path, capture: Path, *, platform_name: str,
    ) -> tuple[int, str]:
        try:
            if (
                owner.name != cls._GUARD_OWNER_NAME
                or owner.parent != capture
                or owner.is_symlink()
                or not owner.is_file()
                or (
                    platform_name != "nt"
                    and owner.stat().st_mode & 0o077
                )
            ):
                raise ValueError
            encoded = owner.read_text(encoding="utf-8")

            def reject_constant(_value: str) -> None:
                raise ValueError

            document = json.loads(encoded, parse_constant=reject_constant)
            expected_hash = hashlib.sha256(
                str(capture).encode("utf-8")
            ).hexdigest()
            if (
                not isinstance(document, dict)
                or set(document) != {
                    "marker_hash", "pid", "start_token", "version",
                }
                or document["version"] != 1
                or type(document["pid"]) is not int
                or document["pid"] <= 0
                or not isinstance(document["start_token"], str)
                or not document["start_token"]
                or "\0" in document["start_token"]
                or "\n" in document["start_token"]
                or document["marker_hash"] != expected_hash
                or canonical_json(document) != encoded
            ):
                raise ValueError
            return int(document["pid"]), str(document["start_token"])
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            raise ValueError("audio_guard_owner_invalid") from None

    @classmethod
    def _guard_command_for_capture(cls, capture: Path) -> list[str]:
        marker_hash = hashlib.sha256(
            str(capture).encode("utf-8")
        ).hexdigest()
        return [
            sys.executable, "-m", "app.compliance.ffmpeg_guard",
            "--marker-hash", marker_hash,
            "--ack-file", str(capture / ".compliance-guard-ready"),
            "--owner-file", str(capture / cls._GUARD_OWNER_NAME),
        ]

    def _read_process_identity(self, pid: int) -> ProcessIdentity | None:
        if self._process_identity_reader is not None:
            return _coerce_process_identity(self._process_identity_reader(pid))
        return read_process_identity(pid, platform_name=self.platform_name)

    def _guard_identity_matches(
        self, identity: ProcessIdentity, capture: Path, start_token: str,
    ) -> bool:
        return self._guard_identity_matches_for_platform(
            identity,
            capture,
            start_token,
            platform_name=self.platform_name,
        )

    @classmethod
    def _guard_identity_matches_for_platform(
        cls,
        identity: ProcessIdentity,
        capture: Path,
        start_token: str,
        *,
        platform_name: str,
    ) -> bool:
        if identity.start_token != start_token:
            return False
        actual = tuple(token.strip('"') for token in identity.argv)
        expected = tuple(cls._guard_command_for_capture(capture))
        if len(actual) != len(expected):
            return False
        if platform_name == "nt":
            normalize = lambda value: ntpath.normcase(ntpath.normpath(value))
            executable_matches = (
                lambda actual_value, expected_value:
                normalize(actual_value) == normalize(expected_value)
            )
        else:
            normalize = os.path.normpath

            def executable_matches(
                actual_value: str, expected_value: str,
            ) -> bool:
                actual_real = os.path.realpath(actual_value)
                expected_real = os.path.realpath(expected_value)
                if actual_real == expected_real:
                    return True
                if sys.platform != "darwin":
                    return False
                # Framework Python rewrites argv[0] in macOS process listings
                # from ``.../bin/python3.x`` to its paired app image.  Accept
                # only that exact image derived from this trusted interpreter.
                base = Path(getattr(
                    sys, "_base_executable", expected_value
                )).resolve()
                if expected_real != str(base):
                    return False
                framework_image = (
                    base.parent.parent
                    / "Resources"
                    / "Python.app"
                    / "Contents"
                    / "MacOS"
                    / "Python"
                )
                return (
                    framework_image.is_file()
                    and actual_real == os.path.realpath(framework_image)
                )
        return (
            executable_matches(actual[0], expected[0])
            and actual[1:6] == expected[1:6]
            and normalize(actual[6]) == normalize(expected[6])
            and actual[7] == expected[7]
            and normalize(actual[8]) == normalize(expected[8])
        )

    @classmethod
    def guard_owner_is_live(
        cls,
        pid: int,
        marker: Path,
        process_token: str,
        *,
        platform_name: str = os.name,
        identity_reader=None,
    ) -> bool:
        """Prove that SQLite, owner file, and the live guard are one identity."""
        try:
            process_id = int(pid)
            capture = Path(marker)
            if (
                process_id <= 0
                or not capture.is_absolute()
                or capture.is_symlink()
                or not capture.is_dir()
                or not isinstance(process_token, str)
                or not process_token
            ):
                return False
            owner_pid, owner_token = cls._decode_guard_owner(
                capture / cls._GUARD_OWNER_NAME,
                capture,
                platform_name=platform_name,
            )
            if owner_pid != process_id or owner_token != process_token:
                return False
            reader = identity_reader or (
                lambda candidate: read_process_identity(
                    candidate, platform_name=platform_name
                )
            )
            identity = _coerce_process_identity(reader(process_id))
            if identity is None:
                return False
            return cls._guard_identity_matches_for_platform(
                identity,
                capture,
                process_token,
                platform_name=platform_name,
            )
        except Exception:
            return False

    def _remove_guard_owner(self, capture: Path) -> bool:
        owner = capture / self._GUARD_OWNER_NAME
        try:
            if owner.is_symlink():
                raise OSError
            owner.unlink(missing_ok=True)
            self._sync_directory(capture, platform_name=self.platform_name)
            return True
        except OSError:
            self.health_issue = "audio_guard_owner_clear_failed"
            return False

    def _write_guard_owner(
        self, capture: Path, *, pid: int, start_token: str,
    ) -> Path:
        owner = capture / self._GUARD_OWNER_NAME
        document = {
            "marker_hash": hashlib.sha256(
                str(capture).encode("utf-8")
            ).hexdigest(),
            "pid": int(pid),
            "start_token": str(start_token),
            "version": 1,
        }
        try:
            self._write_final_intent(
                owner, document, platform_name=self.platform_name
            )
        except RuntimeError:
            raise RuntimeError("audio_guard_owner_write_failed") from None
        return owner

    def _discover_guard_owner(self) -> None:
        root = self.settings.audio_dir
        if not root.exists():
            return
        try:
            owners: list[tuple[Path, int, str]] = []
            for capture in sorted(root.iterdir()):
                if capture.is_symlink() or not capture.is_dir():
                    continue
                owner = capture / self._GUARD_OWNER_NAME
                if not owner.exists() and not owner.is_symlink():
                    continue
                pid, token = self._decode_guard_owner(
                    owner, capture, platform_name=self.platform_name
                )
                owners.append((owner, pid, token))
            if len(owners) > 1:
                raise ValueError
            if owners:
                self._inherited_guard_owner = owners[0]
                self._reconcile_inherited_guard_owner()
        except (OSError, ValueError):
            raise ValueError("audio_guard_owner_invalid") from None

    def _reconcile_inherited_guard_owner(self) -> bool:
        inherited = self._inherited_guard_owner
        if inherited is None:
            return True
        owner, pid, token = inherited
        try:
            identity = self._read_process_identity(pid)
        except Exception:
            self.health_issue = "audio_process_probe_failed"
            return False
        if identity is None or identity.start_token != token:
            if not self._remove_guard_owner(owner.parent):
                return False
            self._inherited_guard_owner = None
            return True
        if not self._guard_identity_matches(identity, owner.parent, token):
            self.health_issue = "audio_process_identity_invalid"
            return False
        self.health_issue = "audio_process_owned"
        return False

    @staticmethod
    def _replace_journal(
        source: Path, destination: Path, *, platform_name: str = os.name,
    ) -> None:
        if platform_name != "nt":
            os.replace(source, destination)
            return
        try:
            import ctypes

            move_file = ctypes.windll.kernel32.MoveFileExW
            move_file.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint)
            move_file.restype = ctypes.c_int
            replace_existing = 0x1
            write_through = 0x8
            if not move_file(
                str(source), str(destination), replace_existing | write_through
            ):
                raise OSError(ctypes.get_last_error(), "MoveFileExW failed")
        except AttributeError:
            # Tests may emulate Windows on another OS; production Windows has
            # MoveFileExW and never takes this compatibility branch.
            os.replace(source, destination)

    @classmethod
    def _write_observation_journal(
        cls,
        journal: Path,
        offers: tuple[AudioOffer, ...],
        *,
        platform_name: str = os.name,
    ) -> None:
        write_observation_journal(
            journal,
            offers,
            platform_name=platform_name,
            replace=cls._replace_journal,
            sync_directory=cls._sync_directory,
        )

    @classmethod
    def _write_final_intent(
        cls, path: Path, document: dict[str, object], *, platform_name: str,
    ) -> None:
        write_final_intent(
            path,
            document,
            platform_name=platform_name,
            replace=cls._replace_journal,
            sync_directory=cls._sync_directory,
        )

    @classmethod
    def _decode_final_intent(
        cls, path: Path, *, additional_emitted: set[str] | None = None,
    ) -> tuple[
        AudioOffer, tuple[ClosedAudioChunk, ...], tuple[Path, ...]
    ]:
        encoded = path.read_text(encoding="utf-8")
        document = json.loads(
            encoded,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
        common_keys = {
            "boundary_end_ms", "continuity", "emitted", "live_id",
            "origin_ms", "queued_at", "session_started_ms", "target_hash",
            "version", "wordlist_version_id", "creation_mode",
        }
        version = document.get("version") if isinstance(document, dict) else None
        expected_keys = (
            common_keys if version == 1
            else (common_keys - {"emitted"}) | {"segments"}
        )
        if (
            not isinstance(document, dict)
            or version not in {1, 2}
            or set(document) != expected_keys
            or canonical_json(document) != encoded
            or not isinstance(document["live_id"], str)
            or not document["live_id"]
            or type(document["origin_ms"]) is not int
            or document["origin_ms"] < 0
            or type(document["session_started_ms"]) is not int
            or document["session_started_ms"] < 0
            or type(document["boundary_end_ms"]) is not int
            or document["boundary_end_ms"] < document["session_started_ms"]
            or type(document["wordlist_version_id"]) is not int
            or document["wordlist_version_id"] <= 0
            or type(document["queued_at"]) not in {int, float}
            or not math.isfinite(float(document["queued_at"]))
            or float(document["queued_at"]) < 0
            or document["continuity"] not in {"ok", "invalid"}
        ):
            raise ValueError
        cls._validate_authority(
            document["creation_mode"], document["target_hash"]
        )
        capture = path.parent
        paths = cls._capture_segment_paths(capture)
        path_by_name = {item.name: item for item in paths}
        origin = int(document["origin_ms"])
        additional = set(additional_emitted or ())
        if any(not cls._valid_segment_name(name) for name in additional):
            raise ValueError

        segments: list[dict[str, object]] = []
        discarded_tails: list[Path] = []
        if version == 1:
            emitted_names = document["emitted"]
            if (
                not isinstance(emitted_names, list)
                or any(
                    not cls._valid_segment_name(name) for name in emitted_names
                )
                or len(set(emitted_names)) != len(emitted_names)
                or any(name not in path_by_name for name in emitted_names)
            ):
                raise ValueError
            elapsed = 0
            for item in paths:
                duration = _wav_duration_ms(item)
                segments.append({
                    "duration_ms": duration,
                    "emitted": item.name in emitted_names,
                    "name": item.name,
                    "start_ms": origin + elapsed,
                })
                elapsed += duration
        else:
            raw_segments = document["segments"]
            if not isinstance(raw_segments, list):
                raise ValueError
            expected_segment_keys = {
                "duration_ms", "emitted", "name", "start_ms",
            }
            prior_end = origin
            prior_name = ""
            for raw in raw_segments:
                if (
                    not isinstance(raw, dict)
                    or set(raw) != expected_segment_keys
                    or not cls._valid_segment_name(raw["name"])
                    or type(raw["duration_ms"]) is not int
                    or raw["duration_ms"] <= 0
                    or type(raw["start_ms"]) is not int
                    or raw["start_ms"] != prior_end
                    or type(raw["emitted"]) is not bool
                    or raw["name"] <= prior_name
                ):
                    raise ValueError
                segments.append(dict(raw))
                prior_name = str(raw["name"])
                prior_end = int(raw["start_ms"]) + int(raw["duration_ms"])

            known_names = {str(item["name"]) for item in segments}
            for segment in segments:
                name = str(segment["name"])
                item = path_by_name.get(name)
                if item is None:
                    if segment["emitted"] is not True:
                        raise ValueError
                    continue
                actual_duration = _wav_duration_ms(item)
                recorded_duration = int(segment["duration_ms"])
                if actual_duration != recorded_duration:
                    is_last = segment is segments[-1]
                    if (
                        not is_last
                        or segment["emitted"] is True
                        or actual_duration < recorded_duration
                    ):
                        raise ValueError
                    segment["duration_ms"] = actual_duration

            unknown_paths = tuple(
                item for item in paths if item.name not in known_names
            )
            prior_end = (
                int(segments[-1]["start_ms"])
                + int(segments[-1]["duration_ms"])
                if segments else origin
            )
            prior_name = str(segments[-1]["name"]) if segments else ""
            for index, item in enumerate(unknown_paths):
                if item.name <= prior_name:
                    raise ValueError
                try:
                    duration = _wav_duration_ms(item)
                except ValueError:
                    # A running recorder can leave only its newest segment
                    # partially written when its parent exits.  The manifest
                    # contains every finalized predecessor, so ignore this
                    # unverifiable tail rather than treating a normal crash
                    # handoff as a corrupt committed timeline.  An invalid
                    # earlier/ordered segment remains fail-closed.
                    if index != len(unknown_paths) - 1:
                        raise
                    discarded_tails.append(item)
                    break
                segments.append({
                    "duration_ms": duration,
                    "emitted": False,
                    "name": item.name,
                    "start_ms": prior_end,
                })
                prior_name = item.name
                prior_end += duration

        emitted = {
            str(item["name"]) for item in segments if item["emitted"] is True
        } | additional
        chunks: list[ClosedAudioChunk] = []
        for segment in segments:
            name = str(segment["name"])
            if name in emitted:
                continue
            item = path_by_name.get(name)
            if item is None:
                raise ValueError
            duration = int(segment["duration_ms"])
            start = int(segment["start_ms"])
            material = (
                f"{document['live_id']}\0{name}\0{start}\0{duration}"
            )
            chunks.append(ClosedAudioChunk(
                hashlib.sha256(material.encode("utf-8")).hexdigest(),
                str(document["live_id"]), item, start, start + duration,
                duration, document["continuity"], True,
            ))
        boundary_start = (
            chunks[0].capture_start_ms
            if chunks else int(document["session_started_ms"])
        )
        boundary_end = (
            chunks[-1].capture_end_ms
            if chunks else int(document["boundary_end_ms"])
        )
        boundary_id = hashlib.sha256(
            f"{document['live_id']}\0{boundary_start}\0{boundary_end}".encode()
        ).hexdigest()
        provisional = {
            "boundary_id": boundary_id,
            "boundary_end_ms": boundary_end,
            "boundary_start_ms": boundary_start,
            "chunks": [cls._chunk_metadata(chunk) for chunk in chunks],
            "creation_mode": document["creation_mode"],
            "final": True,
            "legacy_quarantine": False,
            "live_id": document["live_id"],
            "queued_at": document["queued_at"],
            "target_hash": document["target_hash"],
            "wordlist_version_id": document["wordlist_version_id"],
        }
        offer_id = hashlib.sha256(
            canonical_json(provisional).encode()
        ).hexdigest()
        offer = AudioOffer(
            offer_id, boundary_id, tuple(chunks),
            int(document["wordlist_version_id"]),
            float(document["queued_at"]), True,
            document["creation_mode"], document["target_hash"], False,
            str(document["live_id"]), boundary_start, boundary_end,
        )
        return offer, tuple(chunks), tuple(discarded_tails)

    @classmethod
    def _remove_recovered_unreadable_tail(
        cls, capture: Path, tail: Path, *, platform_name: str,
    ) -> None:
        if (
            capture.is_symlink()
            or not capture.is_dir()
            or tail.parent != capture
            or not cls._valid_segment_name(tail.name)
            or tail.is_symlink()
            or not tail.is_file()
        ):
            raise ValueError("audio_segment_path_invalid")
        if tail.resolve(strict=True).parent != capture.resolve(strict=True):
            raise ValueError("audio_segment_path_invalid")
        tail.unlink()
        cls._sync_directory(capture, platform_name=platform_name)

    def _recover_final_intents(self) -> None:
        root = self.settings.audio_dir
        if not root.exists():
            return
        try:
            for capture in sorted(root.iterdir()):
                intent = capture / self._FINAL_INTENT_NAME
                if not intent.exists() and not intent.is_symlink():
                    continue
                if capture.is_symlink() or intent.is_symlink() or not intent.is_file():
                    raise ValueError
                journal = capture / self._JOURNAL_NAME
                existing = self._journals.get(journal, ())
                already_offered = {
                    chunk.path.name for item in existing for chunk in item.chunks
                }
                offer, _chunks, discarded_tails = self._decode_final_intent(
                    intent, additional_emitted=already_offered
                )
                prior = next((
                    item for item in existing if item.offer_id == offer.offer_id
                ), None)
                if prior is not None and prior != offer:
                    raise ValueError
                staged = tuple(sorted(
                    {item.offer_id: item for item in (*existing, offer)}.values(),
                    key=lambda item: item.offer_id,
                ))
                self._write_observation_journal(
                    journal, staged, platform_name=self.platform_name
                )
                self._journals[journal] = staged
                for tail in discarded_tails:
                    self._remove_recovered_unreadable_tail(
                        capture, tail, platform_name=self.platform_name
                    )
                intent.unlink()
                self._sync_directory(capture, platform_name=self.platform_name)
        except RuntimeError as exc:
            # Journal persistence already exposes only a fixed, credential-
            # free error class.  Preserve it so the active owner is retained
            # and the caller can retry the exact durability boundary.
            if str(exc) == "audio_observation_journal_failed":
                raise
            raise ValueError("audio_final_intent_invalid") from None
        except Exception:
            raise ValueError("audio_final_intent_invalid") from None

    def recover_final_intents(self) -> None:
        if (
            self._inherited_guard_owner is not None
            and not self._reconcile_inherited_guard_owner()
        ):
            return
        if self._final_intents_recovered:
            return
        self._recover_final_intents()
        self._recovered_offers = tuple(sorted(
            (
                offer for offers in self._journals.values()
                for offer in offers
            ),
            key=lambda item: (item.queued_at, item.offer_id),
        ))
        self._final_intents_recovered = True

    def _stage_observations(
        self,
        chunks: tuple[ClosedAudioChunk, ...],
        *,
        force_final: bool = False,
        live_id: str = "",
        boundary_start_ms: int | None = None,
        boundary_end_ms: int | None = None,
    ) -> AudioOffer | None:
        if not chunks and not force_final:
            return None
        if self._prepared_offer is None:
            raise RuntimeError("audio_offer_authority_missing")
        version_id, queued_at, final, creation_mode, target_hash = (
            self._prepared_offer
        )
        final = bool(final or force_final)
        durable_live_id = chunks[0].live_id if chunks else str(live_id)
        durable_start = (
            chunks[0].capture_start_ms
            if chunks else int(boundary_start_ms or 0)
        )
        durable_end = (
            chunks[-1].capture_end_ms
            if chunks else int(boundary_end_ms or 0)
        )
        if (
            not durable_live_id
            or durable_start < 0
            or durable_end < durable_start
            or (bool(chunks) and durable_end <= durable_start)
        ):
            raise RuntimeError("audio_offer_boundary_missing")
        boundary_material = (
            f"{durable_live_id}\0{durable_start}\0{durable_end}"
        )
        boundary_id = hashlib.sha256(
            boundary_material.encode("utf-8")
        ).hexdigest()
        provisional = {
            "boundary_id": boundary_id,
            "chunks": [self._chunk_metadata(chunk) for chunk in chunks],
            "creation_mode": creation_mode,
            "final": final,
            "legacy_quarantine": False,
            "live_id": durable_live_id,
            "boundary_start_ms": durable_start,
            "boundary_end_ms": durable_end,
            "queued_at": queued_at,
            "target_hash": target_hash,
            "wordlist_version_id": version_id,
        }
        offer_id = hashlib.sha256(
            canonical_json(provisional).encode("utf-8")
        ).hexdigest()
        offer = AudioOffer(
            offer_id=offer_id,
            boundary_id=boundary_id,
            chunks=chunks,
            wordlist_version_id=version_id,
            queued_at=queued_at,
            final=final,
            creation_mode=creation_mode,
            target_hash=target_hash,
            live_id=durable_live_id,
            boundary_start_ms=durable_start,
            boundary_end_ms=durable_end,
        )
        journal = self.capture_dir / self._JOURNAL_NAME
        existing = self._journals.get(journal, ())
        by_key = {item.offer_id: item for item in existing}
        prior = by_key.get(offer.offer_id)
        if prior is not None and prior != offer:
            raise RuntimeError("audio_observation_journal_conflict")
        by_key[offer.offer_id] = offer
        staged = tuple(sorted(by_key.values(), key=lambda item: item.offer_id))
        self._write_observation_journal(
            journal, staged, platform_name=self.platform_name
        )
        self._journals[journal] = staged
        return offer

    def ack(self, offer_id: str | AudioOffer | tuple[ClosedAudioChunk, ...]) -> None:
        if isinstance(offer_id, AudioOffer):
            offer_id = offer_id.offer_id
        elif isinstance(offer_id, tuple):
            matches = {
                offer.offer_id
                for journal_offers in self._journals.values()
                for offer in journal_offers
                if offer.chunks == offer_id
            }
            if len(matches) != 1:
                raise ValueError("audio_observation_ack_invalid")
            offer_id = next(iter(matches))
        if offer_id in self._acked_offer_ids:
            return
        known = {
            offer.offer_id for journal_offers in self._journals.values()
            for offer in journal_offers
        }
        if offer_id not in known:
            raise ValueError("audio_observation_ack_invalid")
        if any(
            offer.offer_id == offer_id and offer.legacy_quarantine
            for journal_offers in self._journals.values()
            for offer in journal_offers
        ):
            raise ValueError("audio_observation_ack_invalid")
        staged_journals = dict(self._journals)
        for journal, journal_offers in tuple(self._journals.items()):
            remaining = tuple(
                offer for offer in journal_offers
                if offer.offer_id != offer_id
            )
            if remaining == journal_offers:
                continue
            if remaining:
                try:
                    self._write_observation_journal(
                        journal, remaining, platform_name=self.platform_name
                    )
                except RuntimeError:
                    raise RuntimeError("audio_observation_ack_failed") from None
                staged_journals[journal] = remaining
            else:
                try:
                    self._write_observation_journal(
                        journal, (), platform_name=self.platform_name
                    )
                except RuntimeError:
                    raise RuntimeError(
                        "audio_observation_ack_failed"
                    ) from None
                staged_journals[journal] = ()
        self._journals = staged_journals
        self._recovered_offers = tuple(
            offer for offer in self._recovered_offers
            if offer.offer_id != offer_id
        )
        self._returned_offer_ids.discard(offer_id)
        self._acked_offer_ids.add(offer_id)
        for journal, offers in tuple(self._journals.items()):
            if offers:
                continue
            try:
                journal.unlink(missing_ok=True)
                self._sync_directory(
                    journal.parent, platform_name=self.platform_name
                )
            except OSError:
                continue
            self._journals.pop(journal, None)

    def _install_capture_dir(self) -> None:
        self.settings.audio_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.settings.audio_dir.chmod(0o700)
        self.capture_dir = Path(tempfile.mkdtemp(
            prefix="capture-", dir=self.settings.audio_dir
        ))
        self.capture_dir.chmod(0o700)
        self._reset_timeline()
        self._session_retired = False

    def _retire_session(self) -> None:
        self._session_retired = True

    def _rollback_unowned_start(self) -> None:
        if self.process is not None:
            return
        capture = self.capture_dir
        try:
            (capture / self._FINAL_INTENT_NAME).unlink(missing_ok=True)
            if capture.is_dir():
                self._sync_directory(
                    capture, platform_name=self.platform_name
                )
                capture.rmdir()
        except OSError:
            # Never recursively remove an unexpected file.  A later startup
            # recovery can inspect the private directory without risking
            # evidence owned by another state transition.
            pass
        self._retire_session()
        self._session_authority = None
        self._session_started_ms = None
        self._authority_change_pending = False
        self.live_id = ""
        self._reset_timeline()

    def _stop_guard_process(
        self, process, *, timeout_seconds: float = 5.0,
    ) -> bool:
        budget = max(0.0, min(float(timeout_seconds), 5.0))
        if budget <= 0:
            return process.poll() is not None
        try:
            control = getattr(process, "stdin", None)
            if control is not None and not getattr(control, "closed", False):
                control.close()
            try:
                process.wait(timeout=budget * 0.6)
            except subprocess.TimeoutExpired:
                terminate_fraction = (
                    0.4 if self.platform_name == "nt" else 0.25
                )
                return _stop_owned_process(
                    process,
                    platform_name=self.platform_name,
                    terminate_timeout=budget * terminate_fraction,
                    kill_timeout=(
                        0.0 if self.platform_name == "nt" else budget * 0.15
                    ),
                )
            return process.poll() is not None
        except Exception:
            return process.poll() is not None

    def _spawn_guard(self, command: list[str]):
        ack_file = self.capture_dir / ".compliance-guard-ready"
        guard_command = self._guard_command_for_capture(self.capture_dir)
        kwargs: dict[str, object] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "text": True,
        }
        if self.platform_name == "nt":
            kwargs["creationflags"] = WINDOWS_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        process = self._popen(guard_command, **kwargs)
        self.process = process
        owner_written = False
        sqlite_owned = False
        ownership_ready = False
        identity: ProcessIdentity | None = None
        try:
            identity = self._read_process_identity(int(process.pid))
            if identity is None:
                raise RuntimeError("audio_process_identity_missing")
            self._write_guard_owner(
                self.capture_dir,
                pid=int(process.pid),
                start_token=identity.start_token,
            )
            owner_written = True
            if self.store is None:
                raise RuntimeError("audio_process_store_missing")
            self.store.set_audio_process(
                int(process.pid), str(self.capture_dir), self.live_id,
                identity.start_token,
            )
            sqlite_owned = True
            ownership_ready = True
            control = process.stdin
            if control is None:
                raise RuntimeError
            control.write(json.dumps(
                command, ensure_ascii=False, separators=(",", ":")
            ) + "\n")
            control.flush()
            deadline = float(self._monotonic()) + 5.0
            probes = 0
            while not ack_file.is_file():
                if process.poll() is not None or self._monotonic() >= deadline:
                    raise RuntimeError
                probes += 1
                if probes > 102:
                    raise RuntimeError
                self._sleep(0.05)
            if ack_file.read_text(encoding="utf-8") != "ready\n":
                raise RuntimeError
            ack_file.unlink()
            self._sync_directory(
                self.capture_dir, platform_name=self.platform_name
            )
            return process
        except Exception:
            stopped = self._stop_guard_process(process)
            if not stopped:
                self.health_issue = "audio_process_stop_failed"
                self._guard_start_unconfirmed = True
            else:
                self.process = None
                state_cleared = not sqlite_owned
                if sqlite_owned:
                    try:
                        self.store.clear_audio_process(int(process.pid))
                        state_cleared = True
                    except Exception:
                        self.health_issue = "audio_process_clear_failed"
                if owner_written and state_cleared:
                    self._remove_guard_owner(self.capture_dir)
                elif owner_written and identity is not None:
                    self._inherited_guard_owner = (
                        self.capture_dir / self._GUARD_OWNER_NAME,
                        int(process.pid),
                        identity.start_token,
                    )
            error_class = (
                "audio_process_start_failed"
                if ownership_ready else "audio_process_persistence_failed"
            )
            raise RuntimeError(error_class) from None

    def _start(self, now_ms: int) -> None:
        if self._prepared_offer is None:
            raise RuntimeError("audio_offer_authority_missing")
        target = self.target_provider.get()
        if target is None:
            return
        live_id, urls = target
        self.live_id = str(live_id)
        version_id, _queued_at, _final, creation_mode, target_hash = (
            self._prepared_offer
        )
        self._session_authority = (
            version_id, creation_mode, target_hash,
        )
        self._session_started_ms = int(now_ms)
        try:
            selected = select_audio_url(tuple(urls))
            self._install_capture_dir()
            self._persist_session_manifest(int(now_ms))
        except ValueError:
            self._rollback_unowned_start()
            raise
        except Exception:
            self._rollback_unowned_start()
            raise _AudioStartPreparationError(
                "audio_session_persistence_failed"
            ) from None
        command = [
            self.settings.ffmpeg,
            "-hide_banner", "-loglevel", "warning",
        ]
        if ".m3u8" not in selected.casefold():
            command.extend([
                "-reconnect", "1", "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5", "-reconnect_at_eof", "1",
            ])
        command.extend([
            "-headers",
            f"User-Agent: {self.user_agent}\r\nReferer: {self.referer}\r\n",
            "-i", selected, "-map", "0:a:0", "-vn", "-ac", "1",
            "-ar", "16000", "-c:a", "pcm_s16le", "-f", "segment",
            "-segment_time", "120", "-reset_timestamps", "1",
            str(self.capture_dir / "segment_%06d.wav"),
        ])
        if self._command_observer is not None:
            self._command_observer(tuple(command))
        try:
            if self._use_guard:
                self._spawn_guard(command)
            else:
                kwargs = {
                    "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL,
                }
                if self.platform_name == "nt":
                    kwargs["creationflags"] = WINDOWS_NEW_PROCESS_GROUP
                else:
                    kwargs["start_new_session"] = True
                self.process = self._popen(command, **kwargs)
        except Exception as exc:
            if self.process is None:
                self._rollback_unowned_start()
            if (
                isinstance(exc, RuntimeError)
                and str(exc) == "audio_process_persistence_failed"
            ):
                raise
            raise RuntimeError("audio_process_start_failed") from None
        try:
            self.process.args = (self.settings.ffmpeg, "<private-input>")
        except Exception:
            pass
        if not self._use_guard:
            try:
                if self.store is not None:
                    if self._process_identity_reader is not None:
                        identity = _coerce_process_identity(
                            self._process_identity_reader(int(self.process.pid))
                        )
                    elif hasattr(self.process, "start_token"):
                        identity = ProcessIdentity(
                            str(self.process.start_token), tuple(command)
                        )
                    else:
                        identity = read_process_identity(
                            int(self.process.pid), platform_name=self.platform_name
                        )
                    if identity is None:
                        raise RuntimeError("audio_process_identity_missing")
                    self.store.set_audio_process(
                        int(self.process.pid), str(self.capture_dir), self.live_id,
                        identity.start_token,
                    )
            except Exception:
                if self._stop_process(clear_state=False):
                    self._retire_session()
                raise RuntimeError("audio_process_persistence_failed") from None

    def _closed_paths(self, *, include_last: bool) -> tuple[Path, ...]:
        paths = self._capture_segment_paths(self.capture_dir)
        if include_last:
            return paths
        return paths[:-1]

    def _stop_process(
        self, *, clear_state: bool = True, retain_handle: bool = False,
        timeout_seconds: float | None = None,
    ) -> bool:
        process = self.process
        if process is None:
            return True
        pid = int(process.pid)
        try:
            if timeout_seconds is not None and self._use_guard:
                stopped = bool(self._stop_guard_process(
                    process, timeout_seconds=timeout_seconds
                ))
            else:
                stopped = bool(self._process_stopper(process))
        except Exception:
            stopped = False
        if not stopped or process.poll() is None:
            self.health_issue = "audio_process_stop_failed"
            return False
        if not retain_handle:
            self.process = None
        if clear_state and not self._clear_runtime_guard_owner(pid):
            return False
        return True

    def _clear_runtime_guard_owner(self, pid: int) -> bool:
        if self.store is not None:
            try:
                self.store.clear_audio_process(pid)
            except Exception:
                self.health_issue = "audio_process_clear_failed"
                return False
        if self._use_guard and not self._remove_guard_owner(self.capture_dir):
            return False
        return True

    def _session_manifest_document(self, now_ms: int) -> dict[str, object]:
        if self._prepared_offer is None or not self.live_id:
            raise RuntimeError("audio_offer_authority_missing")
        version_id, queued_at, _final, creation_mode, target_hash = (
            self._prepared_offer
        )
        origin = self._origin_ms
        if origin is None:
            origin = int(self._session_started_ms or now_ms)
        segments: list[dict[str, object]] = []
        elapsed_ms = 0
        for name in sorted(self._finalized_segment_names):
            duration_ms = self._known_durations.get(name)
            if duration_ms is None:
                raise RuntimeError("audio_session_timeline_invalid")
            duration_ms = int(duration_ms)
            if not self._valid_segment_name(name) or duration_ms <= 0:
                raise RuntimeError("audio_session_timeline_invalid")
            segments.append({
                "duration_ms": duration_ms,
                "emitted": name in self._emitted,
                "name": name,
                "start_ms": int(origin) + elapsed_ms,
            })
            elapsed_ms += duration_ms
        return {
            "boundary_end_ms": int(now_ms),
            "continuity": self._observed_continuity,
            "creation_mode": creation_mode,
            "live_id": self.live_id,
            "origin_ms": int(origin),
            "queued_at": float(queued_at),
            "segments": segments,
            "session_started_ms": int(self._session_started_ms or now_ms),
            "target_hash": target_hash,
            "version": 2,
            "wordlist_version_id": int(version_id),
        }

    def _persist_session_manifest(self, now_ms: int) -> Path:
        intent = self.capture_dir / self._FINAL_INTENT_NAME
        self._write_final_intent(
            intent, self._session_manifest_document(now_ms),
            platform_name=self.platform_name,
        )
        return intent

    def _persist_final_intent(self, now_ms: int) -> Path:
        self._observe(now_ms, include_last=False)
        return self._persist_session_manifest(now_ms)

    def _complete_finalization(self, pid: int, intent: Path) -> None:
        intent.unlink(missing_ok=True)
        self._sync_directory(intent.parent, platform_name=self.platform_name)
        self.process = None
        self._clear_runtime_guard_owner(pid)
        self._retire_session()
        self._session_authority = None
        self._session_started_ms = None
        self._authority_change_pending = False
        self._guard_start_unconfirmed = False

    def _settle_unconfirmed_guard(
        self, *, timeout_seconds: float = 0.25,
    ) -> AudioOffer | None:
        if not self._guard_start_unconfirmed:
            return None
        process = self.process
        if process is None:
            return None
        pid = int(process.pid)
        if not self._stop_guard_process(
            process, timeout_seconds=timeout_seconds
        ):
            self.health_issue = "audio_process_stop_failed"
            return None
        self.process = None
        if not self._clear_runtime_guard_owner(pid):
            return None
        self._retire_session()
        self._guard_start_unconfirmed = False
        self._final_intents_recovered = False
        self.recover_final_intents()
        return self._next_recovered_offer()

    def _recover_exited_process(
        self,
    ) -> AudioOffer | tuple[ClosedAudioChunk, ...]:
        """Recover the durable manifest left by an unexpectedly exited guard.

        The newest WAV can be mid-header when ffmpeg exits.  Re-reading that
        live tail through ``_collect(include_last=True)`` would fail forever
        before ownership is cleared.  The persisted final intent is the crash
        boundary: its decoder deliberately ignores only an unreadable newest
        tail while preserving every finalized predecessor.
        """
        process = self.process
        if process is None:
            return ()
        if process.poll() is None:
            return ()
        pid = int(process.pid)
        try:
            process.wait(timeout=0)
        except Exception:
            pass
        self._final_intents_recovered = False
        # Keep the exited process handle until recovery succeeds.  If the
        # manifest is genuinely corrupt, subsequent polls remain fail-closed
        # instead of starting a second capture beside uncommitted evidence.
        self.recover_final_intents()
        # Ownership is released only after every recoverable observation has
        # an fsynced journal.  A listener crash between these steps therefore
        # rediscovers the dead owner and the same durable final intent.
        if not self._clear_runtime_guard_owner(pid):
            return ()
        self.process = None
        self._retire_session()
        self._session_authority = None
        self._session_started_ms = None
        self._authority_change_pending = False
        self._guard_start_unconfirmed = False
        recovered = self._next_recovered_offer()
        return recovered if recovered is not None else ()

    def _reset_timeline(self) -> None:
        self._emitted = set()
        self._origin_ms = None
        self._known_durations = {}
        self._last_checked_closed_media_ms = None
        self._last_observed_at_ms = None
        self._last_observed_total_media_ms = None
        self._unsettled_wallclock_drift_ms = 0
        self._finalized_segment_names = set()
        self._observed_continuity = "ok"

    def _active_identity_is_safe(
        self, now_ms: int,
    ) -> bool | AudioOffer:
        reader = getattr(self.target_provider, "active_live_ids", None)
        if not callable(reader) or self.process is None:
            return True
        try:
            live_ids = tuple(reader())
        except Exception:
            self.health_issue = "audio_source_state_failed"
            if self._stop_process():
                self._retire_session()
            return False
        if len(live_ids) == 1 and str(live_ids[0]) == self.live_id:
            return True
        if len(live_ids) > 1:
            self.health_issue = "audio_source_ambiguous"
        elif len(live_ids) == 1:
            self.health_issue = "audio_source_changed"
        else:
            self.health_issue = ""
        try:
            final_offer = self.finish(int(now_ms))
        except Exception:
            return False
        if isinstance(final_offer, AudioOffer):
            return final_offer
        return False

    def _observe(self, now_ms: int, *, include_last: bool) -> None:
        paths = self._capture_segment_paths(self.capture_dir)
        if not paths:
            return
        present = {path.name for path in paths}
        if any(
            name not in present and name not in self._emitted
            for name in self._known_durations
        ):
            raise ValueError("audio_segment_missing")
        growing = None if include_last else paths[-1]
        for path in paths:
            if path == growing:
                try:
                    self._known_durations[path.name] = (
                        _observed_pcm_duration_ms(path)
                    )
                except ValueError:
                    self._known_durations.pop(path.name, None)
            else:
                self._known_durations[path.name] = _wav_duration_ms(path)
        total_media_ms = sum(self._known_durations.values())
        newly_finalized_names = {
            path.name for path in paths if path != growing
        }
        self._finalized_segment_names.update(newly_finalized_names)
        finalized_media_ms = sum(
            self._known_durations[name]
            for name in self._finalized_segment_names
            if name in self._known_durations
        )
        if self._origin_ms is None and total_media_ms > 0:
            self._origin_ms = int(now_ms) - total_media_ms
        if self._origin_ms is None:
            return
        if (
            self._last_observed_at_ms is not None
            and self._last_observed_total_media_ms is not None
        ):
            wall_elapsed_ms = int(now_ms) - self._last_observed_at_ms
            media_elapsed_ms = (
                total_media_ms - self._last_observed_total_media_ms
            )
            timely_observation_limit_ms = max(
                5_000,
                self.settings.wallclock_tolerance_ms
                + round(self.settings.poll_seconds * 1000),
            )
            if (
                0 <= wall_elapsed_ms <= timely_observation_limit_ms
                and media_elapsed_ms >= 0
            ):
                self._unsettled_wallclock_drift_ms += (
                    wall_elapsed_ms - media_elapsed_ms
                )
            else:
                # Recognition runs synchronously today.  While it owns the
                # listener loop, ffmpeg can continue catching up through the
                # HLS buffer.  That unobserved interval cannot prove a media
                # gap, so start a new local timing observation instead of
                # manufacturing an invalid timeline.
                self._unsettled_wallclock_drift_ms = 0
        self._last_observed_at_ms = int(now_ms)
        self._last_observed_total_media_ms = total_media_ms
        if (
            finalized_media_ms > 0
            and self._last_checked_closed_media_ms != finalized_media_ms
            # The just-created successor can exist before its PCM header is
            # readable.  It carries the media elapsed since the closed
            # boundary, so defer this one checkpoint until that duration is
            # observable instead of manufacturing a wall-clock gap from a
            # transient file-write state.
            and (growing is None or growing.name in self._known_durations)
        ):
            signed_drift_ms = (
                (int(now_ms) - self._origin_ms) - total_media_ms
            )
            if self._last_checked_closed_media_ms is None:
                # A live HLS feed has no source wall-clock marker.  Anchor
                # the first fully readable closed boundary to local time, then
                # assess only drift accumulated by timely observations.  If
                # an unreadable successor delayed this checkpoint until after
                # the first closed chunk was already staged, that chunk's
                # persisted capture range has frozen the origin.  Moving it
                # now would manufacture a gap before the next chunk.
                if not self._emitted:
                    self._origin_ms += signed_drift_ms
                self._unsettled_wallclock_drift_ms = 0
            elif (
                abs(self._unsettled_wallclock_drift_ms)
                > self.settings.wallclock_tolerance_ms
            ):
                self._observed_continuity = "invalid"
            self._last_checked_closed_media_ms = finalized_media_ms

    def _collect(
        self, now_ms: int, *, include_last: bool,
    ) -> AudioOffer | tuple[ClosedAudioChunk, ...]:
        if self._session_retired:
            return ()
        self._observe(now_ms, include_last=include_last)
        manifest = self.capture_dir / self._FINAL_INTENT_NAME
        if manifest.is_file():
            self._persist_session_manifest(now_ms)
        paths = self._closed_paths(include_last=include_last)
        pending = tuple(path for path in paths if path.name not in self._emitted)
        if not pending and not include_last:
            return ()
        if self._origin_ms is None and pending:
            raise RuntimeError("audio_timeline_missing")
        chunks: list[ClosedAudioChunk] = []
        ordered_names = tuple(sorted(self._known_durations))
        for path in pending:
            duration_ms = self._known_durations[path.name]
            prefix_ms = sum(
                self._known_durations[name]
                for name in ordered_names
                if name < path.name
            )
            start_ms = self._origin_ms + prefix_ms
            end_ms = start_ms + duration_ms
            material = f"{self.live_id}\0{path.name}\0{start_ms}\0{duration_ms}"
            chunks.append(ClosedAudioChunk(
                chunk_key=hashlib.sha256(material.encode("utf-8")).hexdigest(),
                live_id=self.live_id,
                path=path,
                capture_start_ms=start_ms,
                capture_end_ms=end_ms,
                media_duration_ms=duration_ms,
                continuity=self._observed_continuity,
                delete_after_use=True,
            ))
        pending_names = tuple(path.name for path in pending)
        result = tuple(chunks)
        if result and any(chunk.continuity == "invalid" for chunk in result):
            if not self._stop_process():
                return ()
            self._retire_session()
        offer = self._stage_observations(
            result,
            force_final=bool(include_last or self.process is None),
            live_id=self.live_id,
            boundary_start_ms=int(self._session_started_ms or now_ms),
            boundary_end_ms=int(now_ms),
        )
        self._emitted.update(pending_names)
        if manifest.is_file():
            self._persist_session_manifest(now_ms)
        return offer if offer is not None else result

    def _next_recovered_offer(self) -> AudioOffer | None:
        for offer in self._recovered_offers:
            if offer.offer_id not in self._returned_offer_ids:
                self._returned_offer_ids.add(offer.offer_id)
                return offer
        return None

    def release_offer(self, offer_id: str) -> None:
        if not isinstance(offer_id, str) or not offer_id:
            raise ValueError("audio observation release invalid")
        known = tuple(
            offer for offers in self._journals.values() for offer in offers
        )
        if offer_id not in {offer.offer_id for offer in known}:
            return
        self._recovered_offers = tuple(sorted(
            {offer.offer_id: offer for offer in (
                *self._recovered_offers, *known,
            )}.values(),
            key=lambda item: (item.queued_at, item.offer_id),
        ))
        self._returned_offer_ids.discard(offer_id)

    @property
    def quarantined_offers(self) -> tuple[AudioOffer, ...]:
        return tuple(
            offer for journal_offers in self._journals.values()
            for offer in journal_offers if offer.legacy_quarantine
        )

    @property
    def has_pending_recovery(self) -> bool:
        return (
            self._inherited_guard_owner is not None
            or any(self._journals.values())
        )

    def poll(self, now_ms: int) -> AudioOffer | tuple[ClosedAudioChunk, ...]:
        if self._inherited_guard_owner is not None:
            if not self._reconcile_inherited_guard_owner():
                return ()
            self._final_intents_recovered = False
            self.recover_final_intents()
        recovered = self._next_recovered_offer()
        if recovered is not None:
            return recovered
        if self.quarantined_offers:
            return ()
        if self._guard_start_unconfirmed:
            settled = self._settle_unconfirmed_guard()
            return settled if settled is not None else ()
        # A dead guard owns no live capture.  Recover its durable crash
        # boundary before asking for the current live identity: rotation can
        # make that identity empty/different (or temporarily unavailable),
        # and the normal identity-transition path calls finish() on the
        # unreadable tail that this recovery path is specifically for.
        if self.process is not None and self.process.poll() is not None:
            return self._recover_exited_process()
        if any(self._journals.values()):
            return ()
        identity_result = self._active_identity_is_safe(now_ms)
        if isinstance(identity_result, AudioOffer):
            return identity_result
        if not identity_result:
            return ()
        if self.process is not None and self._authority_change_pending:
            return self.finish(now_ms)
        if self.process is None:
            try:
                self._start(now_ms)
            except AudioTargetError as exc:
                self.health_issue = str(exc)
                return ()
            except ValueError:
                self.health_issue = "audio_target_invalid"
                return ()
            except _AudioStartPreparationError as exc:
                self.health_issue = str(exc)
                return ()
        if self.process is None:
            return ()
        return self._collect(now_ms, include_last=False)

    def finish(
        self, now_ms: int, *, timeout_seconds: float | None = None,
    ) -> AudioOffer | tuple[ClosedAudioChunk, ...]:
        if (
            self._inherited_guard_owner is not None
            and not self._reconcile_inherited_guard_owner()
        ):
            return ()
        if timeout_seconds is not None:
            timeout_seconds = float(timeout_seconds)
            if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
                return ()
        recovered = self._next_recovered_offer()
        if recovered is not None:
            return recovered
        if self.quarantined_offers:
            return ()
        if self._session_retired:
            return ()
        intent = self._persist_final_intent(now_ms)
        process = self.process
        if process is None:
            pid = 0
        else:
            pid = int(process.pid)
        if not self._stop_process(
            clear_state=False,
            retain_handle=True,
            timeout_seconds=timeout_seconds,
        ):
            return ()
        closed = self._collect(now_ms, include_last=True)
        self._complete_finalization(pid, intent)
        return closed


class ReplayAudioSource:
    def __init__(self, manifest: Path) -> None:
        self.manifest = Path(manifest)
        self._chunks = self._load()
        self._returned = False

    def _load(self) -> tuple[ClosedAudioChunk, ...]:
        chunks: list[ClosedAudioChunk] = []
        allowed_keys = {
            "chunk_key", "audio_path", "origin_ms", "duration_ms", "live_id",
            "continuity",
        }
        required_keys = allowed_keys - {"continuity"}
        for line in self.manifest.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise TypeError
                if set(item) - allowed_keys or not required_keys <= set(item):
                    raise TypeError
                path = Path(item["audio_path"])
                duration_value = item["duration_ms"]
                origin_value = item["origin_ms"]
                if (
                    isinstance(duration_value, bool)
                    or not isinstance(duration_value, int)
                    or isinstance(origin_value, bool)
                    or not isinstance(origin_value, int)
                    or origin_value < 0
                    or not isinstance(item["audio_path"], str)
                    or not isinstance(item["live_id"], str)
                    or not isinstance(item["chunk_key"], str)
                ):
                    raise TypeError
                duration_ms = duration_value
                origin_ms = origin_value
                live_id = str(item["live_id"])
                chunk_key = str(item["chunk_key"])
                continuity = item.get("continuity", "ok")
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError("replay_manifest_invalid") from exc
            if (
                duration_ms <= 0
                or not live_id
                or not chunk_key
                or not path.is_absolute()
                or continuity not in {"ok", "invalid"}
            ):
                raise ValueError("replay_manifest_invalid")
            try:
                decoded_duration_ms = _wav_duration_ms(path)
            except ValueError as exc:
                raise ValueError("replay_manifest_invalid") from exc
            if decoded_duration_ms != duration_ms:
                raise ValueError("replay_manifest_invalid")
            chunks.append(ClosedAudioChunk(
                chunk_key=chunk_key,
                live_id=live_id,
                path=path,
                capture_start_ms=origin_ms,
                capture_end_ms=origin_ms + duration_ms,
                media_duration_ms=duration_ms,
                continuity=continuity,
                delete_after_use=False,
            ))
        chunks.sort(key=lambda chunk: (
            chunk.capture_start_ms, chunk.chunk_key, str(chunk.path)
        ))
        seen_keys: set[str] = set()
        last_end_by_live: dict[str, int] = {}
        for chunk in chunks:
            if chunk.chunk_key in seen_keys:
                raise ValueError("replay_manifest_invalid")
            seen_keys.add(chunk.chunk_key)
            if chunk.capture_start_ms < last_end_by_live.get(chunk.live_id, -1):
                raise ValueError("replay_manifest_invalid")
            last_end_by_live[chunk.live_id] = chunk.capture_end_ms
        if not chunks:
            raise ValueError("replay_manifest_invalid")
        return tuple(chunks)

    def poll(self, now_ms: int) -> tuple[ClosedAudioChunk, ...]:
        del now_ms
        if self._returned:
            return ()
        self._returned = True
        return self._chunks

    def finish(
        self, now_ms: int, *, timeout_seconds: float | None = None,
    ) -> tuple[ClosedAudioChunk, ...]:
        del timeout_seconds
        return self.poll(now_ms)

    def ack(self, chunks: tuple[ClosedAudioChunk, ...]) -> None:
        if tuple(chunks) != self._chunks:
            raise ValueError("replay_audio_ack_invalid")
        self._returned = True


def _audio_path(value: Path | ClosedAudioChunk) -> Path:
    return value.path if isinstance(value, ClosedAudioChunk) else Path(value)


def _read_pcm(path: Path) -> tuple[int, bytes]:
    try:
        with wave.open(str(path), "rb") as handle:
            if (
                handle.getnchannels() != 1
                or handle.getsampwidth() != 2
                or handle.getframerate() != 16_000
                or handle.getcomptype() != "NONE"
            ):
                raise ValueError("audio_context_format_invalid")
            frames = handle.getnframes()
            payload = handle.readframes(frames)
    except ValueError:
        raise
    except (EOFError, OSError, wave.Error) as exc:
        raise ValueError("audio_context_invalid") from exc
    if frames <= 0 or len(payload) != frames * 2:
        raise ValueError("audio_context_invalid")
    return frames, payload


def build_context_wav(
    previous: Path | ClosedAudioChunk | None,
    current: Path | ClosedAudioChunk,
    destination: Path,
    overlap_ms: int = 30_000,
) -> int:
    if not 0 <= int(overlap_ms) <= 30_000:
        raise ValueError("audio_context_overlap_invalid")
    current_frames, current_payload = _read_pcm(_audio_path(current))
    del current_frames
    copied_frames = 0
    previous_tail = b""
    if previous is not None and overlap_ms:
        previous_frames, previous_payload = _read_pcm(_audio_path(previous))
        copied_frames = min(previous_frames, int(overlap_ms) * 16)
        previous_tail = previous_payload[-copied_frames * 2:]

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        temporary.chmod(0o600)
        with wave.open(str(temporary), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16_000)
            handle.writeframes(previous_tail)
            handle.writeframes(current_payload)
        os.replace(temporary, destination)
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("audio_context_write_failed") from exc
    return copied_frames * 1000 // 16_000


def plan_audio_job(
    previous: ClosedAudioChunk | None,
    current: ClosedAudioChunk,
    cursor_ms: int,
    version_id: int,
    final: bool = False,
    chain_id: str = "",
    source_kind: str = "realtime",
) -> AudioJob:
    if (
        current.capture_end_ms <= current.capture_start_ms
        or current.capture_end_ms - current.capture_start_ms
        != current.media_duration_ms
    ):
        raise ValueError("audio_timeline_invalid")
    if source_kind not in {"realtime", "main_repair"}:
        raise ValueError("audio_source_kind_invalid")
    if current.continuity != "ok":
        raise ValueError("audio_timeline_invalid")
    if previous is not None:
        if (
            previous.live_id != current.live_id
            or previous.continuity != "ok"
            or previous.capture_end_ms != current.capture_start_ms
            or previous.capture_end_ms - previous.capture_start_ms
            != previous.media_duration_ms
        ):
            raise ValueError("audio_context_invalid")
        overlap_ms = min(30_000, previous.media_duration_ms)
        recognition_origin_ms = previous.capture_end_ms - overlap_ms
    else:
        recognition_origin_ms = current.capture_start_ms
    commit_start_ms = int(cursor_ms)
    commit_end_ms = (
        current.capture_end_ms if final else current.capture_end_ms - 15_000
    )
    if commit_start_ms >= commit_end_ms:
        raise ValueError("audio_commit_range_empty")
    if commit_start_ms < recognition_origin_ms:
        raise ValueError("audio_commit_range_invalid")
    material = (
        f"{current.live_id}\0{current.chunk_key}\0"
        f"{commit_start_ms}\0{commit_end_ms}"
    )
    if source_kind == "main_repair":
        material = "main_repair\0" + material
    job_key = hashlib.sha256(material.encode("utf-8")).hexdigest()
    anchor = previous if previous is not None else current
    if source_kind == "main_repair":
        chain_material = (
            f"main_repair\0{current.live_id}\0"
            f"{commit_start_ms}\0{commit_end_ms}"
        )
    else:
        chain_material = (
            f"chain\0{current.live_id}\0{anchor.capture_start_ms}\0"
            f"{anchor.chunk_key}"
        )
    derived_chain_id = hashlib.sha256(
        chain_material.encode("utf-8")
    ).hexdigest()
    if chain_id and (
        len(chain_id) != 64
        or any(character not in "0123456789abcdef" for character in chain_id)
    ):
        raise ValueError("audio_chain_identity_invalid")
    context_path = current.path.with_name(f".{current.path.stem}.context.wav")
    return AudioJob(
        job_key=job_key,
        live_id=current.live_id,
        context_path=context_path,
        recognition_origin_ms=recognition_origin_ms,
        commit_start_ms=commit_start_ms,
        commit_end_ms=commit_end_ms,
        wordlist_version_id=int(version_id),
        continuity=current.continuity,
        chain_id=chain_id or derived_chain_id,
        source_kind=source_kind,
    )
