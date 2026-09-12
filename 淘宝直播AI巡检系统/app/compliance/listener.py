from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from .config import ComplianceSettings
from .health import ComplianceHealthService
from .jobs import AudioJobPlanner, AudioJobProcessor, ProcessedJob
from .models import AudioOffer, ClosedAudioChunk, WordlistSnapshot


@dataclass(frozen=True)
class CycleResult:
    chunks_queued: int = 0
    leases_recovered: int = 0
    jobs_processed: int = 0
    events_created: int = 0
    cards_frozen: int = 0
    messages_sent: int = 0


@dataclass(frozen=True)
class _PendingAudioOffer:
    owner: object
    chunks: tuple[object, ...]
    wordlist: WordlistSnapshot
    queued_at: float
    final: bool
    creation_mode: str
    target_hash: str
    offer_id: str = ""
    live_id: str = ""
    boundary_start_ms: int = 0
    boundary_end_ms: int = 0


class ComplianceListener:
    def __init__(
        self,
        *,
        config_loader: Callable[[], dict],
        store: object | None = None,
        notifier: object | None = None,
        wordlists: object | None = None,
        recognizer: object | None = None,
        audio: object | None = None,
        jobs: object | None = None,
        events: object | None = None,
        store_factory: Callable[..., object] | None = None,
        notifier_factory: Callable[..., object] | None = None,
        wordlist_service_factory: Callable[..., object] | None = None,
        recognizer_factory: Callable[..., object] | None = None,
        audio_factory: Callable[..., object] | None = None,
        target_provider_factory: Callable[..., object] | None = None,
        event_service_factory: Callable[..., object] | None = None,
        context_builder: Callable[..., int] | None = None,
        file_remover: Callable[[Path], None] | None = None,
        source_duration_reader: Callable[[Path], int] | None = None,
        recovery_logger: Callable[[str], None] | None = None,
        target_provider: object | None = None,
        replay_manifest: Path | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        stop_waiter: Callable[[object, float], bool] | None = None,
        lock_acquirer: Callable[[Path], object | None] | None = None,
        owned_ffmpeg_terminator: Callable[[int, Path], bool] | None = None,
        repair: object | None = None,
        repair_factory: Callable[..., object] | None = None,
        **dependencies: Any,
    ) -> None:
        self.config_loader = config_loader
        self.store = store
        self.notifier = notifier
        self.wordlists = wordlists
        self.recognizer = recognizer
        self.audio = audio
        self.jobs = jobs
        self.events = events
        self.repair = repair
        self.target_provider = target_provider
        self.store_factory = store_factory
        self.notifier_factory = notifier_factory
        self.wordlist_service_factory = wordlist_service_factory
        self.recognizer_factory = recognizer_factory
        self.audio_factory = audio_factory
        self.target_provider_factory = target_provider_factory
        self.event_service_factory = event_service_factory
        self.repair_factory = repair_factory
        if context_builder is None:
            from .audio import build_context_wav, _wav_duration_ms

            context_builder = build_context_wav
            if source_duration_reader is None:
                source_duration_reader = _wav_duration_ms
        elif source_duration_reader is None:
            from .audio import _wav_duration_ms

            source_duration_reader = _wav_duration_ms
        self.context_builder = context_builder
        self.source_duration_reader = source_duration_reader
        self.file_remover = file_remover or (
            lambda path: path.unlink(missing_ok=True)
        )
        self.recovery_logger = recovery_logger or (lambda _record: None)
        self.replay_manifest = (
            Path(replay_manifest) if replay_manifest is not None else None
        )
        self.clock = clock
        self.monotonic = monotonic
        self.sleep = sleep
        self.stop_waiter = stop_waiter
        if lock_acquirer is None:
            from app.runtime.locking import acquire_process_lock

            lock_acquirer = acquire_process_lock
        self.lock_acquirer = lock_acquirer
        if owned_ffmpeg_terminator is None:
            from .process_control import terminate_owned_ffmpeg

            owned_ffmpeg_terminator = terminate_owned_ffmpeg
        self.owned_ffmpeg_terminator = owned_ffmpeg_terminator
        self.dependencies = dependencies
        self.settings: ComplianceSettings | None = None
        self.config: dict[str, Any] = {}
        self.audio_start_blocked = False
        self._fingerprints: dict[str, str] = {}
        self._owned: set[str] = set()
        self._pending_audio_offer: _PendingAudioOffer | None = None
        self._audio_transition_stopped = False
        self._started = False
        self._shutdown = False
        self._heartbeat = None
        self._watchdog_process = None
        self._listener_started_at_ms = 0

    @staticmethod
    def _fingerprint(name: str, values: tuple[object, ...]) -> str:
        encoded = json.dumps(
            [name, *[str(value) for value in values]],
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _settings_fingerprints(
        self, settings: ComplianceSettings,
    ) -> dict[str, str]:
        return {
            "wordlist": self._fingerprint("wordlist-v1", (
                settings.wordlist_spreadsheet_token,
                settings.wordlist_sheet_name,
                settings.wordlist_range,
                settings.wordlist_sync_seconds,
            )),
            "audio": self._fingerprint("audio-v1", (
                settings.audio_dir,
                settings.ffmpeg,
                settings.segment_seconds,
                settings.overlap_seconds,
                settings.holdback_seconds,
                settings.wallclock_tolerance_ms,
                self.dependencies.get(
                    "target_provider_identity", "current-live-target-v1"
                ),
                self.replay_manifest or "live",
            )),
            "recognizer": self._fingerprint("recognizer-v1", (
                settings.recognizer_model,
                settings.recognizer_revision,
                settings.vad_model,
                settings.punc_model,
                settings.recognizer_device,
            )),
            "delivery": self._fingerprint("delivery-v1", (
                settings.recipient_chat_id,
            )),
        }

    def _effective_mode(self, settings: ComplianceSettings) -> str:
        if self.replay_manifest is not None:
            return "shadow"
        return settings.mode

    def _settings_for_current_process(
        self, configured: ComplianceSettings,
    ) -> ComplianceSettings:
        """Require a process restart before enabling or redirecting live."""
        current = self.settings
        if current is None or self.replay_manifest is not None:
            return configured
        current_mode = self._effective_mode(current)
        if configured.mode != "live":
            return configured
        if current_mode != "live":
            return replace(
                configured,
                mode=current_mode,
                recipient_chat_id="",
            )
        if configured.recipient_chat_id != current.recipient_chat_id:
            return replace(
                configured,
                mode="shadow",
                recipient_chat_id="",
            )
        return configured

    def _queue_authority(
        self, settings: ComplianceSettings,
    ) -> tuple[str, str]:
        mode = self._effective_mode(settings)
        if mode == "live":
            return mode, hashlib.sha256(
                settings.recipient_chat_id.encode("utf-8")
            ).hexdigest()
        return "shadow", ""

    def _prepare_audio_offer(
        self,
        wordlist: WordlistSnapshot,
        *,
        now: float,
        final: bool,
        settings: ComplianceSettings,
    ) -> None:
        if self.audio is None:
            return
        prepare = getattr(self.audio, "prepare_offer", None)
        if not callable(prepare):
            return
        creation_mode, target_hash = self._queue_authority(settings)
        prepare(
            wordlist_version_id=wordlist.version_id,
            queued_at=float(now),
            final=bool(final),
            creation_mode=creation_mode,
            target_hash=target_hash,
        )

    def _construct_notifier(self) -> object:
        if self.notifier_factory is None:
            from .notifier import ComplianceNotifier

            self.notifier_factory = ComplianceNotifier
        notifier = self.notifier_factory(self.store, config=self.config)
        self._owned.add("notifier")
        return notifier

    def _construct_wordlists(self, settings: ComplianceSettings) -> object:
        if self.wordlist_service_factory is None:
            from .wordlist import (
                FeishuSheetWordlistProvider,
                WordlistService,
            )

            provider = FeishuSheetWordlistProvider(
                settings.wordlist_spreadsheet_token,
                settings.wordlist_sheet_name,
                settings.wordlist_range,
            )
            service = WordlistService(
                self.store,
                provider,
                sync_seconds=settings.wordlist_sync_seconds,
            )
        else:
            service = self.wordlist_service_factory(self.store, settings)
        self._owned.add("wordlists")
        return service

    def _construct_recognizer(self, settings: ComplianceSettings) -> object:
        if self.recognizer_factory is None:
            from .recognizer import SeacoParaformerRecognizer

            recognizer = SeacoParaformerRecognizer(
                settings, worker_owner=self.store,
            )
        else:
            recognizer = self.recognizer_factory(settings)
        self._owned.add("recognizer")
        return recognizer

    def _construct_target_provider(self) -> object | None:
        if self.replay_manifest is not None:
            return None
        if self.target_provider_factory is None:
            from .audio import CurrentLiveTargetProvider

            self.target_provider_factory = CurrentLiveTargetProvider
        target = self.target_provider_factory(
            self.store, config_loader=self.config_loader
        )
        self._owned.add("target_provider")
        return target

    def _construct_audio(self, settings: ComplianceSettings) -> object:
        if self.replay_manifest is not None:
            from .audio import ReplayAudioSource

            audio = ReplayAudioSource(self.replay_manifest)
        else:
            if self.audio_factory is None:
                from .audio import LiveAudioSource

                self.audio_factory = LiveAudioSource
            audio = self.audio_factory(
                settings,
                target_provider=self.target_provider,
                store=self.store,
            )
        self._owned.add("audio")
        return audio

    def _construct_events(self) -> object:
        if self.event_service_factory is None:
            from .events import EventService

            self.event_service_factory = EventService
        events = self.event_service_factory(self.store)
        self._owned.add("events")
        return events

    def _construct_repair(self, settings: ComplianceSettings) -> object:
        if self.repair_factory is None:
            from .repair import (
                MainRecordingRepairService,
                RepairMediaBuilder,
            )

            repair = MainRecordingRepairService(
                self.store,
                RepairMediaBuilder(
                    ffmpeg=settings.ffmpeg,
                    repair_dir=settings.audio_dir / "repair",
                ),
            )
        else:
            repair = self.repair_factory(self.store, settings)
        self._owned.add("repair")
        return repair

    def _finish_audio(
        self, now: float, *, timeout_seconds: float | None = None,
    ) -> tuple[bool, AudioOffer | tuple[ClosedAudioChunk, ...]]:
        if self.audio is None:
            return True, ()
        try:
            if timeout_seconds is None:
                result = self.audio.finish(  # type: ignore[attr-defined]
                    int(float(now) * 1000)
                )
            else:
                result = self.audio.finish(  # type: ignore[attr-defined]
                    int(float(now) * 1000),
                    timeout_seconds=float(timeout_seconds),
                )
        except Exception:
            return False, ()
        if result is False:
            return False, ()
        chunks = result if isinstance(result, AudioOffer) else tuple(result or ())
        stopped = (
            getattr(self.audio, "process", None) is None
            if hasattr(self.audio, "process")
            else True
        )
        return stopped, chunks

    def _queue_pending_audio(self) -> tuple[bool, int]:
        offer = self._pending_audio_offer
        if offer is None:
            return True, 0
        if self.jobs is None or offer.owner is not self.audio:
            return False, 0
        # A normal live poll can legitimately have no closed segment during
        # startup.  It has no durable boundary or offer ID, so it must not be
        # handed to the planner (which rejects empty non-final chunks) or kept
        # as a retry barrier for later polls.
        if not offer.chunks and not offer.offer_id:
            self._pending_audio_offer = None
            return True, 0
        try:
            kwargs: dict[str, object] = {
                "now": offer.queued_at,
                "final": offer.final,
                "creation_mode": offer.creation_mode,
                "target_hash": offer.target_hash,
            }
            if offer.offer_id:
                kwargs.update({
                    "live_id": offer.live_id,
                    "boundary_start_ms": offer.boundary_start_ms,
                    "boundary_end_ms": offer.boundary_end_ms,
                })
            queued = self.jobs.queue_closed_chunks(  # type: ignore[attr-defined]
                offer.chunks, offer.wordlist, **kwargs
            )
            acknowledge = getattr(offer.owner, "ack", None)
            if callable(acknowledge):
                acknowledge(offer.offer_id or offer.chunks)
        except Exception:
            return False, 0
        self._pending_audio_offer = None
        return True, int(queued)

    def _quarantine_legacy_audio(self, now: float) -> bool:
        if self.audio is None or self.store is None:
            return False
        try:
            raw = getattr(self.audio, "quarantined_offers", ())
            offers = tuple(raw() if callable(raw) else raw)
        except Exception:
            return True
        if not offers:
            return False
        try:
            for offer in offers:
                if not isinstance(offer, AudioOffer) or not offer.legacy_quarantine:
                    raise ValueError("legacy audio quarantine invalid")
                self.store.record_legacy_audio_quarantine(  # type: ignore[attr-defined]
                    offer, created_at=float(now)
                )
        except Exception:
            return True
        return True

    def _queue_final_chunks(
        self,
        chunks: AudioOffer | tuple[ClosedAudioChunk, ...],
        *,
        now: float,
        authority_settings: ComplianceSettings | None = None,
    ) -> bool:
        if self.jobs is None or self.store is None or self.settings is None:
            return True
        if self._pending_audio_offer is not None:
            return False
        try:
            if isinstance(chunks, AudioOffer):
                wordlist = self.store.wordlist_version(  # type: ignore[attr-defined]
                    chunks.wordlist_version_id
                )
                observed = chunks.chunks
                queued_at = chunks.queued_at
                final = chunks.final
                creation_mode = chunks.creation_mode
                target_hash = chunks.target_hash
                offer_id = chunks.offer_id
            else:
                wordlist = self.store.active_wordlist()  # type: ignore[attr-defined]
                observed = tuple(chunks)
                if not observed:
                    return True
                queued_at = float(now)
                final = True
                creation_mode, target_hash = self._queue_authority(
                    authority_settings or self.settings
                )
                offer_id = ""
            if wordlist is not None:
                self._pending_audio_offer = _PendingAudioOffer(
                    owner=self.audio,
                    chunks=observed,
                    wordlist=wordlist,
                    queued_at=queued_at,
                    final=final,
                    creation_mode=creation_mode,
                    target_hash=target_hash,
                    offer_id=offer_id,
                    live_id=(
                        chunks.live_id if isinstance(chunks, AudioOffer)
                        else (
                            observed[0].live_id
                            if observed
                            and isinstance(observed[0], ClosedAudioChunk)
                            else ""
                        )
                    ),
                    boundary_start_ms=(
                        chunks.boundary_start_ms
                        if isinstance(chunks, AudioOffer)
                        else (
                            observed[0].capture_start_ms
                            if observed
                            and isinstance(observed[0], ClosedAudioChunk)
                            else 0
                        )
                    ),
                    boundary_end_ms=(
                        chunks.boundary_end_ms
                        if isinstance(chunks, AudioOffer)
                        else (
                            observed[-1].capture_end_ms
                            if observed
                            and isinstance(observed[-1], ClosedAudioChunk)
                            else 0
                        )
                    ),
                )
        except Exception:
            return False
        queued, _count = self._queue_pending_audio()
        return queued

    def _record_audio_stop_failure(
        self, now: float, *, allow_technical_notifications: bool | None = None,
    ) -> None:
        self.audio_start_blocked = True
        if self.store is None:
            return
        try:
            alert_due, _recovered = self.store.update_health_condition(  # type: ignore[attr-defined]
                "COMPLIANCE_AUDIO_BLIND", active=True, now=float(now)
            )
        except Exception:
            return
        technical_allowed = (
            self.settings is not None
            and self._effective_mode(self.settings) == "live"
        )
        if allow_technical_notifications is not None:
            technical_allowed = technical_allowed and bool(
                allow_technical_notifications
            )
        if alert_due and self.notifier is not None and technical_allowed:
            try:
                self.notifier.send_technical_issue(  # type: ignore[attr-defined]
                    "COMPLIANCE_AUDIO_BLIND",
                    "合规音频进程无法确认已停止，已阻止重复启动，请人工核查。",
                )
            except Exception:
                pass

    def _apply_settings(self, settings: ComplianceSettings) -> None:
        if self.store is None:
            if self.store_factory is None:
                from .store import ComplianceStore

                self.store_factory = ComplianceStore
            self.store = self.store_factory(settings.db_path)
        fingerprints = self._settings_fingerprints(settings)
        runtime_mode = self._effective_mode(settings)
        previous = dict(self._fingerprints)
        if self.notifier is None or (
            "notifier" in self._owned
            and previous.get("delivery") not in {None, fingerprints["delivery"]}
        ):
            self.notifier = self._construct_notifier()
        elif hasattr(self.notifier, "config"):
            self.notifier.config = self.config  # type: ignore[attr-defined]

        audio_changed = (
            previous.get("audio") is not None
            and previous["audio"] != fingerprints["audio"]
        )
        disabling = runtime_mode == "disabled" and self.audio is not None
        transition_pending = self._audio_transition_stopped
        audio_replacement_blocked = False
        if audio_changed or disabling or transition_pending:
            prior_settings = self.settings or settings
            pending_was_stopped = self._audio_transition_stopped
            pending_ok, _queued = self._queue_pending_audio()
            if not pending_ok:
                self.audio_start_blocked = True
                audio_replacement_blocked = True
            elif pending_was_stopped:
                stopped = True
                chunks: tuple[ClosedAudioChunk, ...] = ()
                self._audio_transition_stopped = False
            else:
                try:
                    final_wordlist = self.store.active_wordlist()  # type: ignore[attr-defined]
                    if final_wordlist is not None:
                        self._prepare_audio_offer(
                            final_wordlist,
                            now=self.clock(),
                            final=True,
                            settings=prior_settings,
                        )
                except Exception:
                    final_wordlist = None
                stopped, chunks = self._finish_audio(self.clock())
                if not stopped:
                    self._record_audio_stop_failure(
                        self.clock(),
                        allow_technical_notifications=(runtime_mode == "live"),
                    )
                    audio_replacement_blocked = True
                else:
                    queued_final = self._queue_final_chunks(
                        chunks,
                        now=self.clock(),
                        authority_settings=prior_settings,
                    )
                    if not queued_final:
                        self._audio_transition_stopped = True
                        self.audio_start_blocked = True
                        audio_replacement_blocked = True
            if not audio_replacement_blocked:
                self.audio = None
                self.audio_start_blocked = False

        if runtime_mode != "disabled":
            if self.wordlists is None or (
                "wordlists" in self._owned
                and previous.get("wordlist") not in {
                    None, fingerprints["wordlist"]
                }
            ):
                self.wordlists = self._construct_wordlists(settings)
            if self.recognizer is None or (
                "recognizer" in self._owned
                and previous.get("recognizer") not in {
                    None, fingerprints["recognizer"]
                }
            ):
                self.recognizer = self._construct_recognizer(settings)
            if self.target_provider is None and self.replay_manifest is None:
                self.target_provider = self._construct_target_provider()
            if (
                self.audio is None
                and not self.audio_start_blocked
                and not audio_replacement_blocked
            ):
                self.audio = self._construct_audio(settings)
            if self.jobs is None:
                self.jobs = AudioJobPlanner(self.store)
            if self.events is None:
                self.events = self._construct_events()

        if runtime_mode == "shadow" and self.replay_manifest is None:
            if self.repair is None or (
                "repair" in self._owned and audio_changed
            ):
                self.repair = self._construct_repair(settings)
        elif "repair" in self._owned:
            self.repair = None
            self._owned.discard("repair")

        self.settings = settings
        self._fingerprints = fingerprints
        if audio_replacement_blocked and "audio" in previous:
            self._fingerprints["audio"] = previous["audio"]

    def startup(self, now: float) -> None:
        if self._started:
            return
        self._started = True
        assert self.store is not None
        assert self.notifier is not None
        try:
            state = self.store.runtime_state()  # type: ignore[attr-defined]
            raw_pid = state["audio_ffmpeg_pid"]
            raw_marker = state["audio_marker"]
            raw_process_token = state["audio_process_token"]
        except Exception:
            raw_pid = None
            raw_marker = ""
            raw_process_token = ""
            self._record_audio_stop_failure(now)
        if raw_pid is not None:
            try:
                pid = int(raw_pid)
                marker = Path(str(raw_marker))
                identity_valid = (
                    pid > 0 and bool(str(raw_marker)) and marker.is_absolute()
                    and bool(str(raw_process_token))
                )
                terminated = identity_valid and bool(
                    self.owned_ffmpeg_terminator(
                        pid, marker, process_token=str(raw_process_token)
                    )
                )
            except Exception:
                terminated = False
            if terminated:
                try:
                    self.store.clear_audio_process(pid)  # type: ignore[attr-defined]
                except Exception:
                    self._record_audio_stop_failure(now)
            else:
                self._record_audio_stop_failure(now)
        if not self.audio_start_blocked and self.audio is not None:
            recover_intents = getattr(self.audio, "recover_final_intents", None)
            if callable(recover_intents):
                try:
                    recover_intents()
                except Exception:
                    self._record_audio_stop_failure(now)
        try:
            self.store.recover_expired_audio_leases(  # type: ignore[attr-defined]
                float(now)
            )
        except Exception:
            pass
        try:
            self.notifier.reconcile_sending()  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            self.store.force_wordlist_sync()  # type: ignore[attr-defined]
        except Exception:
            pass
        self._reconcile_audio_cleanup()

    @staticmethod
    def _lock_path(settings: ComplianceSettings) -> Path:
        return settings.db_path.with_name("compliance-listener.lock")

    def _begin_listener_process(self, settings: ComplianceSettings) -> None:
        if self.store is None:
            if self.store_factory is None:
                from .store import ComplianceStore

                self.store_factory = ComplianceStore
            self.store = self.store_factory(settings.db_path)
        started_at_ms = int(round(float(self.clock()) * 1000))
        started = self.store.begin_listener_process(  # type: ignore[attr-defined]
            started_at_ms
        )
        if started is not True:
            raise RuntimeError("compliance listener process state is unavailable")
        self._listener_started_at_ms = started_at_ms

    def _start_progress_watchdog(self, settings: ComplianceSettings) -> None:
        if (
            self.replay_manifest is not None
            or self.dependencies.get("progress_watchdog", True) is False
        ):
            return
        from .watchdog import (
            HeartbeatPublisher,
            WatchdogSettings,
            build_watchdog_command,
            process_identity,
        )

        pid = os.getpid()
        identity_reader = self.dependencies.get(
            "watchdog_identity_reader", process_identity
        )
        token_factory = self.dependencies.get(
            "watchdog_token_factory", lambda: secrets.token_hex(32)
        )
        token = token_factory()
        identity = identity_reader(pid)
        runtime_dir = settings.db_path.parent / "compliance"
        watchdog_settings = WatchdogSettings(
            heartbeat_path=runtime_dir / "listener-heartbeat.json",
            budget_path=runtime_dir / "watchdog-state.json",
            db_path=settings.db_path,
            parent_pid=pid,
            token=token,
            process_identity=identity,
        )
        heartbeat_factory = self.dependencies.get(
            "heartbeat_factory", HeartbeatPublisher
        )
        self._heartbeat = heartbeat_factory(
            watchdog_settings.heartbeat_path,
            pid=pid,
            token=token,
            process_identity=identity,
        )
        self._heartbeat.beat(force=True)
        command_builder = self.dependencies.get(
            "watchdog_command_builder", build_watchdog_command
        )
        command = command_builder(
            watchdog_settings, python_executable=sys.executable
        )
        spawn = self.dependencies.get("watchdog_spawner", subprocess.Popen)
        kwargs: dict[str, object] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
            "cwd": Path(__file__).resolve().parents[2],
        }
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000200
        else:
            kwargs["start_new_session"] = True
        child = spawn(command, **kwargs)
        if child.poll() is not None:
            raise RuntimeError("compliance progress watchdog failed to start")
        self._watchdog_process = child

    def _beat_progress_watchdog(self) -> None:
        if self._heartbeat is None:
            return
        try:
            self._heartbeat.beat()
        except Exception:
            pass

    def _stop_progress_watchdog(self) -> None:
        heartbeat = self._heartbeat
        child = self._watchdog_process
        if heartbeat is not None:
            try:
                heartbeat.mark_stopping()
            except Exception:
                pass
        if child is not None:
            try:
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=2.0)
            except Exception:
                try:
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=2.0)
                except Exception:
                    pass
        self._watchdog_process = None
        self._heartbeat = None

    def run(self, stop_event: object) -> None:
        initial_config = self.config_loader()
        initial_settings = ComplianceSettings.from_config(initial_config)
        lock_handle = self.lock_acquirer(self._lock_path(initial_settings))
        if lock_handle is None:
            return
        try:
            self.config = initial_config
            self._begin_listener_process(initial_settings)
            self._start_progress_watchdog(initial_settings)
            self._apply_settings(initial_settings)
            self.startup(self.clock())
            while not bool(stop_event.is_set()):  # type: ignore[attr-defined]
                self._beat_progress_watchdog()
                self.cycle(self.clock())
                self._beat_progress_watchdog()
                if self.settings is None:
                    delay = 30.0
                elif self._effective_mode(self.settings) == "disabled":
                    delay = 30.0
                else:
                    delay = min(float(self.settings.poll_seconds), 5.0)
                if self.stop_waiter is not None:
                    stopped = bool(self.stop_waiter(stop_event, delay))
                else:
                    wait = getattr(stop_event, "wait", None)
                    if callable(wait):
                        stopped = bool(wait(delay))
                    else:
                        self.sleep(delay)
                        stopped = bool(  # type: ignore[attr-defined]
                            stop_event.is_set()
                        )
                if stopped:
                    break
                self._beat_progress_watchdog()
        finally:
            try:
                self._stop_progress_watchdog()
                self.shutdown(self.clock())
            finally:
                close = getattr(self.store, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
                try:
                    lock_handle.close()  # type: ignore[attr-defined]
                except Exception:
                    pass

    def run_once(self) -> bool:
        initial_config = self.config_loader()
        initial_settings = ComplianceSettings.from_config(initial_config)
        lock_handle = self.lock_acquirer(self._lock_path(initial_settings))
        if lock_handle is None:
            return False
        try:
            self.config = initial_config
            self._begin_listener_process(initial_settings)
            self._apply_settings(initial_settings)
            self.startup(self.clock())
            self.cycle(self.clock())
            return True
        finally:
            try:
                self.shutdown(self.clock())
            finally:
                close = getattr(self.store, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
                try:
                    lock_handle.close()  # type: ignore[attr-defined]
                except Exception:
                    pass

    def shutdown(self, now: float | None = None) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        deadline = self.monotonic() + 30.0
        shutdown_now = float(self.clock() if now is None else now)
        if self.monotonic() >= deadline:
            return
        pending_ok, _queued = self._queue_pending_audio()
        if self.monotonic() >= deadline:
            return
        self._reconcile_audio_cleanup(deadline=deadline)
        if self.monotonic() >= deadline:
            return
        final_wordlist = None
        if self.store is not None and self.settings is not None:
            try:
                final_wordlist = self.store.active_wordlist()  # type: ignore[attr-defined]
                if final_wordlist is not None and self.audio is not None:
                    self._prepare_audio_offer(
                        final_wordlist,
                        now=shutdown_now,
                        final=True,
                        settings=self.settings,
                    )
            except Exception:
                pass
        finish_started_at = self.monotonic()
        if finish_started_at >= deadline:
            return
        if self.audio is None:
            stopped, chunks = True, ()
        else:
            remaining = deadline - finish_started_at
            finish_budget = min(5.0, remaining - 0.25)
            if finish_budget <= 0:
                return
            stopped, chunks = self._finish_audio(
                shutdown_now, timeout_seconds=finish_budget
            )
        if self.monotonic() >= deadline:
            return
        if not stopped:
            if self.monotonic() >= deadline:
                return
            self._record_audio_stop_failure(shutdown_now)
            if self.monotonic() >= deadline:
                return
        if pending_ok and stopped:
            if self.monotonic() >= deadline:
                return
            self._queue_final_chunks(chunks, now=shutdown_now)
        if self.monotonic() >= deadline:
            return
        # A shutdown is a durable handoff, not a second processing window.
        # Audio observations above are queued atomically before the capture is
        # released, so the next singleton can recover them.  Starting a
        # bounded recognizer worker here can outlive launchd's parent stop and
        # prevent that replacement listener from proving exclusive model
        # ownership.  Leave every queued/leased job for normal recovery.
        if self.notifier is None or self.settings is None:
            return
        effective_mode = self._effective_mode(self.settings)
        if effective_mode == "disabled":
            try:
                self.notifier.suppress_for_non_live_mode()  # type: ignore[attr-defined]
            except Exception:
                pass
            return
        if self.monotonic() >= deadline:
            return
        try:
            self.notifier.freeze_pending()  # type: ignore[attr-defined]
        except Exception:
            pass
        delivery_before_deadline = self.monotonic() < deadline
        if not delivery_before_deadline:
            return
        if effective_mode == "live":
            try:
                self.notifier.deliver_due(self.settings)  # type: ignore[attr-defined]
            except Exception:
                pass
        else:
            try:
                self.notifier.suppress_for_non_live_mode()  # type: ignore[attr-defined]
            except Exception:
                pass

    def _queue_main_recording_repair(
        self, now: float, *, effective_mode: str,
    ) -> int:
        if (
            effective_mode != "shadow"
            or self.replay_manifest is not None
            or self.repair is None
            or self.audio is None
            or self.audio_start_blocked
            or self._pending_audio_offer is not None
            or self._listener_started_at_ms <= 0
        ):
            return 0
        try:
            pending_recovery = getattr(
                self.audio, "has_pending_recovery", False
            )
            if callable(pending_recovery):
                pending_recovery = pending_recovery()
            if bool(pending_recovery):
                return 0
            queued = self.repair.scan_and_queue(  # type: ignore[attr-defined]
                float(now),
                listener_started_at_ms=self._listener_started_at_ms,
                limit=1,
            )
            if type(queued) is not int or queued not in {0, 1}:
                return 0
            return queued
        except Exception:
            return 0

    def cycle(self, now: float) -> CycleResult:
        try:
            config = self.config_loader()
        except Exception:
            return CycleResult()
        configured_settings = ComplianceSettings.from_config(config)
        settings = self._settings_for_current_process(configured_settings)
        self.config = config
        try:
            self._apply_settings(settings)
        except Exception:
            return CycleResult()
        assert self.store is not None
        effective_mode = self._effective_mode(settings)
        try:
            self.store.set_listener_mode(  # type: ignore[attr-defined]
                effective_mode
            )
        except Exception:
            return CycleResult()
        if self._quarantine_legacy_audio(now):
            try:
                quarantined_wordlist = self.store.active_wordlist()  # type: ignore[attr-defined]
            except Exception:
                quarantined_wordlist = None
            self._safe_health(now, settings, quarantined_wordlist)
            return CycleResult()
        self._reconcile_audio_cleanup()
        if effective_mode == "disabled":
            assert self.notifier is not None
            chunks: AudioOffer | tuple[ClosedAudioChunk, ...] = ()
            try:
                self.notifier.suppress_for_non_live_mode()  # type: ignore[attr-defined]
            except Exception:
                pass
            return CycleResult()
        assert self.wordlists is not None
        try:
            wordlist = self.wordlists.sync_if_due(now)  # type: ignore[attr-defined]
        except Exception:
            self._safe_health(now, settings, None)
            return CycleResult()
        if wordlist is None:
            self._safe_health(now, settings, wordlist)
            return CycleResult()
        queued = 0
        retried_offer = self._pending_audio_offer is not None
        if retried_offer:
            queued_ok, queued = self._queue_pending_audio()
            if not queued_ok:
                self._safe_health(now, settings, wordlist)
                return CycleResult()
        if not retried_offer and not self.audio_start_blocked:
            assert self.audio is not None
            # Recovered observation journals are already durable.  Queue and
            # acknowledge several of them before touching the model so a
            # restart backlog cannot postpone the current live capture for
            # minutes.  The fixed cap keeps a corrupt source from hot-looping.
            for _offer_index in range(64):
                chunks: AudioOffer | tuple[ClosedAudioChunk, ...] = ()
                try:
                    self._prepare_audio_offer(
                        wordlist, now=now, final=False, settings=settings
                    )
                    chunks = self.audio.poll(  # type: ignore[attr-defined]
                        int(now * 1000)
                    )
                except Exception:
                    if (
                        isinstance(chunks, AudioOffer)
                        and self._pending_audio_offer is None
                    ):
                        release = getattr(self.audio, "release_offer", None)
                        if callable(release):
                            try:
                                release(chunks.offer_id)
                            except Exception:
                                pass
                    self._safe_health(now, settings, wordlist)
                    return CycleResult(chunks_queued=int(queued))
                assert self.jobs is not None
                try:
                    if isinstance(chunks, AudioOffer):
                        durable_wordlist = self.store.wordlist_version(  # type: ignore[attr-defined]
                            chunks.wordlist_version_id
                        )
                        if durable_wordlist is None:
                            raise ValueError("audio offer wordlist missing")
                        observed = chunks.chunks
                        queued_at = chunks.queued_at
                        final = chunks.final
                        creation_mode = chunks.creation_mode
                        target_hash = chunks.target_hash
                        offer_id = chunks.offer_id
                    else:
                        durable_wordlist = wordlist
                        observed = tuple(chunks)
                        queued_at = float(now)
                        final = False
                        creation_mode, target_hash = self._queue_authority(settings)
                        offer_id = ""
                    self._pending_audio_offer = _PendingAudioOffer(
                        owner=self.audio,
                        chunks=observed,
                        wordlist=durable_wordlist,
                        queued_at=queued_at,
                        final=final,
                        creation_mode=creation_mode,
                        target_hash=target_hash,
                        offer_id=offer_id,
                        live_id=(
                            chunks.live_id if isinstance(chunks, AudioOffer)
                            else (
                                observed[0].live_id
                                if observed
                                and isinstance(observed[0], ClosedAudioChunk)
                                else ""
                            )
                        ),
                        boundary_start_ms=(
                            chunks.boundary_start_ms
                            if isinstance(chunks, AudioOffer)
                            else (
                                observed[0].capture_start_ms
                                if observed
                                and isinstance(observed[0], ClosedAudioChunk)
                                else 0
                            )
                        ),
                        boundary_end_ms=(
                            chunks.boundary_end_ms
                            if isinstance(chunks, AudioOffer)
                            else (
                                observed[-1].capture_end_ms
                                if observed
                                and isinstance(observed[-1], ClosedAudioChunk)
                                else 0
                            )
                        ),
                    )
                    queued_ok, queued_now = self._queue_pending_audio()
                    if not queued_ok:
                        raise RuntimeError(
                            "compliance audio offer was not queued"
                        )
                    queued += int(queued_now)
                except Exception:
                    if (
                        isinstance(chunks, AudioOffer)
                        and self._pending_audio_offer is None
                    ):
                        release = getattr(self.audio, "release_offer", None)
                        if callable(release):
                            try:
                                release(chunks.offer_id)
                            except Exception:
                                pass
                    self._safe_health(now, settings, wordlist)
                    return CycleResult(chunks_queued=int(queued))
                if not isinstance(chunks, AudioOffer):
                    break
        queued += self._queue_main_recording_repair(
            now, effective_mode=effective_mode,
        )
        if not self._model_start_allowed(now, newly_queued=int(queued)):
            self._safe_health(now, settings, wordlist)
            return CycleResult(chunks_queued=int(queued))
        try:
            recognizer_ready = self._ensure_recognizer_ready(now)
        except Exception:
            recognizer_ready = False
        if not recognizer_ready:
            self._safe_health(now, settings, wordlist)
            return CycleResult(chunks_queued=int(queued))
        try:
            recovered = self.store.recover_expired_audio_leases(  # type: ignore[attr-defined]
                now
            )
        except Exception:
            recovered = 0
        try:
            processed = self._process_one_job(now, wordlist)
        except Exception:
            processed = ProcessedJob()
        assert self.notifier is not None
        try:
            frozen = self.notifier.freeze_pending()  # type: ignore[attr-defined]
        except Exception:
            frozen = 0
        if effective_mode == "live":
            try:
                sent = self.notifier.deliver_due(settings)  # type: ignore[attr-defined]
            except Exception:
                sent = 0
        else:
            try:
                self.notifier.suppress_for_non_live_mode()  # type: ignore[attr-defined]
            except Exception:
                pass
            sent = 0
        self._safe_health(now, settings, wordlist)
        return CycleResult(
            chunks_queued=int(queued),
            leases_recovered=int(recovered),
            jobs_processed=int(processed.claimed),
            events_created=int(processed.events),
            cards_frozen=int(frozen),
            messages_sent=int(sent),
        )

    def _safe_health(
        self, now: float, settings: ComplianceSettings, wordlist: object,
    ) -> None:
        try:
            self._health(now, settings, wordlist)
        except Exception:
            pass

    def _job_processor(self) -> AudioJobProcessor:
        return AudioJobProcessor(
            store=self.store,
            recognizer=self.recognizer,
            events=self.events,
            settings=self.settings,
            context_builder=self.context_builder,
            file_remover=self.file_remover,
            source_duration_reader=self.source_duration_reader,
            monotonic=self.monotonic,
            replay_manifest=self.replay_manifest,
            cleanup_committed_callback=self._cleanup_committed_job,
        )

    def _model_start_allowed(self, now: float, *, newly_queued: int) -> bool:
        return self._job_processor()._model_start_allowed(
            now, newly_queued=newly_queued
        )

    def _ensure_recognizer_ready(self, now: float) -> bool:
        return self._job_processor()._ensure_recognizer_ready(now)

    def _process_one_job(
        self,
        now: float,
        wordlist: object,
        deadline: float | None = None,
    ) -> ProcessedJob:
        return self._job_processor()._process_one_job(
            now, wordlist, deadline=deadline
        )

    def _cleanup_committed_job(
        self, job: object, *, deadline: float | None = None,
    ) -> None:
        self._job_processor()._cleanup_committed_job(
            job, deadline=deadline
        )

    def _reconcile_audio_cleanup(
        self, *, deadline: float | None = None,
    ) -> None:
        self._job_processor()._reconcile_audio_cleanup(deadline=deadline)

    def _health(
        self, now: float, settings: ComplianceSettings, wordlist: object,
    ) -> None:
        del wordlist
        assert self.store is not None
        transitions = ComplianceHealthService(self.store).evaluate(
            now=float(now), settings=settings,
        )
        for transition in transitions:
            if transition.recovered:
                try:
                    self.recovery_logger(f"{transition.code}_RECOVERED")
                except Exception:
                    pass
            if (
                transition.alert_due
                and self.notifier is not None
                and self._effective_mode(settings) == "live"
            ):
                try:
                    self.notifier.send_technical_issue(  # type: ignore[attr-defined]
                        transition.code, transition.detail
                    )
                except Exception:
                    pass


def configure_logging(path: Path) -> logging.Logger:
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        log_path.parent.chmod(0o700)
    except OSError:
        pass
    logger = logging.getLogger("app.compliance.listener")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in tuple(logger.handlers):
        handler.close()
        logger.removeHandler(handler)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s"
    )
    rotating = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    try:
        log_path.chmod(0o600)
    except OSError:
        pass
    rotating.setFormatter(formatter)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    logger.addHandler(rotating)
    logger.addHandler(console)
    return logger


def deployed_commit(*, run: Callable[..., object] = subprocess.run) -> str:
    try:
        completed = run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[2],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            shell=False,
        )
        if getattr(completed, "returncode", 1) != 0:
            return "unknown"
        value = str(getattr(completed, "stdout", "") or "").strip()
        if len(value) != 40 or any(
            char not in "0123456789abcdef" for char in value
        ):
            return "unknown"
        return value
    except Exception:
        return "unknown"


def _default_config_loader() -> dict:
    from app.config import load_config

    return load_config()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Independent live compliance listener"
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--replay-manifest", type=Path)
    return parser


def _install_stop_signal_handlers(
    stop_event: object,
    *,
    signal_setter: Callable[[object, object], object] = signal.signal,
) -> Callable[[], None]:
    installed: list[tuple[object, object]] = []

    def request_stop(_signum: object, _frame: object) -> None:
        stop_event.set()  # type: ignore[attr-defined]

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous = signal_setter(signum, request_stop)
            installed.append((signum, previous))
    except (OSError, RuntimeError, ValueError):
        for signum, previous in reversed(installed):
            try:
                signal_setter(signum, previous)
            except (OSError, RuntimeError, ValueError):
                pass
        installed.clear()

    def restore() -> None:
        for signum, previous in installed:
            try:
                signal_setter(signum, previous)
            except (OSError, RuntimeError, ValueError):
                pass

    return restore


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.replay_manifest is not None and not args.replay_manifest.is_absolute():
        raise SystemExit("--replay-manifest must be absolute")
    root = Path(__file__).resolve().parents[2]
    os.chdir(root)
    logger = configure_logging(root / "data" / "logs" / "compliance.log")
    logger.info("compliance listener commit=%s", deployed_commit())
    listener = ComplianceListener(
        config_loader=_default_config_loader,
        replay_manifest=args.replay_manifest,
    )
    if args.once:
        return 0 if listener.run_once() else 1
    stop = threading.Event()
    restore_signals = _install_stop_signal_handlers(stop)
    try:
        listener.run(stop)
    except KeyboardInterrupt:
        stop.set()
    finally:
        restore_signals()
    return 0


__all__ = [
    "AudioJobPlanner", "ComplianceListener", "CycleResult", "ProcessedJob",
    "configure_logging", "deployed_commit", "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
