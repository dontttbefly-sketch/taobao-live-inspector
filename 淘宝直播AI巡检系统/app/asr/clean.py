"""转写文本展示补全：原始 ASR 保留，所有面向阅读的内容使用补全版。"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading

log = logging.getLogger("asr.clean")

DISPLAY_FALLBACK = "（该段转写待回听确认）"
OUTWARD_RAW_ASR = "raw_asr"
OUTWARD_DISPLAY_REPAIRED = "display_repaired"
OUTWARD_REVIEW_PLACEHOLDER = "【待回听确认】"
DISPLAY_REPAIR_VERSION = "display-v1"
MAX_DISPLAY_LINE_CHARS = 260   # 展示补全只接受短句；整段长 ASR 块一律拒绝
CHUNK_LINES = 60               # 长转写分块补全的行数（2026-08-04：降低整段被拒概率）
_DISPLAY_CACHE: dict[tuple[str, str, int | None], str] = {}
_DISPLAY_LOCK = threading.Lock()

_ALLOWED_LATIN = {"ok", "iphone", "pro", "max", "ipad", "usb", "type", "c",
                  "pd", "w", "kg", "ml", "cm", "magsafe", "bling", "pocket",
                  "promise", "plus", "mini", "ultra", "lite", "watch", "air",
                  "mate", "nova", "note", "vivo", "oppo", "huawei", "xiaomi",
                  "apple", "samsung", "android", "bluetooth", "wifi", "fastcharge",
                  "led", "oled", "amoled"}

DISPLAY_COMPLETE_PROMPT = (
    "你是直播语音转写修复助手。每行以稳定标识 [L01] 开头。请把每行 ASR 原文大胆修复为"
    "自然、完整、通顺的直播口语。\n"
    "规则：\n"
    "1. 对口音、方言、吞字、错字造成的含糊句，必须结合上下文与直播语境主动推断并还原成"
    "通顺普通话，不得保持原样；如『主播去输哈』『非常之貌美』这类含糊表达要还原为自然句子。\n"
    "2. 必须保留每个 [Lxx] 标识，数量、顺序、每行结构都不能改变。\n"
    "3. 不得改变任何数字、金额、时间、链接号、型号（如 105号、298）；"
    "不得新增编造的商品、价格、库存、优惠、活动、发货、退款或用户信息。\n"
    "4. 只有整行都是无上下文的乱码、确实无法推断原意时，才输出『[Lxx] 【待回听确认】』，"
    "否则必须给出还原后的句子。\n"
    "5. 每行必须是自然完整的句子，以中文句末标点收尾。\n"
    "6. 只输出修复后的 [Lxx] 行，不要解释。"
)


def _line_id(index: int) -> str:
    return f"L{index:03d}"


_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
# 数字单元：连续出现的数字类字符（阿拉伯/小数点/中文数字/中文单位）
# 整体为一个单元，覆盖中英混写："十4"、"三5万"、"1.2万"
_NUM_SEG_RE = re.compile(r"[0-9.零一二两三四五六七八九十百千万亿]+")


_CN_UNIT_ORDER = {"十": 1, "百": 2, "千": 3, "万": 4, "亿": 5}


def _cn_num_value(s: str) -> int | None:
    """纯中文数字串 → 数值："三万五千" → 35000；含非数字字符返回 None。

    支持口语省略尾单位：一百五=150、一千五=1500、一万五=15000
    （大单位后直接跟个位数字且该数字之后不再有更小单位时，按省略处理）。
    """
    chars = list(str(s))
    expanded: list[str] = []
    for index, ch in enumerate(chars):
        if (ch in _CN_DIGIT and _CN_DIGIT[ch] != 0
                and _is_omitted_tail(chars, index)):
            expanded.append(ch)
            expanded.append(_omitted_unit(chars, index))
        else:
            expanded.append(ch)
    return _parse_standard_cn("".join(expanded))


def _is_omitted_tail(chars: list[str], index: int) -> bool:
    """chars[index] 是个位数字：判断它是否为「大单位后的省略尾数」。

    例：一百五=150（省略『十』）、一千五=1500（省略『百』）、
    一万五=15000（省略『千』）；而『三万五千』『一百五十』不是省略。
    """
    big: str | None = None
    for j in range(index - 1, -1, -1):
        ch = chars[j]
        if ch in _CN_DIGIT:
            if _CN_DIGIT[ch] == 0:
                return False  # 中间有『零』显式空位 → 不省略（一百零五=105）
            continue
        if ch in _CN_UNIT_ORDER:
            big = ch
            if ch in ("百", "千", "万", "亿"):
                break
            return False  # 最近单位是『十』 → 正常（五十=50）
        break
    if big is None:
        return False
    # 向后看：后面若存在比 big 更小的单位，则本数字是正常位（三万五千=35000）
    for ch in chars[index + 1:]:
        if ch in _CN_UNIT_ORDER:
            if _CN_UNIT_ORDER[ch] < _CN_UNIT_ORDER[big]:
                return False
            break
    return True


def _omitted_unit(chars: list[str], index: int) -> str:
    """省略尾数补位：一百五 → 五+十；一千五 → 五+百；一万五 → 五+千。"""
    for j in range(index - 1, -1, -1):
        ch = chars[j]
        if ch in ("百", "千", "万", "亿"):
            return {"百": "十", "千": "百", "万": "千", "亿": "千万"}[ch]
    return "十"


def _parse_standard_cn(s: str) -> int | None:
    """标准中文数字解析（无省略形态）：三万五千 → 35000。"""
    total = section = num = 0
    for ch in s:
        if ch in _CN_DIGIT:
            num = _CN_DIGIT[ch]
        elif ch == "十":
            section += (num if num else 1) * 10
            num = 0
        elif ch == "百":
            section += (num if num else 1) * 100
            num = 0
        elif ch == "千":
            section += (num if num else 1) * 1000
            num = 0
        elif ch == "万":
            total += (section + num) * 10000
            section = num = 0
        elif ch == "亿":
            total = (total + section + num) * 100000000
            section = num = 0
        else:
            return None
    return total + section + num


def _mixed_digit_candidates(s: str) -> set[float]:
    """中文与阿拉伯混写的数字串 → 候选数值集合。

    "三5" → {35, 3.5}（"3.5万"的小数点被 ASR 吞掉）；"17" → {17}；
    只在中文与阿拉伯混写时才生成小数候选，纯阿拉伯不生成，防 17→1.7 误放行。
    """
    seq: list[str] = []
    has_cn = has_ar = False
    for ch in s:
        if ch in _CN_DIGIT or ch == "十":
            seq.append("1" if ch == "十" else str(_CN_DIGIT[ch]))
            has_cn = True
        elif ch.isdigit():
            seq.append(ch)
            has_ar = True
        elif ch in ".,":
            continue
        else:
            return set()
    if not seq:
        return set()
    joined = "".join(seq)
    out = {float(joined)}
    if has_cn and has_ar and len(seq) >= 2:
        out.add(float(joined[0] + "." + joined[1:]))
    return out


def _unit_candidates(unit: str) -> set[str]:
    """单个数字单元 → 十进制 digit 串候选集（用于等价比较）。

    三层：阿拉伯+中文单位（1.2万）→ 纯中文（三万五千）→
    混写+单位（三5万=3.5万被 ASR 吞点）→ 混写（十4、7 1）。
    """
    unit = unit.strip()
    if not unit:
        return set()
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([亿万千百十]*)", unit)
    if m and m.group(1):
        factor = 1.0
        for ch in (m.group(2) or ""):
            factor *= {"万": 10000, "千": 1000, "百": 100, "十": 10}[ch]
        return {str(int(float(m.group(1)) * factor))}
    m2 = re.fullmatch(r"([零一二两三四五六七八九十百千万亿]+)", unit)
    if m2:
        value = _cn_num_value(m2.group(1))
        if value is not None:
            return {str(value)}
    m3 = re.fullmatch(r"(.+?)([亿万千百十]+)", unit)
    if m3 and re.search(r"[0-9零一二两三四五六七八九十]", m3.group(1)):
        factor = 1.0
        for ch in m3.group(2):
            factor *= {"万": 10000, "千": 1000, "百": 100, "十": 10}[ch]
        return {str(int(v * factor)) for v in _mixed_digit_candidates(m3.group(1))}
    return {str(int(v)) for v in _mixed_digit_candidates(unit)}


def numbers_equivalent(source: str, target: str) -> bool:
    """数字保真比较（2026-08-04 升级）：允许 ASR 破坏的数值还原。

    允许：空格断裂（2 98→298）、中英混写还原（十4→14、三5万→三万五千）、
    粘连/拆分（7 172→71、72）；拒绝数值被改动（17→16、1.2万→12万）。
    单元数不同时按全部 digit 拼接比较，覆盖粘连/拆分场景。
    """
    def units(text: str) -> list[set[str]]:
        norm = re.sub(r"(?<=\d)\s+(?=\d)", "", text or "")
        return [_unit_candidates(u) for u in _NUM_SEG_RE.findall(norm)]

    s_units, d_units = units(source), units(target)
    if not s_units and not d_units:
        return True
    if not s_units or not d_units:
        return False
    if len(s_units) == len(d_units):
        # 空集单元（无法解析的中文数字片段）不参与判定
        return all((s & d) if (s or d) else True for s, d in zip(s_units, d_units))
    s_join = "".join(sorted("".join(sorted(c)) for c in s_units))
    d_join = "".join(sorted("".join(sorted(c)) for c in d_units))
    return s_join == d_join


# 历史内部调用/测试兼容名。新跨模块调用使用公开名。
_numbers_equivalent = numbers_equivalent


def _garbled_latin(text: str) -> bool:
    """连续 8 个以上非白名单拉丁字母视为乱码（如 TBAICKDXM）。

    2026-08-04 收紧：模型语义还原会新增/改写品牌词（bling bling、pocket、
    poet→pocket），4 字符阈值误杀太多，改为 8 字符以上才判乱码。
    """
    for run in re.findall(r"[A-Za-z]{8,}", text or ""):
        if run.lower() not in _ALLOWED_LATIN:
            return True
    return False


def _display_lines(text: str) -> list[str]:
    lines = [re.sub(r"\s+", " ", line).strip() for line in str(text or "").splitlines()]
    return [line for line in lines if line]


def _split_leading_ts(line: str) -> tuple[str | None, str]:
    """把行首 [MM:SS] / [HH:MM:SS] 时间戳与正文分开；时间戳由调用方展示，不要求模型重写。"""
    match = re.match(r"^(\[\d{1,2}:\d{2}(?::\d{2})?\])\s*(.*)$", line.strip())
    if match:
        return match.group(1), match.group(2).strip()
    return None, line.strip()


_DISPLAY_SEMANTIC_RED_FLAGS = (
    # 2026-08-05 已证实残句：宾语停在裸数字（「买到70。」），数字后
    # 只有句尾语气词仍属于同一残句。明确量词/对象（「买到3件」、
    # 「买到70号链接」）不属于该规则。
    re.compile(
        r"买到\s*(?:[0-9]+(?:\.[0-9]+)?|[零一二两三四五六七八九十百千万亿]+)"
        r"\s*(?:(?:哈|啊|呀|哦|呢|吧|啦|嘛|呐|哇|诶)\s*)*"
        r"(?=[，、；。！？：,.!?;:]|$)"
    ),
    # 保留「买到70号链接的产品后……」等有明确对象的完整句，只继续拦截
    # 已证实的「产品回来之后，三个步骤」残缺表面形式。
    re.compile(
        r"买到\s*(?:[0-9]+|[零一二两三四五六七八九十百千万亿]+)\s*"
        r"号链接的产品回来之后\s*[，,]\s*三个步骤"
    ),
)


def has_display_semantic_red_flag(text: str) -> bool:
    """检出形式合法但仍明显残缺的展示句；时间戳不参与判定。"""
    _timestamp, body = _split_leading_ts(str(text or "").strip())
    return any(pattern.search(body) for pattern in _DISPLAY_SEMANTIC_RED_FLAGS)


def _validate_display_lines(source: list[str], parsed: list[tuple[str, str]]) -> list[str]:
    problems: list[str] = []
    expected = [_line_id(index) for index in range(1, len(source) + 1)]
    actual = [line_id for line_id, _ in parsed]
    if actual != expected:
        problems.append("行标识或顺序不一致")
        return problems
    for index, (original, (_line_id_value, body)) in enumerate(zip(source, parsed), 1):
        body = body.strip()
        if not body:
            problems.append(f"第 {index} 行为空")
            continue
        if body == "【待回听确认】":
            continue
        source_ts, source_body = _split_leading_ts(original)
        body_ts, body_text = _split_leading_ts(body)
        # 时间戳由展示层自己拼回；模型输出带时间戳时必须一致，不带则不判错。
        if source_ts is not None and body_ts is not None and source_ts != body_ts:
            problems.append(f"第 {index} 行时间标记发生变化")
        if not numbers_equivalent(source_body, body_text):
            problems.append(f"第 {index} 行数字发生变化")
        if _garbled_latin(body_text):
            problems.append(f"第 {index} 行疑似乱码英文")
        if has_display_semantic_red_flag(body_text):
            problems.append(f"第 {index} 行仍含语义残句")
        if body_text[0] in "，、；。！？,.!?;":
            problems.append(f"第 {index} 行有异常句首标点")
        if re.search(r"([，、；。！？,.!?;])\1+", body_text):
            problems.append(f"第 {index} 行有重复标点")
        if not re.search(r"[。！？；…]$", body_text):
            problems.append(f"第 {index} 行未以完整句末标点结束")
    return problems


def can_show_raw_excerpt(row: str) -> bool:
    """只允许把干净、完整的短句展示为原文。

    长块、乱码、句首标点或未收尾的句子一律不展示，防止旧脏数据回流。
    """
    line = (row or "").strip()
    if not line or len(line) > MAX_DISPLAY_LINE_CHARS:
        return False
    _ts, body = _split_leading_ts(line)
    if not body or body[0] in "，、；。！？,.!?;":
        return False
    if has_display_semantic_red_flag(body):
        return False
    if _garbled_latin(body):
        return False
    if not re.search(r"[。！？；…]$", body):
        return False
    return True


def truncate_display_text(text: str, max_chars: int | None = None) -> str:
    """展示限长只在完整句末截断，不能截断半句话。"""
    if max_chars is None or len(text) <= max_chars:
        return text
    pieces = re.findall(r".+?(?:[。！？；…]+|$)", text, re.S)
    kept: list[str] = []
    used = 0
    for piece in pieces:
        if not re.search(r"[。！？；…]+$", piece):
            continue
        if used + len(piece) > max_chars:
            break
        kept.append(piece)
        used += len(piece)
    return "".join(kept).strip() or DISPLAY_FALLBACK


def _complete_display_chunk(cfg: dict, chunk_lines: list[str],
                            max_attempts: int = 3) -> list[tuple[str, str]] | None:
    """补全一块（≤60 行）：返回 [(Lxx, body)]；连续失败返回 None。

    分块后每块独立过结构/数字门禁，长转写不再整段被拒（2026-08-04）。
    行级降级：只有个别行不合格时，坏行标【待回听确认】、其余保留，
    好行占比不足一半或结构性问题（行标识乱序）才整块重试。
    """
    from ..review.ai_report import chat_completion
    request = "\n".join(f"[{_line_id(index)}] {line}"
                        for index, line in enumerate(chunk_lines, 1))
    for attempt in range(max_attempts):
        try:
            out = chat_completion(cfg, [
                {"role": "system", "content": DISPLAY_COMPLETE_PROMPT},
                {"role": "user", "content": request},
            ], temperature=0.1, max_tokens=min(4000, len(request) * 2 + 500),
                thinking="disabled")
            parsed: list[tuple[str, str]] = []
            for line in _display_lines(out):
                match = re.fullmatch(r"\[(L\d{3})\]\s*(.+)", line)
                if not match:
                    parsed = []
                    break
                parsed.append((match.group(1), match.group(2).strip()))
            if not parsed or len(parsed) != len(chunk_lines):
                continue
            problems = _validate_display_lines(chunk_lines, parsed)
            if not problems:
                return parsed
            line_problems = [p for p in problems if re.match(r"^第 \d+ 行", p)]
            if len(line_problems) != len(problems):
                continue  # 结构性问题（行标识/顺序），整块重试
            bad = {int(re.search(r"^第 (\d+) 行", p).group(1)) for p in line_problems}
            ok_ratio = (len(parsed) - len(bad)) / len(parsed)
            if ok_ratio >= 0.5:
                return [(lid, "【待回听确认】" if i in bad else body)
                        for i, (lid, body) in enumerate(parsed, 1)]
        except Exception as exc:
            log.warning("展示语义补全块异常: %s", exc)
    return None


def _prepare_display_bodies(source: list[str], cfg: dict) -> list[str]:
    """按60行分块补全，为每个输入行返回且只返回一个展示正文。"""
    if not source:
        return []
    bodies = ["【待回听确认】"] * len(source)
    repairable = [(index, text) for index, text in enumerate(source)
                  if text and text.strip()]
    if not repairable:
        return bodies
    repairable_lines = [text for _, text in repairable]
    repaired_bodies: list[str] = []
    ok_chunks = 0
    total_chunks = (len(repairable_lines) + CHUNK_LINES - 1) // CHUNK_LINES
    for start in range(0, len(repairable_lines), CHUNK_LINES):
        chunk = repairable_lines[start:start + CHUNK_LINES]
        parsed = _complete_display_chunk(cfg, chunk)
        if parsed is None or len(parsed) != len(chunk):
            repaired_bodies.extend("【待回听确认】" for _ in chunk)
        else:
            ok_chunks += 1
            repaired_bodies.extend(body for _, body in parsed)
    if ok_chunks == 0:
        log.warning("展示语义补全全部分块失败，改为待回听提示（%d 行）", len(source))
    elif ok_chunks * 2 < total_chunks:
        # 通过率不足一半时不向下游暴露残缺的修复结果。
        log.warning("展示语义补全分块通过率过低（%d/%d），整段降级待回听",
                    ok_chunks, total_chunks)
        return ["【待回听确认】"] * len(source)
    elif total_chunks > 1:
        log.info("展示语义补全分块完成：%d/%d 块通过", ok_chunks, total_chunks)
    for (index, _text), body in zip(repairable, repaired_bodies):
        bodies[index] = body
    return bodies


def prepare_display_sentences(
        sentences: list[tuple[int, int, str]], cfg: dict,
) -> list[tuple[int, int, str]]:
    """返回可展示的语义补全句子，保持原数量、顺序和时间戳不变。"""
    if not sentences:
        return []
    bodies = _prepare_display_bodies([text for _, _, text in sentences], cfg)
    return [
        (start_ms, end_ms, body)
        for (start_ms, end_ms, _raw), body in zip(sentences, bodies)
    ]


def prepare_display_text(text: str, cfg: dict, max_chars: int | None = None,
                         line_limit: int | None = MAX_DISPLAY_LINE_CHARS) -> str:
    """返回可对外展示的语义补全版；不合格或超长块绝不回退展示原始 ASR。

    长转写（>CHUNK_LINES 行）分块补全：每块独立过门禁，失败块降级为
    『待回听确认』，不拖垮整段（DeepSeek 不稳定期提升简报 AI 出勤率）。
    """
    source = _display_lines(text)
    if not source:
        return DISPLAY_FALLBACK
    if line_limit and any(len(line) > line_limit for line in source):
        log.warning("展示语义补全拒绝超长块（%d 行，最大 %d 字符）",
                    len(source), line_limit)
        return DISPLAY_FALLBACK
    raw = "\n".join(source)
    llm = (cfg.get("llm") or {}) if cfg else {}
    if not str(llm.get("api_key") or "").strip():
        llm = ((cfg.get("talktrack", {}) or {}).get("llm") or {}) if cfg else {}
    if not str(llm.get("api_key") or "").strip():
        return DISPLAY_FALLBACK
    fingerprint = f"{llm.get('provider', '')}|{llm.get('base_url', '')}|{llm.get('model', '')}"
    cache_key = (hashlib.sha256(raw.encode("utf-8")).hexdigest(), fingerprint, max_chars)
    with _DISPLAY_LOCK:
        cached = _DISPLAY_CACHE.get(cache_key)
    if cached is not None:
        return cached

    bodies = _prepare_display_bodies(source, cfg)
    if all(body == "【待回听确认】" for body in bodies):
        result = DISPLAY_FALLBACK
    else:
        result = truncate_display_text("\n".join(bodies), max_chars)

    with _DISPLAY_LOCK:
        _DISPLAY_CACHE[cache_key] = result
    return result


def outward_display_text(
        text: str, cfg: dict, *, provenance: str,
        max_chars: int | None = None, placeholder: str = "",
) -> str:
    """唯一外发文本门禁：显式来源、失败关闭，绝不回退原始 ASR。

    ``raw_asr`` 必须先通过语义修复；``display_repaired`` 只做确定性完整句
    门禁与限长。未知来源一律按失败处理。调用方可选择过滤（空字符串）或
    明确显示 ``【待回听确认】``，但不能把输入原文作为兜底。
    """
    raw = str(text or "").strip()
    if not raw:
        return placeholder
    source = str(provenance or "").strip()
    if source == OUTWARD_RAW_ASR:
        body = prepare_display_text(raw, cfg, max_chars=max_chars)
    elif source == OUTWARD_DISPLAY_REPAIRED:
        body = truncate_display_text(raw, max_chars)
        if body != DISPLAY_FALLBACK and not can_show_raw_excerpt(body):
            body = DISPLAY_FALLBACK
    else:
        body = DISPLAY_FALLBACK
    return placeholder if body == DISPLAY_FALLBACK else body


def outward_record_text(
        cfg: dict, record, *, field: str = "transcript",
        meta_field: str = "", max_chars: int | None = None,
        placeholder: str = "",
) -> str:
    """正式读取只接受已持久化的展示语料，未知/raw 来源直接失败关闭。"""
    try:
        source = dict(record)
    except (TypeError, ValueError):
        source = record if isinstance(record, dict) else {}
    provenance = str(source.get("text_provenance") or "")
    if not provenance and meta_field:
        meta = source.get(meta_field) or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (TypeError, ValueError):
                meta = {}
        if isinstance(meta, dict):
            provenance = str(meta.get("text_provenance") or "")
            if not provenance and meta.get("display_repaired") is True:
                provenance = OUTWARD_DISPLAY_REPAIRED
    if provenance != OUTWARD_DISPLAY_REPAIRED:
        return placeholder
    return outward_display_text(
        str(source.get(field) or ""), cfg,
        provenance=OUTWARD_DISPLAY_REPAIRED,
        max_chars=max_chars, placeholder=placeholder,
    )


def persisted_display_record_text(
        cfg: dict, record, *, field: str = "transcript",
        meta_field: str = "", max_chars: int | None = None,
        placeholder: str = "",
) -> str:
    """正式报告只读已持久化的修复文本，历史 raw 不在读阶段重跑 AI。"""
    try:
        source = dict(record)
    except (TypeError, ValueError):
        source = record if isinstance(record, dict) else {}
    provenance = str(source.get("text_provenance") or "")
    if not provenance and meta_field:
        meta = source.get(meta_field) or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (TypeError, ValueError):
                meta = {}
        if isinstance(meta, dict):
            provenance = str(meta.get("text_provenance") or "")
            if not provenance and meta.get("display_repaired") is True:
                provenance = OUTWARD_DISPLAY_REPAIRED
    if provenance != OUTWARD_DISPLAY_REPAIRED:
        return placeholder
    return outward_display_text(
        str(source.get(field) or ""), cfg,
        provenance=OUTWARD_DISPLAY_REPAIRED,
        max_chars=max_chars, placeholder=placeholder,
    )


# 兼容既有调用：此函数用于内部模型输入，失败时仍保留原文以免中断流水线。
def clean_text_chunk(text: str, cfg: dict, max_retries: int = 1,
                     mode: str = "complete") -> str:
    """整理/补全内部模型输入；外部展示必须使用 prepare_display_text。"""
    if not text or len(text) < 20:
        return text
    from ..review.ai_report import chat_completion

    prompt = DISPLAY_COMPLETE_PROMPT if mode == "complete" else (
        "请只修正标点、断句和明显错别字，不改数字或语义。保留每行 [Lxx] 标识并输出完整句。"
    )
    source = _display_lines(text)
    request = "\n".join(f"[{_line_id(index)}] {line}" for index, line in enumerate(source, 1))
    for attempt in range(max_retries + 1):
        try:
            out = chat_completion(cfg, [
                {"role": "system", "content": prompt},
                {"role": "user", "content": request},
            ], temperature=0.2 if mode == "complete" else 0.1,
                max_tokens=min(4000, len(request) * 2 + 500),
                thinking="disabled")
            parsed = []
            for line in _display_lines(out):
                match = re.fullmatch(r"\[(L\d{3})\]\s*(.+)", line)
                if not match:
                    parsed = []
                    break
                parsed.append((match.group(1), match.group(2).strip()))
            if parsed and not _validate_display_lines(source, parsed):
                return "\n".join(body for _, body in parsed)
        except Exception as exc:
            log.warning("内部语义补全第 %d 次失败：%s", attempt + 1, exc)
    return text


def clean_sentences(sentences: list[tuple[int, int, str]], cfg: dict,
                    chunk_chars: int = 2500) -> list[tuple[int, int, str]]:
    """兼容批量内部整理；原始时间戳保持不变。"""
    if not sentences:
        return sentences
    out: list[tuple[int, int, str]] = []
    for start_ms, end_ms, text in sentences:
        cleaned = clean_text_chunk(text, cfg)
        out.append((start_ms, end_ms, cleaned or text))
    return out
