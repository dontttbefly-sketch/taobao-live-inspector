from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from .codec import decode_audio_sources
from .models import AudioJob, ClosedAudioChunk


AUDIO_STATUSES = frozenset({
    "queued", "recognizing", "retry_wait", "committed", "blocked_timeline",
    "needs_attention",
})
CONTINUITIES = frozenset({"ok", "invalid"})
CLEANUP_STATUSES = frozenset({"pending", "retained", "deleted", "failed"})
LISTENER_MODES = frozenset({"disabled", "shadow", "live"})
CREATION_MODES = frozenset({"shadow", "live"})
SOURCE_KINDS = frozenset({"realtime", "main_repair"})
DELIVERY_STATUSES = frozenset({
    "pending_payload", "frozen", "shadow", "pending", "sending", "sent",
    "delivery_unknown", "failed", "needs_attention", "suppressed",
})


CREATE_TABLES = (
    """CREATE TABLE IF NOT EXISTS compliance_wordlist_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_hash TEXT NOT NULL UNIQUE,
    entries_json TEXT NOT NULL,
    entry_count INTEGER NOT NULL,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    activated_at REAL NOT NULL,
    last_checked_at REAL NOT NULL,
    created_at TEXT DEFAULT (datetime('now','localtime'))
    )""",
    """CREATE TABLE IF NOT EXISTS compliance_runtime_state (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
    active_wordlist_version INTEGER REFERENCES compliance_wordlist_versions(id),
    next_wordlist_sync_at REAL NOT NULL DEFAULT 0,
    wordlist_failure_count INTEGER NOT NULL DEFAULT 0,
    wordlist_last_error_class TEXT NOT NULL DEFAULT '',
    wordlist_last_error_at REAL NOT NULL DEFAULT 0,
    current_live_id TEXT NOT NULL DEFAULT '',
    audio_ffmpeg_pid INTEGER,
    audio_marker TEXT NOT NULL DEFAULT '',
    audio_process_token TEXT NOT NULL DEFAULT '',
    last_valid_audio_ms INTEGER NOT NULL DEFAULT 0,
    last_valid_audio_live_id TEXT NOT NULL DEFAULT '',
    audio_health_live_id TEXT NOT NULL DEFAULT '',
    commit_cursor_ms INTEGER NOT NULL DEFAULT 0,
    model_failure_count INTEGER NOT NULL DEFAULT 0,
    next_model_attempt_at REAL NOT NULL DEFAULT 0,
    model_may_be_loaded INTEGER NOT NULL DEFAULT 0
        CHECK(model_may_be_loaded IN (0,1)),
    model_worker_pid INTEGER,
    model_worker_start_token TEXT NOT NULL DEFAULT '',
    health_json TEXT NOT NULL DEFAULT '{}',
    listener_mode TEXT NOT NULL DEFAULT 'disabled',
    listener_started_at_ms INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now','localtime'))
    )""",
    """CREATE TABLE IF NOT EXISTS compliance_audio_jobs (
    job_key TEXT PRIMARY KEY,
    live_id TEXT NOT NULL,
    chunk_keys_json TEXT NOT NULL,
    source_paths_json TEXT NOT NULL,
    context_path TEXT NOT NULL DEFAULT '',
    recognition_origin_ms INTEGER NOT NULL,
    capture_end_ms INTEGER NOT NULL,
    commit_start_ms INTEGER NOT NULL,
    commit_end_ms INTEGER NOT NULL,
    continuity TEXT NOT NULL,
    wordlist_version_id INTEGER NOT NULL REFERENCES compliance_wordlist_versions(id),
    chain_id TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT 'realtime',
    creation_mode TEXT NOT NULL DEFAULT 'shadow',
    target_hash TEXT NOT NULL DEFAULT '',
    model_version TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    lease_until REAL NOT NULL DEFAULT 0,
    error_class TEXT NOT NULL DEFAULT '',
    cleanup_status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS compliance_audio_chains (
    chain_id TEXT PRIMARY KEY,
    live_id TEXT NOT NULL,
    cursor_ms INTEGER NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS compliance_audio_job_sources (
    job_key TEXT PRIMARY KEY REFERENCES compliance_audio_jobs(job_key),
    chain_id TEXT NOT NULL DEFAULT '',
    source_metadata_json TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS compliance_audio_quarantines (
    offer_id TEXT PRIMARY KEY,
    boundary_id TEXT NOT NULL,
    source_metadata_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    creation_mode TEXT NOT NULL DEFAULT 'shadow',
    target_hash TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS compliance_events (
    event_key TEXT PRIMARY KEY,
    delivery_key TEXT NOT NULL UNIQUE,
    job_key TEXT NOT NULL REFERENCES compliance_audio_jobs(job_key),
    live_id TEXT NOT NULL,
    wordlist_version_id INTEGER NOT NULL REFERENCES compliance_wordlist_versions(id),
    raw_term TEXT NOT NULL,
    normalized_term TEXT NOT NULL,
    sentence_text TEXT NOT NULL,
    occurrence_index INTEGER NOT NULL,
    hit_start_ms INTEGER NOT NULL,
    hit_end_ms INTEGER NOT NULL,
    anchor_name TEXT NOT NULL,
    model_version TEXT NOT NULL,
    creation_mode TEXT NOT NULL,
    target_hash TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '',
    payload_hash TEXT NOT NULL DEFAULT '',
    delivery_status TEXT NOT NULL DEFAULT 'pending_payload',
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    remote_message_id TEXT NOT NULL DEFAULT '',
    error_class TEXT NOT NULL DEFAULT '',
    created_at_ms INTEGER NOT NULL,
    sent_at_ms INTEGER NOT NULL DEFAULT 0
    )""",
)

MIGRATION_COLUMNS = {
    "compliance_wordlist_versions": {
        "entries_json": "TEXT NOT NULL",
        "entry_count": "INTEGER NOT NULL",
        "warnings_json": "TEXT NOT NULL DEFAULT '[]'",
        "activated_at": "REAL NOT NULL",
        "last_checked_at": "REAL NOT NULL",
        "created_at": "TEXT",
    },
    "compliance_runtime_state": {
        "next_wordlist_sync_at": "REAL NOT NULL DEFAULT 0",
        "wordlist_failure_count": "INTEGER NOT NULL DEFAULT 0",
        "wordlist_last_error_class": "TEXT NOT NULL DEFAULT ''",
        "wordlist_last_error_at": "REAL NOT NULL DEFAULT 0",
        "current_live_id": "TEXT NOT NULL DEFAULT ''",
        "audio_ffmpeg_pid": "INTEGER",
        "audio_marker": "TEXT NOT NULL DEFAULT ''",
        "audio_process_token": "TEXT NOT NULL DEFAULT ''",
        "last_valid_audio_ms": "INTEGER NOT NULL DEFAULT 0",
        "last_valid_audio_live_id": "TEXT NOT NULL DEFAULT ''",
        "audio_health_live_id": "TEXT NOT NULL DEFAULT ''",
        "commit_cursor_ms": "INTEGER NOT NULL DEFAULT 0",
        "model_failure_count": "INTEGER NOT NULL DEFAULT 0",
        "next_model_attempt_at": "REAL NOT NULL DEFAULT 0",
        "model_may_be_loaded": (
            "INTEGER NOT NULL DEFAULT 0 CHECK(model_may_be_loaded IN (0,1))"
        ),
        "model_worker_pid": "INTEGER",
        "model_worker_start_token": "TEXT NOT NULL DEFAULT ''",
        "health_json": "TEXT NOT NULL DEFAULT '{}'",
        "listener_mode": "TEXT NOT NULL DEFAULT 'disabled'",
        "listener_started_at_ms": "INTEGER NOT NULL DEFAULT 0",
        "updated_at": "TEXT",
    },
    "compliance_audio_jobs": {
        "live_id": "TEXT NOT NULL",
        "chunk_keys_json": "TEXT NOT NULL",
        "source_paths_json": "TEXT NOT NULL",
        "context_path": "TEXT NOT NULL DEFAULT ''",
        "recognition_origin_ms": "INTEGER NOT NULL",
        "capture_end_ms": "INTEGER NOT NULL",
        "commit_start_ms": "INTEGER NOT NULL",
        "commit_end_ms": "INTEGER NOT NULL",
        "continuity": "TEXT NOT NULL",
        "chain_id": "TEXT NOT NULL DEFAULT ''",
        "source_kind": "TEXT NOT NULL DEFAULT 'realtime'",
        "creation_mode": "TEXT NOT NULL DEFAULT 'shadow'",
        "target_hash": "TEXT NOT NULL DEFAULT ''",
        "model_version": "TEXT NOT NULL DEFAULT ''",
        "result_json": "TEXT NOT NULL DEFAULT ''",
        "status": "TEXT NOT NULL DEFAULT 'queued'",
        "attempts": "INTEGER NOT NULL DEFAULT 0",
        "next_attempt_at": "REAL NOT NULL DEFAULT 0",
        "lease_until": "REAL NOT NULL DEFAULT 0",
        "error_class": "TEXT NOT NULL DEFAULT ''",
        "cleanup_status": "TEXT NOT NULL DEFAULT 'pending'",
        "created_at": "REAL NOT NULL",
        "updated_at": "REAL NOT NULL",
    },
    "compliance_audio_job_sources": {
        "chain_id": "TEXT NOT NULL DEFAULT ''",
        "source_metadata_json": "TEXT NOT NULL",
    },
    "compliance_events": {
        "live_id": "TEXT NOT NULL",
        "raw_term": "TEXT NOT NULL",
        "normalized_term": "TEXT NOT NULL",
        "sentence_text": "TEXT NOT NULL",
        "occurrence_index": "INTEGER NOT NULL",
        "hit_start_ms": "INTEGER NOT NULL",
        "hit_end_ms": "INTEGER NOT NULL",
        "anchor_name": "TEXT NOT NULL",
        "model_version": "TEXT NOT NULL",
        "creation_mode": "TEXT NOT NULL",
        "target_hash": "TEXT NOT NULL DEFAULT ''",
        "payload_json": "TEXT NOT NULL DEFAULT ''",
        "payload_hash": "TEXT NOT NULL DEFAULT ''",
        "delivery_status": "TEXT NOT NULL DEFAULT 'pending_payload'",
        "delivery_attempts": "INTEGER NOT NULL DEFAULT 0",
        "next_attempt_at": "REAL NOT NULL DEFAULT 0",
        "remote_message_id": "TEXT NOT NULL DEFAULT ''",
        "error_class": "TEXT NOT NULL DEFAULT ''",
        "created_at_ms": "INTEGER NOT NULL",
        "sent_at_ms": "INTEGER NOT NULL DEFAULT 0",
    },
}

STRUCTURAL_SCHEMA = {
    "compliance_wordlist_versions": {
        "primary_key": "id",
        "unique": ("source_hash",),
        "foreign_keys": (),
    },
    "compliance_runtime_state": {
        "primary_key": "singleton_id",
        "unique": (),
        "foreign_keys": (
            ("active_wordlist_version", "compliance_wordlist_versions", "id"),
        ),
    },
    "compliance_audio_jobs": {
        "primary_key": "job_key",
        "unique": (),
        "foreign_keys": (
            ("wordlist_version_id", "compliance_wordlist_versions", "id"),
        ),
    },
    "compliance_audio_chains": {
        "primary_key": "chain_id",
        "unique": (),
        "foreign_keys": (),
    },
    "compliance_audio_job_sources": {
        "primary_key": "job_key",
        "unique": (),
        "foreign_keys": (
            ("job_key", "compliance_audio_jobs", "job_key"),
        ),
    },
    "compliance_audio_quarantines": {
        "primary_key": "offer_id",
        "unique": (),
        "foreign_keys": (),
    },
    "compliance_events": {
        "primary_key": "event_key",
        "unique": ("delivery_key",),
        "foreign_keys": (
            ("job_key", "compliance_audio_jobs", "job_key"),
            ("wordlist_version_id", "compliance_wordlist_versions", "id"),
        ),
    },
}

INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_compliance_audio_due "
    "ON compliance_audio_jobs(status,next_attempt_at,lease_until)",
    "CREATE INDEX IF NOT EXISTS idx_compliance_audio_live_chain_start "
    "ON compliance_audio_jobs(live_id,chain_id,commit_start_ms)",
    "CREATE INDEX IF NOT EXISTS idx_compliance_audio_chain_start "
    "ON compliance_audio_jobs(chain_id,commit_start_ms)",
    "CREATE INDEX IF NOT EXISTS idx_compliance_event_due "
    "ON compliance_events(delivery_status,next_attempt_at)",
    "CREATE INDEX IF NOT EXISTS idx_compliance_event_hit "
    "ON compliance_events(live_id,hit_start_ms)",
    "CREATE INDEX IF NOT EXISTS idx_compliance_event_job "
    "ON compliance_events(job_key)",
)


def _sql_values(values: frozenset[str]) -> str:
    return ",".join(f"'{value}'" for value in sorted(values))


VALIDATED_COLUMNS = (
    ("compliance_audio_jobs", "status", AUDIO_STATUSES),
    ("compliance_audio_jobs", "continuity", CONTINUITIES),
    ("compliance_audio_jobs", "cleanup_status", CLEANUP_STATUSES),
    ("compliance_audio_jobs", "creation_mode", CREATION_MODES),
    ("compliance_audio_jobs", "source_kind", SOURCE_KINDS),
    ("compliance_runtime_state", "listener_mode", LISTENER_MODES),
    ("compliance_events", "creation_mode", CREATION_MODES),
    ("compliance_events", "delivery_status", DELIVERY_STATUSES),
)

TRIGGERS = (
    (
        "compliance_audio_jobs_validate_insert",
        f"""CREATE TRIGGER compliance_audio_jobs_validate_insert
        BEFORE INSERT ON compliance_audio_jobs
        WHEN NEW.status IS NULL OR NEW.status NOT IN ({_sql_values(AUDIO_STATUSES)})
          OR NEW.continuity IS NULL OR NEW.continuity NOT IN ({_sql_values(CONTINUITIES)})
          OR NEW.cleanup_status IS NULL OR NEW.cleanup_status NOT IN ({_sql_values(CLEANUP_STATUSES)})
          OR NEW.creation_mode IS NULL OR NEW.creation_mode NOT IN ({_sql_values(CREATION_MODES)})
          OR NEW.source_kind IS NULL OR NEW.source_kind NOT IN ({_sql_values(SOURCE_KINDS)})
          OR (NEW.creation_mode='shadow' AND NEW.target_hash<>'')
          OR (NEW.creation_mode='live' AND (length(NEW.target_hash)<>64
              OR NEW.target_hash GLOB '*[^0-9a-f]*'))
          OR (NEW.status NOT IN ('blocked_timeline','needs_attention') AND (length(NEW.chain_id)<>64
              OR NEW.chain_id GLOB '*[^0-9a-f]*'))
        BEGIN SELECT RAISE(ABORT, 'invalid compliance audio state'); END""",
    ),
    (
        "compliance_audio_jobs_validate_update",
        f"""CREATE TRIGGER compliance_audio_jobs_validate_update
        BEFORE UPDATE OF status,continuity,cleanup_status,creation_mode,target_hash,chain_id,source_kind ON compliance_audio_jobs
        WHEN NEW.status IS NULL OR NEW.status NOT IN ({_sql_values(AUDIO_STATUSES)})
          OR NEW.continuity IS NULL OR NEW.continuity NOT IN ({_sql_values(CONTINUITIES)})
          OR NEW.cleanup_status IS NULL OR NEW.cleanup_status NOT IN ({_sql_values(CLEANUP_STATUSES)})
          OR NEW.creation_mode IS NULL OR NEW.creation_mode NOT IN ({_sql_values(CREATION_MODES)})
          OR NEW.source_kind IS NULL OR NEW.source_kind NOT IN ({_sql_values(SOURCE_KINDS)})
          OR (NEW.creation_mode='shadow' AND NEW.target_hash<>'')
          OR (NEW.creation_mode='live' AND (length(NEW.target_hash)<>64
              OR NEW.target_hash GLOB '*[^0-9a-f]*'))
          OR (NEW.status NOT IN ('blocked_timeline','needs_attention') AND (length(NEW.chain_id)<>64
              OR NEW.chain_id GLOB '*[^0-9a-f]*'))
        BEGIN SELECT RAISE(ABORT, 'invalid compliance audio state'); END""",
    ),
    (
        "compliance_runtime_state_validate_insert",
        f"""CREATE TRIGGER compliance_runtime_state_validate_insert
        BEFORE INSERT ON compliance_runtime_state
        WHEN NEW.listener_mode IS NULL OR NEW.listener_mode NOT IN ({_sql_values(LISTENER_MODES)})
          OR (NEW.model_worker_pid IS NULL AND NEW.model_worker_start_token<>'')
          OR (NEW.model_worker_pid IS NOT NULL AND
              (typeof(NEW.model_worker_pid)<>'integer' OR NEW.model_worker_pid<=0
               OR NEW.model_worker_start_token=''))
        BEGIN SELECT RAISE(ABORT, 'invalid compliance runtime state'); END""",
    ),
    (
        "compliance_runtime_state_validate_update",
        f"""CREATE TRIGGER compliance_runtime_state_validate_update
        BEFORE UPDATE OF listener_mode,model_worker_pid,model_worker_start_token
        ON compliance_runtime_state
        WHEN NEW.listener_mode IS NULL OR NEW.listener_mode NOT IN ({_sql_values(LISTENER_MODES)})
          OR (NEW.model_worker_pid IS NULL AND NEW.model_worker_start_token<>'')
          OR (NEW.model_worker_pid IS NOT NULL AND
              (typeof(NEW.model_worker_pid)<>'integer' OR NEW.model_worker_pid<=0
               OR NEW.model_worker_start_token=''))
        BEGIN SELECT RAISE(ABORT, 'invalid compliance runtime state'); END""",
    ),
    (
        "compliance_events_validate_insert",
        f"""CREATE TRIGGER compliance_events_validate_insert
        BEFORE INSERT ON compliance_events
        WHEN NEW.creation_mode IS NULL OR NEW.creation_mode NOT IN ({_sql_values(CREATION_MODES)})
          OR NEW.delivery_status IS NULL OR NEW.delivery_status NOT IN ({_sql_values(DELIVERY_STATUSES)})
        BEGIN SELECT RAISE(ABORT, 'invalid compliance event state'); END""",
    ),
    (
        "compliance_events_validate_update",
        f"""CREATE TRIGGER compliance_events_validate_update
        BEFORE UPDATE OF creation_mode,delivery_status ON compliance_events
        WHEN NEW.creation_mode IS NULL OR NEW.creation_mode NOT IN ({_sql_values(CREATION_MODES)})
          OR NEW.delivery_status IS NULL OR NEW.delivery_status NOT IN ({_sql_values(DELIVERY_STATUSES)})
        BEGIN SELECT RAISE(ABORT, 'invalid compliance event state'); END""",
    ),
)


class StoreSchemaMixin:
    def _migrate_schema(self) -> None:
        with self._write_lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in CREATE_TABLES:
                    self.conn.execute(statement)
                self._validate_structural_schema()
                for table, additions in MIGRATION_COLUMNS.items():
                    columns = {
                        str(row["name"])
                        for row in self.conn.execute(f"PRAGMA table_info({table})")
                    }
                    for column, declaration in additions.items():
                        if column not in columns:
                            if self._requires_empty_table(declaration):
                                populated = self.conn.execute(
                                    f"SELECT 1 FROM {table} LIMIT 1"
                                ).fetchone()
                                if populated is not None:
                                    raise ValueError(
                                        f"cannot safely add required column "
                                        f"{table}.{column} to a nonempty table"
                                    )
                            self.conn.execute(
                                f"ALTER TABLE {table} ADD COLUMN "
                                f"{column} {declaration}"
                            )
                for name, _statement in TRIGGERS:
                    self.conn.execute(f"DROP TRIGGER IF EXISTS {name}")
                self._migrate_legacy_audio_chains()
                self._validate_existing_states()
                self.conn.execute(
                    "INSERT OR IGNORE INTO compliance_runtime_state(singleton_id) "
                    "VALUES(1)"
                )
                for statement in INDEXES:
                    self.conn.execute(statement)
                for name, statement in TRIGGERS:
                    self.conn.execute(statement)
            except BaseException:
                self.conn.rollback()
                raise
            else:
                self.conn.commit()

    @staticmethod
    def _requires_empty_table(declaration: str) -> bool:
        normalized = declaration.upper()
        return "NOT NULL" in normalized and "DEFAULT" not in normalized

    def _migrate_legacy_audio_chains(self) -> None:
        rows = self.conn.execute(
            "SELECT jobs.*,sources.source_metadata_json "
            "FROM compliance_audio_jobs AS jobs LEFT JOIN "
            "compliance_audio_job_sources AS sources "
            "ON sources.job_key=jobs.job_key WHERE jobs.chain_id='' "
            "ORDER BY jobs.live_id,jobs.commit_start_ms,jobs.created_at,jobs.job_key"
        ).fetchall()
        if not rows:
            return
        from .audio import plan_audio_job

        migrated: list[tuple[AudioJob, tuple[ClosedAudioChunk, ...], str]] = []
        attention: list[str] = []
        for row in rows:
            if (
                str(row["status"]) == "blocked_timeline"
                and str(row["error_class"]) == "audio_chain_legacy"
                and str(row["cleanup_status"]) == "retained"
            ):
                attention.append(str(row["job_key"]))
                continue
            try:
                encoded = row["source_metadata_json"]
                chunks = decode_audio_sources(encoded)
                if len(chunks) not in {1, 2}:
                    raise ValueError
                job_without_chain = AudioJob(
                    job_key=str(row["job_key"]),
                    live_id=str(row["live_id"]),
                    context_path=Path(str(row["context_path"])),
                    recognition_origin_ms=int(row["recognition_origin_ms"]),
                    commit_start_ms=int(row["commit_start_ms"]),
                    commit_end_ms=int(row["commit_end_ms"]),
                    wordlist_version_id=int(row["wordlist_version_id"]),
                    continuity=str(row["continuity"]),
                )
                self._validate_audio_job_sources(job_without_chain, chunks)
                if (
                    json.loads(str(row["chunk_keys_json"]))
                    != [chunk.chunk_key for chunk in chunks]
                    or json.loads(str(row["source_paths_json"]))
                    != [str(chunk.path) for chunk in chunks]
                ):
                    raise ValueError
                predecessor = None
                if len(chunks) == 2:
                    predecessor = next((
                        item for item in reversed(migrated)
                        if item[0].live_id == job_without_chain.live_id
                        and item[0].commit_end_ms == job_without_chain.commit_start_ms
                        and item[1][-1] == chunks[0]
                    ), None)
                elif job_without_chain.commit_start_ms > chunks[-1].capture_start_ms:
                    predecessor = next((
                        item for item in reversed(migrated)
                        if item[0].live_id == job_without_chain.live_id
                        and item[0].commit_end_ms == job_without_chain.commit_start_ms
                        and item[1][-1] == chunks[-1]
                    ), None)
                if predecessor is not None:
                    chain_id = predecessor[2]
                elif (
                    len(chunks) == 1
                    and job_without_chain.commit_start_ms
                    == chunks[-1].capture_start_ms
                ):
                    chain_id = plan_audio_job(
                        None, chunks[-1], job_without_chain.commit_start_ms,
                        job_without_chain.wordlist_version_id,
                        final=(job_without_chain.commit_end_ms == chunks[-1].capture_end_ms),
                    ).chain_id
                else:
                    raise ValueError
                migrated.append((job_without_chain, chunks, chain_id))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                attention.append(str(row["job_key"]))

        for job, _chunks, chain_id in migrated:
            self.conn.execute(
                "UPDATE compliance_audio_jobs SET chain_id=? WHERE job_key=?",
                (chain_id, job.job_key),
            )
            self.conn.execute(
                "UPDATE compliance_audio_job_sources SET chain_id=? WHERE job_key=?",
                (chain_id, job.job_key),
            )
        by_chain: dict[str, list[AudioJob]] = {}
        for job, _chunks, chain_id in migrated:
            by_chain.setdefault(chain_id, []).append(job)
        for chain_id, jobs in by_chain.items():
            ordered = sorted(jobs, key=lambda job: (job.commit_start_ms, job.job_key))
            cursor = ordered[0].commit_start_ms
            for job in ordered:
                state = self.conn.execute(
                    "SELECT status FROM compliance_audio_jobs WHERE job_key=?",
                    (job.job_key,),
                ).fetchone()
                if (
                    state is not None and str(state[0]) == "committed"
                    and job.commit_start_ms == cursor
                ):
                    cursor = job.commit_end_ms
                else:
                    break
            self.conn.execute(
                "INSERT OR REPLACE INTO compliance_audio_chains("
                "chain_id,live_id,cursor_ms,created_at,updated_at) VALUES(?,?,?,?,?)",
                (chain_id, ordered[0].live_id, cursor, 0.0, 0.0),
            )
        for job_key in attention:
            self.conn.execute(
                "UPDATE compliance_audio_jobs SET status='needs_attention',"
                "error_class='audio_chain_migration_attention',lease_until=0 "
                "WHERE job_key=?",
                (job_key,),
            )
            self.conn.execute(
                "UPDATE compliance_events SET delivery_status='needs_attention',"
                "error_class='audio_chain_migration_attention' "
                "WHERE job_key=? AND delivery_status='pending_payload'",
                (job_key,),
            )

    def _validate_structural_schema(self) -> None:
        for table, requirements in STRUCTURAL_SCHEMA.items():
            columns = {
                str(row["name"]): row
                for row in self.conn.execute(f"PRAGMA table_info({table})")
            }
            primary_key = str(requirements["primary_key"])
            primary_columns = sorted(
                (int(row["pk"]), name)
                for name, row in columns.items()
                if int(row["pk"]) > 0
            )
            if primary_columns != [(1, primary_key)]:
                raise ValueError(
                    f"{table}.{primary_key} must be the PRIMARY KEY"
                )
            unique_columns = self._single_column_unique_indexes(table)
            for column in requirements["unique"]:
                if column not in unique_columns:
                    raise ValueError(f"{table}.{column} must be UNIQUE")
            foreign_key_groups: dict[int, list[sqlite3.Row]] = {}
            for row in self.conn.execute(f"PRAGMA foreign_key_list({table})"):
                foreign_key_groups.setdefault(int(row["id"]), []).append(row)
            foreign_keys = set()
            for rows in foreign_key_groups.values():
                ordered = sorted(rows, key=lambda row: int(row["seq"]))
                if len(ordered) == 1 and int(ordered[0]["seq"]) == 0:
                    foreign_keys.add((
                        str(ordered[0]["from"]),
                        str(ordered[0]["table"]),
                        str(ordered[0]["to"]),
                    ))
            for foreign_key in requirements["foreign_keys"]:
                if foreign_key not in foreign_keys:
                    column, target_table, target_column = foreign_key
                    raise ValueError(
                        f"{table}.{column} must reference "
                        f"{target_table}({target_column})"
                    )

    def _single_column_unique_indexes(self, table: str) -> set[str]:
        columns: set[str] = set()
        for index in self.conn.execute(f"PRAGMA index_list({table})"):
            if int(index["unique"]) != 1 or int(index["partial"]) != 0:
                continue
            name = str(index["name"]).replace('"', '""')
            indexed = self.conn.execute(
                f'PRAGMA index_info("{name}")'
            ).fetchall()
            if len(indexed) == 1:
                columns.add(str(indexed[0]["name"]))
        return columns

    def _validate_existing_states(self) -> None:
        for table, column, allowed in VALIDATED_COLUMNS:
            placeholders = ",".join("?" for _ in allowed)
            row = self.conn.execute(
                f"SELECT rowid,{column} FROM {table} "
                f"WHERE {column} IS NULL OR {column} NOT IN ({placeholders}) "
                "LIMIT 1",
                tuple(sorted(allowed)),
            ).fetchone()
            if row is not None:
                raise ValueError(
                    f"invalid {table}.{column}: {row[column]!r}"
                )
        invalid_authority = self.conn.execute(
            "SELECT rowid FROM compliance_audio_jobs WHERE "
            "(creation_mode='shadow' AND target_hash<>'') OR "
            "(creation_mode='live' AND (length(target_hash)<>64 OR "
            "target_hash GLOB '*[^0-9a-f]*')) LIMIT 1"
        ).fetchone()
        if invalid_authority is not None:
            raise ValueError("invalid compliance audio job delivery authority")
        invalid_chain = self.conn.execute(
            "SELECT job_key FROM compliance_audio_jobs WHERE "
            "status NOT IN ('blocked_timeline','needs_attention') AND "
            "(length(chain_id)<>64 OR "
            "chain_id GLOB '*[^0-9a-f]*') LIMIT 1"
        ).fetchone()
        if invalid_chain is not None:
            raise ValueError("invalid compliance audio chain identity")


__all__ = [
    "AUDIO_STATUSES",
    "CLEANUP_STATUSES",
    "CONTINUITIES",
    "CREATION_MODES",
    "DELIVERY_STATUSES",
    "LISTENER_MODES",
    "SOURCE_KINDS",
    "StoreSchemaMixin",
]
