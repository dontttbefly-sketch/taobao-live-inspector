from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from .models import ClosedAudioChunk, WordlistSnapshot


@dataclass(frozen=True)
class ProcessedJob:
    claimed: bool = False
    events: int = 0


class AudioJobPlanner:
    def __init__(self, store: object) -> None:
        self.store = store

    @staticmethod
    def _valid_chunk(chunk: object) -> bool:
        return (
            isinstance(chunk, ClosedAudioChunk)
            and bool(chunk.chunk_key)
            and bool(chunk.live_id)
            and isinstance(chunk.path, Path)
            and chunk.path.is_absolute()
            and type(chunk.capture_start_ms) is int
            and type(chunk.capture_end_ms) is int
            and type(chunk.media_duration_ms) is int
            and chunk.capture_start_ms >= 0
            and chunk.capture_end_ms > chunk.capture_start_ms
            and chunk.capture_end_ms - chunk.capture_start_ms
            == chunk.media_duration_ms
            and type(chunk.delete_after_use) is bool
            and chunk.continuity in {"ok", "invalid"}
        )

    def _valid_records(self) -> list[tuple[object, tuple[ClosedAudioChunk, ...]]]:
        records: list[tuple[object, tuple[ClosedAudioChunk, ...]]] = []
        for row in self.store.audio_source_rows():  # type: ignore[attr-defined]
            if str(row["status"]) in {"blocked_timeline", "needs_attention"}:
                continue
            job, chunks = self.store.reconstruct_audio_job(  # type: ignore[attr-defined]
                row
            )
            records.append((job, chunks))
        return records

    def _existing_authority(
        self, job: object, fallback: tuple[str, str],
    ) -> tuple[str, str]:
        try:
            row = self.store.get_audio_job(  # type: ignore[attr-defined]
                str(job.job_key)  # type: ignore[attr-defined]
            )
            if row is None:
                return fallback
            return self.store.audio_job_authority(row)  # type: ignore[attr-defined]
        except Exception:
            raise ValueError("audio job delivery authority is invalid") from None

    def queue_closed_chunks(
        self,
        chunks: tuple[ClosedAudioChunk, ...],
        wordlist: WordlistSnapshot,
        *,
        now: float,
        final: bool = False,
        creation_mode: str = "shadow",
        target_hash: str = "",
        live_id: str = "",
        boundary_start_ms: int | None = None,
        boundary_end_ms: int | None = None,
    ) -> int:
        from .audio import plan_audio_job

        if not isinstance(wordlist, WordlistSnapshot):
            raise ValueError("compliance wordlist snapshot is invalid")
        observed = tuple(chunks)
        if any(not isinstance(chunk, ClosedAudioChunk) for chunk in observed):
            raise ValueError("audio closed chunk is invalid")
        if any(not self._valid_chunk(chunk) for chunk in observed):
            raise ValueError("audio closed chunk is invalid")
        observed_live_ids = {chunk.live_id for chunk in observed}
        batch_ambiguous = len(observed_live_ids) > 1
        if batch_ambiguous:
            for current in observed:
                self.store.record_blocked_audio_chunk(  # type: ignore[attr-defined]
                    current,
                    wordlist_version_id=wordlist.version_id,
                    created_at=float(now),
                )
            return 0
        records = self._valid_records()
        seen: dict[str, list[ClosedAudioChunk]] = {}
        for _job, sources in records:
            for source in sources:
                seen.setdefault(source.chunk_key, []).append(source)
        current_live_id = (
            next(iter(observed_live_ids))
            if len(observed_live_ids) == 1
            else ""
        )
        barrier = (
            self.store.latest_blocked_audio_observation(  # type: ignore[attr-defined]
                current_live_id
            )
            if current_live_id
            else None
        )
        if not observed and final:
            if (
                not isinstance(live_id, str)
                or not live_id
                or type(boundary_start_ms) is not int
                or type(boundary_end_ms) is not int
                or boundary_start_ms < 0
                or boundary_end_ms < boundary_start_ms
            ):
                raise ValueError("audio final boundary invalid")
            latest_by_chain: dict[
                tuple[str, str],
                tuple[object, tuple[ClosedAudioChunk, ...]],
            ] = {}
            for record in records:
                record_job, record_sources = record
                latest_by_chain[
                    (
                        str(record_job.live_id),  # type: ignore[attr-defined]
                        str(record_job.chain_id),  # type: ignore[attr-defined]
                    )
                ] = record
            open_chains = []
            for record in latest_by_chain.values():
                record_job, record_sources = record
                record_source = record_sources[-1]
                record_barrier = (
                    self.store.latest_blocked_audio_observation(  # type: ignore[attr-defined]
                        record_source.live_id
                    )
                )
                if (
                    record_source.live_id == live_id
                    and int(record_job.commit_end_ms)  # type: ignore[attr-defined]
                    < record_source.capture_end_ms
                    and (
                        record_barrier is None
                        or record_source.capture_end_ms
                        > record_barrier.capture_end_ms
                    )
                ):
                    open_chains.append(record)
            matching_open_chains = []
            mismatch_seen = False
            for record in open_chains:
                open_job, _open_sources = record
                chain_sources = tuple(
                    source.capture_start_ms
                    for record_job, record_sources in records
                    if (
                        str(record_job.live_id) == live_id  # type: ignore[attr-defined]
                        and str(record_job.chain_id)  # type: ignore[attr-defined]
                        == str(open_job.chain_id)  # type: ignore[attr-defined]
                    )
                    for source in record_sources
                )
                chain_start_ms = min(chain_sources)
                first_source_end_ms = min(
                    source.capture_end_ms
                    for record_job, record_sources in records
                    if (
                        str(record_job.live_id) == live_id  # type: ignore[attr-defined]
                        and str(record_job.chain_id)  # type: ignore[attr-defined]
                        == str(open_job.chain_id)  # type: ignore[attr-defined]
                    )
                    for source in record_sources
                    if source.capture_start_ms == chain_start_ms
                )
                if boundary_end_ms <= chain_start_ms:
                    # A durable empty-final offer can outlive the chain it
                    # originally closed.  When a newer chain later reuses the
                    # same live ID, acknowledging that older marker is safe:
                    # it contains no media and cannot close the newer chain.
                    continue
                chain_created_at: list[float] = []
                try:
                    for record_job, _record_sources in records:
                        if (
                            str(record_job.live_id) == live_id  # type: ignore[attr-defined]
                            and str(record_job.chain_id)  # type: ignore[attr-defined]
                            == str(open_job.chain_id)  # type: ignore[attr-defined]
                        ):
                            row = self.store.get_audio_job(  # type: ignore[attr-defined]
                                str(record_job.job_key)  # type: ignore[attr-defined]
                            )
                            if row is None:
                                raise ValueError
                            chain_created_at.append(float(row["created_at"]))
                except (KeyError, TypeError, ValueError):
                    raise ValueError("audio final boundary metadata invalid") from None
                if not chain_created_at:
                    raise ValueError("audio final boundary metadata invalid")
                if float(now) < min(chain_created_at):
                    # This empty marker was durably created before the open
                    # chain existed.  Wall-clock reconstruction can overlap
                    # the first seconds of the newer chain, so timestamps
                    # alone are insufficient to identify it as stale.
                    continue
                if (
                    boundary_start_ms >= first_source_end_ms
                    # The guard can flush already-buffered media while it is
                    # stopping, so a durable source may end slightly after
                    # the wall-clock shutdown marker.  The marker only needs
                    # to reach into the still-held tail; the source itself is
                    # the authoritative media boundary.
                    or boundary_end_ms <= int(open_job.commit_end_ms)
                ):
                    mismatch_seen = True
                    continue
                matching_open_chains.append(record)
            if len(matching_open_chains) > 1:
                raise ValueError("audio final boundary ambiguous")
            if not matching_open_chains:
                if mismatch_seen:
                    raise ValueError("audio final boundary mismatch")
                return 0
            open_chains = matching_open_chains
            live_records = open_chains
        else:
            started_at_ms = 0
            try:
                started_at_ms = int(
                    self.store.runtime_state()["listener_started_at_ms"]  # type: ignore[attr-defined]
                    or 0
                )
            except Exception:
                started_at_ms = 0
            live_records = [
                record for record in records
                if record[1][-1].live_id == current_live_id
                and (
                    barrier is None
                    or record[1][-1].capture_end_ms
                    > barrier.capture_end_ms
                )
                and (
                    started_at_ms <= 0
                    or record[1][-1].capture_end_ms > started_at_ms
                )
            ]
        latest_job = live_records[-1][0] if live_records else None
        previous = live_records[-1][1][-1] if live_records else None
        queued = 0
        chain_blocked = False

        if not observed and final and latest_job is not None and previous is not None:
            if int(latest_job.commit_end_ms) >= previous.capture_end_ms:
                return 0
            job = plan_audio_job(
                None,
                previous,
                int(latest_job.commit_end_ms),
                int(latest_job.wordlist_version_id),
                final=True,
                chain_id=str(latest_job.chain_id),
            )
            final_mode, final_target = self._existing_authority(
                latest_job, (creation_mode, target_hash)
            )
            return int(self.store.queue_audio_job(  # type: ignore[attr-defined]
                job,
                (previous,),
                created_at=float(now),
                creation_mode=final_mode,
                target_hash=final_target,
            ))

        for index, current in enumerate(observed):
            is_final = bool(final and index == len(observed) - 1)
            existing = seen.get(current.chunk_key, [])
            if existing:
                if any(item != current for item in existing):
                    self.store.record_blocked_audio_chunk(  # type: ignore[attr-defined]
                        current,
                        wordlist_version_id=wordlist.version_id,
                        created_at=float(now),
                    )
                    chain_blocked = True
                    continue
                if current.continuity == "ok":
                    self.store.record_valid_audio(  # type: ignore[attr-defined]
                        current.capture_end_ms,
                        live_id=current.live_id,
                    )
                if (
                    is_final
                    and latest_job is not None
                    and previous == current
                    and int(latest_job.commit_end_ms) < current.capture_end_ms
                ):
                    job = plan_audio_job(
                        None,
                        current,
                        int(latest_job.commit_end_ms),
                        int(latest_job.wordlist_version_id),
                        final=True,
                        chain_id=str(latest_job.chain_id),
                    )
                    final_mode, final_target = self._existing_authority(
                        latest_job, (creation_mode, target_hash)
                    )
                    inserted = self.store.queue_audio_job(  # type: ignore[attr-defined]
                        job,
                        (current,),
                        created_at=float(now),
                        creation_mode=final_mode,
                        target_hash=final_target,
                    )
                    queued += int(inserted)
                    if inserted:
                        latest_job = job
                continue
            if (
                barrier is not None
                and current.capture_start_ms < barrier.capture_end_ms
            ):
                self.store.record_blocked_audio_chunk(  # type: ignore[attr-defined]
                    current,
                    wordlist_version_id=wordlist.version_id,
                    created_at=float(now),
                )
                chain_blocked = True
                continue
            if current.continuity != "ok":
                self.store.record_blocked_audio_chunk(  # type: ignore[attr-defined]
                    current,
                    wordlist_version_id=wordlist.version_id,
                    created_at=float(now),
                    chain_id=(
                        str(latest_job.chain_id)
                        if latest_job is not None else ""
                    ),
                )
                chain_blocked = True
                continue
            if latest_job is not None and previous is not None:
                prior_mode, prior_target = self._existing_authority(
                    latest_job, (creation_mode, target_hash)
                )
                authority_changed = (
                    int(latest_job.wordlist_version_id) != wordlist.version_id
                    or prior_mode != creation_mode
                    or prior_target != target_hash
                )
                if authority_changed:
                    if int(latest_job.commit_end_ms) < previous.capture_end_ms:
                        tail = plan_audio_job(
                            None,
                            previous,
                            int(latest_job.commit_end_ms),
                            int(latest_job.wordlist_version_id),
                            final=True,
                            chain_id=str(latest_job.chain_id),
                        )
                        queued += int(self.store.queue_audio_job(  # type: ignore[attr-defined]
                            tail,
                            (previous,),
                            created_at=float(now),
                            creation_mode=prior_mode,
                            target_hash=prior_target,
                        ))
                    latest_job = None
                    previous = None
            if chain_blocked or (
                previous is not None
                and (
                    previous.live_id != current.live_id
                    or previous.capture_end_ms != current.capture_start_ms
                )
            ):
                self.store.record_blocked_audio_chunk(  # type: ignore[attr-defined]
                    current,
                    wordlist_version_id=wordlist.version_id,
                    created_at=float(now),
                    chain_id=(
                        str(latest_job.chain_id)
                        if latest_job is not None else ""
                    ),
                )
                chain_blocked = True
                continue
            if previous is None:
                cursor = current.capture_start_ms
                sources = (current,)
            else:
                cursor = int(latest_job.commit_end_ms)
                sources = (previous, current)
            job = plan_audio_job(
                previous,
                current,
                cursor,
                wordlist.version_id,
                final=is_final,
                chain_id=(
                    str(latest_job.chain_id) if previous is not None else ""
                ),
            )
            inserted = self.store.queue_audio_job(  # type: ignore[attr-defined]
                job,
                sources,
                created_at=float(now),
                creation_mode=creation_mode,
                target_hash=target_hash,
            )
            self.store.record_valid_audio(  # type: ignore[attr-defined]
                current.capture_end_ms,
                live_id=current.live_id,
            )
            queued += int(inserted)
            if inserted:
                latest_job = job
                previous = current
                records.append((job, sources))
                seen.setdefault(current.chunk_key, []).append(current)
        return queued


class AudioJobProcessor:
    def __init__(
        self,
        *,
        store: object | None,
        recognizer: object | None,
        events: object | None,
        settings: object | None,
        context_builder,
        file_remover,
        source_duration_reader,
        monotonic,
        replay_manifest: Path | None,
        cleanup_committed_callback,
    ) -> None:
        self.store = store
        self.recognizer = recognizer
        self.events = events
        self.settings = settings
        self.context_builder = context_builder
        self.file_remover = file_remover
        self.source_duration_reader = source_duration_reader
        self.monotonic = monotonic
        self.replay_manifest = replay_manifest
        self.cleanup_committed_callback = cleanup_committed_callback

    def _model_start_allowed(self, now: float, *, newly_queued: int) -> bool:
        """Do not let slow model loading delay establishment of live capture."""
        if int(newly_queued) > 0 or self.replay_manifest is not None:
            return True
        assert self.store is not None
        actionable = getattr(
            self.store, "oldest_actionable_audio_job_age", None)
        if callable(actionable):
            try:
                if actionable(float(now)) is not None:
                    return True
            except Exception:
                return False
        runtime_state = getattr(self.store, "runtime_state", None)
        if not callable(runtime_state):
            # Lightweight injected stores predate the runtime ownership API.
            return True
        try:
            raw_pid = runtime_state()["audio_ffmpeg_pid"]
            return type(raw_pid) is int and int(raw_pid) > 0
        except Exception:
            return False

    def _ensure_recognizer_ready(self, now: float) -> bool:
        assert self.store is not None
        if not self.store.model_attempt_due(now):  # type: ignore[attr-defined]
            return False
        assert self.recognizer is not None
        try:
            marked = self.store.mark_model_load_attempt()  # type: ignore[attr-defined]
        except Exception:
            return False
        if marked is not True:
            return False
        try:
            self.recognizer.ensure_ready()  # type: ignore[attr-defined]
        except Exception:
            failures = int(
                self.store.runtime_state()[  # type: ignore[attr-defined]
                    "model_failure_count"
                ]
            )
            delays = (30.0, 120.0, 300.0)
            delay = delays[min(failures, len(delays) - 1)]
            self.store.record_model_failure(  # type: ignore[attr-defined]
                now=float(now),
                error_class="model_load_failed",
                delay=delay,
            )
            return False
        return True

    def _process_one_job(
        self,
        now: float,
        wordlist: object,
        deadline: float | None = None,
    ) -> ProcessedJob:
        del wordlist
        assert self.store is not None
        lease_seconds = 600.0
        if deadline is not None:
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                return ProcessedJob()
            lease_seconds = min(lease_seconds, remaining)
        claimed = self.store.claim_due_audio_jobs(  # type: ignore[attr-defined]
            now=float(now), lease_seconds=lease_seconds, limit=1
        )
        if not claimed:
            return ProcessedJob()
        row = claimed[0]
        job_key = str(row["job_key"])
        attempts = int(row["attempts"])
        if deadline is not None and self.monotonic() >= deadline:
            return ProcessedJob(True, 0)
        try:
            job, sources = self.store.reconstruct_audio_job(  # type: ignore[attr-defined]
                row
            )
            creation_mode, target_hash = (
                self.store.audio_job_authority(row)  # type: ignore[attr-defined]
            )
            historical = self.store.wordlist_version(  # type: ignore[attr-defined]
                job.wordlist_version_id
            )
            if historical is None:
                raise ValueError("audio job wordlist is missing")
        except (KeyError, TypeError, ValueError):
            self.store.block_claimed_audio_job(  # type: ignore[attr-defined]
                job_key,
                now=float(now),
                error_class="audio_timeline_invalid",
            )
            return ProcessedJob(True, 0)

        previous = sources[0] if len(sources) == 2 else None
        current = sources[-1]
        if deadline is not None and self.monotonic() >= deadline:
            return ProcessedJob(True, 0)
        try:
            self._validate_source_files(sources)
        except Exception:
            if deadline is not None and self.monotonic() >= deadline:
                return ProcessedJob(True, 0)
            self.store.block_claimed_audio_job(  # type: ignore[attr-defined]
                job_key,
                now=float(now),
                error_class="audio_timeline_invalid",
            )
            return ProcessedJob(True, 0)
        if deadline is not None and self.monotonic() >= deadline:
            return ProcessedJob(True, 0)
        try:
            self._validate_context_destination(job, sources)
            copied_ms = self.context_builder(
                previous, current, job.context_path, 30_000
            )
            expected_overlap = (
                current.capture_start_ms - job.recognition_origin_ms
            )
            if (
                isinstance(copied_ms, bool)
                or not isinstance(copied_ms, int)
                or copied_ms != expected_overlap
                or self.source_duration_reader(job.context_path)
                != expected_overlap + current.media_duration_ms
            ):
                raise ValueError("audio context overlap is invalid")
        except Exception:
            if deadline is not None and self.monotonic() >= deadline:
                return ProcessedJob(True, 0)
            self._remove_context(job, sources, fail_cleanup=False)
            self.store.block_claimed_audio_job(  # type: ignore[attr-defined]
                job_key,
                now=float(now),
                error_class="audio_context_invalid",
            )
            return ProcessedJob(True, 0)

        if deadline is not None and self.monotonic() >= deadline:
            return ProcessedJob(True, 0)
        assert self.recognizer is not None
        try:
            marked = self.store.mark_model_load_attempt()  # type: ignore[attr-defined]
            if marked is not True:
                raise RuntimeError("model residency latch is unavailable")
            hotwords = tuple(entry.raw for entry in historical.entries)
            if deadline is None:
                batch = self.recognizer.transcribe(  # type: ignore[attr-defined]
                    job.context_path, hotwords,
                )
            else:
                remaining = deadline - self.monotonic()
                bounded = getattr(self.recognizer, "transcribe_bounded", None)
                if remaining <= 1.0 or not callable(bounded):
                    return ProcessedJob(True, 0)
                batch = bounded(
                    job.context_path,
                    hotwords,
                    timeout_seconds=remaining - 0.5,
                )
        except Exception as exc:
            if deadline is not None and self.monotonic() >= deadline:
                return ProcessedJob(True, 0)
            self._remove_context(job, sources, fail_cleanup=False)
            from .recognizer import TimestampContractError

            self._retry_claimed_job(
                job_key,
                now=float(now),
                attempts=attempts,
                error_class=(
                    "model_timestamp_invalid"
                    if isinstance(exc, TimestampContractError)
                    else "model_transcribe_failed"
                ),
            )
            return ProcessedJob(True, 0)

        if deadline is not None and self.monotonic() >= deadline:
            return ProcessedJob(True, 0)
        assert self.events is not None
        try:
            event_keys = self.events.commit(  # type: ignore[attr-defined]
                job,
                batch,
                historical,
                creation_mode,
                target_hash,
            )
        except Exception:
            if deadline is not None and self.monotonic() >= deadline:
                return ProcessedJob(True, 0)
            self._remove_context(job, sources, fail_cleanup=False)
            self._retry_claimed_job(
                job_key,
                now=float(now),
                attempts=attempts,
                error_class="event_commit_failed",
            )
            return ProcessedJob(True, 0)

        if deadline is not None and self.monotonic() >= deadline:
            return ProcessedJob(True, len(tuple(event_keys)))
        self.store.reset_model_failures()  # type: ignore[attr-defined]
        self.cleanup_committed_callback(job, deadline=deadline)
        return ProcessedJob(True, len(tuple(event_keys)))

    def _retry_claimed_job(
        self,
        job_key: str,
        *,
        now: float,
        attempts: int,
        error_class: str,
    ) -> None:
        assert self.store is not None
        delays = (30.0, 120.0, 300.0)
        delay = delays[min(max(int(attempts), 1), len(delays)) - 1]
        self.store.retry_audio_job(  # type: ignore[attr-defined]
            job_key,
            now=float(now),
            error_class=error_class,
            delay=delay,
        )
        self.store.record_model_failure(  # type: ignore[attr-defined]
            now=float(now), error_class=error_class, delay=delay
        )

    @staticmethod
    def _path_is_beneath(path: Path, root: Path) -> bool:
        try:
            candidate = path.absolute()
            boundary = root.absolute()
            return os.path.commonpath((str(candidate), str(boundary))) == str(
                boundary
            )
        except (OSError, ValueError):
            return False

    @staticmethod
    def _path_has_symlink(path: Path, root: Path) -> bool:
        current = path.absolute()
        boundary = root.absolute()
        while True:
            if current.is_symlink():
                return True
            if current == boundary:
                return False
            if current.parent == current:
                return True
            current = current.parent

    def _context_root(self, current: ClosedAudioChunk) -> Path:
        assert self.settings is not None
        return (
            self.settings.audio_dir
            if current.delete_after_use
            else current.path.parent
        )

    def _validate_source_files(
        self, sources: tuple[ClosedAudioChunk, ...],
    ) -> None:
        assert self.settings is not None
        for source in sources:
            root = (
                self.settings.audio_dir
                if source.delete_after_use
                else source.path.parent
            )
            if (
                self._path_has_symlink(source.path, root)
                or (
                    source.delete_after_use
                    and
                    not self._path_is_beneath(
                        source.path, self.settings.audio_dir
                    )
                )
            ):
                raise ValueError("audio source ownership is invalid")
            if (
                self.source_duration_reader(source.path)
                != source.media_duration_ms
            ):
                raise ValueError("audio source duration is invalid")

    def _validate_context_destination(
        self,
        job: object,
        sources: tuple[ClosedAudioChunk, ...],
    ) -> None:
        current = sources[-1]
        context_path = Path(job.context_path)  # type: ignore[attr-defined]
        expected = current.path.with_name(f".{current.path.stem}.context.wav")
        root = self._context_root(current)
        if (
            context_path != expected
            or not self._path_is_beneath(context_path, root)
            or self._path_has_symlink(context_path, root)
        ):
            raise ValueError("audio context ownership is invalid")

    def _remove_context(
        self,
        job: object,
        sources: tuple[ClosedAudioChunk, ...],
        *,
        fail_cleanup: bool,
        deadline: float | None = None,
    ) -> str:
        assert self.store is not None
        context_path = Path(job.context_path)  # type: ignore[attr-defined]
        try:
            self._validate_context_destination(job, sources)
        except ValueError:
            return "failed"
        if self.store.has_pending_context_reference(  # type: ignore[attr-defined]
            context_path, excluding_job_key=str(job.job_key)  # type: ignore[attr-defined]
        ):
            return "retained"
        if deadline is not None and self.monotonic() >= deadline:
            return "retained"
        try:
            self.file_remover(context_path)
            return "deleted"
        except Exception:
            return "failed"

    def _cleanup_committed_job(
        self, job: object, *, deadline: float | None = None,
    ) -> None:
        assert self.store is not None
        assert self.settings is not None
        source_cleanup_ready, candidates = self.store.audio_cleanup_plan(  # type: ignore[attr-defined]
            str(job.job_key)  # type: ignore[attr-defined]
        )
        if candidates:
            safe = all(
                self._path_is_beneath(path, self.settings.audio_dir)
                and not self._path_has_symlink(path, self.settings.audio_dir)
                for path in candidates
            )
            if not safe:
                self.store.finalize_audio_cleanup(  # type: ignore[attr-defined]
                    str(job.job_key), "failed"  # type: ignore[attr-defined]
                )
                return
            else:
                try:
                    for path in candidates:
                        if (
                            deadline is not None
                            and self.monotonic() >= deadline
                        ):
                            return
                        self.file_remover(path)
                except Exception:
                    self.store.finalize_audio_cleanup(  # type: ignore[attr-defined]
                        str(job.job_key), "failed"  # type: ignore[attr-defined]
                    )
                    return
        row = self.store.get_audio_job(  # type: ignore[attr-defined]
            str(job.job_key)  # type: ignore[attr-defined]
        )
        if row is not None:
            try:
                _durable_job, sources = self.store.reconstruct_audio_job(  # type: ignore[attr-defined]
                    row
                )
            except ValueError:
                return
            context_outcome = self._remove_context(
                job, sources, fail_cleanup=True, deadline=deadline
            )
            if not source_cleanup_ready:
                return
            if context_outcome == "deleted":
                if deadline is not None and self.monotonic() >= deadline:
                    return
                self.store.finalize_audio_cleanup(  # type: ignore[attr-defined]
                    str(job.job_key), "deleted"  # type: ignore[attr-defined]
                )
            elif context_outcome == "failed":
                if deadline is not None and self.monotonic() >= deadline:
                    return
                self.store.finalize_audio_cleanup(  # type: ignore[attr-defined]
                    str(job.job_key), "failed"  # type: ignore[attr-defined]
                )

    def _reconcile_audio_cleanup(
        self, *, deadline: float | None = None,
    ) -> None:
        if self.store is None or self.settings is None:
            return
        try:
            rows = tuple(
                self.store.audio_cleanup_reconciliation_rows()  # type: ignore[attr-defined]
            )
        except Exception:
            return
        for row in rows:
            if deadline is not None and self.monotonic() >= deadline:
                return
            try:
                job, _sources = self.store.reconstruct_audio_job(  # type: ignore[attr-defined]
                    row
                )
                self.cleanup_committed_callback(job, deadline=deadline)
            except Exception:
                if deadline is not None and self.monotonic() >= deadline:
                    return
                try:
                    self.store.finalize_audio_cleanup(  # type: ignore[attr-defined]
                        str(row["job_key"]), "failed"
                    )
                except Exception:
                    pass


__all__ = ["AudioJobPlanner", "AudioJobProcessor", "ProcessedJob"]
