"""Compatibility facade for liveId-level intelligence.

Implementation responsibilities live in the focused ``platform_*`` modules;
existing imports remain stable through this facade.
"""
from .platform_context import build_platform_intelligence_context
from .platform_models import (
    PlatformIntelligenceContext,
    PlatformIntelligenceResult,
    PlatformSource,
    bound_platform_intelligence_from_summary,
    build_platform_intelligence_binding,
    platform_context_input_hash,
    sanitize_platform_intelligence_payload,
)
from .platform_service import (
    PlatformIntelligenceBlocked,
    PlatformIntelligenceService,
    load_frozen_platform_intelligence,
)
from .platform_validators import PlatformValidationResult, validate_platform_output

__all__ = [
    "PlatformIntelligenceBlocked",
    "PlatformIntelligenceContext",
    "PlatformIntelligenceResult",
    "PlatformIntelligenceService",
    "PlatformSource",
    "PlatformValidationResult",
    "bound_platform_intelligence_from_summary",
    "build_platform_intelligence_binding",
    "build_platform_intelligence_context",
    "load_frozen_platform_intelligence",
    "platform_context_input_hash",
    "sanitize_platform_intelligence_payload",
    "validate_platform_output",
]
