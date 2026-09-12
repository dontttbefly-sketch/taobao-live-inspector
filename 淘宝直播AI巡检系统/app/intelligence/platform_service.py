"""Persisted, leased, fenced platform intelligence state machine."""
from __future__ import annotations

import time
from typing import Any, Callable

from app.db import Store
from app.llm_client import llm_config

from .deepseek import DeepSeekIntelligenceProvider, ProviderResult
from .integrity import validated_result_integrity_matches
from .models import canonical_json
from .platform_models import (
    PlatformIntelligenceContext,
    PlatformIntelligenceResult,
    platform_context_input_hash,
)
from .platform_validators import validate_platform_output
from .prompts import PROMPT_VERSION
from .settings import load_intelligence_settings
from .service import IntelligenceRetryPending


_TERMINAL = frozenset(("ready", "blocked"))


class PlatformIntelligenceBlocked(RuntimeError):
    pass


def load_frozen_platform_intelligence(
        store: Store, live_id: str, *, job_key: str | None = None,
        expected_result: object | None = None,
) -> tuple[PlatformIntelligenceContext, PlatformIntelligenceResult, list[dict[str, Any]]]:
    """Load one exact job/snapshot/result/source binding for downstream consumers."""
    live_id = str(live_id)
    key = str(job_key or "")
    if not key and isinstance(expected_result, dict):
        key = str(expected_result.get("job_key") or "")
    if not key:
        row = store.query(
            """SELECT job_key FROM intelligence_jobs
               WHERE task_type='platform' AND live_id=? AND status='ready'
               ORDER BY created_at DESC, job_key DESC LIMIT 1""",
            (live_id,),
        )
        key = str(row[0]["job_key"]) if row else ""
    job = store.get_intelligence_job(key)
    artifact = store.get_intelligence_artifact(key)
    if (not job or str(job.get("task_type") or "") != "platform"
            or str(job.get("live_id") or "") != live_id
            or str(job.get("status") or "") != "ready"
            or not artifact or not artifact.get("context_snapshot")
            or not artifact.get("validated_result")):
        raise ValueError("missing frozen platform intelligence binding")
    context = PlatformIntelligenceContext.from_dict(artifact["context_snapshot"])
    if (not context.input_hash
            or platform_context_input_hash(context) != context.input_hash
            or context.input_hash != str(job.get("input_hash") or "")
            or context.live_id != live_id):
        raise ValueError("frozen platform context binding mismatch")
    payload = artifact["validated_result"]
    if not validated_result_integrity_matches(
            payload, artifact.get("validated_result_hash")):
        raise ValueError("frozen platform result integrity mismatch")
    result = PlatformIntelligenceResult.from_dict(payload)
    if result.job_key != key or result.status != str(job.get("status") or ""):
        raise ValueError("frozen platform result identity mismatch")
    if expected_result is not None:
        expected = PlatformIntelligenceResult.from_dict(expected_result)
        if canonical_json(expected.to_dict()) != canonical_json(result.to_dict()):
            raise ValueError("formal review and platform artifact differ")
    rejected = artifact.get("rejected")
    if not isinstance(rejected, list) or any(not isinstance(item, dict) for item in rejected):
        raise ValueError("platform rejection audit is invalid")
    return context, result, list(rejected)


class PlatformIntelligenceService:
    def __init__(
        self, cfg: dict, store: Store,
        provider: DeepSeekIntelligenceProvider | None = None,
        now_fn: Callable[[], float] = time.time,
    ):
        self.cfg = cfg
        self.store = store
        self.settings = load_intelligence_settings(cfg)
        self.provider = provider or DeepSeekIntelligenceProvider(cfg)
        self.now_fn = now_fn

    def _advance(self, key: str, status: str, generation: int, now: float,
                 error: str = "", fallback_reason: str = "") -> None:
        if not self.store.advance_intelligence_job(
            key, status, claim_generation=generation, now=now,
            error=error, fallback_reason=fallback_reason,
        ):
            raise PlatformIntelligenceBlocked("platform intelligence lease lost")

    def _save(self, key: str, generation: int, now: float, **kwargs: object) -> None:
        if not self.store.save_intelligence_artifact(
            key, claim_generation=generation, now=now, **kwargs,
        ):
            raise PlatformIntelligenceBlocked("platform intelligence artifact lease lost")

    def _defer_retry(self, key: str, generation: int, now: float, reason: str) -> None:
        retry_at = float(now) + self.settings.retry_delay_seconds
        deadline_at = retry_at + self.settings.deadline_seconds
        if not self.store.defer_intelligence_job_retry(
            key,
            claim_generation=generation,
            now=now,
            retry_at=retry_at,
            deadline_at=deadline_at,
            error=reason,
        ):
            raise PlatformIntelligenceBlocked("platform intelligence retry lease lost")
        raise IntelligenceRetryPending(key, retry_at, reason)

    def _terminal(self, job: dict) -> PlatformIntelligenceResult:
        key, status = str(job["job_key"]), str(job["status"])
        if status == "blocked":
            raise PlatformIntelligenceBlocked(f"blocked platform intelligence: {key}")
        try:
            _context, result, _rejected = load_frozen_platform_intelligence(
                self.store, str(job.get("live_id") or ""), job_key=key)
            return result
        except (KeyError, TypeError, ValueError) as exc:
            raise PlatformIntelligenceBlocked(
                f"terminal platform intelligence rejected: {key}") from exc

    def _call(self, context: PlatformIntelligenceContext, deadline: float,
              repair_errors: list[dict[str, Any]] | None = None) -> ProviderResult:
        try:
            kwargs: dict[str, Any] = {"deadline_at": deadline}
            if repair_errors is not None:
                kwargs["repair_errors"] = repair_errors
            result = self.provider.analyze_platform_review(context, **kwargs)
            if isinstance(result, ProviderResult):
                return result
        except Exception as exc:
            return ProviderResult(None, "", 0, {}, type(exc).__name__)
        return ProviderResult(None, "", 0, {}, "invalid_provider_result")

    def analyze_platform(
        self, context: PlatformIntelligenceContext, *, created_at: float | None = None,
    ) -> PlatformIntelligenceResult:
        started = float(self.now_fn())
        created = started if created_at is None else float(created_at)
        deadline = created + self.settings.deadline_seconds
        if (not context.live_id or not context.input_hash
                or platform_context_input_hash(context) != context.input_hash):
            raise PlatformIntelligenceBlocked("platform intelligence input binding mismatch")
        llm = llm_config(self.cfg)
        key = self.store.ensure_intelligence_job(
            task_type="platform", live_id=context.live_id,
            input_hash=context.input_hash, deadline_at=deadline,
            model=str(llm.get("model") or ""), prompt_version=PROMPT_VERSION,
        )
        job = self.store.get_intelligence_job(key)
        if job is None:
            raise PlatformIntelligenceBlocked("platform intelligence job disappeared")
        if (str(job.get("live_id") or "") != context.live_id
                or str(job.get("input_hash") or "") != context.input_hash):
            raise PlatformIntelligenceBlocked("platform intelligence input binding mismatch")
        status = str(job["status"])
        if str(job["status"]) in _TERMINAL:
            return self._terminal(job)
        claim = self.store.claim_intelligence_job(
            key, now=started,
            lease_sec=self.settings.deadline_seconds + 60)
        if claim is None:
            raise PlatformIntelligenceBlocked("platform intelligence already claimed")
        generation = int(claim["attempts"])
        status = str(claim["status"])
        deadline = float(claim.get("deadline_at") or deadline)
        try:
            artifact = self.store.get_intelligence_artifact(key) or {}
        except ValueError as exc:
            self._advance(key, "blocked", generation, started, error="invalid_persisted_json")
            raise PlatformIntelligenceBlocked("invalid persisted platform artifact") from exc
        rejected = list(artifact.get("rejected") or context.rejected_inputs)
        frozen_raw = artifact.get("context_snapshot")
        if frozen_raw:
            try:
                frozen = PlatformIntelligenceContext.from_dict(frozen_raw)
            except (TypeError, ValueError) as exc:
                self._advance(key, "blocked", generation, started, error="invalid_context_snapshot")
                raise PlatformIntelligenceBlocked("invalid platform context snapshot") from exc
            if (platform_context_input_hash(frozen) != frozen.input_hash
                    or frozen.input_hash != str(claim.get("input_hash") or "")):
                self._advance(key, "blocked", generation, started, error="context_snapshot_mismatch")
                raise PlatformIntelligenceBlocked("platform context snapshot mismatch")
            context = frozen
        else:
            self._save(key, generation, started,
                       context_snapshot=context.to_dict(), rejected=rejected)
        if not self.settings.enabled:
            self._advance(key, "blocked", generation, started,
                          error="intelligence disabled")
            raise PlatformIntelligenceBlocked("intelligence disabled")
        if started >= deadline:
            self._defer_retry(
                key, generation, started, "deadline_exceeded_before_start")
        if not any(source.source_type.startswith("hourly_")
                   for source in context.sources):
            self._defer_retry(
                key, generation, started, "no_trusted_hourly_artifacts")
        if not str(llm.get("api_key") or "").strip():
            self._defer_retry(key, generation, started, "missing_api_key")

        if status == "queued":
            self._advance(key, "analyzing_evidence", generation, started)
            status = "analyzing_evidence"
        raw = artifact.get("raw_evidence")
        usage = dict(artifact.get("usage") or {})
        latency_ms = int(artifact.get("latency_ms") or 0)
        provider_result: ProviderResult | None = None
        if status == "analyzing_evidence":
            if not isinstance(raw, dict) or not raw:
                provider_result = self._call(context, deadline)
                now = float(self.now_fn())
                latency_ms += int(provider_result.latency_ms)
                for name, value in provider_result.usage.items():
                    usage[str(name)] = usage.get(str(name), 0) + int(value)
                raw = (provider_result.value if provider_result.value is not None else {
                    "_provider_error": provider_result.error,
                    "_raw": provider_result.raw,
                })
                self._save(key, generation, now, raw_evidence=raw,
                           latency_ms=latency_ms, usage=usage)
                if now >= deadline:
                    self._defer_retry(
                        key, generation, now, "late_platform_result")
                if provider_result.value is None and provider_result.error not in {
                        "invalid_json", "top_level_not_object"}:
                    self._defer_retry(
                        key, generation, now,
                        provider_result.error or "empty_platform_result")
            self._advance(key, "validating_evidence", generation, float(self.now_fn()))
            status = "validating_evidence"

        validation = validate_platform_output(context, raw)
        if status == "validating_evidence":
            if not validation.structurally_valid:
                repair_used = any(
                    "structure_repair_attempted" in str(item.get("detail") or "")
                    for item in rejected if isinstance(item, dict)
                )
                if (repair_used
                        or self.settings.structure_repair_attempts == 0):
                    rejected.extend(validation.rejections)
                    self._defer_retry(
                        key, generation, float(self.now_fn()),
                        "invalid_platform_schema")
                repair_errors = list(validation.rejections)
                rejected.extend({**item, "detail": "structure_repair_attempted: "
                                 + str(item.get("detail") or "")}
                                for item in repair_errors)
                self._save(key, generation, float(self.now_fn()), rejected=rejected)
                repaired = self._call(context, deadline, repair_errors=repair_errors)
                now = float(self.now_fn())
                latency_ms += int(repaired.latency_ms)
                for name, value in repaired.usage.items():
                    usage[str(name)] = usage.get(str(name), 0) + int(value)
                raw = repaired.value if repaired.value is not None else {
                    "_provider_error": repaired.error, "_raw": repaired.raw,
                }
                self._save(key, generation, now, raw_evidence=raw,
                           rejected=rejected, latency_ms=latency_ms, usage=usage)
                if now >= deadline:
                    self._defer_retry(
                        key, generation, now, "late_platform_repair")
                validation = validate_platform_output(context, raw)
                if not validation.structurally_valid:
                    rejected.extend(validation.rejections)
                    self._defer_retry(
                        key, generation, now,
                        repaired.error or "invalid_platform_schema")
            rejected.extend(validation.rejections)
            validated = {
                "full_analysis": validation.full_analysis,
                "business_conclusions": list(validation.business_conclusions),
                "next_actions": list(validation.next_actions),
            }
            self._save(key, generation, float(self.now_fn()),
                       validated_evidence=validated, rejected=rejected,
                       latency_ms=latency_ms, usage=usage)
            self._advance(key, "designing_actions", generation, float(self.now_fn()))
            status = "designing_actions"
        else:
            persisted = artifact.get("validated_evidence")
            if isinstance(persisted, dict) and persisted:
                validation = validate_platform_output(context, persisted)

        if status == "designing_actions":
            self._save(key, generation, float(self.now_fn()), raw_actions=raw)
            self._advance(key, "validating_actions", generation, float(self.now_fn()))
            status = "validating_actions"
        if status != "validating_actions":
            raise PlatformIntelligenceBlocked("unknown persisted platform stage")
        if not validation.structurally_valid:
            self._defer_retry(
                key, generation, float(self.now_fn()),
                "invalid_persisted_platform_validation")
        if not validation.has_content():
            self._defer_retry(
                key, generation, float(self.now_fn()),
                "no_valid_platform_items")
        if (not validation.full_analysis
                or not validation.business_conclusions
                or not validation.next_actions):
            self._defer_retry(
                key, generation, float(self.now_fn()),
                "incomplete_daily_analysis")
        ready_at = float(self.now_fn())
        if ready_at >= deadline:
            self._defer_retry(
                key, generation, ready_at,
                "deadline_exceeded_before_platform_ready")
        result = PlatformIntelligenceResult(
            job_key=key, status="ready",
            full_analysis=validation.full_analysis,
            business_conclusions=validation.business_conclusions,
            next_actions=validation.next_actions,
            rejected_reasons=list(dict.fromkeys(
                str(item.get("code") or "") for item in rejected
                if isinstance(item, dict) and item.get("code"))),
        )
        self._save(key, generation, ready_at,
                   validated_result=result.to_dict(), rejected=rejected,
                   latency_ms=latency_ms, usage=usage)
        self._advance(key, "ready", generation, ready_at)
        return result
