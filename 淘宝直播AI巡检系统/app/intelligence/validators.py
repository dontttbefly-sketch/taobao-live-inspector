"""Fail-closed validators for untrusted intelligence model output.

Validation is deliberately item-scoped: one bad observation, talktrack, or
action is rejected without discarding siblings that can still be audited.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Literal

from app.asr.clean import numbers_equivalent
from app.highlight.peak import WINDOW_BEFORE_MS
from app.highlight.quality import quality_candidate

from .facts import (
    SemanticFacts,
    CANONICAL_METRICS,
    canonical_metric_name,
    claim_semantically_supported,
    hourly_source_facts,
    validated_phenomenon_identity,
)
from .models import (
    ActionExperiment,
    EvidenceRef,
    IntelligenceContext,
    Observation,
    ReusableTalktrack,
)


RejectionCode = Literal[
    "unknown_source",
    "source_outside_window",
    "text_mismatch",
    "number_mismatch",
    "incomplete_sentence",
    "invalid_peak_relation",
    "causal_overclaim",
    "forbidden_claim",
    "duplicate_weak_item",
    "invalid_action_schema",
    "metric_series_missing",
    "invalid_experiment_contract",
]

REJECTION_CODES = frozenset((
    "unknown_source",
    "source_outside_window",
    "text_mismatch",
    "number_mismatch",
    "incomplete_sentence",
    "invalid_peak_relation",
    "causal_overclaim",
    "forbidden_claim",
    "duplicate_weak_item",
    "invalid_action_schema",
    "metric_series_missing",
    "invalid_experiment_contract",
))


_BUSINESS_RESULT_PATTERN = (
    r"(?:成交|转化|点击|销量|购买|订单|加购|"
    r"观看(?!角度|方式|模式|体验)|在线|热度|峰值)"
)
_BUSINESS_CHANGE_PATTERN = r"(?:上涨|提升|提高|增加|改善|增长)"
_CAUSAL_LINK_PATTERN = (
    r"(?:导致|造成|引发|带来|助推|促(?:进|成)|[推拉带驱]动)"
)
_CAUSAL_RE = re.compile(
    rf"{_CAUSAL_LINK_PATTERN}.{{0,16}}{_BUSINESS_RESULT_PATTERN}|"
    rf"因此.{{0,16}}{_BUSINESS_RESULT_PATTERN}|"
    rf"(?:提升|提高|增加|改善).{{0,8}}{_BUSINESS_RESULT_PATTERN}|"
    rf"使(?:得)?.{{0,8}}{_BUSINESS_RESULT_PATTERN}.{{0,8}}"
    rf"{_BUSINESS_CHANGE_PATTERN}|"
    rf"(?:让|令).{{0,16}}{_BUSINESS_RESULT_PATTERN}.{{0,8}}"
    rf"{_BUSINESS_CHANGE_PATTERN}|"
    rf"{_BUSINESS_RESULT_PATTERN}.{{0,12}}"
    r"(?:归(?:因|功)于|源于|得益于)|"
    rf"{_BUSINESS_RESULT_PATTERN}.{{0,8}}"
    rf"{_BUSINESS_CHANGE_PATTERN}.{{0,8}}(?:是)?(?:因为|由于)|"
    rf"(?:因为|由于).{{0,24}}{_BUSINESS_RESULT_PATTERN}.{{0,8}}"
    rf"{_BUSINESS_CHANGE_PATTERN}"
)
_ALWAYS_FORBIDDEN_RE = re.compile(
    r"用户心理|消费心理|购买意愿|购买欲|"
    r"(?:用户|观众|消费者).{0,12}"
    r"(?:心理|信任|意愿|兴趣|认知|感受|需求|顾虑|犹豫|担忧|焦虑)|"
    r"(?:用户|观众|消费者).{0,12}(?:觉得|认为|感觉).{0,12}"
    r"(?:划算|便宜|值得|想买|想下单|心动|风险.{0,3}(?:低|小))|"
    r"(?:用户|观众|消费者).{0,12}"
    r"(?:更)?(?:想买|想下单|愿意购买|愿意下单|放心)"
)
_SOURCED_CLAIM_RE = re.compile(
    r"库存|只剩|限量|限时|名额|订单|已有.{0,8}(?:人|位).{0,8}(?:下单|购买)|"
    r"购买人数|优惠|优惠券|补贴|满减|赠品|免单|折扣|到手价"
)
# 虚构类红线词：库存/限量/名额/订单/购买人数——任何情况下都要求片段内有出处。
_HARD_SOURCED_CLAIM_RE = re.compile(
    r"库存|只剩|限量|限时|名额|订单|已有.{0,8}(?:人|位).{0,8}(?:下单|购买)|购买人数"
)
_ACTION_FIELDS = frozenset((
    "issue", "action", "script", "trigger", "duration", "metric_name",
    "comparison", "evidence",
))
_REQUIRED_ANALYST_ACTION_FIELDS = frozenset(("issue", "action"))
_ACTION_TEXT_FIELDS = _ACTION_FIELDS - {"evidence", "metric_name"}
_CONCRETE_UNSOURCED_SALES_RE = re.compile(
    r"(?:库存(?:只剩|仅剩|还剩|紧张|告急|有限|不多)|只剩|仅剩).{0,10}"
    r"(?:\d|[零〇一二两三四五六七八九十百千万])|"
    r"(?:到手价|优惠券|补贴|满减|折扣).{0,10}"
    r"(?:\d|[零〇一二两三四五六七八九十百千万])|"
    r"买.{0,3}(?:\d|[一二三四五六七八九十]).{0,3}送.{0,3}"
    r"(?:\d|[一二三四五六七八九十])|"
    r"(?:已有|已经).{0,8}(?:\d|[零〇一二两三四五六七八九十百千万]).{0,8}"
    r"(?:人|位|单).{0,8}(?:下单|购买|成交)"
)
_NUMBER_TOKEN_RE = re.compile(
    r"\d+(?:[.,]\d+)?(?:\s+\d+)*(?:亿|万|千|百|十)*|"
    r"[零〇一二两三四五六七八九十百千万亿\d]+"
)
_PEAK_VALUE_CLAIM_RE = re.compile(
    rf"(?:成交|点击|在线)?峰值(?:为|是|达到|达|约为|约)?"
    rf"(?P<value>{_NUMBER_TOKEN_RE.pattern})(?P<unit>元|次|人)"
)
_THREE_MINUTE_RE = re.compile(r"(?:峰值)?前(?:三|3)分钟")
_SAME_MINUTE_RE = re.compile(r"同(?:一|1)分钟")
_PEAK_OFFSET_CLAIM_RE = re.compile(
    rf"(?:峰值)?前(?P<value>{_NUMBER_TOKEN_RE.pattern})(?P<unit>秒|分钟)"
)
_PEAK_WINDOW_CLAIM_RES = (
    re.compile(
        rf"峰值(?P<value>{_NUMBER_TOKEN_RE.pattern})"
        rf"(?P<unit>秒|分钟|小时)窗口"
    ),
    re.compile(
        rf"峰值窗口(?:为|是|共|长|长度为)?"
        rf"(?P<value>{_NUMBER_TOKEN_RE.pattern})(?P<unit>秒|分钟|小时)"
    ),
)
_NON_CAUSAL_TEMPORAL_RE = re.compile(
    r"(?:仅|只)(?:有|是)?时间(?:相关|关联).{0,12}"
    r"(?:不代表|不说明|不能说明|无法证明)因果"
)
_TEMPORAL_PEAK_WORDING_RE = re.compile(
    r"(?:出现|发生|先于|位于|处于).{0,24}峰值|"
    r"峰值.{0,24}(?:出现|发生|关联)"
)
_VERIFIED_TEMPORAL_NUMBER_RE = re.compile(
    rf"{_PEAK_OFFSET_CLAIM_RE.pattern}|{_SAME_MINUTE_RE.pattern}"
)
_NON_FACT_NUMERIC_KEYS = frozenset((
    "id", "source_id", "window_id", "peak_id", "experiment_id",
    "version", "ts", "time", "timestamp", "minute", "minute_ms",
    "start", "end", "start_ms", "end_ms", "window",
))
_NON_FACT_ORDINAL_RE = re.compile(
    r"(?:下一(?:场|轮|小时)|上一小时|前一小时|"
    r"同一(?:小时|分钟|窗口|口径|完整|话术|表达|问题|现象))"
)


@dataclass(frozen=True)
class ValidationRejection:
    code: RejectionCode
    item_type: str
    index: int
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "item_type": self.item_type,
            "index": self.index,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class EvidenceValidationResult:
    observations: list[Observation] = field(default_factory=list)
    talktracks: list[ReusableTalktrack] = field(default_factory=list)
    rejections: list[ValidationRejection] = field(default_factory=list)
    structurally_valid: bool = True

    @property
    def reusable_talktracks(self) -> list[ReusableTalktrack]:
        return self.talktracks

    def to_dict(self) -> dict[str, Any]:
        return {
            "observations": [item.to_dict() for item in self.observations],
            "talktracks": [item.to_dict() for item in self.talktracks],
        }


@dataclass(frozen=True)
class ActionValidationResult:
    action_experiments: list[ActionExperiment] = field(default_factory=list)
    rejections: list[ValidationRejection] = field(default_factory=list)
    structurally_valid: bool = True
    full_analysis: str = ""
    business_conclusions: list[str] = field(default_factory=list)

    @property
    def actions(self) -> list[ActionExperiment]:
        return self.action_experiments

    def to_dict(self) -> dict[str, Any]:
        return {
            "full_analysis": self.full_analysis,
            "business_conclusions": self.business_conclusions,
            "actions": [item.to_dict() for item in self.action_experiments],
        }


def _compact_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = re.sub(r"^\s*\[\d{1,3}:\d{2}(?::\d{2})?\]\s*", "", text)
    return re.sub(r"\s+", "", text)


def _numbers_equivalent_text(source: object, target: object) -> bool:
    return numbers_equivalent(
        unicodedata.normalize("NFKC", str(source or "")),
        unicodedata.normalize("NFKC", str(target or "")),
    )


def _source_maps(context: IntelligenceContext) -> dict[str, dict[str, object]]:
    transcripts = {item.segment_id: item for item in context.transcripts}
    peaks = {
        str(item.get("peak_id") or item.get("source_id") or ""): item
        for item in context.peak_context
        if isinstance(item, dict) and (item.get("peak_id") or item.get("source_id"))
    }
    metric_windows: dict[str, object] = {}
    raw_windows = context.metrics.get("metric_windows")
    if isinstance(raw_windows, list):
        for item in raw_windows:
            if not isinstance(item, dict):
                continue
            source_id = str(
                item.get("source_id") or item.get("window_id") or item.get("id") or "")
            if source_id:
                metric_windows[source_id] = item
    series = context.metrics.get("series")
    if isinstance(series, dict):
        for name, value in series.items():
            metric_windows.setdefault(str(name), value)
            metric_windows.setdefault(f"M:{name}", value)
    return {
        "transcript": transcripts,
        "peak": peaks,
        "metric_window": metric_windows,
    }


def _relative_window(context: IntelligenceContext) -> tuple[int, int]:
    duration = max(0, context.window_end_ms - context.window_start_ms)
    return 0, duration


def _source_time_range(source_type: str, source: object) -> tuple[int, int] | None:
    if source_type == "transcript":
        return int(getattr(source, "start_ms", 0)), int(getattr(source, "end_ms", 0))
    if not isinstance(source, dict):
        return None
    payload = source.get("peak_meta") if source_type == "peak" else source
    payload = payload if isinstance(payload, dict) else source
    window = payload.get("window")
    if isinstance(window, dict):
        try:
            return int(window.get("start_ms")), int(window.get("end_ms"))
        except (TypeError, ValueError):
            return None
    try:
        if payload.get("start_ms") is not None and payload.get("end_ms") is not None:
            return int(payload["start_ms"]), int(payload["end_ms"])
    except (TypeError, ValueError):
        return None
    return None


def _inside_context_window(context: IntelligenceContext, source_type: str,
                           source: object) -> bool:
    source_range = _source_time_range(source_type, source)
    if source_range is None:
        return True
    start_ms, end_ms = source_range
    relative_start, relative_end = _relative_window(context)
    if relative_start <= start_ms <= end_ms <= relative_end:
        return True
    return (
        context.window_start_ms <= start_ms <= end_ms <= context.window_end_ms
    )


def _validate_refs(
    context: IntelligenceContext,
    raw: object,
) -> tuple[list[EvidenceRef], dict[str, list[object]], RejectionCode | None]:
    if not isinstance(raw, list) or not raw:
        return [], {}, "unknown_source"
    maps = _source_maps(context)
    refs: list[EvidenceRef] = []
    resolved: dict[str, list[object]] = {}
    seen_refs: set[tuple[str, str]] = set()
    for item in raw:
        try:
            ref = EvidenceRef.from_dict(item)
        except (TypeError, ValueError):
            return [], {}, "unknown_source"
        source = maps.get(ref.source_type, {}).get(ref.source_id)
        if not ref.source_id or source is None:
            return [], {}, "unknown_source"
        ref_key = (ref.source_type, ref.source_id)
        if ref_key in seen_refs:
            return [], {}, "duplicate_weak_item"
        if not _inside_context_window(context, ref.source_type, source):
            return [], {}, "source_outside_window"
        seen_refs.add(ref_key)
        refs.append(ref)
        resolved.setdefault(ref.source_type, []).append(source)
    return refs, resolved, None


def _transcript_texts(resolved: dict[str, list[object]]) -> list[str]:
    return [str(getattr(item, "text", "") or "")
            for item in resolved.get("transcript", [])]


def _resolved_semantic_facts(
        refs: list[EvidenceRef],
        resolved: dict[str, list[object]],
) -> SemanticFacts:
    offsets: dict[str, int] = {}
    facts = SemanticFacts()
    for ref in refs:
        offset = offsets.get(ref.source_type, 0)
        sources = resolved.get(ref.source_type, [])
        if offset >= len(sources):
            continue
        facts = facts.merged(hourly_source_facts(
            ref.source_type, ref.source_id, sources[offset]))
        offsets[ref.source_type] = offset + 1
    return facts


def _claim_has_source(claim: str, source_texts: list[str]) -> bool:
    compact = _compact_text(claim)
    for source in source_texts:
        source_compact = _compact_text(source)
        if compact and (compact in source_compact or source_compact in compact):
            return _numbers_equivalent_text(source, claim)
    return False


def _has_literal_support(claim: str, source_texts: list[str]) -> bool:
    compact_claim = _compact_text(claim)
    if not compact_claim:
        return False
    for source in source_texts:
        compact_source = _compact_text(source)
        if not compact_source:
            continue
        if compact_claim in compact_source or compact_source in compact_claim:
            return True
        if SequenceMatcher(
                None, compact_claim, compact_source).find_longest_match().size >= 4:
            return True
    return False


def _talktrack_rewrite_is_grounded(original: str, script: str) -> bool:
    """Allow model phrasing while rejecting a rewrite unrelated to its quote."""
    if _has_literal_support(script, [original]):
        return True
    original_key = _compact_text(original)
    script_key = _compact_text(script)
    return bool(original_key and script_key) and SequenceMatcher(
        None, original_key, script_key).ratio() >= 0.30


def _numbers_have_sources(claim: str, source_texts: list[str]) -> bool:
    normalized_claim = _NON_FACT_ORDINAL_RE.sub(
        "", unicodedata.normalize("NFKC", claim))
    normalized_sources = [
        unicodedata.normalize("NFKC", source) for source in source_texts
    ]
    target_tokens = _NUMBER_TOKEN_RE.findall(normalized_claim)
    if not target_tokens:
        return True
    source_tokens = [
        token
        for source in normalized_sources
        for token in _NUMBER_TOKEN_RE.findall(source)
    ]
    if all(
        any(_numbers_equivalent_text(source, target) for source in source_tokens)
        for target in target_tokens
    ):
        return True
    # Preserve the established ASR split/join recovery (for example
    # ``7 172`` -> ``71、72``) without weakening ordinary per-value checks.
    joined_targets = " ".join(target_tokens)
    return any(
        re.search(r"(?<=\d)\s+(?=\d)", source)
        and _numbers_equivalent_text(source, joined_targets)
        for source in source_tokens
    )


def _numeric_value_texts(value: object, *, key: str = "") -> list[str]:
    """Collect auditable values while excluding clocks and opaque identifiers."""
    if key.casefold() in _NON_FACT_NUMERIC_KEYS or key.casefold().endswith("_at"):
        return []
    if isinstance(value, dict):
        return [
            text
            for item_key, item_value in value.items()
            for text in _numeric_value_texts(item_value, key=str(item_key))
        ]
    if isinstance(value, (list, tuple)):
        return [
            text
            for item in value
            for text in _numeric_value_texts(item, key=key)
        ]
    if isinstance(value, bool) or value is None:
        return []
    text = unicodedata.normalize("NFKC", str(value))
    return [text] if _NUMBER_TOKEN_RE.search(text) else []


def _observation_number_sources(
    resolved: dict[str, list[object]],
) -> list[str]:
    sources = _transcript_texts(resolved)
    for peak in resolved.get("peak", []):
        if not isinstance(peak, dict):
            continue
        meta = peak.get("peak_meta")
        meta = meta if isinstance(meta, dict) else peak
        sources.extend(_numeric_value_texts(meta.get("value"), key="value"))
    for source_type in ("metric_window",):
        for source in resolved.get(source_type, []):
            if hasattr(source, "to_dict"):
                source = source.to_dict()
            sources.extend(_numeric_value_texts(source))
    return sources


def _peak_value_claim_has_source(
    statement: str,
    resolved: dict[str, list[object]],
) -> bool:
    claims = list(_PEAK_VALUE_CLAIM_RE.finditer(_compact_text(statement)))
    if not claims:
        return True
    peak_values: list[tuple[object, str]] = []
    for peak in resolved.get("peak", []):
        if not isinstance(peak, dict):
            continue
        meta = peak.get("peak_meta")
        meta = meta if isinstance(meta, dict) else peak
        if meta.get("value") is not None and str(meta.get("unit") or ""):
            peak_values.append((meta["value"], str(meta["unit"])))
    return all(
        any(
            unit == match.group("unit")
            and _numbers_equivalent_text(value, match.group("value"))
            for value, unit in peak_values
        )
        for match in claims
    )


def _observation_number_claim(statement: str, relation: str) -> str:
    if relation != "temporal_association":
        return statement
    # These numbers describe relation forms independently verified by
    # _peak_relation_code; all remaining numbers must still match evidence.
    remaining = _VERIFIED_TEMPORAL_NUMBER_RE.sub("", _compact_text(statement))
    for pattern in _PEAK_WINDOW_CLAIM_RES:
        remaining = pattern.sub("", remaining)
    return remaining


_SOURCED_CLAIM_WORDS = (
    "库存", "只剩", "限量", "限时", "名额", "订单", "购买人数",
    "优惠", "优惠券", "补贴", "满减", "赠品", "免单", "折扣", "到手价",
)


def _claim_has_relaxed_source(claim: str, source_texts: list[str]) -> bool:
    """宽松来源判定：claim 中出现的每个业务声明词，必须在引用片段中出现过。"""
    compact_claim = _compact_text(claim)
    all_sources = "".join(_compact_text(source) for source in source_texts)
    for word in _SOURCED_CLAIM_WORDS:
        if word in compact_claim and word not in all_sources:
            return False
    return True


def _forbidden_claim(
        claim: str, source_texts: list[str], *,
        relaxed_source: bool = False,
        ignore_sourced_claim: bool = False,
        ignore_always_forbidden: bool = False) -> bool:
    normalized_claim = _compact_text(claim)
    if not ignore_always_forbidden and _ALWAYS_FORBIDDEN_RE.search(normalized_claim):
        return True
    if ignore_sourced_claim:
        # 只放行营销词（优惠/赠品/补贴等）；虚构类红线词仍必须有出处。
        if _HARD_SOURCED_CLAIM_RE.search(normalized_claim):
            return not _claim_has_source(normalized_claim, source_texts)
        return False
    if not _SOURCED_CLAIM_RE.search(normalized_claim):
        return False
    if relaxed_source:
        return not _claim_has_relaxed_source(normalized_claim, source_texts)
    return not _claim_has_source(normalized_claim, source_texts)


def _unsupported_concrete_sales_claim(
        claim: str, source_texts: list[str]) -> bool:
    """Reject invented concrete sales facts without policing analyst wording.

    General recommendations such as "核对官方优惠后再讲清规则" remain free.
    Concrete stock/price/order assertions need a cited transcript containing
    the same business terms and numerically equivalent values.
    """
    normalized = _compact_text(claim)
    if not _CONCRETE_UNSOURCED_SALES_RE.search(normalized):
        return False
    return not (
        source_texts
        and _claim_has_relaxed_source(normalized, source_texts)
        and _numbers_have_sources(normalized, source_texts)
    )


def _temporal_statement_allowed(statement: str) -> bool:
    normalized = _compact_text(statement)
    return bool(
        _NON_CAUSAL_TEMPORAL_RE.search(normalized)
        or _TEMPORAL_PEAK_WORDING_RE.search(normalized)
    )


def _peak_relation_code(
    statement: str,
    relation: str,
    refs: list[EvidenceRef],
    resolved: dict[str, list[object]],
) -> RejectionCode | None:
    normalized_statement = _compact_text(statement)
    same_minute_claim = bool(_SAME_MINUTE_RE.search(normalized_statement))
    three_minute_claim = bool(_THREE_MINUTE_RE.search(normalized_statement))
    offset_claims = list(_PEAK_OFFSET_CLAIM_RE.finditer(normalized_statement))
    window_claims = [
        match
        for pattern in _PEAK_WINDOW_CLAIM_RES
        for match in pattern.finditer(normalized_statement)
    ]
    peak_refs = [ref for ref in refs if ref.source_type == "peak"]
    transcript_refs = [ref for ref in refs if ref.source_type == "transcript"]
    if not peak_refs:
        return "invalid_peak_relation" if relation == "temporal_association" else None
    if relation != "temporal_association" or not transcript_refs:
        return "invalid_peak_relation"
    if not _temporal_statement_allowed(normalized_statement):
        return "invalid_peak_relation"
    transcripts = resolved.get("transcript", [])
    for peak in resolved.get("peak", []):
        if not isinstance(peak, dict):
            return "invalid_peak_relation"
        meta = peak.get("peak_meta") if isinstance(peak.get("peak_meta"), dict) else peak
        quality = meta.get("quality") if isinstance(meta, dict) else None
        if not isinstance(quality, dict) or quality.get("association_allowed") is not True:
            return "invalid_peak_relation"
        window = meta.get("window") if isinstance(meta, dict) else None
        if not isinstance(window, dict):
            return "invalid_peak_relation"
        try:
            start_ms, end_ms = int(window["start_ms"]), int(window["end_ms"])
        except (KeyError, TypeError, ValueError):
            return "invalid_peak_relation"
        window_sec = (end_ms - start_ms) / 1000
        for claim in window_claims:
            actual = {
                "秒": window_sec,
                "分钟": window_sec / 60,
                "小时": window_sec / 3600,
            }[claim.group("unit")]
            if not _numbers_equivalent_text(actual, claim.group("value")):
                return "invalid_peak_relation"
        nearby = peak.get("nearby_segment_ids")
        if isinstance(nearby, list) and any(
                ref.source_id not in {str(item) for item in nearby}
                for ref in transcript_refs):
            return "source_outside_window"
        if any(
            int(getattr(item, "end_ms", 0)) < start_ms
            or int(getattr(item, "start_ms", 0)) > end_ms
            for item in transcripts
        ):
            return "source_outside_window"
        excerpts = meta.get("excerpts")
        if not isinstance(excerpts, list) or not excerpts:
            return "invalid_peak_relation"
        for transcript in transcripts:
            transcript_text = _compact_text(getattr(transcript, "text", ""))
            matching = [
                item for item in excerpts
                if isinstance(item, dict)
                and transcript_text
                and (
                    transcript_text in _compact_text(item.get("text"))
                    or _compact_text(item.get("text")) in transcript_text
                )
            ]
            if not matching:
                return "invalid_peak_relation"
            for excerpt in matching:
                excerpt_relation = str(excerpt.get("relation") or "")
                try:
                    lag_sec = float(excerpt.get("lag_sec"))
                except (TypeError, ValueError):
                    return "invalid_peak_relation"
                if excerpt_relation == "同分钟":
                    explicitly_uncertain = (
                        same_minute_claim
                        and "先后无法确定" in normalized_statement
                    )
                    if lag_sec != 0 or not explicitly_uncertain or offset_claims:
                        return "invalid_peak_relation"
                elif excerpt_relation == "之后":
                    if lag_sec <= 0 or same_minute_claim:
                        return "invalid_peak_relation"
                    if (three_minute_claim
                            and lag_sec > WINDOW_BEFORE_MS / 1000):
                        return "invalid_peak_relation"
                    for offset in offset_claims:
                        if _THREE_MINUTE_RE.fullmatch(offset.group(0)):
                            continue
                        actual = (
                            lag_sec if offset.group("unit") == "秒"
                            else lag_sec / 60
                        )
                        if not _numbers_equivalent_text(
                                actual, offset.group("value")):
                            return "invalid_peak_relation"
                else:
                    return "invalid_peak_relation"
    return None


def _reject(code: RejectionCode, item_type: str, index: int,
            detail: str = "") -> ValidationRejection:
    return ValidationRejection(code, item_type, index, detail)


def validate_evidence_output(
    context: IntelligenceContext,
    raw: object,
) -> EvidenceValidationResult:
    """Validate model evidence item by item against the frozen context."""
    if not isinstance(raw, dict):
        return EvidenceValidationResult(
            rejections=[_reject("text_mismatch", "payload", 0, "top level must be object")],
            structurally_valid=False,
        )
    if "observations" not in raw or "talktracks" not in raw:
        return EvidenceValidationResult(
            rejections=[_reject(
                "text_mismatch", "payload", 0,
                "observations and talktracks are required",
            )],
            structurally_valid=False,
        )
    observations_raw = raw["observations"]
    talktracks_raw = raw["talktracks"]
    if not isinstance(observations_raw, list) or not isinstance(talktracks_raw, list):
        return EvidenceValidationResult(
            rejections=[_reject("text_mismatch", "payload", 0, "item collections must be arrays")],
            structurally_valid=False,
        )

    observation_candidates: list[tuple[int, Observation]] = []
    talktrack_candidates: list[tuple[int, int, ReusableTalktrack]] = []
    rejections: list[ValidationRejection] = []

    for index, item in enumerate(observations_raw):
        if not isinstance(item, dict) or not str(item.get("statement") or "").strip():
            rejections.append(_reject("text_mismatch", "observation", index))
            continue
        refs, resolved, ref_error = _validate_refs(context, item.get("evidence"))
        if ref_error:
            rejections.append(_reject(ref_error, "observation", index))
            continue
        relation = str(item.get("relation") or "")
        if relation == "causal" or _CAUSAL_RE.search(
                _compact_text(item.get("statement"))):
            rejections.append(_reject("causal_overclaim", "observation", index))
            continue
        source_texts = _transcript_texts(resolved)
        if _forbidden_claim(str(item["statement"]), source_texts):
            rejections.append(_reject("forbidden_claim", "observation", index))
            continue
        if not _peak_value_claim_has_source(str(item["statement"]), resolved):
            rejections.append(_reject("number_mismatch", "observation", index))
            continue
        number_claim = _observation_number_claim(
            str(item["statement"]), relation)
        semantic_facts = _resolved_semantic_facts(refs, resolved)
        if not _numbers_have_sources(number_claim, list(semantic_facts.texts)):
            rejections.append(_reject("number_mismatch", "observation", index))
            continue
        peak_error = _peak_relation_code(
            str(item["statement"]), relation, refs, resolved)
        if peak_error:
            rejections.append(_reject(peak_error, "observation", index))
            continue
        # The relation itself is admitted only by _peak_relation_code.  It
        # never licenses an otherwise empty business claim.
        if not claim_semantically_supported(
                str(item["statement"]), semantic_facts):
            rejections.append(_reject("text_mismatch", "observation", index))
            continue
        phenomenon_key, phenomenon_statement = validated_phenomenon_identity(
            item["statement"])
        try:
            observation = Observation.from_dict({
                **item,
                "evidence": [ref.to_dict() for ref in refs],
                "live_id": context.live_id,
                "source_id": str(item.get("source_id") or refs[0].source_id),
                "phenomenon_key": phenomenon_key,
                "phenomenon_statement": phenomenon_statement,
            })
        except (TypeError, ValueError):
            rejections.append(_reject("text_mismatch", "observation", index))
            continue
        observation_candidates.append((index, observation))

    ranked_observations = sorted(
        observation_candidates,
        key=lambda row: (
            -len(row[1].evidence),
            -len(_compact_text(row[1].statement)),
            _compact_text(row[1].statement),
        ),
    )
    unique_observations: list[tuple[int, Observation]] = []
    seen_observation_keys: set[str] = set()
    for index, observation in ranked_observations:
        key = _compact_text(observation.statement)
        if key in seen_observation_keys:
            rejections.append(_reject(
                "duplicate_weak_item", "observation", index))
            continue
        seen_observation_keys.add(key)
        unique_observations.append((index, observation))
    observations = [item for _index, item in unique_observations]

    for index, item in enumerate(talktracks_raw):
        if not isinstance(item, dict):
            rejections.append(_reject("text_mismatch", "talktrack", index))
            continue
        refs, resolved, ref_error = _validate_refs(context, item.get("evidence"))
        if ref_error:
            rejections.append(_reject(ref_error, "talktrack", index))
            continue
        original = str(item.get("original_text") or "").strip()
        script = str(item.get("reusable_script") or "").strip()
        scene = str(item.get("scene") or "").strip()
        source_texts = _transcript_texts(resolved)
        if not original or not script or not scene or not any(
                _compact_text(original) == _compact_text(source) for source in source_texts):
            rejections.append(_reject("text_mismatch", "talktrack", index))
            continue
        if not _numbers_have_sources(script, [original]):
            rejections.append(_reject("number_mismatch", "talktrack", index))
            continue
        if not _talktrack_rewrite_is_grounded(original, script):
            rejections.append(_reject("text_mismatch", "talktrack", index))
            continue
        if not re.search(r"[。！？；!?]$", script):
            rejections.append(_reject("incomplete_sentence", "talktrack", index))
            continue
        if (_forbidden_claim(script, [original], relaxed_source=True)
                or _unsupported_concrete_sales_claim(script, [original])):
            rejections.append(_reject("forbidden_claim", "talktrack", index))
            continue
        transcript = resolved["transcript"][source_texts.index(next(
            source for source in source_texts
            if _compact_text(original) == _compact_text(source)
        ))]
        quality = quality_candidate(
            int(getattr(transcript, "start_ms", 0)),
            int(getattr(transcript, "end_ms", 0)), script)
        if quality is None:
            rejections.append(_reject("incomplete_sentence", "talktrack", index))
            continue
        talktrack_candidates.append((index, int(quality["rule_score"]), ReusableTalktrack(
            original_text=original,
            reusable_script=script,
            scene=scene,
            evidence=refs,
        )))

    ranked_talktracks = sorted(
        talktrack_candidates,
        key=lambda row: (
            -row[1],
            -len(row[2].evidence),
            -len(_compact_text(row[2].reusable_script)),
            _compact_text(row[2].reusable_script),
        ),
    )
    unique_talktracks: list[tuple[int, int, ReusableTalktrack]] = []
    seen_talktrack_keys: set[str] = set()
    for index, score, talktrack in ranked_talktracks:
        key = _compact_text(talktrack.reusable_script)
        if key in seen_talktrack_keys:
            rejections.append(_reject(
                "duplicate_weak_item", "talktrack", index))
            continue
        seen_talktrack_keys.add(key)
        unique_talktracks.append((index, score, talktrack))
    talktracks = [item for _index, _score, item in unique_talktracks]

    return EvidenceValidationResult(observations, talktracks, rejections)


def _available_metrics(context: IntelligenceContext) -> set[str]:
    """Return only canonical metrics backed by a usable time series."""
    available: set[str] = set()
    series = context.metrics.get("series")
    if isinstance(series, dict):
        for key, points in series.items():
            if str(key) not in CANONICAL_METRICS or not isinstance(points, list):
                continue
            if any(
                isinstance(point, dict)
                and isinstance(point.get("ts"), (int, float))
                and not isinstance(point.get("ts"), bool)
                and isinstance(point.get("value"), (int, float))
                and not isinstance(point.get("value"), bool)
                for point in points
            ):
                available.add(str(key))
    return available


_CONTRACT_TIME_RE = re.compile(
    r"(?P<number>\d+|[\u96f6\u3007\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]+)"
    r"(?P<unit>\u79d2|\u5206\u949f|\u5c0f\u65f6)"
)
_CN_CONTRACT_DIGITS = {
    "\u96f6": 0, "\u3007": 0, "\u4e00": 1, "\u4e8c": 2, "\u4e24": 2,
    "\u4e09": 3, "\u56db": 4, "\u4e94": 5, "\u516d": 6, "\u4e03": 7,
    "\u516b": 8, "\u4e5d": 9,
}


def _contract_number(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    if value == "\u5341":
        return 10
    if "\u5341" in value:
        left, right = value.split("\u5341", 1)
        if left not in ("", *tuple(_CN_CONTRACT_DIGITS)):
            return None
        if right not in ("", *tuple(_CN_CONTRACT_DIGITS)):
            return None
        return (1 if not left else _CN_CONTRACT_DIGITS[left]) * 10 + (
            0 if not right else _CN_CONTRACT_DIGITS[right])
    if all(char in _CN_CONTRACT_DIGITS for char in value):
        return int("".join(str(_CN_CONTRACT_DIGITS[char]) for char in value))
    return None


def _time_expression_seconds(number: str, unit: str) -> int | None:
    amount = _contract_number(number)
    if amount is None:
        return None
    multiplier = {"\u79d2": 1, "\u5206\u949f": 60, "\u5c0f\u65f6": 3_600}[unit]
    seconds = amount * multiplier
    return seconds if 1 <= seconds <= 3_600 else None


def _evaluation_window_seconds(duration: str, comparison: str) -> int | None:
    duration_text = re.sub(r"\s+", "", str(duration or ""))
    duration_text = duration_text.replace("\u4e0b\u4e00\u5c0f\u65f6", "1\u5c0f\u65f6")
    duration_match = re.fullmatch(
        r"(?:\u672a\u6765|\u63a5\u4e0b\u6765|\u6301\u7eed)?" + _CONTRACT_TIME_RE.pattern,
        duration_text,
    )
    if duration_match is None:
        return None
    duration_seconds = _time_expression_seconds(
        duration_match.group("number"), duration_match.group("unit"))
    comparison_text = re.sub(r"\s+", "", str(comparison or ""))
    has_equal_sides = bool(
        re.search(r"(?:\u6267\u884c)?\u524d\u540e\u5404", comparison_text)
        or re.search(r"\u4e0e(?:\u6267\u884c)?\u524d", comparison_text)
        or "\u4e0a\u4e00\u5c0f\u65f6" in comparison_text
        or "\u524d\u4e00\u5c0f\u65f6" in comparison_text
    )
    comparison_text = comparison_text.replace("\u4e0a\u4e00\u5c0f\u65f6", "1\u5c0f\u65f6")
    comparison_text = comparison_text.replace("\u524d\u4e00\u5c0f\u65f6", "1\u5c0f\u65f6")
    if (not has_equal_sides or "\u6bd4\u8f83" not in comparison_text
            or not re.search(r"\u540c\u53e3\u5f84|\u540c\u7a97\u53e3|\u76f8\u540c\u957f\u5ea6|\u7b49\u957f", comparison_text)):
        return None
    comparison_windows = {
        _time_expression_seconds(match.group("number"), match.group("unit"))
        for match in _CONTRACT_TIME_RE.finditer(comparison_text)
    }
    comparison_windows.discard(None)
    if (duration_seconds is None or comparison_windows != {duration_seconds}):
        return None
    return duration_seconds


def _validated_source_keys(
    validated: EvidenceValidationResult | dict[str, Any],
) -> set[tuple[str, str]]:
    if isinstance(validated, EvidenceValidationResult):
        return {
            (ref.source_type, ref.source_id)
            for item in (*validated.observations, *validated.talktracks)
            for ref in item.evidence
        }
    if not isinstance(validated, dict):
        return set()
    keys: set[tuple[str, str]] = set()
    for collection_name in ("observations", "talktracks", "reusable_talktracks"):
        collection = validated.get(collection_name)
        if not isinstance(collection, list):
            continue
        for item in collection:
            if not isinstance(item, dict) or not isinstance(item.get("evidence"), list):
                continue
            for raw_ref in item["evidence"]:
                try:
                    ref = EvidenceRef.from_dict(raw_ref)
                except (TypeError, ValueError):
                    continue
                if ref.source_id:
                    keys.add((ref.source_type, ref.source_id))
    return keys


def validate_action_output(
    context: IntelligenceContext,
    raw: object,
    *,
    job_key: str,
    validated_evidence: EvidenceValidationResult | dict[str, Any] | None = None,
) -> ActionValidationResult:
    """Validate analyst output while preserving free analysis and advice."""
    if not isinstance(raw, dict) or not isinstance(raw.get("actions"), list):
        return ActionValidationResult(
            rejections=[_reject("invalid_action_schema", "payload", 0)],
            structurally_valid=False,
        )
    raw_full_analysis = raw.get("full_analysis", "")
    if not isinstance(raw_full_analysis, str):
        return ActionValidationResult(
            rejections=[_reject("invalid_action_schema", "full_analysis", 0)],
            structurally_valid=False,
        )
    full_analysis = raw_full_analysis.strip()
    raw_conclusions = raw.get("business_conclusions", [])
    if (not isinstance(raw_conclusions, list)
            or any(not isinstance(item, str) for item in raw_conclusions)):
        return ActionValidationResult(
            rejections=[_reject("invalid_action_schema", "business_conclusions", 0)],
            structurally_valid=False,
        )
    business_conclusions: list[str] = []
    seen_conclusions: set[str] = set()
    for index, raw_conclusion in enumerate(raw_conclusions):
        conclusion = raw_conclusion.strip()
        key = _compact_text(conclusion)
        if (not conclusion
                or _unsupported_concrete_sales_claim(conclusion, [])
                or key in seen_conclusions):
            continue
        seen_conclusions.add(key)
        business_conclusions.append(conclusion)
    actions: list[ActionExperiment] = []
    rejections: list[ValidationRejection] = []
    seen: set[tuple[str, str]] = set()
    allowed_metrics = _available_metrics(context)
    retained_sources = (
        None if validated_evidence is None
        else _validated_source_keys(validated_evidence)
    )
    for index, item in enumerate(raw["actions"]):
        if (not isinstance(item, dict)
                or not _REQUIRED_ANALYST_ACTION_FIELDS.issubset(item)):
            rejections.append(_reject("invalid_action_schema", "action", index))
            continue
        item = dict(item)
        for optional in _ACTION_FIELDS - _REQUIRED_ANALYST_ACTION_FIELDS:
            item.setdefault(optional, [] if optional == "evidence" else "")
        if any(not isinstance(item.get(field), str) or not item[field].strip()
               for field in ("issue", "action")):
            rejections.append(_reject("invalid_action_schema", "action", index))
            continue
        raw_refs = item.get("evidence")
        if raw_refs in (None, [], ""):
            refs, resolved, ref_error = [], {}, None
        else:
            refs, resolved, ref_error = _validate_refs(context, raw_refs)
            if ref_error:
                rejections.append(_reject(ref_error, "action", index))
                continue
        # 分析型建议（2026-08-08 改版）允许引用整轮逐字稿中任意真实存在的
        # 片段/指标窗口（_validate_refs 已校验存在性），不限于 evidence 阶段
        # 通过校验的片段。
        source_texts = _transcript_texts(resolved)
        if any(_unsupported_concrete_sales_claim(
                str(item.get(field) or ""), source_texts)
                for field in ("issue", "action", "script", "trigger")):
            rejections.append(_reject("forbidden_claim", "action", index))
            continue
        # 建议和建议话术是分析师的创作，不冒充主播原话，因此不要求逐字稿
        # 中存在同一句。真正展示为“主播原话”的 talktrack 仍走严格证据门禁。
        script_key = ""
        if item["script"].strip():
            script_key = _compact_text(item["script"])
        canonical_metric = (canonical_metric_name(item["metric_name"])
                            if item["metric_name"].strip() else "")
        if (item["metric_name"].strip()
                and (canonical_metric is None or canonical_metric not in allowed_metrics)):
            rejections.append(_reject("metric_series_missing", "action", index))
            # The analyst contract keeps the why/action advice even when an
            # optional metric field is not present in this hour's series.
            canonical_metric = ""
            item["metric_name"] = ""
        # 分析型建议（2026-08-08 改版）：duration/comparison 为可选实验字段，
        # 提供但格式不可解析时丢弃字段本身，不拖累整条建议；复验按无窗口处理。
        evaluation_window_seconds = None
        if (canonical_metric and item["duration"].strip()
                and item["comparison"].strip()):
            evaluation_window_seconds = _evaluation_window_seconds(
                item["duration"], item["comparison"])
            if evaluation_window_seconds is None:
                item["duration"] = ""
                item["comparison"] = ""
        duplicate_key = (_compact_text(item["action"]), script_key)
        if duplicate_key in seen:
            rejections.append(_reject("duplicate_weak_item", "action", index))
            continue
        seen.add(duplicate_key)
        actions.append(ActionExperiment(
            experiment_id=f"{job_key}:E{len(actions) + 1:02d}",
            issue=item["issue"].strip(),
            action=item["action"].strip(),
            script=item["script"].strip(),
            trigger=item["trigger"].strip(),
            duration=item["duration"].strip(),
            metric_name=canonical_metric,
            comparison=item["comparison"].strip(),
            evaluation_window_seconds=evaluation_window_seconds,
            evidence=refs,
        ))
    return ActionValidationResult(
        actions,
        rejections,
        full_analysis=full_analysis,
        business_conclusions=business_conclusions,
    )


__all__ = [
    "ActionValidationResult",
    "EvidenceValidationResult",
    "REJECTION_CODES",
    "ValidationRejection",
    "validate_action_output",
    "validate_evidence_output",
]
