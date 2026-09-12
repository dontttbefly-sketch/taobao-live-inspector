"""Strict, deadline-aware DeepSeek intelligence provider."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from typing import Any, Literal

from app.llm_client import LLMResponse, chat_completion, llm_config

from .models import IntelligenceContext
from .prompts.hourly import build_actions_messages
from .prompts.platform import build_platform_messages
from .settings import load_intelligence_settings


DEEPSEEK_MAX_OUTPUT_TOKENS = 384_000


@dataclass(frozen=True)
class ProviderResult:
    value: dict | None
    raw: str
    latency_ms: int
    usage: dict[str, int]
    error: str = ""


def _response_error(exc: Exception) -> str:
    if isinstance(exc, TimeoutError) and str(exc) == "deadline_exceeded":
        return "deadline_exceeded"
    return type(exc).__name__


def _thinking_not_supported(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) != 400:
        return False
    body = str(getattr(response, "text", "") or getattr(exc, "args", "")).casefold()
    return "thinking" in body and any(marker in body for marker in (
        "not support", "unsupported", "not enabled", "unavailable", "unknown",
    ))


class DeepSeekIntelligenceProvider:
    """Provider boundary: untrusted facts in, strict JSON object results out."""

    _thinking_capabilities: dict[tuple[str, str, str], bool] = {}
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.settings = load_intelligence_settings(cfg)

    def _capability_key(self) -> tuple[str, str, str]:
        llm = llm_config(self.cfg)
        provider = str(llm.get("provider", "deepseek"))
        base_url = str(llm.get("base_url", "")) or (
            "https://dashscope.aliyuncs.com/compatible-mode/v1"
            if provider == "dashscope" else "https://api.deepseek.com"
        )
        return provider, base_url.rstrip("/"), str(llm.get("model", "deepseek-chat"))

    @staticmethod
    def _before_deadline(deadline_at: float | None) -> bool:
        return deadline_at is None or time.time() < deadline_at

    @staticmethod
    def _with_elapsed(started_at: float, result: ProviderResult) -> ProviderResult:
        """Public provider calls report their complete wall time, including failures."""
        elapsed_ms = max(0, int((time.monotonic() - started_at) * 1000))
        return replace(result, latency_ms=elapsed_ms)

    def _request(self, messages: list[dict], *, deadline_at: float | None,
                 thinking: Literal["enabled", "disabled"] = "enabled") -> ProviderResult:
        if not self._before_deadline(deadline_at):
            return ProviderResult(None, "", 0, {}, "deadline_exceeded")
        capability_key = self._capability_key()
        mode: Literal["enabled", "disabled"] = (
            "disabled" if self._thinking_capabilities.get(capability_key) is False else thinking
        )
        try:
            response = chat_completion(
                self.cfg, messages, temperature=0.0,
                max_tokens=DEEPSEEK_MAX_OUTPUT_TOKENS,
                thinking=mode, deadline_at=deadline_at,
            )
        except Exception as exc:
            if mode == "enabled" and _thinking_not_supported(exc) and self._before_deadline(deadline_at):
                self._thinking_capabilities[capability_key] = False
                try:
                    response = chat_completion(
                        self.cfg, messages, temperature=0.0,
                        max_tokens=DEEPSEEK_MAX_OUTPUT_TOKENS,
                        thinking="disabled", deadline_at=deadline_at,
                    )
                except Exception as retry_exc:
                    return ProviderResult(None, "", 0, {}, _response_error(retry_exc))
            else:
                return ProviderResult(None, "", 0, {}, _response_error(exc))

        raw = response.content
        try:
            value = json.loads(raw.strip())
        except (TypeError, ValueError, json.JSONDecodeError):
            return ProviderResult(None, raw, response.latency_ms, response.usage, "invalid_json")
        if not isinstance(value, dict):
            return ProviderResult(None, raw, response.latency_ms, response.usage, "top_level_not_object")
        return ProviderResult(value, raw, response.latency_ms, response.usage)

    def design_actions(self, context: IntelligenceContext, evidence: dict[str, Any], *,
                       deadline_at: float | None = None,
                       repair_errors: list[dict[str, Any]] | None = None) -> ProviderResult:
        started_at = time.monotonic()
        return self._with_elapsed(
            started_at,
            self._request(
                build_actions_messages(
                    context, evidence, validation_errors=repair_errors),
                deadline_at=deadline_at,
                thinking=self.settings.hourly_reasoning,
            ),
        )

    def analyze_platform_review(
        self,
        context: object,
        *,
        deadline_at: float | None = None,
        repair_errors: list[dict[str, Any]] | None = None,
    ) -> ProviderResult:
        started_at = time.monotonic()
        return self._with_elapsed(
            started_at,
            self._request(
                build_platform_messages(
                    context, validation_errors=repair_errors),
                deadline_at=deadline_at,
                thinking=self.settings.platform_reasoning,
            ),
        )
