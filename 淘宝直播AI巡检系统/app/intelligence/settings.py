"""Single bounded configuration surface for the intelligence loop."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ReasoningMode = Literal["enabled", "disabled"]


@dataclass(frozen=True)
class IntelligenceSettings:
    enabled: bool = True
    # 2026-08-08 owner 拍板：分析截止从 300 秒放宽到 600 秒（thinking 模式下
    # 一小时上下文约需 5-10 分钟），上限保留 3600 秒的弹性。
    deadline_seconds: float = 600.0
    structure_repair_attempts: int = 1
    hourly_reasoning: ReasoningMode = "enabled"
    platform_reasoning: ReasoningMode = "enabled"
    retry_delay_seconds: float = 300.0


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "yes", "on", "1", "enabled"}:
            return True
        if normalized in {"false", "no", "off", "0", "disabled"}:
            return False
    return default


def _float(value: object, default: float, lower: float, upper: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    if parsed != parsed:  # NaN
        parsed = default
    return max(lower, min(upper, parsed))


def _int(value: object, default: int, lower: int, upper: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(lower, min(upper, parsed))


def _reasoning(value: object) -> ReasoningMode:
    return "disabled" if str(value or "").strip() == "disabled" else "enabled"


def load_intelligence_settings(cfg: object) -> IntelligenceSettings:
    """Normalize all advertised intelligence options once and within bounds."""
    root = _mapping(cfg)
    raw = _mapping(root.get("intelligence"))
    reasoning = _mapping(raw.get("reasoning"))
    return IntelligenceSettings(
        enabled=_bool(raw.get("enabled"), True),
        # Sub-second values are useful for deterministic deadline probes while
        # the production ceiling remains the documented five minutes.
        deadline_seconds=_float(
            raw.get("deadline_seconds"), 600.0, 0.001, 3600.0),
        structure_repair_attempts=_int(
            raw.get("structure_repair_attempts"), 1, 0, 1),
        hourly_reasoning=_reasoning(reasoning.get("hourly")),
        platform_reasoning=_reasoning(reasoning.get("platform")),
        retry_delay_seconds=_float(
            raw.get("retry_delay_seconds"), 300.0, 1.0, 3600.0),
    )


__all__ = [
    "IntelligenceSettings",
    "ReasoningMode",
    "load_intelligence_settings",
]
