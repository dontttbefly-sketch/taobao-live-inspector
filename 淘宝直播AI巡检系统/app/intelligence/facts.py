"""Type-aware extraction of business facts from frozen evidence.

Opaque identity, clock, window, and integrity fields are deliberately absent
from this boundary.  Validators consume only the returned business text and
metric values, so metadata can never ground a model claim by accident.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable


CANONICAL_METRICS = frozenset(("uv", "itemClick", "deal", "heatScore"))
_METRIC_ALIASES = {
    "uv": "uv",
    "online_uv": "uv",
    "viewer_uv": "uv",
    "look_uv": "uv",
    "itemclick": "itemClick",
    "item_click": "itemClick",
    "ipv_total": "itemClick",
    "deal": "deal",
    "pay_amt": "deal",
    "heatscore": "heatScore",
    "heat_score": "heatScore",
}
_METRIC_LABELS = {
    "uv": ("在线人数", "观看人数", "在线", "观看", "UV"),
    "itemClick": ("商品点击", "点击次数", "点击", "itemClick"),
    "deal": ("成交金额", "成交", "deal"),
    "heatScore": ("热度值", "热度", "heatScore"),
}
_METRIC_UNITS = {
    "uv": "人",
    "itemClick": "次",
    "deal": "元",
    "heatScore": "",
}

# Platform review totals and anchor settlement fields are a different schema
# from the four minute-series metrics.  Keep this allowlist independent so a
# metadata field such as ``duration_sec`` can never become business evidence.
_PLATFORM_OFFICIAL_METRICS: dict[str, tuple[tuple[str, ...], str]] = {
    "pay_amt": (("成交金额",), "元"),
    "viewer_uv": (("观看人数",), "人"),
    "viewer_pv": (("观看次数",), "次"),
    "max_online_uv": (("最高在线", "最高在线人数"), "人"),
    "buyer_cnt": (("成交人数",), "人"),
    "order_cnt": (("成交订单", "成交订单数"), "单"),
    "item_qty": (("成交件数",), "件"),
    "look_uv": (("观看人数",), "人"),
    "look_uv_segment_sum": (("观看人次分段累计",), "人次"),
    "look_pv": (("观看次数",), "次"),
    "pay_byr_cnt": (("成交人数",), "人"),
    "pay_ord_cnt": (("成交订单", "成交订单数"), "单"),
    "pay_itm_qty": (("成交件数",), "件"),
    "cvr_pay": (("成交转化率",), "%"),
    "atv": (("客单价",), "元"),
    "ipv_uv": (("商品点击人数",), "人"),
    "ipv": (("商品点击次数",), "次"),
    "ipv_uv_segment_sum": (("商品点击人次分段累计",), "人次"),
    "ctr_itm": (("商品点击率",), "%"),
    "cart_uv": (("加购人数",), "人"),
    "cart_uv_segment_sum": (("加购人次分段累计",), "人次"),
    "cart_pv": (("加购次数",), "次"),
    "cart_itm_qty": (("加购件数",), "件"),
    "atn_uv": (("新增粉丝",), "人"),
    "atn_uv_rate": (("转粉率",), "%"),
    "cmt": (("评论次数",), "次"),
    "shr": (("分享次数",), "次"),
    "fvr": (("点赞次数",), "次"),
    "look_time_sec": (("观看时长",), "秒"),
}

_STRATEGY_WORDS = tuple(sorted({
    "上一个小时", "与上一小时同口径比较", "与前一小时同窗口比较",
    "与上一小时同窗口比较", "下一小时", "上一小时", "前一小时",
    "横跨不同小时", "不同小时", "跨小时", "重复出现", "本场保留了", "不同阶段",
    "两个阶段", "观众询问", "产品讲解", "使用方式", "同口径",
    "小时智能产物", "通过校验", "智能产物", "实验终态",
    "小时证据", "全场指标", "主播指标", "跨主播证据",
    "与成交峰值发生在同一分钟内", "与成交峰值仅有时间相关",
    "不代表因果", "先后无法确定", "出现在成交峰值前三分钟",
    "话术结束后", "成交峰值", "点击峰值", "在线峰值",
    "时间相关", "同一分钟内", "峰值前三分钟", "峰值窗口",
    "出现在", "发生在", "位于", "先于", "之后观察到",
    "主播", "本场", "整场", "本小时", "当前", "阶段", "保持", "完整",
    "产品", "商品", "这款", "这段", "讲解", "演示", "介绍", "说明",
    "强调", "展示", "卖点", "话术", "表达", "动作", "问题", "需要",
    "继续", "下一场", "下一轮", "开头", "更早", "然后", "先", "再",
    "加入", "增加", "完成", "重点", "针对", "观众", "用户", "询问", "时候",
    "场景", "触发", "验证", "复验", "比较", "口径", "重复", "出现",
    "变化", "保留", "已经", "进行", "使用", "方式", "顺序", "适用",
    "采用", "可以", "解释", "实验", "终态", "记录", "分钟", "小时", "秒",
    "提到", "讲", "时", "已", "该",
}, key=len, reverse=True))
_DEGREE_MODIFIER_RE = re.compile(r"更(?=[㐀-鿿])")
_FUNCTION_CHARS_RE = re.compile(r"[的了和与及是在都为把将并而于中内每个种款]+")
_NUMBER_WITH_UNIT_RE = re.compile(
    r"\d+(?:[.,]\d+)?(?:亿|万|千|百|十)?(?:元|次|人|%|\b)?|"
    r"[零〇一二两三四五六七八九十百千万亿]+(?:元|次|人)?"
)
_STRONG_CLAUSE_SPLIT_RE = re.compile(r"[。！？；.!?;\n]+")
_RELATION_CLAUSE_SPLIT_RE = re.compile(r"[,，]+")
_DEMONSTRATIVE_BOUNDARY_RE = re.compile(r"(?:这款|该款|这个|这件)")
_EXPLICIT_ENTITY_RELATION_RE = re.compile(
    r"^\s*(?:这款|该款|这个|这件)?"
    r"(?P<entity>[㐀-鿿]{1,12}?)(?=采用|使用|支持|具备|拥有|配备|搭载)"
)
_DEMONSTRATIVE_ENTITY_RE = re.compile(
    r"(?:这款|该款|这个|这件)[㐀-鿿]{1,8}"
)
_IMPLICIT_CONTINUATION_RE = re.compile(
    r"(?:[㐀-鿿]{2}|[㐀-鿿]{4})"
    r"(?:后|时|前|中)(?![间段刻期候点])(?=[㐀-鿿])"
)
_NEGATION_RE = re.compile(
    r"并非|不是|不能|无法|没有|没能|"
    r"无(?!线)|未(?!来)|不(?!但|仅)"
)

@dataclass(frozen=True)
class SemanticFacts:
    texts: tuple[str, ...] = ()
    metric_names: tuple[str, ...] = ()
    # Each inner tuple is one trusted source (or one explicit structured
    # source).  Claims may combine fields inside a group, never across groups.
    source_groups: tuple[tuple[str, ...], ...] = ()

    def __post_init__(self) -> None:
        if self.texts and not self.source_groups:
            object.__setattr__(
                self, "source_groups",
                tuple((text,) for text in self.texts if str(text).strip()),
            )

    def merged(self, *others: "SemanticFacts") -> "SemanticFacts":
        texts = list(self.texts)
        metrics = list(self.metric_names)
        groups = list(self.source_groups)
        for other in others:
            texts.extend(other.texts)
            metrics.extend(other.metric_names)
            groups.extend(other.source_groups)
        return SemanticFacts(
            tuple(dict.fromkeys(text for text in texts if str(text).strip())),
            tuple(dict.fromkeys(metric for metric in metrics if metric)),
            tuple(group for group in groups if group),
        )


def validated_phenomenon_identity(statement: object) -> tuple[str, str]:
    """Return a conservative typed identity for an already-grounded claim."""
    text = unicodedata.normalize("NFKC", str(statement or "")).casefold()
    compact = re.sub(r"\s+", "", text)
    has_orientations = (
        "横竖屏" in compact
        or ("横屏" in compact and "竖屏" in compact)
    )
    if has_orientations and any(word in compact for word in ("切换", "转换")):
        return "phenomenon-v1:orientation-switch", "横竖屏切换"
    normalized = re.sub(
        r"[，。！？；：、,.!?;:'\"“”‘’（）()\[\]【】]", "", compact)
    if not normalized:
        return "", ""
    return (
        "phenomenon-v1:exact:" + hashlib.sha256(
            normalized.encode("utf-8")).hexdigest(),
        str(statement or "").strip(),
    )


def canonical_metric_name(value: object) -> str | None:
    raw = str(value or "").strip()
    if raw in CANONICAL_METRICS:
        return raw
    return _METRIC_ALIASES.get(raw.casefold())


def metric_labels(metric_name: object) -> tuple[str, ...]:
    canonical = canonical_metric_name(metric_name)
    return () if canonical is None else _METRIC_LABELS[canonical]


def _metric_values(value: object) -> list[object]:
    if isinstance(value, list):
        return [item.get("value") for item in value
                if isinstance(item, dict) and item.get("value") is not None]
    if not isinstance(value, dict):
        return []
    if isinstance(value.get("series"), list):
        return _metric_values(value["series"])
    if value.get("value") is not None:
        return [value["value"]]
    return [value[key] for key in ("before_value", "after_value")
            if value.get(key) is not None]


def metric_facts(metric_name: object, value: object) -> SemanticFacts:
    canonical = canonical_metric_name(metric_name)
    if canonical is None:
        return SemanticFacts()
    label = _METRIC_LABELS[canonical][0]
    unit = _METRIC_UNITS[canonical]
    texts = [label]
    texts.extend(f"{label}{item}{unit}" for item in _metric_values(value))
    return SemanticFacts(tuple(texts), (canonical,), (tuple(texts),))


def is_platform_official_metric(metric_name: object) -> bool:
    return str(metric_name or "").strip() in _PLATFORM_OFFICIAL_METRICS


def platform_official_metric_facts(
        metric_name: object, value: object) -> SemanticFacts:
    name = str(metric_name or "").strip()
    spec = _PLATFORM_OFFICIAL_METRICS.get(name)
    if spec is None or isinstance(value, bool) or value is None:
        return SemanticFacts()
    labels, unit = spec
    texts = [*labels]
    texts.extend(f"{label}{value}{unit}" for label in labels)
    return SemanticFacts(tuple(texts), (name,), (tuple(texts),))


def hourly_source_facts(
        source_type: str, source_id: str, source: object) -> SemanticFacts:
    if source_type == "transcript":
        text = (source.get("text") if isinstance(source, dict)
                else getattr(source, "text", ""))
        texts = (str(text),) if str(text or "").strip() else ()
        return SemanticFacts(texts, (), (texts,) if texts else ())
    if source_type == "metric_window":
        metric = source_id.removeprefix("M:")
        if isinstance(source, dict) and source.get("metric_name"):
            metric = str(source["metric_name"])
        return metric_facts(metric, source)
    if source_type == "peak" and isinstance(source, dict):
        meta = source.get("peak_meta")
        meta = meta if isinstance(meta, dict) else source
        texts: list[str] = []
        if meta.get("value") is not None and str(meta.get("unit") or ""):
            texts.append(f"峰值{meta['value']}{meta['unit']}")
        excerpts = meta.get("excerpts")
        if isinstance(excerpts, list):
            texts.extend(str(item.get("text") or "") for item in excerpts
                         if isinstance(item, dict) and item.get("text"))
        values = tuple(texts)
        return SemanticFacts(
            values, (), tuple((value,) for value in values if value))
    return SemanticFacts()


def _nested_evidence_facts(payload: object) -> SemanticFacts:
    if not isinstance(payload, list):
        return SemanticFacts()
    facts = SemanticFacts()
    for item in payload:
        if not isinstance(item, dict):
            continue
        facts = facts.merged(hourly_source_facts(
            str(item.get("source_type") or ""),
            str(item.get("source_id") or ""),
            item.get("payload"),
        ))
    return facts


def platform_source_facts(source_type: str, payload: object) -> SemanticFacts:
    """Extract only business-bearing fields from one frozen platform source."""
    if not isinstance(payload, dict):
        return SemanticFacts()
    if source_type == "official_metric":
        return platform_official_metric_facts(
            payload.get("metric_name"), payload.get("value"))
    if source_type == "hourly_analysis":
        conclusions = payload.get("business_conclusions")
        texts = [str(payload.get("full_analysis") or "")]
        if isinstance(conclusions, list):
            texts.extend(str(item) for item in conclusions)
        return SemanticFacts(tuple(text for text in texts if text.strip()))
    if source_type.startswith("hourly_"):
        item = payload.get("item")
        item = item if isinstance(item, dict) else {}
        fields = {
            "hourly_observation": ("statement", "scene"),
            "hourly_talktrack": ("original_text", "reusable_script", "scene"),
            "hourly_experiment": (
                "issue", "action", "script", "trigger", "duration", "comparison",
            ),
        }.get(source_type, ())
        item_texts = tuple(
            str(item.get(key) or "") for key in fields
            if str(item.get(key) or "").strip()
        )
        facts = SemanticFacts(
            item_texts, (), (item_texts,) if item_texts else ())
        if source_type == "hourly_experiment":
            facts = facts.merged(metric_facts(item.get("metric_name"), item))
        return facts.merged(_nested_evidence_facts(payload.get("evidence")))
    return SemanticFacts()


def _semantic_assertions(value: object) -> list[list[str]]:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    assertions: list[list[str]] = []
    # Strong punctuation separates independent assertions.  Commas stay
    # inside one assertion so an entity and predicate cannot be satisfied by
    # different sources merely because the model inserted punctuation.
    for clause in _STRONG_CLAUSE_SPLIT_RE.split(text):
        clause = _NUMBER_WITH_UNIT_RE.sub("|", clause)
        for word in _STRATEGY_WORDS:
            clause = clause.replace(word.casefold(), "|")
        protected_negations: list[tuple[str, str]] = []

        def protect_negation(match: re.Match[str]) -> str:
            token = f"qzneg{'x' * (len(protected_negations) + 1)}zq"
            protected_negations.append((token, match.group(0)))
            return token

        clause = _NEGATION_RE.sub(protect_negation, clause)
        # Degree modifiers do not establish a new semantic unit.  Removing
        # them keeps ``携带更方便`` aligned with ``携带方便`` without
        # weakening entity boundaries.
        clause = _DEGREE_MODIFIER_RE.sub("", clause)
        clause = _FUNCTION_CHARS_RE.sub("|", clause)
        clause = re.sub(r"[^0-9a-z\u3400-\u9fff]+", "|", clause)
        chunks = []
        for part in clause.split("|"):
            for token, negation in protected_negations:
                part = part.replace(token, negation)
            if len(part) >= 2:
                chunks.append(part)
        if chunks:
            assertions.append(chunks)
    return assertions


def _has_trailing_negation(value: object) -> bool:
    normalized = re.sub(r"[,，:：\s]+$", "", str(value or ""))
    return any(
        match.end() == len(normalized)
        for match in _NEGATION_RE.finditer(normalized)
    )


def _relation_occurs_without_outer_negation(
        relation: str, normalized_source: str) -> bool:
    relation_negations = len(_NEGATION_RE.findall(relation))
    source_negations = len(_NEGATION_RE.findall(normalized_source))
    if relation_negations != source_negations:
        return False
    start = 0
    while relation and (index := normalized_source.find(relation, start)) >= 0:
        if not _has_trailing_negation(normalized_source[:index]):
            return True
        start = index + 1
    return False


def _relation_is_bound(chunks: list[str], unit: object) -> bool:
    relation = "".join(chunks)
    semantic_unit = "".join(
        chunk
        for assertion in _semantic_assertions(unit)
        for chunk in assertion
    )
    if not (_NEGATION_RE.search(relation) or _NEGATION_RE.search(semantic_unit)):
        return True
    # Entity, scoped polarity, and predicate must occur as one normalized
    # relation.  This preserves neutral wording removed by the semantic
    # normalizer without moving a predicate or negation across entities.
    return _relation_occurs_without_outer_negation(relation, semantic_unit)


def _exact_claim_supported(claim: object, facts: SemanticFacts) -> bool:
    normalized_claim = re.sub(
        r"\s+", "", unicodedata.normalize("NFKC", str(claim or "")).casefold())
    if not normalized_claim:
        return False
    return any(
        _relation_occurs_without_outer_negation(
            normalized_claim, normalized_source)
        for group in facts.source_groups
        for source in group
        if (normalized_source := re.sub(
            r"\s+", "",
            unicodedata.normalize("NFKC", str(source or "")).casefold(),
        ))
    )


def _chunk_supported(chunk: str, sources: Iterable[str]) -> bool:
    normalized_sources = [
        _DEGREE_MODIFIER_RE.sub(
            "", re.sub(
                r"\s+", "",
                unicodedata.normalize("NFKC", str(source)).casefold(),
            ),
        )
        for source in sources if str(source).strip()
    ]
    if len(chunk) < 2:
        return False
    # A paraphrased claim may concatenate units from one source clause (for
    # example, ``支架`` + ``横竖屏切换``).  Require an exact cover by
    # substrings instead of a fuzzy majority of bigrams, which allowed an
    # unsupported suffix such as ``防水`` to hitchhike.
    for source in normalized_sources:
        if chunk in source:
            return True
        reachable = {0}
        for start in range(len(chunk)):
            if start not in reachable:
                continue
            for end in range(start + 2, len(chunk) + 1):
                if chunk[start:end] in source:
                    reachable.add(end)
        if len(chunk) in reachable:
            return True
    return False


def _continuation_aligns_with_claim(
        entity: str, remainder: str, chunks: list[str]) -> bool:
    remainder_assertions = _semantic_assertions(remainder)
    if not remainder_assertions or not remainder_assertions[0]:
        return False
    lead = remainder_assertions[0][0]
    entity_assertions = _semantic_assertions(entity)
    entity_chunk = (
        entity_assertions[0][0]
        if entity_assertions and entity_assertions[0]
        else ""
    )
    for chunk in chunks:
        candidate = chunk
        if entity_chunk and candidate.startswith(entity_chunk):
            candidate = candidate[len(entity_chunk):]
        aligned_candidates = [candidate]
        if (marker := _IMPLICIT_CONTINUATION_RE.match(candidate, 0)):
            aligned_candidates.append(candidate[marker.end():])
        # Two-character attributes (for example ``防水``) are too short
        # to distinguish from a new entity such as ``防水袋``.  Longer
        # remainders must align in full at the start of the continuation.
        if any(
            len(aligned) >= 3 and lead.startswith(aligned)
            for aligned in aligned_candidates
        ):
            return True
    return False


def _source_relation_units(source: object, chunks: list[str]) -> list[str]:
    units: list[str] = []
    for sentence in _STRONG_CLAUSE_SPLIT_RE.split(str(source)):
        current_entity = ""
        current_relation = ""
        # A comma cannot safely terminate an unresolved negation scope.
        # Treat the entire strong sentence as one ambiguous relation and let
        # the fail-closed polarity binding below decide it.
        punctuated_clauses = (
            [sentence]
            if _NEGATION_RE.search(sentence)
            else _RELATION_CLAUSE_SPLIT_RE.split(sentence)
        )
        clauses: list[str] = []
        for punctuated_clause in punctuated_clauses:
            boundaries = [
                match.start()
                for match in _DEMONSTRATIVE_BOUNDARY_RE.finditer(
                    punctuated_clause)
                if match.start() > 0
                and not _NEGATION_RE.search(
                    punctuated_clause[:match.start()])
            ]
            start = 0
            for boundary in boundaries:
                clauses.append(punctuated_clause[start:boundary])
                start = boundary
            clauses.append(punctuated_clause[start:])
        for clause in clauses:
            clause = clause.strip()
            if not clause:
                continue
            match = _EXPLICIT_ENTITY_RELATION_RE.search(clause)
            explicit_entity = str(match.group("entity") if match else "").strip()
            # Temporal/locative prefixes before a predicate are continuations,
            # not new business entities (for example, ``展开后支持``).
            if explicit_entity.endswith(("后", "时", "前", "中")):
                explicit_entity = ""
            if not explicit_entity and _DEMONSTRATIVE_ENTITY_RE.search(clause):
                explicit_entity = clause
            # Consider overlapping temporal/locative markers.  A leading
            # relation phrase may otherwise greedily hide the shorter marker
            # whose remainder actually aligns with the claim.
            implicit_matches = [
                match
                for offset in range(len(clause))
                if (match := _IMPLICIT_CONTINUATION_RE.match(clause, offset))
            ]
            implicit_continuation = bool(
                current_entity
                and any(_continuation_aligns_with_claim(
                    current_entity, clause[match.end():], chunks)
                    for match in implicit_matches))
            if explicit_entity:
                current_entity = explicit_entity
                current_relation = clause
            elif current_relation and implicit_continuation:
                current_relation += clause
            else:
                current_entity = ""
                current_relation = clause
            units.append(clause)
            if not explicit_entity and implicit_continuation and current_entity:
                units.append(current_entity + clause)
                units.append(current_relation)
    return units


# 行动指令（issue/action/trigger）允许自由使用的分析性表达。宽松模式下，
# 无源词块若完全由这些词（与源文本子串）构成则放行；事实性实体仍必须有源。
_ACTION_ANALYSIS_WORDS: tuple[str, ...] = (
    "讲解", "重复", "完整", "补充", "提前", "确保", "强化", "对比", "信息",
    "相关", "对应", "配合", "展示", "覆盖范围", "适用条件", "使用场景", "细节",
    "差异", "仅", "提及", "主播", "介绍", "开始", "但", "未", "以", "并", "及",
    "当", "时", "前", "后", "中", "到", "在", "对", "与", "和", "的", "了",
    "是", "有", "就", "只", "也", "还", "都", "更", "可以", "需要", "应该",
    "要", "会", "能", "把", "被", "让", "给", "您", "你", "我们", "咱们",
    "这个", "那个", "层面", "整体", "同时", "先", "再", "重点", "单独",
    "分点", "强调", "引导", "该话术", "话术", "完整度", "点", "说明",
    "描述", "关于", "围绕", "调整", "同样", "一样", "一下", "一步", "等",
    "这样", "那么", "一下", "相关", "部分", "一起",
)


def _is_analysis_chunk(chunk: str, source_texts: Iterable[str]) -> bool:
    """无源词块是否为“分析词 + 源文本子串”的组合（宽松模式放行条件）。"""
    remaining = chunk
    while remaining:
        consumed = False
        for word in _ACTION_ANALYSIS_WORDS:
            if remaining.startswith(word):
                remaining = remaining[len(word):]
                consumed = True
                break
        if consumed:
            continue
        found = False
        for size in range(min(6, len(remaining)), 1, -1):
            prefix = remaining[:size]
            if any(prefix in source for source in source_texts):
                remaining = remaining[size:]
                found = True
                break
        if not found:
            return False
    return True


def _assertion_supported(
        chunks: list[str], group: tuple[str, ...], *,
        require_all_chunks: bool = True) -> bool:
    # A transcript may contain several unrelated sentences.  Each concrete
    # assertion must fit one sentence; only multi-field structured groups may
    # additionally combine their explicitly related fields.
    units = [
        unit
        for source in group
        for unit in _source_relation_units(source, chunks)
    ]
    if require_all_chunks:
        return any(
            _relation_is_bound(chunks, unit)
            and all(
                _chunk_supported(chunk, (unit,)) for chunk in chunks)
            for unit in units
        )
    # Relaxed mode: every word block must either bind to a source unit or be
    # composed of analysis vocabulary (plus source substrings).  Factual
    # entities still need a source; only phrasing is free.
    return all(
        any(_chunk_supported(chunk, (unit,)) for unit in units)
        or _is_analysis_chunk(chunk, group)
        for chunk in chunks
    )


def claim_semantically_supported(
        claim: object, facts: SemanticFacts, *,
        allow_generic: bool = False,
        require_all_assertions: bool = True) -> bool:
    assertions = _semantic_assertions(claim)
    if not assertions:
        return allow_generic and bool(facts.texts)
    # Exact trusted source text is already a complete same-source relation;
    # keep it intact even when it contains internal punctuation.
    if _exact_claim_supported(claim, facts):
        return True
    supported = (
        any(
            any(
                _assertion_supported(
                    chunks, group, require_all_chunks=False)
                for group in facts.source_groups
            )
            for chunks in assertions
        )
        if not require_all_assertions
        else all(
            any(
                _assertion_supported(chunks, group)
                for group in facts.source_groups
            )
            for chunks in assertions
        )
    )
    return bool(facts.source_groups) and supported


__all__ = [
    "CANONICAL_METRICS",
    "SemanticFacts",
    "canonical_metric_name",
    "claim_semantically_supported",
    "hourly_source_facts",
    "is_platform_official_metric",
    "metric_facts",
    "metric_labels",
    "platform_official_metric_facts",
    "platform_source_facts",
    "validated_phenomenon_identity",
]
