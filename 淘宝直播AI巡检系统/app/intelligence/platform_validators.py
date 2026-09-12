"""Validation for the single current DeepSeek daily-analysis contract."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .platform_models import PlatformIntelligenceContext


_OUTPUT_FIELDS = frozenset((
    "full_analysis", "business_conclusions", "next_actions",
))
_CAUSAL_RE = re.compile(
    r"导致|造成|引发|助推|促成|促进|推动|拉动|驱动|归因于|归功于|得益于"
)
_FORBIDDEN_RE = re.compile(
    r"用户心理|消费心理|购买意愿|购买欲|"
    r"(?:用户|观众|消费者).{0,12}(?:心理|信任|意愿|心动|想下单)"
)


@dataclass(frozen=True)
class PlatformValidationResult:
    full_analysis: str = ""
    business_conclusions: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    rejections: list[dict[str, Any]] = field(default_factory=list)
    structurally_valid: bool = True

    def has_content(self) -> bool:
        return bool(
            self.full_analysis
            and self.business_conclusions
            and self.next_actions
        )


def _rejection(code: str, item_type: str, index: int, detail: str) -> dict[str, Any]:
    return {
        "code": code,
        "item_type": item_type,
        "index": int(index),
        "detail": str(detail)[:240],
    }


def _validate_text(text: str) -> str:
    from .validators import _unsupported_concrete_sales_claim

    if not text.strip():
        return "empty_text"
    if _unsupported_concrete_sales_claim(text, []):
        return "forbidden_claim"
    if _FORBIDDEN_RE.search(text):
        return "forbidden_claim"
    if _CAUSAL_RE.search(text):
        return "causal_overclaim"
    return ""


def validate_platform_output(
        context: PlatformIntelligenceContext, value: object) -> PlatformValidationResult:
    """Accept only the v2 daily payload; old summary shapes fail closed."""
    del context
    if not isinstance(value, dict) or set(value) != _OUTPUT_FIELDS:
        return PlatformValidationResult(
            rejections=[_rejection(
                "invalid_schema", "output", -1,
                "requires exactly full_analysis, business_conclusions, next_actions",
            )],
            structurally_valid=False,
        )
    if (not isinstance(value.get("full_analysis"), str)
            or not isinstance(value.get("business_conclusions"), list)
            or not isinstance(value.get("next_actions"), list)
            or any(not isinstance(item, str)
                   for item in value["business_conclusions"])
            or any(not isinstance(item, str) for item in value["next_actions"])):
        return PlatformValidationResult(
            rejections=[_rejection(
                "invalid_schema", "output", -1, "daily fields have invalid types",
            )],
            structurally_valid=False,
        )

    rejections: list[dict[str, Any]] = []
    full_analysis = value["full_analysis"].strip()
    issue = _validate_text(full_analysis)
    if issue:
        rejections.append(_rejection(issue, "full_analysis", -1, issue))
        full_analysis = ""

    sections: dict[str, list[str]] = {}
    for section in ("business_conclusions", "next_actions"):
        accepted: list[str] = []
        seen: set[str] = set()
        for index, raw in enumerate(value[section]):
            text = raw.strip()
            key = re.sub(r"[\W_]+", "", text.casefold())
            issue = _validate_text(text)
            if not key or key in seen or issue:
                if issue:
                    rejections.append(_rejection(issue, section, index, issue))
                continue
            seen.add(key)
            accepted.append(text)
        sections[section] = accepted

    return PlatformValidationResult(
        full_analysis=full_analysis,
        business_conclusions=sections["business_conclusions"],
        next_actions=sections["next_actions"],
        rejections=rejections,
        structurally_valid=True,
    )


__all__ = ["PlatformValidationResult", "validate_platform_output"]
