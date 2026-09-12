from __future__ import annotations

import json
import sqlite3
import threading
import math
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .codec import canonical_json
from .store_audio import StoreAudioMixin
from .store_events import StoreEventsMixin, decode_frozen_payload
from .store_schema import (
    AUDIO_STATUSES,
    CLEANUP_STATUSES,
    CONTINUITIES,
    CREATION_MODES,
    DELIVERY_STATUSES,
    LISTENER_MODES,
    SOURCE_KINDS,
    StoreSchemaMixin,
    TRIGGERS,
)
from .store_wordlist import StoreWordlistMixin
HEALTH_CODES = frozenset({
    "COMPLIANCE_AUDIO_BLIND",
    "COMPLIANCE_BACKLOG",
    "COMPLIANCE_WORDLIST",
    "COMPLIANCE_MODEL",
    "COMPLIANCE_DELIVERY_UNKNOWN",
})
class ComplianceStore(
    StoreSchemaMixin,
    StoreWordlistMixin,
    StoreAudioMixin,
    StoreEventsMixin,
):
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=8000")
        try:
            self._migrate_schema()
        except BaseException:
            self.conn.close()
            raise

    @contextmanager
    def _immediate(self) -> Iterator[None]:
        with self._write_lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.conn.rollback()
                raise
            else:
                self.conn.commit()

    @staticmethod
    def _decode_health_json(encoded: object) -> dict[str, dict[str, object]]:
        if not isinstance(encoded, str) or not encoded:
            raise ValueError("compliance health state is invalid")
        try:
            payload = json.loads(encoded)
            if not isinstance(payload, dict) or any(
                code not in HEALTH_CODES for code in payload
            ):
                raise ValueError
            result: dict[str, dict[str, object]] = {}
            for code, raw in payload.items():
                if (
                    not isinstance(raw, dict)
                    or set(raw) != {
                        "active", "last_alert_at", "observed_since"
                    }
                    or type(raw["active"]) is not bool
                    or isinstance(raw["last_alert_at"], bool)
                    or not isinstance(raw["last_alert_at"], (int, float))
                    or isinstance(raw["observed_since"], bool)
                    or not isinstance(raw["observed_since"], (int, float))
                    or not math.isfinite(float(raw["last_alert_at"]))
                    or not math.isfinite(float(raw["observed_since"]))
                    or float(raw["last_alert_at"]) < 0
                    or float(raw["observed_since"]) < 0
                ):
                    raise ValueError
                result[str(code)] = {
                    "active": raw["active"],
                    "last_alert_at": float(raw["last_alert_at"]),
                    "observed_since": float(raw["observed_since"]),
                }
            canonical = canonical_json(result)
            if canonical != encoded:
                raise ValueError
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ValueError("compliance health state is invalid") from None
        return result

    def health_state(self) -> dict[str, dict[str, object]]:
        return self._decode_health_json(self.runtime_state()["health_json"])

    def health_observation_anchor(self, code: str, *, now: float) -> float:
        if code not in HEALTH_CODES:
            raise ValueError("invalid compliance health code")
        with self._immediate():
            row = self.conn.execute(
                "SELECT health_json FROM compliance_runtime_state "
                "WHERE singleton_id=1"
            ).fetchone()
            if row is None:
                raise RuntimeError("compliance runtime state is missing")
            state = self._decode_health_json(row["health_json"])
            incident = state.get(code)
            if incident is None:
                incident = {
                    "active": False,
                    "last_alert_at": 0.0,
                    "observed_since": float(now),
                }
                state[code] = incident
                encoded = canonical_json(state)
                self.conn.execute(
                    "UPDATE compliance_runtime_state SET health_json=?,"
                    "updated_at=datetime('now','localtime') "
                    "WHERE singleton_id=1",
                    (encoded,),
                )
            return float(incident["observed_since"])

    def update_health_condition(
        self, code: str, *, active: bool, now: float,
    ) -> tuple[bool, bool]:
        if code not in HEALTH_CODES:
            raise ValueError("invalid compliance health code")
        if type(active) is not bool or not math.isfinite(float(now)) or now < 0:
            raise ValueError("invalid compliance health condition")
        with self._immediate():
            row = self.conn.execute(
                "SELECT health_json FROM compliance_runtime_state "
                "WHERE singleton_id=1"
            ).fetchone()
            if row is None:
                raise RuntimeError("compliance runtime state is missing")
            state = self._decode_health_json(row["health_json"])
            incident = state.get(code, {
                "active": False,
                "last_alert_at": 0.0,
                "observed_since": float(now),
            })
            was_active = bool(incident["active"])
            last_alert = float(incident["last_alert_at"])
            alert_due = bool(active) and (
                not was_active or float(now) - last_alert >= 3600.0
            )
            recovered = was_active and not active
            incident["active"] = active
            if alert_due:
                incident["last_alert_at"] = float(now)
            if not active:
                incident["observed_since"] = float(now)
            state[code] = incident
            encoded = canonical_json(state)
            self.conn.execute(
                "UPDATE compliance_runtime_state SET health_json=?,"
                "updated_at=datetime('now','localtime') WHERE singleton_id=1",
                (encoded,),
            )
        return alert_due, recovered

    def close(self) -> None:
        with self._write_lock:
            self.conn.close()
