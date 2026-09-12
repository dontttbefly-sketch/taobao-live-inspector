from __future__ import annotations

import hashlib
import json
import re
import sqlite3

from .codec import canonical_json


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_DUE_EVENT_SELECT = (
    "SELECT rowid AS compliance_rowid,* FROM compliance_events "
    "WHERE delivery_status IN ('frozen','pending','failed') "
    "AND next_attempt_at<=? AND delivery_attempts<5 "
    "ORDER BY created_at_ms,event_key LIMIT 1"
)


def decode_frozen_payload(
    payload_json: object, payload_hash: object,
) -> dict:
    if not isinstance(payload_json, str) or not payload_json:
        raise ValueError("frozen payload is missing")
    if (
        not isinstance(payload_hash, str)
        or _SHA256.fullmatch(payload_hash) is None
        or hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        != payload_hash
    ):
        raise ValueError("frozen payload hash is invalid")

    def reject_constant(_value: str) -> None:
        raise ValueError("frozen payload contains a non-finite number")

    try:
        card = json.loads(payload_json, parse_constant=reject_constant)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("frozen payload JSON is invalid") from exc
    if not isinstance(card, dict):
        raise ValueError("frozen payload root is invalid")
    canonical = canonical_json(card)
    if canonical != payload_json:
        raise ValueError("frozen payload JSON is not canonical")
    return card


class StoreEventsMixin:
    def has_delivery_unknown(self) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM compliance_events "
            "WHERE delivery_status='delivery_unknown' LIMIT 1"
        ).fetchone() is not None

    def get_event(self, event_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM compliance_events WHERE event_key=?",
            (str(event_key),),
        ).fetchone()

    def pending_payload_events(self) -> tuple[sqlite3.Row, ...]:
        return tuple(self.conn.execute(
            "SELECT * FROM compliance_events "
            "WHERE delivery_status='pending_payload' "
            "ORDER BY created_at_ms,event_key"
        ).fetchall())

    def freeze_event_payload(
        self, event_key: str, *, payload_json: str, payload_hash: str,
    ) -> bool:
        decode_frozen_payload(payload_json, payload_hash)
        with self._immediate():
            row = self.conn.execute(
                "SELECT creation_mode FROM compliance_events "
                "WHERE event_key=? AND delivery_status='pending_payload' "
                "AND payload_json='' AND payload_hash=''",
                (str(event_key),),
            ).fetchone()
            if row is None:
                return False
            status = "shadow" if str(row["creation_mode"]) == "shadow" else "frozen"
            changed = self.conn.execute(
                "UPDATE compliance_events SET payload_json=?,payload_hash=?,"
                "delivery_status=? WHERE event_key=? "
                "AND delivery_status='pending_payload' "
                "AND payload_json='' AND payload_hash=''",
                (
                    str(payload_json), str(payload_hash), status,
                    str(event_key),
                ),
            ).rowcount
            return changed == 1

    def fail_pending_event(self, event_key: str, error_class: str) -> bool:
        with self._immediate():
            changed = self.conn.execute(
                "UPDATE compliance_events SET delivery_status='needs_attention',"
                "error_class=? WHERE event_key=? "
                "AND delivery_status='pending_payload' "
                "AND payload_json='' AND payload_hash=''",
                (str(error_class), str(event_key)),
            ).rowcount
            return changed == 1

    def suppress_unsent_live_events(self) -> int:
        with self._immediate():
            return self.conn.execute(
                "UPDATE compliance_events SET delivery_status='suppressed',"
                "error_class='mode_downgrade' "
                "WHERE creation_mode='live' AND delivery_status IN "
                "('pending_payload','frozen','pending','failed')"
            ).rowcount

    def reject_delivery_targets(
        self, *, current_target_hash: str | None, error_class: str,
    ) -> int:
        with self._immediate():
            if current_target_hash is None:
                predicate = "1=1"
                params: tuple[object, ...] = (str(error_class),)
            else:
                predicate = "target_hash<>?"
                params = (str(error_class), str(current_target_hash))
            return self.conn.execute(
                "UPDATE compliance_events SET delivery_status='needs_attention',"
                "error_class=? WHERE creation_mode='live' "
                "AND delivery_status IN ('frozen','pending','failed') AND "
                + predicate,
                params,
            ).rowcount

    def claim_due_event(
        self, *, now: float, target_hash: str,
    ) -> sqlite3.Row | None:
        with self._immediate():
            self.conn.execute(
                "UPDATE compliance_events SET "
                "delivery_status='needs_attention',"
                "error_class='retry_exhausted' "
                "WHERE delivery_status='failed' AND delivery_attempts>=5"
            )
            for _candidate_index in range(100):
                candidate = self.conn.execute(
                    _DUE_EVENT_SELECT,
                    (float(now),),
                ).fetchone()
                if candidate is None:
                    return None
                rowid = int(candidate["compliance_rowid"])
                event_key = candidate["event_key"]
                delivery_key = candidate["delivery_key"]
                if (
                    not isinstance(event_key, str)
                    or _SHA256.fullmatch(event_key) is None
                    or delivery_key != f"compliance:{event_key}"
                ):
                    self.conn.execute(
                        "UPDATE compliance_events SET "
                        "delivery_status='needs_attention',"
                        "error_class='invalid_delivery_identity' "
                        "WHERE rowid=? AND delivery_status=?",
                        (rowid, str(candidate["delivery_status"])),
                    )
                    continue
                durable_target = candidate["target_hash"]
                if (
                    candidate["creation_mode"] != "live"
                    or not isinstance(durable_target, str)
                    or _SHA256.fullmatch(durable_target) is None
                ):
                    self.conn.execute(
                        "UPDATE compliance_events SET "
                        "delivery_status='needs_attention',"
                        "error_class='invalid_creation_target' "
                        "WHERE rowid=? AND delivery_status=?",
                        (rowid, str(candidate["delivery_status"])),
                    )
                    continue
                if durable_target != str(target_hash):
                    self.conn.execute(
                        "UPDATE compliance_events SET "
                        "delivery_status='needs_attention',"
                        "error_class='recipient_mismatch' "
                        "WHERE rowid=? AND delivery_status=?",
                        (rowid, str(candidate["delivery_status"])),
                    )
                    continue
                try:
                    decode_frozen_payload(
                        candidate["payload_json"], candidate["payload_hash"]
                    )
                except ValueError:
                    self.conn.execute(
                        "UPDATE compliance_events SET "
                        "delivery_status='needs_attention',"
                        "error_class='invalid_frozen_payload' "
                        "WHERE rowid=? AND delivery_status=?",
                        (
                            rowid,
                            str(candidate["delivery_status"]),
                        ),
                    )
                    continue
                if not self._claim_event_candidate(candidate, float(now)):
                    continue
                return self.conn.execute(
                    "SELECT * FROM compliance_events WHERE rowid=?",
                    (rowid,),
                ).fetchone()
            return None

    def _claim_event_candidate(
        self, candidate: sqlite3.Row, now: float,
    ) -> bool:
        changed = self.conn.execute(
            "UPDATE compliance_events SET delivery_status='sending',"
            "delivery_attempts=delivery_attempts+1,error_class='' "
            "WHERE rowid=? AND delivery_status=? "
            "AND next_attempt_at<=? AND delivery_attempts<5",
            (
                int(candidate["compliance_rowid"]),
                str(candidate["delivery_status"]),
                float(now),
            ),
        ).rowcount
        return changed == 1

    def sending_events(self) -> tuple[sqlite3.Row, ...]:
        return tuple(self.conn.execute(
            "SELECT * FROM compliance_events "
            "WHERE delivery_status='sending' "
            "ORDER BY created_at_ms,event_key"
        ).fetchall())

    def finalize_claimed_event(
        self,
        event_key: str,
        *,
        status: str,
        error_class: str = "",
        next_attempt_at: float = 0,
        sent_at_ms: int = 0,
    ) -> bool:
        if status not in {
            "sent", "delivery_unknown", "failed", "needs_attention"
        }:
            raise ValueError("invalid compliance delivery final status")
        with self._immediate():
            changed = self.conn.execute(
                "UPDATE compliance_events SET delivery_status=?,error_class=?,"
                "next_attempt_at=?,sent_at_ms=CASE "
                "WHEN sent_at_ms=0 AND ?>0 THEN ? ELSE sent_at_ms END "
                "WHERE event_key=? AND delivery_status='sending'",
                (
                    status, str(error_class), float(next_attempt_at),
                    int(sent_at_ms), int(sent_at_ms), str(event_key),
                ),
            ).rowcount
            return changed == 1


__all__ = ["StoreEventsMixin", "decode_frozen_payload"]
