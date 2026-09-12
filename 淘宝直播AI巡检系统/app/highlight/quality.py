"""优质可复用话术精选。

数据峰值回答“数据变化附近说了什么”，本模块回答“哪些表达本身
完整、具体、值得复用”。两者分榜，禁止把数据相关性当成话术质量。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re

from rapidfuzz import fuzz

log = logging.getLogger("highlight.quality")

QUALITY_CATEGORIES = ("产品讲解", "利益表达", "异议处理", "转化推进", "信任建立", "互动承接")
LIBRARY_CATEGORY = {
    "产品讲解": "产品", "利益表达": "福利", "异议处理": "答疑",
    "转化推进": "催付", "信任建立": "产品", "互动承接": "互动",
}
MAX_CANDIDATES_FOR_LLM = 30
MIN_RULE_SCORE = 48
MIN_FINAL_SCORE = 58
MIN_VISIBLE_CHARS = 16
MAX_VISIBLE_CHARS = 220
MIN_TIME_GAP_MS = 75_000

SIGNALS = {
    "产品讲解": (
        "材质", "工艺", "防摔", "磁吸", "充电", "功率", "容量", "支架", "镜头",
        "手机壳", "充电宝", "透明", "磨砂", "型号", "兼容", "支持", "设计", "结构",
    ),
    "利益表达": (
        "优惠", "到手", "券", "补贴", "满减", "赠品", "送", "保价", "售后", "免单", "抽奖",
    ),
    "异议处理": (
        "如果", "担心", "不用担心", "不合适", "能不能", "可以吗", "怎么",
        "退换", "退货", "发货", "适合", "区别", "问题", "会不会", "没有",
    ),
    "转化推进": (
        "下单", "拍下", "链接", "去拍", "加购", "购买", "选择", "付款", "安排上",
    ),
    "信任建立": (
        "一年", "保修", "保障", "官方", "实测", "安全", "认证", "耐用", "不伤",
    ),
    "互动承接": (
        "公屏", "评论", "扣1", "扣一", "告诉主播", "有没有", "想看", "关注",
    ),
}
EXPLANATION = ("因为", "所以", "这样", "相当于", "也就是", "好处", "优势", "对比")
GENERIC_NOISE = (
    "大家还有没有问题", "有问题扣公屏", "感谢宝贝下单", "明白了吗",
    "可以看到吗", "好不好", "对不对", "走过路过别错过",
)
NOISE_RE = re.compile(r"[A-Z]{8,}|([，、；。！？,.!?;])\1+")

PRODUCT_OBJECT_RE = re.compile(
    r"产品|商品|手机壳|充电宝|充电器|支架|镜头膜|数据线|保护壳|链接|型号"
)
PRODUCT_ATTRIBUTE_RE = re.compile(
    r"材质|结构|工艺|容量|毫安|功率|瓦快充|防摔|防爆|稳固|兼容|接口|"
    r"尺寸|重量|续航|散热|磁吸|颜色|售后|保修"
)
PRODUCT_DETAIL_RE = re.compile(
    PRODUCT_ATTRIBUTE_RE.pattern + r"|\d+(?:\.\d+)?(?:号|瓦|毫安|年)"
)
REASON_RE = re.compile(r"因为|所以|这样|相比|区别|更(?:加)?(?:稳|适合|耐用|方便|好)|好处|优势")
ACTION_RE = re.compile(r"下单|拍下|去拍|购买|加购|付款|选择|安装|使用|操作|领取|退回")
SEQUENCE_RE = re.compile(
    r"先.{0,24}(?:再|然后)|第[一二三四五六七八九十\d]+步|"
    r"[一二两三四五六七八九十\d]+个?步骤"
)
CONCRETE_BENEFIT_RE = re.compile(
    r"优惠券|补贴|满减|赠品|(?:赠送|送)(?:给|到|上)?[^，。！？；]{0,16}"
    r"(?:抛光布|挂绳|贴纸|礼品)|运费险|"
    r"无理由退货|售后|保修|保价|免单|\d+(?:\.\d+)?折"
)
CONDITION_RE = re.compile(
    r"直播间下单|下单|拍下|拍完|去拍|付款|加入购物车|购买|"
    r"会员|今天|之前|以后|截止|满\s*\d+|"
    r"\d{1,2}(?:点|时|:|：)|上午场|下午场|当天|次日"
)
CONCERN_RE = re.compile(r"担心|顾虑|会不会|能不能|不合适|怎么选|如何选|哪款")
ANSWER_RE = re.compile(r"建议|可以|支持|适合|不用担心|选择|因为|所以")


def _has_reusable_structure(body: str) -> bool:
    """判断一句话能否脱离上下文直接复用，而不只是字面完整或命中关键词。"""
    has_object = bool(PRODUCT_OBJECT_RE.search(body))
    has_attribute = bool(PRODUCT_ATTRIBUTE_RE.search(body))
    has_detail = bool(PRODUCT_DETAIL_RE.search(body))
    has_reason = bool(REASON_RE.search(body))
    has_action = bool(ACTION_RE.search(body))
    has_sequence = bool(SEQUENCE_RE.search(body))
    has_benefit = bool(CONCRETE_BENEFIT_RE.search(body))
    has_condition = bool(CONDITION_RE.search(body))

    explains_detail = bool(re.search(
        r"因为|所以|这样|相比|区别|好处|优势", body))
    product_explanation = has_object and has_attribute and has_reason and explains_detail
    concrete_instruction = has_object and has_action and (
        has_sequence
        or re.search(r"分别.{0,12}(?:选择|使用|安装|操作)", body)
        or (has_detail and re.search(r"分别|再选|按.{0,8}使用", body))
    )
    benefit_explanation = has_benefit and has_condition
    objection_answer = bool(CONCERN_RE.search(body) and ANSWER_RE.search(body)) and (
        has_object or has_detail
    )
    has_deadline = bool(re.search(
        r"\d{1,2}(?:点|时|:|：)|之前|以后|截止|准点|上午场|下午场|当天|次日",
        body,
    ))
    deadline_fulfilment = bool(
        re.search(r"发货|订单|下单|去拍|拍下", body)
        and has_deadline
        and re.search(r"商品|宝贝|订单|发货|链接", body)
    )
    return bool(product_explanation or concrete_instruction
                or benefit_explanation or objection_answer
                or deadline_fulfilment)


def _norm(text: str) -> str:
    return re.sub(r"[\s，。！？、；：,.!?;:]+", "", text or "").lower()


def quality_text_hash(text: str) -> str:
    return hashlib.sha256(_norm(text).encode("utf-8")).hexdigest()[:24]


def _rule_candidate(start_ms: int, end_ms: int, text: str, *,
                    min_score: int = MIN_RULE_SCORE,
                    min_chars: int = MIN_VISIBLE_CHARS) -> dict | None:
    body = re.sub(r"\s+", " ", str(text or "")).strip()
    if not min_chars <= len(body) <= MAX_VISIBLE_CHARS:
        return None
    from ..asr.clean import has_display_semantic_red_flag
    if has_display_semantic_red_flag(body):
        return None
    if body[0] in "，、；。！？,.!?;" or not re.search(r"[。！？；…]$", body):
        return None
    if NOISE_RE.search(body) or len(re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", body)) < 10:
        return None

    hits = {category: [word for word in words if word in body]
            for category, words in SIGNALS.items()}
    hits = {category: words for category, words in hits.items() if words}
    if not hits:
        return None

    category = max(hits, key=lambda key: (len(hits[key]), -QUALITY_CATEGORIES.index(key)))
    if not _has_reusable_structure(body):
        return None
    score = 24
    reasons: list[str] = []
    if 24 <= len(body) <= 140:
        score += 10
        reasons.append("表达完整")
    if len(hits) >= 2:
        score += 14
        reasons.append("动作链完整")
    score += min(18, sum(min(3, len(words)) for words in hits.values()) * 4)
    if any(word in body for word in EXPLANATION):
        score += 10
        reasons.append("有讲解逻辑")
    if CONCRETE_BENEFIT_RE.search(body) and CONDITION_RE.search(body):
        score += 12
        reasons.append("条件与权益完整")
    if re.search(r"[0-9零一二两三四五六七八九十百千万]+(?:元|瓦|毫安|号|年|次|个|件)?", body):
        score += 8
        reasons.append("信息具体")
    if any(noise in body for noise in GENERIC_NOISE):
        score -= 24
    if category == "互动承接" and len(hits) == 1:
        score -= 12
    if body.count("宝贝") + body.count("小宝") >= 4:
        score -= 8
    if score < min_score:
        return None
    if not reasons:
        reasons.append("场景动作明确")
    return {
        "start_ms": int(start_ms), "end_ms": int(end_ms), "text": body,
        "category": category, "rule_score": min(100, score),
        "rationale": reasons,
    }


def quality_candidate(start_ms: int, end_ms: int, text: str) -> dict | None:
    """数据榜可保留较短的当时上下文，但仍复用相同信号/噪声门禁。"""
    return _rule_candidate(start_ms, end_ms, text, min_score=28, min_chars=12)


def _llm_scores(candidates: list[dict], cfg: dict) -> dict[int, tuple[int, str, bool]]:
    llm = (cfg.get("llm") or {}) if cfg else {}
    if not str(llm.get("api_key") or "").strip():
        return {}
    from ..review.ai_report import chat_completion
    items = [{"id": index, "time_ms": row["start_ms"], "text": row["text"]}
             for index, row in enumerate(candidates)]
    system = (
        "你是直播话术评审员。只评价原话是否完整、具体、可复用，不根据销量"
        "推断效果。空泛召唤、单纯感谢、语义残缺应低于60分。"
        "只有不需要猜测缺失宾语、指代或数字单位的话术才可 usable=true；"
        "如‘买到70怎么用’‘这个那个’即使能猜出大意也必须为 false。返回严格JSON："
        '{"items":[{"id":0,"score":0,"category":"产品讲解","usable":false}]}。'
        "category只能是：" + "/".join(QUALITY_CATEGORIES) + "。不要改写原话。"
    )
    try:
        output = chat_completion(cfg, [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(items, ensure_ascii=False)},
        ], temperature=0.1, max_tokens=1800, thinking="disabled")
        match = re.search(r"\{.*\}", output or "", re.S)
        parsed = json.loads(match.group(0)) if match else {}
        result: dict[int, tuple[int, str, bool]] = {}
        for item in parsed.get("items") or []:
            index = int(item.get("id"))
            score = max(0, min(100, int(round(float(item.get("score"))))))
            category = str(item.get("category") or "")
            if 0 <= index < len(candidates) and category in QUALITY_CATEGORIES:
                usable = item.get("usable")
                result[index] = (score, category,
                                 usable if isinstance(usable, bool) else True)
        return result
    except Exception as exc:
        log.warning("优质话术批量评分失败，使用规则分: %s", exc)
        return {}


def select_quality_highlights(sentences: list[tuple[int, int, str]], cfg: dict,
                              top_n: int | None = None,
                              feedback: dict[str, dict] | None = None, *,
                              content_is_repaired: bool = False) -> list[dict]:
    """返回 3–5 条优质话术；LLM 只批量复核，失败时有确定性回退。"""
    desired = int(top_n or (cfg.get("highlight", {}) or {}).get("quality_top_n", 5))
    desired = max(1, min(5, desired))
    candidates = [row for start, end, text in sentences
                  if (row := _rule_candidate(start, end, text)) is not None]
    candidates.sort(key=lambda row: (-row["rule_score"], row["start_ms"]))
    candidates = candidates[:MAX_CANDIDATES_FOR_LLM]
    llm_scores = _llm_scores(candidates, cfg)
    for index, row in enumerate(candidates):
        llm = llm_scores.get(index)
        if llm:
            row["llm_score"], row["category"], row["semantic_usable"] = llm
            row["final_score"] = (round(row["rule_score"] * 0.55 + row["llm_score"] * 0.45)
                                  if row["semantic_usable"] else 0)
        else:
            row["llm_score"] = None
            row["semantic_usable"] = None
            row["final_score"] = row["rule_score"]
        fb = (feedback or {}).get(quality_text_hash(row["text"])) or {}
        rating = str(fb.get("rating") or "")
        adjustment = {"好": 8, "一般": -5, "误判": -40}.get(rating, 0)
        row["feedback_rating"] = rating
        row["feedback_adjustment"] = adjustment
        row["final_score"] = max(0, min(100, row["final_score"] + adjustment))
    candidates = [row for row in candidates if row["final_score"] >= MIN_FINAL_SCORE]
    candidates.sort(key=lambda row: (-row["final_score"], row["start_ms"]))

    selected: list[dict] = []
    category_counts: dict[str, int] = {}
    for row in candidates:
        if category_counts.get(row["category"], 0) >= 2:
            continue
        if any(abs(row["start_ms"] - old["start_ms"]) < MIN_TIME_GAP_MS
               for old in selected):
            continue
        if any(fuzz.ratio(_norm(row["text"]), _norm(old["text"])) >= 86
               for old in selected):
            continue
        selected.append(row)
        category_counts[row["category"]] = category_counts.get(row["category"], 0) + 1
        if len(selected) >= desired:
            break

    # 短场次可能无法满75秒间隔；第二轮只放宽时间，不放宽质量/重复门禁。
    for row in candidates:
        if row in selected or len(selected) >= desired:
            continue
        if any(fuzz.ratio(_norm(row["text"]), _norm(old["text"])) >= 86
               for old in selected):
            continue
        selected.append(row)

    out: list[dict] = []
    for rank, row in enumerate(sorted(selected, key=lambda item: -item["final_score"]), 1):
        out.append({
            "start_ms": row["start_ms"], "end_ms": row["end_ms"],
            "score": round(row["final_score"] / 100 * 6.5, 1),
            "reasons": [f"优质话术·{row['category']}"],
            "transcript": row["text"], "kind": "quality",
            "quality_meta": {
                "version": 1, "rank": rank,
                "display_repaired": bool(content_is_repaired),
                "category": row["category"],
                "quality_score": row["final_score"],
                "rule_score": row["rule_score"], "llm_score": row["llm_score"],
                "semantic_usable": row.get("semantic_usable"),
                "feedback_rating": row.get("feedback_rating") or "",
                "feedback_adjustment": row.get("feedback_adjustment") or 0,
                "rationale": row["rationale"],
            },
        })
    return out


def _quality_meta(highlight: dict) -> dict:
    meta = highlight.get("quality_meta") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (TypeError, ValueError):
            return {}
    return meta if isinstance(meta, dict) else {}


def quality_display_text(cfg: dict, highlight: dict, *, max_chars: int = 220,
                         assume_repaired: bool = False) -> str:
    """质量话术展示的唯一入口。

    新行若持久化了 ``display_repaired=true``，只做确定性截断和
    安全门禁；历史无标记行必须重新修复，失败即过滤，绝不回退原始 ASR。
    """
    from ..asr.clean import (DISPLAY_FALLBACK, can_show_raw_excerpt,
                             prepare_display_text, truncate_display_text)

    raw = str(highlight.get("transcript") or "").strip()
    repaired = assume_repaired or _quality_meta(highlight).get(
        "display_repaired") is True
    if repaired:
        body = truncate_display_text(raw, max_chars=max_chars)
        return (body if body != DISPLAY_FALLBACK
                and can_show_raw_excerpt(body) else "")
    body = prepare_display_text(raw, cfg, max_chars=max_chars)
    if body == DISPLAY_FALLBACK:
        return ""
    return body


def quality_library_entry(highlight: dict) -> tuple[str, str] | None:
    meta = _quality_meta(highlight)
    category = LIBRARY_CATEGORY.get(str(meta.get("category") or ""))
    text = str(highlight.get("transcript") or "").strip()
    return (category, text) if category and text else None
