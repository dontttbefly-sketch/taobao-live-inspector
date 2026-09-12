from __future__ import annotations

import dataclasses
import json
import sqlite3

from .codec import canonical_json
from .models import WordEntry, WordlistSnapshot


WORDLIST_ERROR_CLASSES = frozenset({
    "wordlist_fetch_failed", "wordlist_validation_failed",
})


class StoreWordlistMixin:
    def wordlist_sync_due(self, now: float) -> bool:
        row = self.conn.execute(
            "SELECT next_wordlist_sync_at FROM compliance_runtime_state "
            "WHERE singleton_id=1"
        ).fetchone()
        return row is None or float(row["next_wordlist_sync_at"] or 0) <= now

    def claim_wordlist_sync(
        self, checked_at: float, next_sync_at: float,
    ) -> float | None:
        if float(next_sync_at) <= float(checked_at):
            raise ValueError("wordlist claim deadline must advance")
        with self._immediate():
            claimed = self.conn.execute(
                "UPDATE compliance_runtime_state SET next_wordlist_sync_at=? "
                "WHERE singleton_id=1 AND next_wordlist_sync_at<=?",
                (float(next_sync_at), float(checked_at)),
            ).rowcount
        return float(next_sync_at) if claimed == 1 else None

    def _wordlist_claim_is_current(self, claim_token: float | None) -> bool:
        if claim_token is None:
            return False
        row = self.conn.execute(
            "SELECT next_wordlist_sync_at FROM compliance_runtime_state "
            "WHERE singleton_id=1"
        ).fetchone()
        return row is not None and float(row["next_wordlist_sync_at"]) == claim_token

    def activate_wordlist(
        self,
        source_hash: str,
        entries: tuple[WordEntry, ...],
        warnings: tuple[str, ...] = (),
        *,
        checked_at: float,
        next_sync_at: float,
        claim_token: float,
    ) -> WordlistSnapshot:
        encoded = canonical_json([
            dataclasses.asdict(entry) for entry in entries
        ])
        encoded_warnings = canonical_json(list(warnings))
        with self._immediate():
            if not self._wordlist_claim_is_current(claim_token):
                return self.active_wordlist()
            self.conn.execute(
                """
                INSERT INTO compliance_wordlist_versions(
                    source_hash,entries_json,entry_count,warnings_json,
                    activated_at,last_checked_at
                ) VALUES(?,?,?,?,?,?)
                ON CONFLICT(source_hash) DO UPDATE SET
                    last_checked_at=excluded.last_checked_at
                """,
                (
                    source_hash, encoded, len(entries), encoded_warnings,
                    float(checked_at), float(checked_at),
                ),
            )
            row = self.conn.execute(
                "SELECT id FROM compliance_wordlist_versions WHERE source_hash=?",
                (source_hash,),
            ).fetchone()
            if row is None:  # pragma: no cover - guaranteed by the insert
                raise RuntimeError("compliance wordlist activation failed")
            version_id = int(row[0])
            self.conn.execute(
                """
                UPDATE compliance_runtime_state
                SET active_wordlist_version=?,next_wordlist_sync_at=?,
                    wordlist_failure_count=0,
                    wordlist_last_error_class='',
                    wordlist_last_error_at=0,
                    updated_at=datetime('now','localtime')
                WHERE singleton_id=1
                """,
                (version_id, float(next_sync_at)),
            )
        snapshot = WordlistSnapshot(version_id, source_hash, entries)
        return snapshot

    def mark_wordlist_checked(
        self, *, checked_at: float, next_sync_at: float,
        claim_token: float,
    ) -> WordlistSnapshot | None:
        with self._immediate():
            if not self._wordlist_claim_is_current(claim_token):
                return self.active_wordlist()
            self.conn.execute(
                "UPDATE compliance_wordlist_versions SET last_checked_at=? "
                "WHERE id=(SELECT active_wordlist_version "
                "FROM compliance_runtime_state WHERE singleton_id=1)",
                (float(checked_at),),
            )
            self.conn.execute(
                "UPDATE compliance_runtime_state SET next_wordlist_sync_at=?,"
                "wordlist_failure_count=0,wordlist_last_error_class='',"
                "wordlist_last_error_at=0,updated_at=datetime('now','localtime') "
                "WHERE singleton_id=1",
                (float(next_sync_at),),
            )
        return self.active_wordlist()

    def mark_wordlist_failure(
        self, error_class: str, *, checked_at: float, next_sync_at: float,
        claim_token: float,
    ) -> WordlistSnapshot | None:
        safe_error_class = (
            error_class if error_class in WORDLIST_ERROR_CLASSES
            else "wordlist_fetch_failed"
        )
        with self._immediate():
            if not self._wordlist_claim_is_current(claim_token):
                return self.active_wordlist()
            self.conn.execute(
                "UPDATE compliance_runtime_state SET next_wordlist_sync_at=?,"
                "wordlist_failure_count=wordlist_failure_count+1,"
                "wordlist_last_error_class=?,wordlist_last_error_at=?,"
                "updated_at=datetime('now','localtime') WHERE singleton_id=1",
                (float(next_sync_at), safe_error_class, float(checked_at)),
            )
        return self.active_wordlist()

    def active_wordlist(self) -> WordlistSnapshot | None:
        row = self.conn.execute(
            "SELECT active_wordlist_version FROM compliance_runtime_state "
            "WHERE singleton_id=1"
        ).fetchone()
        if row is None or row["active_wordlist_version"] is None:
            return None
        return self.wordlist_version(int(row["active_wordlist_version"]))

    @staticmethod
    def _decode_wordlist_version(row: sqlite3.Row) -> WordlistSnapshot:
        try:
            decoded = json.loads(str(row["entries_json"]))
            if type(decoded) is not list:
                raise ValueError("wordlist entries are not a list")
            entries: list[WordEntry] = []
            for item in decoded:
                if type(item) is not dict or not set(item) <= {
                    "raw", "normalized", "replacement", "note"
                } or not {"raw", "normalized"} <= set(item):
                    raise ValueError("wordlist entry shape is invalid")
                entry = WordEntry(
                    raw=item["raw"],
                    normalized=item["normalized"],
                    replacement=item.get("replacement", ""),
                    note=item.get("note", ""),
                )
                if any(not isinstance(value, str) for value in dataclasses.astuple(entry)):
                    raise ValueError("wordlist entry value is invalid")
                entries.append(entry)
            if len(entries) != int(row["entry_count"]):
                raise ValueError("wordlist entry count is invalid")
            source_hash = str(row["source_hash"])
            if not source_hash:
                raise ValueError("wordlist source hash is empty")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("compliance wordlist version is invalid") from exc
        return WordlistSnapshot(int(row["id"]), source_hash, tuple(entries))

    def wordlist_version(self, version_id: int) -> WordlistSnapshot | None:
        if isinstance(version_id, bool) or not isinstance(version_id, int):
            raise TypeError("compliance wordlist version id must be an integer")
        row = self.conn.execute(
            "SELECT id,source_hash,entries_json,entry_count "
            "FROM compliance_wordlist_versions WHERE id=?",
            (version_id,),
        ).fetchone()
        return None if row is None else self._decode_wordlist_version(row)

    def force_wordlist_sync(self) -> None:
        with self._immediate():
            self.conn.execute(
                "UPDATE compliance_runtime_state SET next_wordlist_sync_at=0,"
                "updated_at=datetime('now','localtime') WHERE singleton_id=1"
            )



__all__ = ["StoreWordlistMixin"]
