"""Shared compatible-chat-completions client for configured LLM providers."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Literal


log = logging.getLogger("review.ai")
MIN_THINKING_RETRY_TOKENS = 16_000


@dataclass(frozen=True)
class LLMResponse:
    content: str
    latency_ms: int
    usage: dict[str, int]


class LLMCircuitOpen(RuntimeError):
    """The recent request failure threshold opened this LLM's circuit."""


_LLM_CIRCUIT_LOCK = threading.Lock()
_LLM_CIRCUITS: dict[str, dict[str, float | int | bool]] = {}


def llm_config(cfg: dict) -> dict:
    """Read the current LLM section while preserving the historical fallback."""
    llm = cfg.get("llm") or {}
    if not llm.get("api_key"):
        llm = (cfg.get("talktrack", {}).get("llm") or {}) or {}
    return llm or {}


def _endpoint(llm: dict) -> tuple[str, str, str]:
    provider = str(llm.get("provider", "deepseek"))
    base_url = str(llm.get("base_url", ""))
    if provider == "dashscope":
        base_url = base_url or "https://dashscope.aliyuncs.com/compatible-mode/v1"
    else:
        base_url = base_url or "https://api.deepseek.com"
    return provider, base_url, str(llm.get("model", "deepseek-chat"))


def _usage(raw: object) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, int] = {}
    for key, value in raw.items():
        if isinstance(value, bool):
            continue
        try:
            result[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return result


def _request_with_existing_circuit_and_empty_body_retry(
    cfg: dict,
    messages: list[dict],
    temperature: float,
    max_tokens: int,
    thinking: Literal["enabled", "disabled"],
    configured_timeout: int,
    deadline_at: float | None,
) -> LLMResponse:
    """The legacy request path, kept byte-for-behaviour compatible where it matters."""
    import requests

    llm = llm_config(cfg)
    api_key = str(llm.get("api_key") or "").strip()
    if not api_key:
        raise RuntimeError("未配置 LLM api_key")
    provider, base_url, model = _endpoint(llm)
    if thinking not in ("enabled", "disabled"):
        raise ValueError(f"不支持的 LLM 思考模式: {thinking}")

    circuit_mode = thinking if provider == "deepseek" else "provider_default"
    circuit_key = f"{provider}|{base_url}|{model}|thinking={circuit_mode}"
    threshold = max(1, int(llm.get("circuit_failures", 3)))
    cooldown = max(30, int(llm.get("circuit_cooldown_sec", 600)))
    now = time.monotonic()
    with _LLM_CIRCUIT_LOCK:
        circuit = _LLM_CIRCUITS.setdefault(
            circuit_key, {"failures": 0, "open_until": 0.0, "logged": False})
        open_until = float(circuit.get("open_until", 0.0))
        if open_until > now:
            remaining = max(1, int(open_until - now))
            raise LLMCircuitOpen(f"LLM 熔断中，约 {remaining} 秒后再试")
        if open_until:
            circuit.update({"failures": 0, "open_until": 0.0, "logged": False})

    started = time.monotonic()
    try:
        token_budget = max_tokens
        content = ""
        usage: dict[str, int] = {}
        finish_reason = "unknown"
        for response_attempt in range(2):
            if deadline_at is None:
                request_timeout: tuple[float, float] = (10, configured_timeout)
            else:
                remaining = deadline_at - time.time()
                # A requests timeout has distinct connect and read budgets.  Do not
                # issue an attempt unless there is enough budget for both.
                if remaining < 2:
                    raise TimeoutError("deadline_exceeded")
                connect_timeout = min(10.0, max(1.0, remaining / 3))
                read_timeout = min(float(configured_timeout), remaining - connect_timeout)
                request_timeout = (connect_timeout, read_timeout)
            payload = {"model": model, "messages": messages, "max_tokens": token_budget}
            if provider == "deepseek":
                payload["thinking"] = {"type": thinking}
                if thinking == "enabled":
                    payload["reasoning_effort"] = "high"
                if thinking == "disabled":
                    payload["temperature"] = temperature
            else:
                payload["temperature"] = temperature
            response = requests.post(
                base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=request_timeout,
            )
            response.raise_for_status()
            body = response.json()
            choice = body["choices"][0]
            finish_reason = str(choice.get("finish_reason") or "unknown")
            content = choice.get("message", {}).get("content")
            usage = _usage(body.get("usage")) if isinstance(body, dict) else {}
            if isinstance(content, str) and content.strip():
                break
            if response_attempt == 0:
                if finish_reason == "length":
                    token_budget = max(token_budget + 1, token_budget * 2)
                    if provider == "deepseek" and thinking == "enabled":
                        token_budget = max(token_budget, MIN_THINKING_RETRY_TOKENS)
                log.warning("LLM 正文为空（finish_reason=%s），自动重试一次，max_tokens=%d",
                            finish_reason, token_budget)
                continue
            raise RuntimeError(
                f"LLM 响应正文为空（finish_reason={finish_reason}，自动重试后仍为空）")
    except TimeoutError:
        raise
    except Exception:
        with _LLM_CIRCUIT_LOCK:
            circuit = _LLM_CIRCUITS.setdefault(
                circuit_key, {"failures": 0, "open_until": 0.0, "logged": False})
            failures = int(circuit.get("failures", 0)) + 1
            circuit["failures"] = failures
            if failures >= threshold:
                circuit["open_until"] = time.monotonic() + cooldown
                if not circuit.get("logged"):
                    log.warning("LLM 连续失败 %d 次，熔断 %d 秒", failures, cooldown)
                    circuit["logged"] = True
        raise

    with _LLM_CIRCUIT_LOCK:
        _LLM_CIRCUITS[circuit_key] = {"failures": 0, "open_until": 0.0, "logged": False}
    return LLMResponse(
        content=str(content),
        latency_ms=max(0, int((time.monotonic() - started) * 1000)),
        usage=usage,
    )


def chat_completion(
    cfg: dict,
    messages: list[dict],
    *,
    temperature: float,
    max_tokens: int,
    thinking: Literal["enabled", "disabled"],
    deadline_at: float | None = None,
) -> LLMResponse:
    """Submit one bounded request and return response text plus accounting metadata."""
    remaining = None if deadline_at is None else deadline_at - time.time()
    if remaining is not None and remaining <= 0:
        raise TimeoutError("deadline_exceeded")
    configured = max(15, int(llm_config(cfg).get("timeout_sec", 180)))
    return _request_with_existing_circuit_and_empty_body_retry(
        cfg, messages, temperature, max_tokens, thinking, configured, deadline_at)
