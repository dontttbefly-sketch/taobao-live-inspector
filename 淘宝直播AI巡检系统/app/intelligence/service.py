"""Two-stage, persisted hourly intelligence orchestration."""
from __future__ import annotations

import time
from typing import Callable

from app.db import Store
from app.llm_client import llm_config

from .context import intelligence_context_input_hash
from .deepseek import DeepSeekIntelligenceProvider, ProviderResult
from .integrity import validated_result_from_artifact
from .models import HourlyIntelligenceResult, IntelligenceContext
from .prompts import PROMPT_VERSION
from .settings import load_intelligence_settings
from .validators import (
    ActionValidationResult,
    EvidenceValidationResult,
    ValidationRejection,
    validate_action_output,
    validate_evidence_output,
)


LEASE_GRACE_SECONDS = 60
_TERMINAL = frozenset(("ready", "blocked"))


class IntelligenceLeaseLost(RuntimeError):
    """The current worker no longer owns the persisted job generation."""


class IntelligenceBlocked(RuntimeError):
    """A persisted intelligence job cannot safely continue."""


class IntelligenceRetryPending(RuntimeError):
    """The frozen hourly analysis is incomplete and has a persisted retry."""

    def __init__(self, job_key: str, retry_at: float, reason: str):
        self.job_key = str(job_key)
        self.retry_at = float(retry_at)
        self.reason = str(reason)
        super().__init__(
            f"intelligence retry scheduled: {self.job_key} at "
            f"{self.retry_at:.3f} ({self.reason})")


def _provider_audit(result: ProviderResult) -> dict:
    if result.value is not None:
        return result.value
    return {
        "_provider_error": str(result.error or "empty_provider_result"),
        "_raw": str(result.raw or ""),
    }


def _merge_usage(current: dict[str, int], addition: dict[str, int]) -> dict[str, int]:
    merged = dict(current)
    for key, value in addition.items():
        merged[str(key)] = merged.get(str(key), 0) + int(value)
    return merged


class IntelligenceService:
    def __init__(
        self,
        cfg: dict,
        store: Store,
        provider: DeepSeekIntelligenceProvider | None = None,
        now_fn: Callable[[], float] = time.time,
    ):
        self.cfg = cfg
        self.store = store
        self.settings = load_intelligence_settings(cfg)
        self.provider = provider or DeepSeekIntelligenceProvider(cfg)
        self.now_fn = now_fn

    @staticmethod
    def _generation(claim: dict) -> int:
        return int(claim["attempts"])

    def _advance(self, job_key: str, status: str, *, generation: int,
                 now: float, error: str = "", fallback_reason: str = "") -> None:
        if not self.store.advance_intelligence_job(
            job_key,
            status,
            claim_generation=generation,
            now=float(now),
            error=error,
            fallback_reason=fallback_reason,
        ):
            raise IntelligenceLeaseLost(
                f"lost intelligence lease before transition to {status}")

    def _save(self, job_key: str, *, generation: int, now: float,
              **artifact: object) -> None:
        if not self.store.save_intelligence_artifact(
            job_key,
            claim_generation=generation,
            now=float(now),
            **artifact,
        ):
            raise IntelligenceLeaseLost("lost intelligence lease before artifact save")

    def _defer_retry(
        self,
        job_key: str,
        *,
        generation: int,
        reason: str,
        now: float,
    ) -> None:
        retry_at = float(now) + self.settings.retry_delay_seconds
        deadline_at = retry_at + self.settings.deadline_seconds
        if not self.store.defer_intelligence_job_retry(
            job_key,
            claim_generation=generation,
            now=float(now),
            retry_at=retry_at,
            deadline_at=deadline_at,
            error=reason,
        ):
            raise IntelligenceLeaseLost(
                "lost intelligence generation before retry scheduling")
        raise IntelligenceRetryPending(job_key, retry_at, reason)

    @staticmethod
    def _provider_result(value: object) -> ProviderResult | None:
        return value if isinstance(value, ProviderResult) else None

    def _call_actions(
        self,
        context: IntelligenceContext,
        evidence: dict,
        deadline_at: float,
        *,
        repair_errors: list[dict] | None = None,
    ) -> ProviderResult:
        try:
            kwargs: dict[str, object] = {"deadline_at": deadline_at}
            if repair_errors is not None:
                kwargs["repair_errors"] = repair_errors
            result = self._provider_result(self.provider.design_actions(
                context,
                evidence,
                **kwargs,
            ))
        except Exception as exc:
            return ProviderResult(None, "", 0, {}, type(exc).__name__)
        return result or ProviderResult(None, "", 0, {}, "invalid_provider_result")

    @staticmethod
    def _is_structure_error(result: ProviderResult) -> bool:
        return result.error in {"invalid_json", "top_level_not_object"}

    @staticmethod
    def _rejection_dicts(items: list[ValidationRejection]) -> list[dict]:
        return [item.to_dict() for item in items]

    @staticmethod
    def _job_identity_matches_context(
            job: dict, context: IntelligenceContext) -> bool:
        return (
            int(job.get("stream_id") or 0) == context.stream_id
            and str(job.get("live_id") or "") == context.live_id
            and int(job.get("anchor_id") or 0) == context.anchor_id
            and int(job.get("window_start_ms") or 0) == context.window_start_ms
            and int(job.get("window_end_ms") or 0) == context.window_end_ms
        )

    @classmethod
    def _job_matches_context(cls, job: dict, context: IntelligenceContext) -> bool:
        return (
            cls._job_identity_matches_context(job, context)
            and str(job.get("input_hash") or "") == context.input_hash
            and intelligence_context_input_hash(context) == context.input_hash
        )

    def _frozen_context_for_retry(
        self,
        job: dict,
        requested_context: IntelligenceContext,
    ) -> IntelligenceContext | None:
        """Return the first immutable snapshot for the same business hour.

        A later fetch may contain more trend points or transcript text. That is
        useful for a future hour, but changing it while retrying would make one
        brief have multiple factual definitions.
        """
        artifact = self.store.get_intelligence_artifact(str(job["job_key"]))
        snapshot_payload = artifact.get("context_snapshot") if artifact else None
        if not snapshot_payload:
            return None
        try:
            frozen = IntelligenceContext.from_dict(snapshot_payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise IntelligenceBlocked(
                f"invalid frozen intelligence context: {job['job_key']}") from exc
        if (not self._job_identity_matches_context(job, requested_context)
                or not self._job_matches_context(job, frozen)):
            raise IntelligenceBlocked(
                f"frozen intelligence binding rejected: {job['job_key']}")
        return frozen

    def _terminal_result(
        self,
        job_key: str,
        status: str,
        context: IntelligenceContext,
    ) -> HourlyIntelligenceResult:
        if status == "blocked":
            raise IntelligenceBlocked(f"intelligence job is blocked: {job_key}")
        try:
            job = self.store.get_intelligence_job(job_key)
            artifact = self.store.get_intelligence_artifact(job_key)
            if (job is None or not self._job_matches_context(job, context)
                    or not artifact or not artifact.get("context_snapshot")
                    or not artifact.get("validated_result")):
                raise ValueError("missing terminal intelligence binding")
            snapshot = IntelligenceContext.from_dict(artifact["context_snapshot"])
            if (not self._job_matches_context(job, snapshot)
                    or snapshot.input_hash != context.input_hash):
                raise ValueError("terminal context snapshot mismatch")
            result = validated_result_from_artifact(
                artifact,
                expected_job_key=job_key,
                expected_status=status,
            )
            return result
        except (KeyError, TypeError, ValueError) as exc:
            raise IntelligenceBlocked(
                f"terminal intelligence binding rejected: {job_key}") from exc

    def analyze_hourly(
        self,
        context: IntelligenceContext,
        *,
        created_at: float | None = None,
    ) -> HourlyIntelligenceResult:
        """Run the single current hourly analysis path."""
        return self._analyze_hourly(context, created_at=created_at)

    def _analyze_hourly(
        self,
        context: IntelligenceContext,
        *,
        created_at: float | None = None,
    ) -> HourlyIntelligenceResult:
        """Resume persisted stages under one bounded business deadline."""
        started_at = float(self.now_fn())
        requested_created_at = started_at if created_at is None else float(created_at)
        requested_deadline = (
            requested_created_at + self.settings.deadline_seconds)
        llm = llm_config(self.cfg)
        job_key = self.store.ensure_intelligence_job(
            task_type="hourly",
            stream_id=context.stream_id,
            live_id=context.live_id,
            anchor_id=context.anchor_id,
            window_start_ms=context.window_start_ms,
            window_end_ms=context.window_end_ms,
            input_hash=context.input_hash,
            deadline_at=requested_deadline,
            model=str(llm.get("model") or ""),
            prompt_version=PROMPT_VERSION,
        )
        job = self.store.get_intelligence_job(job_key)
        if job is None:
            raise ValueError("intelligence job disappeared after ensure")
        status = str(job["status"])
        if status in _TERMINAL and not self._job_matches_context(job, context):
            frozen = self._frozen_context_for_retry(job, context)
            if frozen is not None:
                context = frozen
        if status in _TERMINAL:
            return self._terminal_result(
                job_key,
                status,
                context,
            )
        if not self._job_matches_context(job, context):
            frozen = self._frozen_context_for_retry(job, context)
            if frozen is not None:
                context = frozen
        if not self._job_matches_context(job, context):
            raise IntelligenceBlocked(
                f"intelligence job binding rejected: {job_key}")

        next_attempt_at = float(job.get("next_attempt_at") or 0)
        if next_attempt_at > started_at:
            raise IntelligenceRetryPending(
                job_key,
                next_attempt_at,
                str(job.get("error") or "retry_pending"),
            )

        claim = self.store.claim_intelligence_job(
            job_key,
            now=started_at,
            lease_sec=self.settings.deadline_seconds + LEASE_GRACE_SECONDS,
        )
        if claim is None:
            current = self.store.get_intelligence_job(job_key) or {}
            retry_at = float(current.get("next_attempt_at") or 0)
            if retry_at > started_at:
                raise IntelligenceRetryPending(
                    job_key,
                    retry_at,
                    str(current.get("error") or "retry_pending"),
                )
            raise IntelligenceLeaseLost(
                f"intelligence job already claimed: {job_key}")
        generation = self._generation(claim)
        status = str(claim["status"])
        deadline_at = float(claim.get("deadline_at") or requested_deadline)
        try:
            artifact = self.store.get_intelligence_artifact(job_key) or {}
        except ValueError:
            self._advance(
                job_key, "blocked", generation=generation, now=started_at,
                error="invalid_persisted_json",
            )
            raise

        rejected: list[dict] = list(artifact.get("rejected") or [])
        repair_used = any(
            "structure_repair_attempted" in str(item.get("detail") or "")
            for item in rejected
            if isinstance(item, dict)
        )
        usage: dict[str, int] = {
            str(key): int(value)
            for key, value in (artifact.get("usage") or {}).items()
        }
        latency_ms = int(artifact.get("latency_ms") or 0)
        stage_now = started_at

        # Freeze immutable source facts before any provider call.
        context_payload = context.to_dict()
        persisted_context = artifact.get("context_snapshot")
        if persisted_context:
            try:
                frozen_context = IntelligenceContext.from_dict(persisted_context)
            except (TypeError, ValueError):
                self._advance(
                    job_key, "blocked", generation=generation, now=started_at,
                    error="invalid_context_snapshot",
                )
                raise
            if (not frozen_context.input_hash
                    or intelligence_context_input_hash(frozen_context)
                    != frozen_context.input_hash
                    or frozen_context.input_hash != context.input_hash):
                self._advance(
                    job_key, "blocked", generation=generation, now=started_at,
                    error="context_snapshot_mismatch",
                )
                raise ValueError("persisted context snapshot does not match job input")
            context = frozen_context
        else:
            self._save(
                job_key,
                generation=generation,
                now=started_at,
                context_snapshot=context_payload,
            )
            artifact["context_snapshot"] = context_payload

        if not self.settings.enabled:
            self._advance(
                job_key, "blocked", generation=generation, now=started_at,
                error="intelligence disabled",
            )
            raise IntelligenceBlocked("intelligence disabled")

        if started_at >= deadline_at:
            self._defer_retry(
                job_key, generation=generation,
                reason="deadline_exceeded_before_start", now=started_at)

        if not str(llm.get("api_key") or "").strip():
            self._defer_retry(
                job_key, generation=generation,
                reason="missing_api_key", now=started_at)

        raw_actions = artifact.get("raw_actions")
        action_validation: ActionValidationResult | None = None
        evidence_validation = EvidenceValidationResult()

        if status == "queued":
            # 完整分析是第一步；任何证据提取/格式清理都不得挡在它前面。
            self._advance(
                job_key, "designing_actions", generation=generation, now=stage_now)
            status = "designing_actions"

        if status == "designing_actions":
            if not isinstance(raw_actions, dict) or not raw_actions:
                provider_result = self._call_actions(
                    context, {}, deadline_at)
                returned_at = float(self.now_fn())
                stage_now = returned_at
                latency_ms += int(provider_result.latency_ms)
                usage = _merge_usage(usage, provider_result.usage)
                if (provider_result.value is None
                        and not self._is_structure_error(provider_result)):
                    self._defer_retry(
                        job_key, generation=generation,
                        reason=provider_result.error or "empty_action_result",
                        now=returned_at)
                raw_actions = _provider_audit(provider_result)
                self._save(
                    job_key,
                    generation=generation,
                    now=returned_at,
                    raw_actions=raw_actions,
                    latency_ms=latency_ms,
                    usage=usage,
                )
            self._advance(
                job_key, "validating_actions", generation=generation, now=stage_now)
            status = "validating_actions"

        if status != "validating_actions":
            raise ValueError(f"unsupported intelligence resume status: {status}")
        if not isinstance(raw_actions, dict) or not raw_actions:
            self._advance(
                job_key, "blocked", generation=generation, now=stage_now,
                error="missing_raw_actions",
            )
            raise ValueError("validating actions without persisted raw actions")

        action_validation: ActionValidationResult = validate_action_output(
            context,
            raw_actions,
            job_key=job_key,
            validated_evidence=None,
        )
        if not action_validation.structurally_valid:
            validation_errors = self._rejection_dicts(
                action_validation.rejections)
            if (repair_used
                    or self.settings.structure_repair_attempts == 0):
                rejected.extend(validation_errors)
                self._defer_retry(
                    job_key, generation=generation,
                    reason="invalid_action_schema", now=stage_now)
            repair_used = True
            repair_claim = [dict(item) for item in validation_errors]
            for item in repair_claim:
                detail = str(item.get("detail") or "")
                item["detail"] = (
                    "structure_repair_attempted"
                    + (f": {detail}" if detail else "")
                )
            rejected.extend(repair_claim)
            self._save(
                job_key,
                generation=generation,
                now=stage_now,
                rejected=rejected,
                latency_ms=latency_ms,
                usage=usage,
            )
            repair = self._call_actions(
                context,
                {},
                deadline_at,
                repair_errors=validation_errors,
            )
            returned_at = float(self.now_fn())
            stage_now = returned_at
            latency_ms += int(repair.latency_ms)
            usage = _merge_usage(usage, repair.usage)
            raw_actions = _provider_audit(repair)
            self._save(
                job_key,
                generation=generation,
                now=returned_at,
                raw_actions=raw_actions,
                rejected=rejected,
                latency_ms=latency_ms,
                usage=usage,
            )
            action_validation = (
                validate_action_output(
                    context,
                    repair.value,
                    job_key=job_key,
                    validated_evidence=None,
                )
                if repair.value is not None
                else ActionValidationResult(structurally_valid=False)
            )
            if not action_validation.structurally_valid:
                self._defer_retry(
                    job_key, generation=generation,
                    reason=repair.error or "invalid_action_schema",
                    now=returned_at)
        rejected.extend(self._rejection_dicts(action_validation.rejections))
        if not action_validation.full_analysis:
            self._defer_retry(
                job_key, generation=generation,
                reason="missing_full_analysis", now=stage_now)
        if not action_validation.business_conclusions:
            self._defer_retry(
                job_key, generation=generation,
                reason="missing_business_conclusions", now=stage_now)
        if not action_validation.action_experiments:
            self._defer_retry(
                job_key, generation=generation,
                reason="no_valid_actions", now=stage_now)

        # 原话/证据与完整分析来自同一次全量请求。证据字段只做后置核验：
        # 缺失或格式错误时隐藏对应展示项，不得推翻完整分析。
        evidence_candidate = validate_evidence_output(context, raw_actions)
        if evidence_candidate.structurally_valid:
            evidence_validation = evidence_candidate
            rejected.extend(self._rejection_dicts(evidence_candidate.rejections))
        else:
            rejected.extend(self._rejection_dicts(evidence_candidate.rejections))
            evidence_validation = EvidenceValidationResult()

        ready_at = float(self.now_fn())
        stage_now = ready_at

        result = HourlyIntelligenceResult(
            job_key=job_key,
            status="ready",
            full_analysis=action_validation.full_analysis,
            business_conclusions=action_validation.business_conclusions,
            observations=evidence_validation.observations,
            reusable_talktracks=evidence_validation.talktracks,
            action_experiments=action_validation.action_experiments,
            rejected_reasons=[str(item.get("code") or "") for item in rejected
                              if item.get("code")],
        )
        self._save(
            job_key,
            generation=generation,
            now=stage_now,
            raw_evidence=raw_actions,
            validated_evidence=evidence_validation.to_dict(),
            validated_result=result.to_dict(),
            rejected=rejected,
            latency_ms=latency_ms,
            usage=usage,
        )
        self._advance(
            job_key, "ready", generation=generation, now=stage_now)
        return result

__all__ = [
    "IntelligenceBlocked",
    "IntelligenceLeaseLost",
    "IntelligenceRetryPending",
    "IntelligenceService",
]
