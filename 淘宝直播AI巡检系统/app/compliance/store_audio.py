from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import TYPE_CHECKING

from .codec import canonical_json, decode_audio_sources, encode_audio_sources
from .models import (
    AudioJob,
    AudioOffer,
    ClosedAudioChunk,
    ComplianceEventDraft,
)
from .process_control import coerce_process_identity, read_process_identity
from .store_schema import LISTENER_MODES, SOURCE_KINDS

if TYPE_CHECKING:
    from .repair import Interval


MODEL_ERROR_CLASSES = frozenset({
    "model_load_failed", "model_transcribe_failed",
    "model_timestamp_invalid", "event_commit_failed",
})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class StoreAudioMixin:
    def active_live_ids(self) -> tuple[str, ...]:
        table = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='streams'"
        ).fetchone()
        if table is None:
            return ()
        recording = self.conn.execute(
            "SELECT DISTINCT live_id FROM streams "
            "WHERE status='recording' "
            "AND live_id IS NOT NULL AND live_id<>'' "
            "ORDER BY live_id"
        ).fetchall()
        if recording:
            return tuple(str(row[0]) for row in recording)
        rows = self.conn.execute(
            "SELECT DISTINCT live_id FROM streams "
            "WHERE status='recovering' "
            "AND live_id IS NOT NULL AND live_id<>'' "
            "ORDER BY live_id"
        )
        return tuple(str(row[0]) for row in rows)

    def active_recording_sessions(self) -> tuple[tuple[str, Path], ...]:
        """Return current main-recorder identities without claiming them."""
        columns = {
            str(row["name"])
            for row in self.conn.execute("PRAGMA table_info(streams)")
        }
        if not {"id", "status", "live_id", "session_dir"} <= columns:
            return ()
        rows = self.conn.execute(
            "SELECT live_id,session_dir FROM streams "
            "WHERE status='recording' AND live_id IS NOT NULL AND live_id<>'' "
            "AND session_dir IS NOT NULL AND session_dir<>'' ORDER BY id"
        ).fetchall()
        sessions: list[tuple[str, Path]] = []
        for row in rows:
            live_id = str(row["live_id"])
            raw_directory = str(row["session_dir"])
            if live_id.strip() and raw_directory.strip():
                sessions.append((live_id, Path(raw_directory)))
        return tuple(sessions)

    def owned_commit_intervals(
        self, live_id: str, *, before_ms: int,
    ) -> tuple["Interval", ...]:
        from .repair import Interval, merge_intervals

        if not isinstance(live_id, str) or not live_id:
            raise ValueError("invalid compliance repair live identity")
        if (
            isinstance(before_ms, bool)
            or not isinstance(before_ms, int)
            or before_ms < 0
        ):
            raise ValueError("invalid compliance repair cutoff")
        if before_ms == 0:
            return ()
        rows = self.conn.execute(
            "SELECT commit_start_ms,commit_end_ms FROM compliance_audio_jobs "
            "WHERE live_id=? AND status IN "
            "('queued','retry_wait','recognizing','committed') "
            "AND commit_start_ms<? ORDER BY commit_start_ms,commit_end_ms,job_key",
            (live_id, before_ms),
        ).fetchall()
        intervals = tuple(
            Interval(
                int(row["commit_start_ms"]),
                min(int(row["commit_end_ms"]), before_ms),
            )
            for row in rows
            if int(row["commit_end_ms"]) > int(row["commit_start_ms"])
            and min(int(row["commit_end_ms"]), before_ms)
            > int(row["commit_start_ms"])
        )
        return merge_intervals(intervals)

    def has_unsettled_realtime_recovery(self) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM compliance_audio_jobs "
            "WHERE source_kind='realtime' "
            "AND status IN ('queued','retry_wait','recognizing') LIMIT 1"
        ).fetchone() is not None

    def has_unsettled_main_repair(self) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM compliance_audio_jobs "
            "WHERE source_kind='main_repair' "
            "AND status IN ('queued','retry_wait','recognizing') LIMIT 1"
        ).fetchone() is not None

    def repair_exclusion_intervals(
        self, live_id: str, *, before_ms: int,
    ) -> tuple["Interval", ...]:
        """Return every previously attempted repair to prevent hot loops."""
        from .repair import Interval, merge_intervals

        if not isinstance(live_id, str) or not live_id:
            raise ValueError("invalid compliance repair live identity")
        if (
            isinstance(before_ms, bool)
            or not isinstance(before_ms, int)
            or before_ms < 0
        ):
            raise ValueError("invalid compliance repair cutoff")
        if before_ms == 0:
            return ()
        rows = self.conn.execute(
            "SELECT commit_start_ms,commit_end_ms FROM compliance_audio_jobs "
            "WHERE live_id=? AND source_kind='main_repair' "
            "AND commit_start_ms<? ORDER BY commit_start_ms,commit_end_ms,job_key",
            (live_id, before_ms),
        ).fetchall()
        return merge_intervals(tuple(
            Interval(
                int(row["commit_start_ms"]),
                min(int(row["commit_end_ms"]), before_ms),
            )
            for row in rows
            if min(int(row["commit_end_ms"]), before_ms)
            > int(row["commit_start_ms"])
        ))

    def wordlist_version_at(self, at_ms: int) -> int | None:
        if (
            isinstance(at_ms, bool)
            or not isinstance(at_ms, int)
            or at_ms < 0
        ):
            raise ValueError("invalid compliance wordlist history time")
        row = self.conn.execute(
            "SELECT id FROM compliance_wordlist_versions "
            "WHERE activated_at<=? ORDER BY activated_at DESC,id DESC LIMIT 1",
            (at_ms / 1000.0,),
        ).fetchone()
        return None if row is None else int(row["id"])

    def active_recording_media_urls(self, live_id: str) -> tuple[str, ...]:
        """Read the main recorder's private restart URL without mutating it."""
        target = str(live_id or "")
        if not target:
            return ()
        columns = {
            str(row["name"])
            for row in self.conn.execute("PRAGMA table_info(streams)")
        }
        if "resume_url" not in columns:
            return ()
        rows = self.conn.execute(
            "SELECT resume_url FROM streams "
            "WHERE status IN ('recording','recovering') AND live_id=? "
            "AND resume_url IS NOT NULL AND resume_url<>'' "
            "ORDER BY CASE status WHEN 'recording' THEN 0 ELSE 1 END,id DESC",
            (target,),
        )
        return tuple(dict.fromkeys(
            str(row[0]) for row in rows if str(row[0] or "")
        ))

    def set_audio_process(
        self, pid: int, marker: str, live_id: str, process_token: str,
    ) -> None:
        if (
            int(pid) <= 0 or not str(marker) or not str(live_id)
            or not isinstance(process_token, str) or not process_token
        ):
            raise ValueError("invalid compliance audio process identity")
        with self._immediate():
            current = self.conn.execute(
                "SELECT audio_ffmpeg_pid,audio_marker,current_live_id,"
                "audio_process_token "
                "FROM compliance_runtime_state WHERE singleton_id=1"
            ).fetchone()
            if current is None:
                raise RuntimeError("compliance runtime state is missing")
            current_pid = current["audio_ffmpeg_pid"]
            identity = (
                int(pid), str(marker), str(live_id), str(process_token),
            )
            if current_pid is not None:
                existing = (
                    int(current_pid), str(current["audio_marker"]),
                    str(current["current_live_id"]),
                    str(current["audio_process_token"]),
                )
                if existing != identity:
                    raise RuntimeError("compliance audio process already owned")
                return
            self.conn.execute(
                "UPDATE compliance_runtime_state "
                "SET audio_ffmpeg_pid=?,audio_marker=?,current_live_id=?,"
                "audio_process_token=?,"
                "updated_at=datetime('now','localtime') WHERE singleton_id=1",
                identity,
            )

    def clear_audio_process(self, pid: int | None = None) -> None:
        with self._immediate():
            if pid is None:
                self.conn.execute(
                "UPDATE compliance_runtime_state SET audio_ffmpeg_pid=NULL,"
                "audio_marker='',audio_process_token='',current_live_id='',"
                    "updated_at=datetime('now','localtime') WHERE singleton_id=1"
                )
            else:
                self.conn.execute(
                    "UPDATE compliance_runtime_state SET audio_ffmpeg_pid=NULL,"
                    "audio_marker='',audio_process_token='',current_live_id='',"
                    "updated_at=datetime('now','localtime') "
                    "WHERE singleton_id=1 AND audio_ffmpeg_pid=?",
                    (int(pid),),
                )

    def runtime_state(self) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM compliance_runtime_state WHERE singleton_id=1"
        ).fetchone()
        if row is None:  # pragma: no cover - protected by the schema singleton
            raise RuntimeError("compliance runtime state is missing")
        return row

    def set_listener_mode(self, mode: str) -> None:
        if mode not in LISTENER_MODES:
            raise ValueError("invalid compliance listener mode")
        with self._immediate():
            self.conn.execute(
                "UPDATE compliance_runtime_state SET listener_mode=?,"
                "updated_at=datetime('now','localtime') WHERE singleton_id=1",
                (mode,),
            )

    def record_valid_audio(self, now_ms: int, *, live_id: str = "") -> None:
        if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
            raise ValueError("invalid compliance audio observation time")
        if not isinstance(live_id, str):
            raise ValueError("invalid compliance audio live identity")
        observed_live_id = live_id
        if not observed_live_id:
            active = self.active_live_ids()
            if len(active) == 1:
                observed_live_id = active[0]
        with self._immediate():
            if observed_live_id:
                self.conn.execute(
                    "UPDATE compliance_runtime_state SET last_valid_audio_ms="
                    "CASE WHEN last_valid_audio_live_id=? "
                    "THEN MAX(last_valid_audio_ms,?) ELSE ? END,"
                    "last_valid_audio_live_id=?,"
                    "updated_at=datetime('now','localtime') "
                    "WHERE singleton_id=1",
                    (
                        observed_live_id,
                        now_ms,
                        now_ms,
                        observed_live_id,
                    ),
                )
            else:
                self.conn.execute(
                    "UPDATE compliance_runtime_state SET last_valid_audio_ms="
                    "MAX(last_valid_audio_ms,?),"
                    "updated_at=datetime('now','localtime') "
                    "WHERE singleton_id=1",
                    (now_ms,),
                )

    def bind_audio_health_live(self, live_id: str, *, now: float) -> int:
        if (
            not isinstance(live_id, str)
            or not math.isfinite(float(now))
            or float(now) < 0
        ):
            raise ValueError("invalid compliance audio health identity")
        with self._immediate():
            row = self.conn.execute(
                "SELECT health_json,audio_health_live_id,"
                "last_valid_audio_ms,last_valid_audio_live_id "
                "FROM compliance_runtime_state WHERE singleton_id=1"
            ).fetchone()
            if row is None:
                raise RuntimeError("compliance runtime state is missing")
            state = self._decode_health_json(row["health_json"])
            if str(row["audio_health_live_id"]) != live_id:
                state["COMPLIANCE_AUDIO_BLIND"] = {
                    "active": False,
                    "last_alert_at": 0.0,
                    "observed_since": float(now),
                }
                encoded = canonical_json(state)
                self.conn.execute(
                    "UPDATE compliance_runtime_state SET "
                    "audio_health_live_id=?,health_json=?,"
                    "updated_at=datetime('now','localtime') "
                    "WHERE singleton_id=1",
                    (live_id, encoded),
                )
            if (
                live_id
                and str(row["last_valid_audio_live_id"]) == live_id
            ):
                return int(row["last_valid_audio_ms"])
            return 0

    def recover_expired_audio_leases(self, now: float) -> int:
        with self._immediate():
            return self.conn.execute(
                "UPDATE compliance_audio_jobs SET status='queued',"
                "lease_until=0,updated_at=? WHERE status='recognizing' "
                "AND lease_until<=?",
                (float(now), float(now)),
            ).rowcount

    def quarantine_shadow_backlog(self, *, before: float, now: float) -> int:
        """Retain superseded shadow work without letting it block new capture.

        Shadow jobs can never be delivered.  A cutover may therefore retire
        only its old, unclaimed queue entries and invalid-timeline barriers to
        ``needs_attention`` while keeping their source evidence intact.  Active
        recognition is excluded: its owner must finish or expire normally
        before an operator retries the same bounded maintenance action.
        """
        if (
            isinstance(before, bool)
            or not isinstance(before, (int, float))
            or not math.isfinite(float(before))
            or isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(float(now))
            or float(now) < float(before)
        ):
            raise ValueError("invalid shadow backlog cutover")
        with self._immediate():
            return self.conn.execute(
                "UPDATE compliance_audio_jobs SET status='needs_attention',"
                "error_class='shadow_backlog_superseded',lease_until=0,"
                "updated_at=? WHERE creation_mode='shadow' "
                "AND status IN ('queued','retry_wait','blocked_timeline') "
                "AND created_at<?",
                (float(now), float(before)),
            ).rowcount

    def retry_audio_job(
        self,
        job_key: str,
        *,
        now: float,
        error_class: str,
        delay: float,
    ) -> bool:
        if float(delay) not in {30.0, 120.0, 300.0}:
            raise ValueError("invalid compliance retry delay")
        safe_error = (
            error_class
            if error_class in MODEL_ERROR_CLASSES
            else "model_transcribe_failed"
        )
        with self._immediate():
            changed = self.conn.execute(
                "UPDATE compliance_audio_jobs SET status='retry_wait',"
                "next_attempt_at=?,lease_until=0,error_class=?,updated_at=? "
                "WHERE job_key=? AND status='recognizing'",
                (
                    float(now) + float(delay), safe_error, float(now),
                    str(job_key),
                ),
            ).rowcount
            return changed == 1

    def block_claimed_audio_job(
        self, job_key: str, *, now: float,
        error_class: str = "audio_timeline_invalid",
    ) -> bool:
        safe_error = (
            error_class
            if error_class in {"audio_timeline_invalid", "audio_context_invalid"}
            else "audio_timeline_invalid"
        )
        with self._immediate():
            changed = self.conn.execute(
                "UPDATE compliance_audio_jobs SET status='blocked_timeline',"
                "lease_until=0,error_class=?,updated_at=? "
                "WHERE job_key=? AND status='recognizing'",
                (safe_error, float(now), str(job_key)),
            ).rowcount
            return changed == 1

    def record_model_failure(
        self, *, now: float, error_class: str, delay: float,
    ) -> int:
        del error_class
        if float(delay) not in {30.0, 120.0, 300.0}:
            raise ValueError("invalid compliance model retry delay")
        with self._immediate():
            self.conn.execute(
                "UPDATE compliance_runtime_state SET "
                "model_failure_count=model_failure_count+1,"
                "next_model_attempt_at=?,"
                "updated_at=datetime('now','localtime') WHERE singleton_id=1",
                (float(now) + float(delay),),
            )
            row = self.conn.execute(
                "SELECT model_failure_count FROM compliance_runtime_state "
                "WHERE singleton_id=1"
            ).fetchone()
            if row is None:  # pragma: no cover - singleton schema guarantee
                raise RuntimeError("compliance runtime state is missing")
            return int(row["model_failure_count"])

    def begin_listener_process(
        self, started_at_ms: int = 0, *, process_identity_reader=None,
    ) -> bool:
        """Clear residency only after any persisted worker identity is gone."""
        if (
            isinstance(started_at_ms, bool)
            or not isinstance(started_at_ms, int)
            or started_at_ms < 0
        ):
            raise ValueError("invalid compliance listener start time")
        row = self.conn.execute(
            "SELECT model_worker_pid,model_worker_start_token "
            "FROM compliance_runtime_state WHERE singleton_id=1"
        ).fetchone()
        if row is None:
            return False
        worker_pid = row["model_worker_pid"]
        start_token = row["model_worker_start_token"]
        if worker_pid is None:
            if start_token != "":
                return False
            with self._immediate():
                changed = self.conn.execute(
                    "UPDATE compliance_runtime_state SET model_may_be_loaded=0,"
                    "listener_started_at_ms=MAX(listener_started_at_ms,?),"
                    "updated_at=datetime('now','localtime') "
                    "WHERE singleton_id=1 AND model_worker_pid IS NULL "
                    "AND model_worker_start_token=''",
                    (int(started_at_ms),),
                ).rowcount
                return changed == 1
        if (
            type(worker_pid) is not int
            or int(worker_pid) <= 0
            or not isinstance(start_token, str)
            or not start_token
        ):
            return False
        if process_identity_reader is None:
            process_identity_reader = read_process_identity
        try:
            identity = process_identity_reader(int(worker_pid))
        except Exception:
            return False
        try:
            candidate = coerce_process_identity(identity)
        except (TypeError, ValueError):
            return False
        if candidate is not None and candidate.start_token == start_token:
            # An exact worker remains owned; a same-token argv mismatch is
            # unprovable and therefore also fails closed.
            return False
        with self._immediate():
            changed = self.conn.execute(
                "UPDATE compliance_runtime_state SET model_worker_pid=NULL,"
                "model_worker_start_token='',model_may_be_loaded=0,"
                "listener_started_at_ms=MAX(listener_started_at_ms,?),"
                "updated_at=datetime('now','localtime') "
                "WHERE singleton_id=1 AND model_worker_pid=? "
                "AND model_worker_start_token=?",
                (int(started_at_ms), int(worker_pid), start_token),
            ).rowcount
            return changed == 1

    def mark_model_worker_started(self, pid: int, start_token: str) -> bool:
        """Persist exact bounded-worker ownership before sending its payload."""
        if (
            type(pid) is not int
            or int(pid) <= 0
            or not isinstance(start_token, str)
            or not start_token
        ):
            raise ValueError("invalid compliance model worker identity")
        with self._immediate():
            row = self.conn.execute(
                "SELECT model_worker_pid,model_worker_start_token "
                "FROM compliance_runtime_state WHERE singleton_id=1"
            ).fetchone()
            if row is None:
                return False
            existing = (row["model_worker_pid"], row["model_worker_start_token"])
            if existing == (int(pid), start_token):
                changed = self.conn.execute(
                    "UPDATE compliance_runtime_state SET model_may_be_loaded=1,"
                    "updated_at=datetime('now','localtime') "
                    "WHERE singleton_id=1"
                ).rowcount
                return changed == 1
            if existing != (None, ""):
                return False
            changed = self.conn.execute(
                "UPDATE compliance_runtime_state SET model_worker_pid=?,"
                "model_worker_start_token=?,model_may_be_loaded=1,"
                "updated_at=datetime('now','localtime') "
                "WHERE singleton_id=1 AND model_worker_pid IS NULL "
                "AND model_worker_start_token=''",
                (int(pid), start_token),
            ).rowcount
            return changed == 1

    def clear_model_worker(self, pid: int, start_token: str) -> bool:
        """CAS-clear worker ownership while keeping the residency latch set."""
        if (
            type(pid) is not int
            or int(pid) <= 0
            or not isinstance(start_token, str)
            or not start_token
        ):
            return False
        with self._immediate():
            changed = self.conn.execute(
                "UPDATE compliance_runtime_state SET model_worker_pid=NULL,"
                "model_worker_start_token='',"
                "updated_at=datetime('now','localtime') "
                "WHERE singleton_id=1 AND model_worker_pid=? "
                "AND model_worker_start_token=?",
                (int(pid), start_token),
            ).rowcount
            return changed == 1

    def mark_model_load_attempt(self) -> bool:
        """Latch residency before model code can allocate or load any weights."""
        with self._immediate():
            changed = self.conn.execute(
                "UPDATE compliance_runtime_state SET model_may_be_loaded=1,"
                "updated_at=datetime('now','localtime') WHERE singleton_id=1"
            ).rowcount
            return changed == 1

    def reset_model_failures(self) -> bool:
        with self._immediate():
            changed = self.conn.execute(
                "UPDATE compliance_runtime_state SET model_failure_count=0,"
                "next_model_attempt_at=0,"
                "updated_at=datetime('now','localtime') WHERE singleton_id=1"
            ).rowcount
            return changed == 1

    def model_attempt_due(self, now: float) -> bool:
        return float(self.runtime_state()["next_model_attempt_at"]) <= float(now)

    def oldest_actionable_audio_job_age(self, now: float) -> float | None:
        row = self.conn.execute(
            "SELECT MIN(created_at) AS oldest FROM compliance_audio_jobs "
            "WHERE status IN ('queued','retry_wait') AND next_attempt_at<=?",
            (float(now),),
        ).fetchone()
        if row is None or row["oldest"] is None:
            return None
        return max(0.0, float(now) - float(row["oldest"]))

    @staticmethod
    def _validate_audio_job_sources(
        job: AudioJob, source_chunks: tuple[ClosedAudioChunk, ...],
    ) -> None:
        if (
            not source_chunks or len(source_chunks) > 2
            or job.continuity != "ok"
            or job.source_kind not in SOURCE_KINDS
        ):
            raise ValueError("audio job source invalid")
        if any(
            chunk.live_id != job.live_id
            or chunk.continuity != "ok"
            or not chunk.path.is_absolute()
            or chunk.capture_end_ms <= chunk.capture_start_ms
            or chunk.capture_end_ms - chunk.capture_start_ms
            != chunk.media_duration_ms
            for chunk in source_chunks
        ):
            raise ValueError("audio job source invalid")
        current = source_chunks[-1]
        if len(source_chunks) == 2:
            previous = source_chunks[0]
            if previous.capture_end_ms != current.capture_start_ms:
                raise ValueError("audio job source invalid")
            expected_origin = previous.capture_end_ms - min(
                30_000, previous.media_duration_ms
            )
        else:
            expected_origin = current.capture_start_ms
        key_material = (
            f"{job.live_id}\0{current.chunk_key}\0"
            f"{job.commit_start_ms}\0{job.commit_end_ms}"
        )
        if job.source_kind == "main_repair":
            key_material = "main_repair\0" + key_material
            expected_chain_id = hashlib.sha256(
                (
                    f"main_repair\0{job.live_id}\0"
                    f"{job.commit_start_ms}\0{job.commit_end_ms}"
                ).encode("utf-8")
            ).hexdigest()
            if job.chain_id != expected_chain_id:
                raise ValueError("audio job source invalid")
        expected_key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()
        if (
            job.job_key != expected_key
            or job.recognition_origin_ms != expected_origin
            or not job.recognition_origin_ms <= job.commit_start_ms
            < job.commit_end_ms <= current.capture_end_ms
        ):
            raise ValueError("audio job source invalid")

    def queue_audio_job(
        self,
        job: AudioJob,
        source_chunks: tuple[ClosedAudioChunk, ...],
        *,
        created_at: float,
        creation_mode: str = "shadow",
        target_hash: str = "",
    ) -> bool:
        source_chunks = tuple(source_chunks)
        self._validate_audio_job_sources(job, source_chunks)
        self._validate_audio_job_authority(creation_mode, target_hash)
        if job.source_kind not in SOURCE_KINDS:
            raise ValueError("invalid audio job source kind")
        if _SHA256.fullmatch(job.chain_id) is None:
            raise ValueError("invalid audio job chain identity")
        chunk_keys_json = canonical_json([
            chunk.chunk_key for chunk in source_chunks
        ])
        source_paths_json = canonical_json([
            str(chunk.path) for chunk in source_chunks
        ])
        source_metadata_json = encode_audio_sources(source_chunks)
        cleanup_status = (
            "pending" if all(chunk.delete_after_use for chunk in source_chunks)
            else "retained"
        )
        immutable = (
            job.live_id,
            chunk_keys_json,
            source_paths_json,
            str(job.context_path),
            int(job.recognition_origin_ms),
            int(source_chunks[-1].capture_end_ms),
            int(job.commit_start_ms),
            int(job.commit_end_ms),
            job.continuity,
            int(job.wordlist_version_id),
            job.chain_id,
            job.source_kind,
        )
        with self._immediate():
            self.conn.execute(
                "INSERT OR IGNORE INTO compliance_audio_chains("
                "chain_id,live_id,cursor_ms,created_at,updated_at) "
                "VALUES(?,?,?,?,?)",
                (
                    job.chain_id, job.live_id, int(job.commit_start_ms),
                    float(created_at), float(created_at),
                ),
            )
            chain = self.conn.execute(
                "SELECT live_id,cursor_ms FROM compliance_audio_chains "
                "WHERE chain_id=?", (job.chain_id,),
            ).fetchone()
            if (
                chain is None or str(chain["live_id"]) != job.live_id
                or int(chain["cursor_ms"]) > int(job.commit_start_ms)
            ):
                raise ValueError("audio job chain conflict")
            inserted = self.conn.execute(
                """
                INSERT OR IGNORE INTO compliance_audio_jobs(
                    job_key,live_id,chunk_keys_json,source_paths_json,
                    context_path,recognition_origin_ms,capture_end_ms,
                    commit_start_ms,commit_end_ms,continuity,
                    wordlist_version_id,chain_id,source_kind,creation_mode,target_hash,
                    cleanup_status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job.job_key, *immutable, creation_mode, target_hash,
                    cleanup_status,
                    float(created_at), float(created_at),
                ),
            ).rowcount
            row = self.conn.execute(
                """SELECT live_id,chunk_keys_json,source_paths_json,context_path,
                          recognition_origin_ms,capture_end_ms,commit_start_ms,
                          commit_end_ms,continuity,wordlist_version_id,chain_id,
                          source_kind
                   FROM compliance_audio_jobs WHERE job_key=?""",
                (job.job_key,),
            ).fetchone()
            if row is None:  # pragma: no cover - insert/fetch share one transaction
                raise RuntimeError("audio job queue failed")
            persisted = tuple(row)
            if persisted != immutable:
                raise ValueError("audio job identity conflict")
            self.conn.execute(
                "INSERT OR IGNORE INTO compliance_audio_job_sources("
                "job_key,chain_id,source_metadata_json) VALUES(?,?,?)",
                (job.job_key, job.chain_id, source_metadata_json),
            )
            source_row = self.conn.execute(
                "SELECT chain_id,source_metadata_json "
                "FROM compliance_audio_job_sources WHERE job_key=?",
                (job.job_key,),
            ).fetchone()
            if (
                source_row is None
                or str(source_row["chain_id"]) != job.chain_id
                or str(source_row["source_metadata_json"])
                != source_metadata_json
            ):
                raise ValueError("audio job identity conflict")
            if inserted == 0:
                existing_cleanup = self.conn.execute(
                    "SELECT cleanup_status FROM compliance_audio_jobs "
                    "WHERE job_key=?", (job.job_key,),
                ).fetchone()
                if (
                    existing_cleanup is not None
                    and str(existing_cleanup[0]) in {"pending", "retained"}
                    and str(existing_cleanup[0]) != cleanup_status
                ):
                    raise ValueError("audio job identity conflict")
        return inserted == 1

    @staticmethod
    def _validate_audio_job_authority(
        creation_mode: str, target_hash: str,
    ) -> None:
        valid = (
            creation_mode == "shadow" and target_hash == ""
        ) or (
            creation_mode == "live"
            and isinstance(target_hash, str)
            and _SHA256.fullmatch(target_hash) is not None
        )
        if not valid:
            raise ValueError("invalid audio job delivery authority")

    def audio_job_authority(self, row: sqlite3.Row) -> tuple[str, str]:
        try:
            creation_mode = str(row["creation_mode"])
            target_hash = str(row["target_hash"])
        except (KeyError, TypeError):
            raise ValueError("invalid audio job delivery authority") from None
        self._validate_audio_job_authority(creation_mode, target_hash)
        return creation_mode, target_hash

    def get_audio_job(self, job_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT jobs.*,sources.chain_id AS source_chain_id,"
            "sources.source_metadata_json "
            "FROM compliance_audio_jobs AS jobs "
            "LEFT JOIN compliance_audio_job_sources AS sources "
            "ON sources.job_key=jobs.job_key WHERE jobs.job_key=?",
            (str(job_key),),
        ).fetchone()

    def audio_source_rows(self) -> tuple[sqlite3.Row, ...]:
        return tuple(self.conn.execute(
            "SELECT jobs.*,sources.chain_id AS source_chain_id,"
            "sources.source_metadata_json "
            "FROM compliance_audio_jobs AS jobs "
            "JOIN compliance_audio_job_sources AS sources "
            "ON sources.job_key=jobs.job_key "
            "ORDER BY jobs.capture_end_ms,jobs.created_at,jobs.job_key"
        ).fetchall())

    def record_legacy_audio_quarantine(
        self, offer: AudioOffer, *, created_at: float,
    ) -> bool:
        if (
            not isinstance(offer, AudioOffer)
            or offer.legacy_quarantine is not True
            or _SHA256.fullmatch(offer.offer_id) is None
            or _SHA256.fullmatch(offer.boundary_id) is None
            or offer.wordlist_version_id != 0
            or offer.creation_mode != "shadow"
            or offer.target_hash != ""
            or not offer.chunks
            or any(
                not isinstance(chunk, ClosedAudioChunk)
                or not chunk.path.is_absolute()
                for chunk in offer.chunks
            )
            or isinstance(created_at, bool)
            or not isinstance(created_at, (int, float))
            or not math.isfinite(float(created_at))
            or float(created_at) < 0
        ):
            raise ValueError("legacy audio quarantine invalid")
        source_metadata_json = encode_audio_sources(offer.chunks)
        immutable = (
            offer.boundary_id,
            source_metadata_json,
            "legacy_offer_missing_authority",
            "shadow",
            "",
        )
        with self._immediate():
            inserted = self.conn.execute(
                "INSERT OR IGNORE INTO compliance_audio_quarantines("
                "offer_id,boundary_id,source_metadata_json,reason,"
                "creation_mode,target_hash,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    offer.offer_id, *immutable,
                    float(created_at), float(created_at),
                ),
            ).rowcount
            row = self.conn.execute(
                "SELECT boundary_id,source_metadata_json,reason,creation_mode,"
                "target_hash FROM compliance_audio_quarantines WHERE offer_id=?",
                (offer.offer_id,),
            ).fetchone()
            if row is None or tuple(row) != immutable:
                raise ValueError("legacy audio quarantine conflict")
            return inserted == 1

    def legacy_audio_quarantines(self) -> tuple[sqlite3.Row, ...]:
        return tuple(self.conn.execute(
            "SELECT * FROM compliance_audio_quarantines "
            "ORDER BY created_at,offer_id"
        ).fetchall())

    def latest_blocked_audio_observation(
        self, live_id: str,
    ) -> ClosedAudioChunk | None:
        if not isinstance(live_id, str) or not live_id:
            raise ValueError("audio blocked live identity invalid")
        row = self.conn.execute(
            "SELECT jobs.*,sources.chain_id AS source_chain_id,"
            "sources.source_metadata_json "
            "FROM compliance_audio_jobs AS jobs "
            "JOIN compliance_audio_job_sources AS sources "
            "ON sources.job_key=jobs.job_key "
            "WHERE jobs.live_id=? AND jobs.status='blocked_timeline' "
            "ORDER BY jobs.capture_end_ms DESC,jobs.created_at DESC,"
            "jobs.job_key DESC LIMIT 1",
            (live_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            chunks = decode_audio_sources(row["source_metadata_json"])
            if len(chunks) > 2 or any(
                item.live_id != live_id for item in chunks
            ):
                raise ValueError
            chunk = chunks[-1]
            if (
                str(row["live_id"]) != chunk.live_id
                or int(row["capture_end_ms"]) != chunk.capture_end_ms
                or str(row["chunk_keys_json"])
                != canonical_json([item.chunk_key for item in chunks])
                or str(row["source_paths_json"])
                != canonical_json([str(item.path) for item in chunks])
                or str(row["source_chain_id"]) != str(row["chain_id"])
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ValueError("audio blocked source metadata invalid") from None
        return chunk

    def record_blocked_audio_chunk(
        self,
        chunk: ClosedAudioChunk,
        *,
        wordlist_version_id: int,
        created_at: float,
        chain_id: str = "",
    ) -> bool:
        if (
            not isinstance(chunk, ClosedAudioChunk)
            or not chunk.path.is_absolute()
            or not chunk.live_id
            or not chunk.chunk_key
            or chunk.capture_end_ms <= chunk.capture_start_ms
            or chunk.capture_end_ms - chunk.capture_start_ms
            != chunk.media_duration_ms
            or self.wordlist_version(int(wordlist_version_id)) is None
        ):
            raise ValueError("audio blocked source invalid")
        durable_chain_id = chain_id or hashlib.sha256(
            (
                f"chain\0{chunk.live_id}\0{chunk.capture_start_ms}\0"
                f"{chunk.chunk_key}"
            ).encode("utf-8")
        ).hexdigest()
        if _SHA256.fullmatch(durable_chain_id) is None:
            raise ValueError("audio blocked chain invalid")
        material = (
            f"blocked\0{chunk.live_id}\0{chunk.chunk_key}\0"
            f"{chunk.capture_start_ms}\0{chunk.capture_end_ms}\0"
            f"{int(wordlist_version_id)}"
        )
        job_key = hashlib.sha256(material.encode("utf-8")).hexdigest()
        context_path = chunk.path.with_name(
            f".{chunk.path.stem}.context.wav"
        )
        chunk_keys_json = canonical_json([chunk.chunk_key])
        source_paths_json = canonical_json([str(chunk.path)])
        source_metadata_json = encode_audio_sources((chunk,))
        immutable = (
            chunk.live_id,
            chunk_keys_json,
            source_paths_json,
            str(context_path),
            chunk.capture_start_ms,
            chunk.capture_end_ms,
            chunk.capture_start_ms,
            chunk.capture_end_ms,
            "invalid",
            int(wordlist_version_id),
            durable_chain_id,
            "blocked_timeline",
            "audio_timeline_invalid",
            "retained",
        )
        with self._immediate():
            self.conn.execute(
                "INSERT OR IGNORE INTO compliance_audio_chains("
                "chain_id,live_id,cursor_ms,created_at,updated_at) "
                "VALUES(?,?,?,?,?)",
                (
                    durable_chain_id, chunk.live_id,
                    chunk.capture_start_ms, float(created_at),
                    float(created_at),
                ),
            )
            inserted = self.conn.execute(
                "INSERT OR IGNORE INTO compliance_audio_jobs("
                "job_key,live_id,chunk_keys_json,source_paths_json,context_path,"
                "recognition_origin_ms,capture_end_ms,commit_start_ms,"
                "commit_end_ms,continuity,wordlist_version_id,chain_id,status,"
                "error_class,cleanup_status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_key, *immutable, float(created_at), float(created_at)),
            ).rowcount
            row = self.conn.execute(
                "SELECT live_id,chunk_keys_json,source_paths_json,context_path,"
                "recognition_origin_ms,capture_end_ms,commit_start_ms,"
                "commit_end_ms,continuity,wordlist_version_id,chain_id,status,"
                "error_class,cleanup_status FROM compliance_audio_jobs "
                "WHERE job_key=?",
                (job_key,),
            ).fetchone()
            if row is None or tuple(row) != immutable:
                raise ValueError("audio blocked source conflict")
            self.conn.execute(
                "INSERT OR IGNORE INTO compliance_audio_job_sources("
                "job_key,chain_id,source_metadata_json) VALUES(?,?,?)",
                (job_key, durable_chain_id, source_metadata_json),
            )
            source = self.conn.execute(
                "SELECT chain_id,source_metadata_json "
                "FROM compliance_audio_job_sources WHERE job_key=?",
                (job_key,),
            ).fetchone()
            if (
                source is None
                or str(source["chain_id"]) != durable_chain_id
                or str(source["source_metadata_json"]) != source_metadata_json
            ):
                raise ValueError("audio blocked source conflict")
            return inserted == 1

    @staticmethod
    def _decode_audio_source_row(
        row: sqlite3.Row,
    ) -> tuple[AudioJob, tuple[ClosedAudioChunk, ...]]:
        chunks = decode_audio_sources(row["source_metadata_json"])
        if len(chunks) > 2:
            raise ValueError
        chunk_keys = json.loads(str(row["chunk_keys_json"]))
        source_paths = json.loads(str(row["source_paths_json"]))
        if (
            type(chunk_keys) is not list
            or type(source_paths) is not list
            or chunk_keys != [chunk.chunk_key for chunk in chunks]
            or source_paths != [str(chunk.path) for chunk in chunks]
            or canonical_json(chunk_keys)
            != str(row["chunk_keys_json"])
            or canonical_json(source_paths) != str(row["source_paths_json"])
        ):
            raise ValueError
        job = AudioJob(
            job_key=str(row["job_key"]),
            live_id=str(row["live_id"]),
            context_path=Path(str(row["context_path"])),
            recognition_origin_ms=int(row["recognition_origin_ms"]),
            commit_start_ms=int(row["commit_start_ms"]),
            commit_end_ms=int(row["commit_end_ms"]),
            wordlist_version_id=int(row["wordlist_version_id"]),
            continuity=str(row["continuity"]),
            chain_id=str(row["chain_id"]),
            source_kind=str(row["source_kind"]),
        )
        sources = chunks
        StoreAudioMixin._validate_audio_job_sources(job, sources)
        current = sources[-1]
        if (
            int(row["capture_end_ms"]) != current.capture_end_ms
            or not job.context_path.is_absolute()
            or job.context_path != current.path.with_name(
                f".{current.path.stem}.context.wav"
            )
            or _SHA256.fullmatch(job.chain_id) is None
            or str(row["source_chain_id"]) != job.chain_id
        ):
            raise ValueError
        return job, sources

    def reconstruct_audio_job(
        self, row: sqlite3.Row | None,
    ) -> tuple[AudioJob, tuple[ClosedAudioChunk, ...]]:
        if row is None:
            raise ValueError("audio job source metadata invalid")
        try:
            job, sources = self._decode_audio_source_row(row)
            current = sources[-1]
            if self.wordlist_version(job.wordlist_version_id) is None:
                raise ValueError
            chain = self.conn.execute(
                "SELECT live_id FROM compliance_audio_chains WHERE chain_id=?",
                (job.chain_id,),
            ).fetchone()
            if chain is None or str(chain["live_id"]) != job.live_id:
                raise ValueError
            from .audio import plan_audio_job

            expected = plan_audio_job(
                sources[0] if len(sources) == 2 else None,
                current,
                job.commit_start_ms,
                job.wordlist_version_id,
                final=(job.commit_end_ms == current.capture_end_ms),
                chain_id=job.chain_id,
                source_kind=job.source_kind,
            )
            if expected != job:
                raise ValueError
            derived = plan_audio_job(
                sources[0] if len(sources) == 2 else None,
                current,
                job.commit_start_ms,
                job.wordlist_version_id,
                final=(job.commit_end_ms == current.capture_end_ms),
                source_kind=job.source_kind,
            )
            if derived.chain_id != job.chain_id:
                predecessor_capture_end = (
                    sources[0].capture_end_ms
                    if len(sources) == 2
                    else current.capture_end_ms
                )
                predecessor_rows = self.conn.execute(
                    "SELECT jobs.*,sources.chain_id AS source_chain_id,"
                    "sources.source_metadata_json FROM compliance_audio_jobs "
                    "AS jobs JOIN compliance_audio_job_sources AS sources "
                    "ON sources.job_key=jobs.job_key WHERE jobs.job_key<>? "
                    "AND jobs.live_id=? AND jobs.chain_id=? "
                    "AND jobs.commit_end_ms=? AND jobs.capture_end_ms=? "
                    "ORDER BY jobs.created_at,jobs.job_key LIMIT 2",
                    (
                        job.job_key, job.live_id, job.chain_id,
                        job.commit_start_ms, predecessor_capture_end,
                    ),
                ).fetchall()
                if (
                    job.commit_start_ms <= sources[0].capture_start_ms
                    or len(predecessor_rows) != 1
                ):
                    raise ValueError
                predecessor_job, predecessor_sources = (
                    self._decode_audio_source_row(predecessor_rows[0])
                )
                if (
                    predecessor_job.live_id != job.live_id
                    or predecessor_job.chain_id != job.chain_id
                    or predecessor_job.commit_end_ms != job.commit_start_ms
                    or predecessor_sources[-1] != sources[0]
                ):
                    raise ValueError
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            raise ValueError("audio job source metadata invalid") from None
        return job, sources

    def committed_audio_event_keys(self, job_key: str) -> tuple[str, ...] | None:
        row = self.conn.execute(
            "SELECT status FROM compliance_audio_jobs WHERE job_key=?",
            (str(job_key),),
        ).fetchone()
        if row is None or str(row["status"]) != "committed":
            return None
        events = self.conn.execute(
            "SELECT event_key FROM compliance_events WHERE job_key=? "
            "ORDER BY hit_start_ms,event_key",
            (str(job_key),),
        ).fetchall()
        return tuple(str(event["event_key"]) for event in events)

    def audio_cleanup_plan(
        self, job_key: str,
    ) -> tuple[bool, tuple[Path, ...]]:
        row = self.conn.execute(
            "SELECT job_key,source_paths_json,capture_end_ms,commit_end_ms,"
            "status,cleanup_status FROM compliance_audio_jobs WHERE job_key=?",
            (str(job_key),),
        ).fetchone()
        if (
            row is None or str(row["status"]) != "committed"
            or str(row["cleanup_status"]) not in {"pending", "failed"}
        ):
            return False, ()
        try:
            raw_paths = json.loads(str(row["source_paths_json"]))
        except (TypeError, ValueError):
            return False, ()
        if (
            not isinstance(raw_paths, list) or not raw_paths
            or any(not isinstance(value, str) or not value for value in raw_paths)
        ):
            return False, ()
        paths = tuple(Path(value) for value in raw_paths)
        if any(not path.is_absolute() for path in paths):
            return False, ()
        is_final = int(row["commit_end_ms"]) == int(row["capture_end_ms"])
        eligible = paths if is_final else paths[:-1]
        if not eligible:
            return True, ()
        if any(
            (
                (path.parent / ".compliance-observations.json").exists()
                or (path.parent / ".compliance-observations.json").is_symlink()
            )
            for path in eligible
        ):
            return False, ()
        for parent in {path.parent for path in eligible}:
            parent_paths = tuple(path for path in eligible if path.parent == parent)
            if not self._session_manifest_allows_cleanup(parent, parent_paths):
                return False, ()

        references: list[tuple[str, str, set[str]]] = []
        for other in self.conn.execute(
            "SELECT job_key,status,cleanup_status,source_paths_json "
            "FROM compliance_audio_jobs WHERE job_key<>?",
            (str(job_key),),
        ):
            try:
                decoded = json.loads(str(other["source_paths_json"]))
            except (TypeError, ValueError):
                return False, ()
            if not isinstance(decoded, list) or any(
                not isinstance(value, str) for value in decoded
            ):
                return False, ()
            references.append((
                str(other["status"]), str(other["cleanup_status"]), set(decoded)
            ))

        candidates: list[Path] = []
        seen: set[str] = set()
        unique_eligible = tuple(dict.fromkeys(str(path) for path in eligible))
        for path in eligible:
            value = str(path)
            if value in seen:
                continue
            blocked = any(
                value in referenced
                and (status != "committed" or cleanup_status == "retained")
                for status, cleanup_status, referenced in references
            )
            if not blocked:
                candidates.append(path)
                seen.add(value)
        return len(candidates) == len(unique_eligible), tuple(candidates)

    @staticmethod
    def _session_manifest_allows_cleanup(
        directory: Path, paths: tuple[Path, ...],
    ) -> bool:
        manifest = directory / ".compliance-final-intent.json"
        if not manifest.exists() and not manifest.is_symlink():
            return True
        try:
            if manifest.is_symlink() or not manifest.is_file():
                return False
            encoded = manifest.read_text(encoding="utf-8")
            document = json.loads(
                encoded,
                parse_constant=lambda _value: (_ for _ in ()).throw(
                    ValueError()
                ),
            )
            keys = {
                "boundary_end_ms", "continuity", "creation_mode", "live_id",
                "origin_ms", "queued_at", "segments", "session_started_ms",
                "target_hash", "version", "wordlist_version_id",
            }
            if (
                not isinstance(document, dict)
                or set(document) != keys
                or document["version"] != 2
                or canonical_json(document) != encoded
                or type(document["origin_ms"]) is not int
                or document["origin_ms"] < 0
                or not isinstance(document["segments"], list)
            ):
                return False
            by_name: dict[str, bool] = {}
            prior_name = ""
            prior_end = int(document["origin_ms"])
            for segment in document["segments"]:
                if (
                    not isinstance(segment, dict)
                    or set(segment) != {
                        "duration_ms", "emitted", "name", "start_ms",
                    }
                    or not isinstance(segment["name"], str)
                    or not re.fullmatch(r"segment_\d{6,}\.wav", segment["name"])
                    or segment["name"] <= prior_name
                    or type(segment["duration_ms"]) is not int
                    or segment["duration_ms"] <= 0
                    or type(segment["start_ms"]) is not int
                    or segment["start_ms"] != prior_end
                    or type(segment["emitted"]) is not bool
                ):
                    return False
                by_name[segment["name"]] = segment["emitted"]
                prior_name = segment["name"]
                prior_end = segment["start_ms"] + segment["duration_ms"]
            return all(
                path.parent == directory and by_name.get(path.name) is True
                for path in paths
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return False

    def audio_cleanup_candidates(self, job_key: str) -> tuple[Path, ...]:
        return self.audio_cleanup_plan(job_key)[1]

    def finalize_audio_cleanup(self, job_key: str, status: str) -> bool:
        if status not in {"deleted", "failed"}:
            raise ValueError("invalid compliance cleanup status")
        with self._immediate():
            changed = self.conn.execute(
                "UPDATE compliance_audio_jobs SET cleanup_status=? "
                "WHERE job_key=? AND cleanup_status IN ('pending','failed')",
                (status, str(job_key)),
            ).rowcount
            return changed == 1

    def audio_cleanup_reconciliation_rows(self) -> tuple[sqlite3.Row, ...]:
        return tuple(self.conn.execute(
            "SELECT jobs.*,sources.chain_id AS source_chain_id,"
            "sources.source_metadata_json "
            "FROM compliance_audio_jobs AS jobs "
            "LEFT JOIN compliance_audio_job_sources AS sources "
            "ON sources.job_key=jobs.job_key "
            "WHERE jobs.status='committed' "
            "AND jobs.cleanup_status IN ('pending','failed') "
            "ORDER BY jobs.commit_start_ms,jobs.created_at,jobs.job_key"
        ).fetchall())

    def has_pending_context_reference(
        self, context_path: Path, *, excluding_job_key: str,
    ) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM compliance_audio_jobs WHERE job_key<>? "
            "AND context_path=? AND status IN "
            "('queued','retry_wait','recognizing') LIMIT 1",
            (str(excluding_job_key), str(context_path)),
        ).fetchone() is not None

    def audio_chain_cursor(self, chain_id: str) -> int | None:
        if _SHA256.fullmatch(str(chain_id)) is None:
            return None
        row = self.conn.execute(
            "SELECT cursor_ms FROM compliance_audio_chains WHERE chain_id=?",
            (str(chain_id),),
        ).fetchone()
        return None if row is None else int(row["cursor_ms"])

    def claim_due_audio_jobs(
        self, *, now: float, lease_seconds: float, limit: int = 1
    ) -> tuple[sqlite3.Row, ...]:
        if limit <= 0:
            return ()
        with self._immediate():
            candidates = self.conn.execute(
                """
                SELECT candidate.job_key FROM compliance_audio_jobs AS candidate
                JOIN compliance_audio_chains AS chain
                  ON chain.chain_id=candidate.chain_id
                 AND chain.live_id=candidate.live_id
                 AND chain.cursor_ms=candidate.commit_start_ms
                WHERE candidate.status IN ('queued','retry_wait','recognizing')
                  AND candidate.next_attempt_at<=?
                  AND candidate.lease_until<=?
                  AND NOT EXISTS (
                    SELECT 1 FROM compliance_audio_jobs AS predecessor
                    WHERE predecessor.live_id=candidate.live_id
                      AND predecessor.chain_id=candidate.chain_id
                      AND predecessor.commit_start_ms<candidate.commit_start_ms
                      AND predecessor.status<>'committed'
                  )
                ORDER BY CASE candidate.source_kind
                           WHEN 'realtime' THEN 0 ELSE 1 END,
                         candidate.commit_start_ms,candidate.created_at,
                         candidate.job_key
                LIMIT ?
                """,
                (float(now), float(now), int(limit)),
            ).fetchall()
            keys = tuple(str(row["job_key"]) for row in candidates)
            if not keys:
                return ()
            placeholders = ",".join("?" for _ in keys)
            self.conn.execute(
                f"""
                UPDATE compliance_audio_jobs
                SET status='recognizing',attempts=attempts+1,
                    lease_until=?,updated_at=?
                WHERE job_key IN ({placeholders})
                """,
                (float(now) + float(lease_seconds), float(now), *keys),
            )
            claimed = self.conn.execute(
                f"SELECT jobs.*,sources.chain_id AS source_chain_id,"
                f"sources.source_metadata_json "
                f"FROM compliance_audio_jobs AS jobs "
                f"LEFT JOIN compliance_audio_job_sources AS sources "
                f"ON sources.job_key=jobs.job_key "
                f"WHERE jobs.job_key IN ({placeholders}) "
                f"ORDER BY CASE jobs.source_kind "
                f"WHEN 'realtime' THEN 0 ELSE 1 END,"
                f"jobs.commit_start_ms,jobs.created_at,jobs.job_key",
                keys,
            ).fetchall()
        return tuple(claimed)

    def commit_audio_events(
        self,
        job_key: str,
        events: tuple[ComplianceEventDraft, ...],
        *,
        result_json: str,
        model_version: str,
        commit_cursor_ms: int,
        created_at_ms: int,
        expected_job: AudioJob | None = None,
    ) -> tuple[str, ...]:
        events = tuple(events)
        # Materialize the durable inputs quickly, then release SQLite entirely
        # while decoding the transcript and deriving provenance.  Those CPU-heavy
        # operations must never hold the process-wide WAL writer slot needed by
        # the recorder/watcher connection.
        with self._write_lock:
            job = self.conn.execute(
                "SELECT * FROM compliance_audio_jobs WHERE job_key=?",
                (job_key,),
            ).fetchone()
            if job is None:
                raise KeyError(job_key)
            if str(job["status"]) == "committed":
                rows = self.conn.execute(
                    "SELECT event_key FROM compliance_events WHERE job_key=? "
                    "ORDER BY hit_start_ms,event_key",
                    (job_key,),
                ).fetchall()
                return tuple(str(row["event_key"]) for row in rows)
            chain = self.conn.execute(
                "SELECT live_id,cursor_ms FROM compliance_audio_chains "
                "WHERE chain_id=?", (str(job["chain_id"]),),
            ).fetchone()
            wordlist_row = self.conn.execute(
                "SELECT id,source_hash,entries_json,entry_count "
                "FROM compliance_wordlist_versions WHERE id=?",
                (int(job["wordlist_version_id"]),),
            ).fetchone()
        job_snapshot = tuple(job)
        chain_snapshot = None if chain is None else tuple(chain)
        wordlist_snapshot = (
            None if wordlist_row is None else tuple(wordlist_row)
        )

        if str(job["status"]) != "recognizing":
            raise RuntimeError(
                f"compliance audio job {job_key} is not recognizing"
            )
        if expected_job is not None:
            persisted_identity = (
                str(job["job_key"]),
                str(job["live_id"]),
                str(job["context_path"]),
                int(job["recognition_origin_ms"]),
                int(job["commit_start_ms"]),
                int(job["commit_end_ms"]),
                int(job["wordlist_version_id"]),
                str(job["continuity"]),
                str(job["chain_id"]),
                str(job["source_kind"]),
            )
            expected_identity = (
                expected_job.job_key,
                expected_job.live_id,
                str(expected_job.context_path),
                expected_job.recognition_origin_ms,
                expected_job.commit_start_ms,
                expected_job.commit_end_ms,
                expected_job.wordlist_version_id,
                expected_job.continuity,
                expected_job.chain_id,
                expected_job.source_kind,
            )
            if persisted_identity != expected_identity:
                raise ValueError("compliance audio job identity mismatch")
        if str(job["continuity"]) != "ok":
            raise ValueError("compliance audio timeline is invalid")
        if (
            chain is None
            or str(chain["live_id"]) != str(job["live_id"])
            or int(chain["cursor_ms"]) != int(job["commit_start_ms"])
        ):
            raise ValueError("compliance audio chain cursor mismatch")
        if not (
            int(job["recognition_origin_ms"])
            <= int(job["commit_start_ms"])
            < int(job["commit_end_ms"])
            <= int(job["capture_end_ms"])
        ):
            raise ValueError("compliance audio job bounds are invalid")
        if (
            isinstance(commit_cursor_ms, bool)
            or not isinstance(commit_cursor_ms, int)
            or commit_cursor_ms != int(job["commit_end_ms"])
        ):
            raise ValueError("compliance commit cursor mismatch")
        from .events import (
            EventProvenance,
            decode_transcript_batch,
            derive_event_provenance,
            event_identity,
        )

        batch = decode_transcript_batch(result_json)
        if (
            not isinstance(model_version, str)
            or not model_version.strip()
            or batch.model_version != model_version
        ):
            raise ValueError("compliance transcript model mismatch")
        if (
            isinstance(created_at_ms, bool)
            or not isinstance(created_at_ms, int)
            or created_at_ms < 0
        ):
            raise ValueError("compliance event created_at_ms is invalid")
        if wordlist_row is None:
            raise ValueError("compliance wordlist version is missing")
        durable_wordlist = self._decode_wordlist_version(wordlist_row)
        expected_provenance = derive_event_provenance(
            live_id=str(job["live_id"]),
            recognition_origin_ms=int(job["recognition_origin_ms"]),
            commit_start_ms=int(job["commit_start_ms"]),
            commit_end_ms=int(job["commit_end_ms"]),
            batch=batch,
            entries=durable_wordlist.entries,
        )

        supplied_provenance: list[EventProvenance] = []
        event_rows: list[tuple[object, ...]] = []
        for draft in events:
            if draft.job_key != job_key:
                raise ValueError(
                    f"compliance event draft job_key does not match {job_key}"
                )
            if draft.live_id != str(job["live_id"]):
                raise ValueError("compliance event live identity mismatch")
            if draft.wordlist_version_id != int(job["wordlist_version_id"]):
                raise ValueError("compliance event wordlist identity mismatch")
            if (
                draft.creation_mode != str(job["creation_mode"])
                or draft.target_hash != str(job["target_hash"])
            ):
                raise ValueError("compliance event authority mismatch")
            if draft.model_version != model_version:
                raise ValueError("compliance event model mismatch")
            if (
                isinstance(draft.hit_start_ms, bool)
                or isinstance(draft.hit_end_ms, bool)
                or not isinstance(draft.hit_start_ms, int)
                or not isinstance(draft.hit_end_ms, int)
                or draft.hit_end_ms <= draft.hit_start_ms
            ):
                raise ValueError("compliance event hit bounds are invalid")
            if not (
                int(job["commit_start_ms"])
                <= draft.hit_start_ms
                < int(job["commit_end_ms"])
            ):
                raise ValueError("compliance event start is outside commit window")
            expected_event_key, expected_delivery_key = event_identity(
                draft.live_id,
                draft.hit_start_ms,
                draft.hit_end_ms,
                draft.normalized_term,
                draft.occurrence_index,
            )
            if (
                draft.event_key != expected_event_key
                or draft.delivery_key != expected_delivery_key
            ):
                raise ValueError("compliance event identity mismatch")
            if draft.creation_mode == "shadow":
                target_valid = draft.target_hash == ""
            elif draft.creation_mode == "live":
                target_valid = bool(re.fullmatch(
                    r"[0-9a-f]{64}", draft.target_hash
                ))
            else:
                target_valid = False
            if not target_valid:
                raise ValueError("compliance event target hash is invalid")
            if (
                not isinstance(draft.anchor_name, str)
                or not draft.anchor_name.strip()
            ):
                raise ValueError("compliance event anchor is required")
            supplied_provenance.append(EventProvenance(
                event_key=draft.event_key,
                delivery_key=draft.delivery_key,
                raw_term=draft.raw_term,
                normalized_term=draft.normalized_term,
                sentence_text=draft.sentence_text,
                occurrence_index=draft.occurrence_index,
                hit_start_ms=draft.hit_start_ms,
                hit_end_ms=draft.hit_end_ms,
                model_version=draft.model_version,
            ))
            event_rows.append((
                draft.event_key,
                draft.delivery_key,
                draft.job_key,
                draft.live_id,
                draft.wordlist_version_id,
                draft.raw_term,
                draft.normalized_term,
                draft.sentence_text,
                draft.occurrence_index,
                draft.hit_start_ms,
                draft.hit_end_ms,
                draft.anchor_name,
                draft.model_version,
                draft.creation_mode,
                draft.target_hash,
                int(created_at_ms),
            ))
        if tuple(sorted(supplied_provenance)) != tuple(
            sorted(expected_provenance)
        ):
            raise ValueError("compliance event provenance mismatch")

        # Re-check every durable input after acquiring the writer slot.  This
        # closes the validation/commit race without lengthening the transaction.
        with self._immediate():
            current_job = self.conn.execute(
                "SELECT * FROM compliance_audio_jobs WHERE job_key=?",
                (job_key,),
            ).fetchone()
            if current_job is None:
                raise KeyError(job_key)
            if str(current_job["status"]) == "committed":
                rows = self.conn.execute(
                    "SELECT event_key FROM compliance_events WHERE job_key=? "
                    "ORDER BY hit_start_ms,event_key",
                    (job_key,),
                ).fetchall()
                return tuple(str(row["event_key"]) for row in rows)
            if tuple(current_job) != job_snapshot:
                raise ValueError("compliance audio job changed during validation")
            current_chain = self.conn.execute(
                "SELECT live_id,cursor_ms FROM compliance_audio_chains "
                "WHERE chain_id=?", (str(job["chain_id"]),),
            ).fetchone()
            if (
                current_chain is None
                or tuple(current_chain) != chain_snapshot
            ):
                raise ValueError("compliance audio chain changed during validation")
            current_wordlist = self.conn.execute(
                "SELECT id,source_hash,entries_json,entry_count "
                "FROM compliance_wordlist_versions WHERE id=?",
                (int(job["wordlist_version_id"]),),
            ).fetchone()
            if (
                current_wordlist is None
                or tuple(current_wordlist) != wordlist_snapshot
            ):
                raise ValueError("compliance wordlist changed during validation")
            self.conn.executemany(
                """
                    INSERT INTO compliance_events(
                        event_key,delivery_key,job_key,live_id,
                        wordlist_version_id,raw_term,normalized_term,
                        sentence_text,occurrence_index,hit_start_ms,hit_end_ms,
                        anchor_name,model_version,creation_mode,target_hash,
                        created_at_ms
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                event_rows,
            )
            self.conn.execute(
                "UPDATE compliance_audio_chains SET cursor_ms=?,updated_at=? "
                "WHERE chain_id=? AND cursor_ms=?",
                (
                    int(commit_cursor_ms), float(created_at_ms) / 1000,
                    str(job["chain_id"]), int(job["commit_start_ms"]),
                ),
            )
            self.conn.execute(
                """
                UPDATE compliance_audio_jobs
                SET status='committed',result_json=?,model_version=?,
                    lease_until=0,updated_at=?
                WHERE job_key=?
                """,
                (
                    result_json,
                    model_version,
                    float(created_at_ms) / 1000,
                    job_key,
                ),
            )
            self.conn.execute(
                """
                UPDATE compliance_runtime_state
                SET commit_cursor_ms=MAX(commit_cursor_ms,?),
                    updated_at=datetime('now','localtime')
                WHERE singleton_id=1
                """,
                (int(commit_cursor_ms),),
            )
            rows = self.conn.execute(
                "SELECT event_key FROM compliance_events WHERE job_key=? "
                "ORDER BY hit_start_ms,event_key",
                (job_key,),
            ).fetchall()
            return tuple(str(row["event_key"]) for row in rows)

    def block_audio_job_timeline(
        self, job: AudioJob, *, updated_at_ms: int
    ) -> None:
        if (
            isinstance(updated_at_ms, bool)
            or not isinstance(updated_at_ms, int)
            or updated_at_ms < 0
        ):
            raise ValueError("invalid compliance timeline update time")
        with self._immediate():
            row = self.conn.execute(
                "SELECT * FROM compliance_audio_jobs WHERE job_key=?",
                (job.job_key,),
            ).fetchone()
            if row is None:
                raise KeyError(job.job_key)
            persisted_identity = (
                str(row["job_key"]),
                str(row["live_id"]),
                str(row["context_path"]),
                int(row["recognition_origin_ms"]),
                int(row["commit_start_ms"]),
                int(row["commit_end_ms"]),
                int(row["wordlist_version_id"]),
                str(row["continuity"]),
            )
            supplied_identity = (
                job.job_key,
                job.live_id,
                str(job.context_path),
                job.recognition_origin_ms,
                job.commit_start_ms,
                job.commit_end_ms,
                job.wordlist_version_id,
                job.continuity,
            )
            if (
                persisted_identity != supplied_identity
                or job.continuity == "ok"
                or str(row["continuity"]) == "ok"
            ):
                raise ValueError("compliance audio timeline identity mismatch")
            if str(row["status"]) == "blocked_timeline":
                if str(row["error_class"]) != "audio_timeline_invalid":
                    raise RuntimeError("compliance timeline block state is invalid")
                return
            if str(row["status"]) != "recognizing":
                raise RuntimeError("compliance audio job is not owned for recognition")
            self.conn.execute(
                """UPDATE compliance_audio_jobs
                   SET status='blocked_timeline',lease_until=0,
                       error_class='audio_timeline_invalid',updated_at=?
                   WHERE job_key=?""",
                (float(updated_at_ms) / 1000, job.job_key),
            )


__all__ = ["StoreAudioMixin"]
