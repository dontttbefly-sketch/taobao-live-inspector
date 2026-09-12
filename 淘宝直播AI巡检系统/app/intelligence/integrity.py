"""Canonical integrity binding for frozen, validated intelligence results."""
from __future__ import annotations

import hashlib
import hmac
from .models import HourlyIntelligenceResult, canonical_json


def validated_result_hash(value: object) -> str:
    """Return the stable digest stored beside one validated result payload."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def validated_result_integrity_matches(value: object, stored_hash: object) -> bool:
    digest = str(stored_hash or "")
    return len(digest) == 64 and hmac.compare_digest(
        validated_result_hash(value), digest)


def validated_result_from_artifact(
    artifact: dict,
    *,
    expected_job_key: str,
    expected_status: str,
) -> HourlyIntelligenceResult:
    """Parse only an exact payload/digest/identity binding; legacy rows fail closed."""
    if not isinstance(artifact, dict):
        raise ValueError("missing validated intelligence artifact")
    payload = artifact.get("validated_result")
    stored_hash = str(artifact.get("validated_result_hash") or "")
    if not isinstance(payload, dict) or not payload or len(stored_hash) != 64:
        raise ValueError("missing validated intelligence result integrity")
    if not validated_result_integrity_matches(payload, stored_hash):
        raise ValueError("validated intelligence result integrity mismatch")
    result = HourlyIntelligenceResult.from_dict(payload)
    if result.job_key != str(expected_job_key) or result.status != str(expected_status):
        raise ValueError("validated intelligence result identity mismatch")
    return result


__all__ = [
    "validated_result_from_artifact",
    "validated_result_hash",
    "validated_result_integrity_matches",
]
