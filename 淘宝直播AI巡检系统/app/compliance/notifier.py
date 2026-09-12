from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from app.config import SHANGHAI
from app.notify.feishu import (
    ExplicitRemoteRejection,
    build_alert_card,
    card_v2,
    delivery_status,
    section_panel,
    send_card_once,
)

from .config import ComplianceSettings
from .store import ComplianceStore, decode_frozen_payload


Sender = Callable[[dict, str, dict, str], bool]
StatusReader = Callable[[str], str]
_EVENT_KEY = re.compile(r"[0-9a-f]{64}\Z")
_TARGET_HASH = re.compile(r"[0-9a-f]{64}\Z")
_RETRY_BACKOFF_SECONDS = (30, 120, 300, 600, 900)
_TECHNICAL_CODES = frozenset({
    "COMPLIANCE_AUDIO_BLIND",
    "COMPLIANCE_BACKLOG",
    "COMPLIANCE_DELIVERY_UNKNOWN",
    "COMPLIANCE_MODEL",
    "COMPLIANCE_WORDLIST",
})


def _markdown_escape(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    for character in "*_[]<>`":
        escaped = escaped.replace(character, f"\\{character}")
    return escaped


def _required_text(event: Mapping[str, object], field: str) -> str:
    value = event[field]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _hit_start_ms(event: Mapping[str, object]) -> int:
    value = event["hit_start_ms"]
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("hit_start_ms must be an integer")
    if value < 0:
        raise ValueError("hit_start_ms must be nonnegative")
    return value


def build_compliance_card(event: Mapping[str, object]) -> dict:
    term = _markdown_escape(_required_text(event, "raw_term"))
    anchor_value = event["anchor_name"]
    anchor = (
        anchor_value.strip()
        if isinstance(anchor_value, str) and anchor_value.strip()
        else "待确认"
    )
    anchor = _markdown_escape(anchor)
    sentence = _markdown_escape(_required_text(event, "sentence_text"))
    try:
        beijing_time = datetime.fromtimestamp(
            _hit_start_ms(event) / 1000, SHANGHAI
        ).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("hit_start_ms is outside the supported range") from exc
    return card_v2(
        "直播极限词告警",
        "red",
        [
            section_panel(
                "命中信息",
                (
                    f"**极限词：** {term}\n"
                    f"**当前排班主播：** {anchor}\n"
                    f"**北京时间：** {beijing_time}\n"
                    f"**完整识别原话：** {sentence}\n"
                    "机器识别，请人工复核。"
                ),
                color="red",
            ),
        ],
        subtitle="直播语音命中机器识别词库",
        status="需复核",
        status_color="red",
        icon="warning_colorful",
    )


def _serialize_payload(card: dict) -> str:
    return json.dumps(
        card,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _validate_event_material(event: Mapping[str, object]) -> None:
    event_key = event["event_key"]
    delivery_key = event["delivery_key"]
    if (
        not isinstance(event_key, str)
        or _EVENT_KEY.fullmatch(event_key) is None
        or delivery_key != f"compliance:{event_key}"
    ):
        raise ValueError("event delivery identity is invalid")
    _required_text(event, "raw_term")
    _required_text(event, "sentence_text")
    _hit_start_ms(event)
    mode = event["creation_mode"]
    target_hash = event["target_hash"]
    valid_target = (
        mode == "shadow" and target_hash == ""
    ) or (
        mode == "live"
        and isinstance(target_hash, str)
        and _TARGET_HASH.fullmatch(target_hash) is not None
    )
    if not valid_target:
        raise ValueError("event target identity is invalid")


def _decode_frozen_payload(event: Mapping[str, object]) -> dict:
    return decode_frozen_payload(event["payload_json"], event["payload_hash"])


class ComplianceNotifier:
    def __init__(
        self,
        store: ComplianceStore,
        *,
        sender: Sender = send_card_once,
        status_reader: StatusReader = delivery_status,
        clock: Callable[[], float] = time.time,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.store = store
        self.sender = sender
        self.status_reader = status_reader
        self.clock = clock
        self.config = config or {}

    def freeze_pending(self) -> int:
        frozen = 0
        for event in self.store.pending_payload_events():
            try:
                _validate_event_material(event)
                card = build_compliance_card(event)
                payload_json = _serialize_payload(card)
                payload_hash = hashlib.sha256(
                    payload_json.encode("utf-8")
                ).hexdigest()
            except (KeyError, TypeError, ValueError, OverflowError):
                self.store.fail_pending_event(
                    str(event["event_key"]), "invalid_event_payload"
                )
                continue
            frozen += int(self.store.freeze_event_payload(
                str(event["event_key"]),
                payload_json=payload_json,
                payload_hash=payload_hash,
            ))
        return frozen

    def deliver_due(self, settings: ComplianceSettings) -> int:
        if settings.mode != "live":
            self.suppress_for_non_live_mode()
            return 0
        recipient = settings.recipient_chat_id
        if not isinstance(recipient, str) or not recipient.strip():
            self.store.reject_delivery_targets(
                current_target_hash=None,
                error_class="recipient_unavailable",
            )
            return 0
        current_target_hash = hashlib.sha256(
            recipient.encode("utf-8")
        ).hexdigest()
        self.store.reject_delivery_targets(
            current_target_hash=current_target_hash,
            error_class="recipient_mismatch",
        )
        sent = 0
        while True:
            event = self.store.claim_due_event(
                now=float(self.clock()), target_hash=current_target_hash
            )
            if event is None:
                return sent
            event_key = str(event["event_key"])
            delivery_key = str(event["delivery_key"])
            try:
                card = _decode_frozen_payload(event)
            except ValueError:
                final_status = self._finalize_claim(
                    event_key,
                    status="needs_attention",
                    error_class="invalid_frozen_payload",
                )
                sent += int(final_status == "sent")
                continue
            explicit_rejection = False
            try:
                self.sender(self.config, recipient, card, delivery_key)
            except ExplicitRemoteRejection:
                explicit_rejection = True
            except Exception:
                pass
            try:
                ledger_status = str(self.status_reader(delivery_key) or "")
            except Exception:
                ledger_status = "delivery_unknown"
            if ledger_status == "sent":
                final_status = self._finalize_claim(
                    event_key,
                    status="sent",
                    sent_at_ms=int(float(self.clock()) * 1000),
                )
                sent += int(final_status == "sent")
                continue
            if explicit_rejection and ledger_status == "failed":
                attempts = int(event["delivery_attempts"])
                if attempts >= len(_RETRY_BACKOFF_SECONDS):
                    final_status = self._finalize_claim(
                        event_key,
                        status="needs_attention",
                        error_class="remote_rejection_exhausted",
                    )
                else:
                    final_status = self._finalize_claim(
                        event_key,
                        status="failed",
                        error_class="remote_rejected",
                        next_attempt_at=(
                            float(self.clock())
                            + _RETRY_BACKOFF_SECONDS[attempts - 1]
                        ),
                    )
                sent += int(final_status == "sent")
                continue
            final_status = self._finalize_claim(
                event_key,
                status="delivery_unknown",
                error_class="delivery_ambiguous",
            )
            sent += int(final_status == "sent")

    def _finalize_claim(
        self,
        event_key: str,
        *,
        status: str,
        error_class: str = "",
        next_attempt_at: float = 0,
        sent_at_ms: int = 0,
    ) -> str:
        changed = self.store.finalize_claimed_event(
            event_key,
            status=status,
            error_class=error_class,
            next_attempt_at=next_attempt_at,
            sent_at_ms=sent_at_ms,
        )
        if changed:
            return status
        current = self.store.get_event(event_key)
        return "" if current is None else str(current["delivery_status"])

    def suppress_for_non_live_mode(self) -> int:
        return self.store.suppress_unsent_live_events()

    def reconcile_sending(self) -> int:
        reconciled = 0
        for event in self.store.sending_events():
            event_key = str(event["event_key"])
            try:
                ledger_status = str(
                    self.status_reader(str(event["delivery_key"])) or ""
                )
            except Exception:
                ledger_status = "delivery_unknown"
            now = float(self.clock())
            attempts = int(event["delivery_attempts"])
            if ledger_status == "sent":
                changed = self.store.finalize_claimed_event(
                    event_key,
                    status="sent",
                    sent_at_ms=int(now * 1000),
                )
            elif ledger_status == "failed" and 0 < attempts < len(
                _RETRY_BACKOFF_SECONDS
            ):
                changed = self.store.finalize_claimed_event(
                    event_key,
                    status="failed",
                    error_class="remote_rejected",
                    next_attempt_at=(
                        now + _RETRY_BACKOFF_SECONDS[attempts - 1]
                    ),
                )
            elif ledger_status == "failed" and attempts >= len(
                _RETRY_BACKOFF_SECONDS
            ):
                changed = self.store.finalize_claimed_event(
                    event_key,
                    status="needs_attention",
                    error_class="remote_rejection_exhausted",
                )
            else:
                changed = self.store.finalize_claimed_event(
                    event_key,
                    status="delivery_unknown",
                    error_class="delivery_ambiguous",
                )
            reconciled += int(changed)
        return reconciled

    def send_technical_issue(self, code: str, detail: str) -> bool:
        notify = self.config.get("notify") or {}
        feishu = notify.get("feishu") or {} if isinstance(notify, dict) else {}
        technical_chat = (
            str(feishu.get("chat_id") or "")
            if isinstance(feishu, dict)
            else ""
        )
        if not technical_chat:
            return False
        raw_code = str(code or "")
        normalized = raw_code.strip().upper()
        if normalized not in _TECHNICAL_CODES:
            normalized = "INVALID_" + hashlib.sha256(
                raw_code.encode("utf-8")
            ).hexdigest()[:12].upper()
        key = (
            f"compliance-tech:{normalized}:"
            f"{int(float(self.clock()) // 3600)}"
        )
        try:
            return bool(self.sender(
                self.config,
                technical_chat,
                build_alert_card("极限词监听器", str(detail)),
                key,
            ))
        except Exception:
            return False


__all__ = ["ComplianceNotifier", "build_compliance_card"]
