from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import quote


_JOB_STATUSES = frozenset({
    "queued", "recognizing", "retry_wait", "committed",
    "blocked_timeline", "needs_attention",
})
_DELIVERY_STATUSES = frozenset({
    "pending_payload", "frozen", "shadow", "pending", "sending", "sent",
    "delivery_unknown", "failed", "needs_attention", "suppressed",
})

@dataclass(frozen=True)
class ComplianceFingerprint:
    events_hash: str
    jobs_hash: str
    payloads_hash: str
    counts: dict[str, int]


def _has_symlink_component(path: Path) -> bool:
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


def _hash_rows(rows: tuple[tuple[str, ...], ...]) -> str:
    encoded = json.dumps(
        rows,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _required_columns(
    connection: sqlite3.Connection,
    table: str,
    required: frozenset[str],
) -> None:
    columns = {
        str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
    }
    if not required <= columns:
        raise ValueError


def fingerprint_database(path: Path) -> ComplianceFingerprint:
    database = Path(path)
    connection: sqlite3.Connection | None = None
    try:
        if (
            not database.is_absolute()
            or _has_symlink_component(database)
            or not database.is_file()
        ):
            raise ValueError
        resolved = database.resolve(strict=True)
        uri = f"file:{quote(str(resolved), safe='/')}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        connection.execute("PRAGMA query_only=ON")
        _required_columns(
            connection,
            "compliance_audio_jobs",
            frozenset({"job_key", "status", "source_kind"}),
        )
        _required_columns(
            connection,
            "compliance_events",
            frozenset({
                "event_key",
                "delivery_key",
                "job_key",
                "creation_mode",
                "delivery_status",
                "payload_hash",
            }),
        )
        jobs = tuple(sorted(
            tuple(str(value) for value in row)
            for row in connection.execute(
                "SELECT job_key,status,source_kind FROM compliance_audio_jobs"
            )
        ))
        events = tuple(sorted(
            tuple(str(value) for value in row)
            for row in connection.execute(
                "SELECT event_key,delivery_key,job_key,creation_mode,"
                "delivery_status FROM compliance_events"
            )
        ))
        payloads = tuple(sorted(
            tuple(str(value) for value in row)
            for row in connection.execute(
                "SELECT event_key,payload_hash,delivery_status "
                "FROM compliance_events"
            )
        ))
        counts: dict[str, int] = {
            "events": len(events),
        }
        for status, count in connection.execute(
            "SELECT delivery_status,COUNT(*) FROM compliance_events "
            "GROUP BY delivery_status ORDER BY delivery_status"
        ):
            if not isinstance(status, str) or status not in _DELIVERY_STATUSES:
                raise ValueError
            counts[f"events:{status}"] = int(count)
        counts["jobs"] = len(jobs)
        for status, count in connection.execute(
            "SELECT status,COUNT(*) FROM compliance_audio_jobs "
            "GROUP BY status ORDER BY status"
        ):
            if not isinstance(status, str) or status not in _JOB_STATUSES:
                raise ValueError
            counts[f"jobs:{status}"] = int(count)
        return ComplianceFingerprint(
            events_hash=_hash_rows(events),
            jobs_hash=_hash_rows(jobs),
            payloads_hash=_hash_rows(payloads),
            counts=counts,
        )
    except (OSError, TypeError, ValueError, sqlite3.Error):
        raise ValueError("compliance_fingerprint_unavailable") from None
    finally:
        if connection is not None:
            connection.close()


__all__ = ["ComplianceFingerprint", "fingerprint_database"]
