from __future__ import annotations

from dataclasses import dataclass


_HEALTH_CODES = (
    "COMPLIANCE_AUDIO_BLIND",
    "COMPLIANCE_BACKLOG",
    "COMPLIANCE_WORDLIST",
    "COMPLIANCE_MODEL",
    "COMPLIANCE_DELIVERY_UNKNOWN",
)

_HEALTH_DETAILS = {
    "COMPLIANCE_AUDIO_BLIND": (
        "连续10分钟未获取有效合规音频，请检查监听器。"
    ),
    "COMPLIANCE_BACKLOG": (
        "合规识别任务积压超过5分钟，请检查监听器。"
    ),
    "COMPLIANCE_WORDLIST": (
        "极限词词库连续同步失败，请检查监听器。"
    ),
    "COMPLIANCE_MODEL": (
        "极限词识别模型连续失败，请检查监听器。"
    ),
    "COMPLIANCE_DELIVERY_UNKNOWN": (
        "极限词告警投递状态不确定，已停止自动重发，请人工核查。"
    ),
}


@dataclass(frozen=True)
class HealthTransition:
    code: str
    detail: str
    alert_due: bool = False
    recovered: bool = False


class ComplianceHealthService:
    """Evaluate and persist health transitions without external delivery."""

    def __init__(self, store: object) -> None:
        self.store = store

    def evaluate(
        self, *, now: float, settings: object,
    ) -> tuple[HealthTransition, ...]:
        del settings
        try:
            live_ids = tuple(
                self.store.active_live_ids()  # type: ignore[attr-defined]
            )
        except Exception:
            return ()
        if len(live_ids) != 1:
            try:
                self.store.bind_audio_health_live(  # type: ignore[attr-defined]
                    "", now=float(now)
                )
            except Exception:
                return ()
            conditions = {code: False for code in _HEALTH_CODES}
            skip_audio_update = True
        else:
            try:
                state = self.store.runtime_state()  # type: ignore[attr-defined]
                now_ms = int(float(now) * 1000)
                last_valid_audio_ms = int(
                    self.store.bind_audio_health_live(  # type: ignore[attr-defined]
                        live_ids[0], now=float(now)
                    )
                )
                skip_audio_update = False
                if last_valid_audio_ms > 0:
                    audio_blind = now_ms - last_valid_audio_ms >= 600_000
                else:
                    anchor = self.store.health_observation_anchor(  # type: ignore[attr-defined]
                        "COMPLIANCE_AUDIO_BLIND", now=float(now)
                    )
                    audio_blind = float(now) - float(anchor) >= 600.0
                    if not audio_blind:
                        current_health = self.store.health_state()  # type: ignore[attr-defined]
                        skip_audio_update = not bool(
                            current_health.get(
                                "COMPLIANCE_AUDIO_BLIND", {}
                            ).get("active", False)
                        )
                oldest = self.store.oldest_actionable_audio_job_age(  # type: ignore[attr-defined]
                    float(now)
                )
                conditions = {
                    "COMPLIANCE_AUDIO_BLIND": audio_blind,
                    "COMPLIANCE_BACKLOG": (
                        oldest is not None and float(oldest) > 300.0
                    ),
                    "COMPLIANCE_WORDLIST": (
                        int(state["wordlist_failure_count"]) >= 3
                    ),
                    "COMPLIANCE_MODEL": (
                        int(state["model_failure_count"]) >= 3
                    ),
                    "COMPLIANCE_DELIVERY_UNKNOWN": bool(
                        self.store.has_delivery_unknown()  # type: ignore[attr-defined]
                    ),
                }
            except Exception:
                return ()

        transitions: list[HealthTransition] = []
        for code in _HEALTH_CODES:
            if code == "COMPLIANCE_AUDIO_BLIND" and skip_audio_update:
                continue
            try:
                alert_due, recovered = (
                    self.store.update_health_condition(  # type: ignore[attr-defined]
                        code, active=conditions[code], now=float(now)
                    )
                )
            except Exception:
                continue
            if alert_due or recovered:
                transitions.append(HealthTransition(
                    code=code,
                    detail=_HEALTH_DETAILS[code],
                    alert_due=bool(alert_due),
                    recovered=bool(recovered),
                ))
        return tuple(transitions)


__all__ = ["ComplianceHealthService", "HealthTransition"]
