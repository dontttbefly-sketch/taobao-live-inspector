"""话术分类：规则为主，可选 LLM 增强

规则分类（按优先级）：
1. 开播前 opening_window_sec 内的高亮 -> 「开场」
2. 句子命中 逼单/催付/福利/产品 关键词 -> 对应类别
3. 疑问句式（为什么/怎么/适合/尺码...）-> 「答疑」
4. 其余不强行入库，保证话术库精度
LLM 模式（配置了 api_key 才启用）：对每个高亮切片提取金句并分类，与规则结果合并
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger("talktrack.classify")

QUESTION_WORDS = (
    "为什么", "怎么", "怎么办", "如何", "可以吗", "能用", "适合", "尺码", "色号",
    "保质期", "怎么洗", "怎么用", "吗？", "吗?", "呢", "能退", "退换", "运费", "发货",
    "是正品", "有货", "什么时候",
)

# 优先级高的类别在前（句子命中多类时取第一个）
PRIORITY = ("逼单", "催付", "福利", "产品", "互动")


def classify_sentence(text: str, categories: dict[str, list[str]]) -> str | None:
    for cat in PRIORITY:
        words = categories.get(cat, [])
        if any(w and w in text for w in words):
            return cat
    return None


def is_question(text: str) -> bool:
    return any(w in text for w in QUESTION_WORDS)


def extract_entries(highlights: list[dict], cfg: dict) -> list[tuple[str, str]]:
    """规则分类：从高亮切片提取 (category, text) 话术条目"""
    hl_cfg = cfg.get("highlight", {})
    categories = hl_cfg.get("categories", {})
    opening_ms = int(cfg.get("talktrack", {}).get("opening_window_sec", 180)) * 1000
    # 开场只认整场第一段高亮（避免把开场 3 分钟内的所有高亮都误判成开场）
    first_start = min((h.get("start_ms", 0) for h in highlights), default=0) if highlights else 0

    entries: list[tuple[str, str]] = []
    for hl in highlights:
        lines = [ln.strip() for ln in (hl.get("transcript") or "").splitlines() if ln.strip()]
        for i, line in enumerate(lines):
            cat = classify_sentence(line, categories)
            if cat:
                entries.append((cat, line))
            elif hl.get("start_ms", 0) == first_start and hl.get("start_ms", 0) < opening_ms and i == 0:
                entries.append(("开场", line))
            elif is_question(line):
                entries.append(("答疑", line))
    return entries


# ---------- LLM 增强（可选） ----------
def extract_entries_llm(highlights: list[dict], cfg: dict) -> list[tuple[str, str]]:
    """调用 LLM 提取金句并分类；未配置或失败返回空列表（不阻断规则流程）"""
    llm = cfg.get("talktrack", {}).get("llm", {}) or {}
    api_key = (llm.get("api_key") or "").strip()
    if not api_key:
        return []

    provider = llm.get("provider", "deepseek")
    base_url = llm.get("base_url", "https://api.deepseek.com")
    if provider == "dashscope":
        base_url = base_url or "https://dashscope.aliyuncs.com/compatible-mode/v1"
    url = base_url.rstrip("/") + "/chat/completions"
    model = llm.get("model", "deepseek-chat")

    import requests
    sys_prompt = (
        "你是直播电商运营专家。给定一段主播高光时刻的转写文本，"
        "提取其中值得收入话术库的「金句」（逼单、催付、福利、产品介绍、互动等），"
        "每句一行输出，格式：类别|话术原文。类别只能是：逼单/催付/福利/产品/互动/开场/答疑/其他。"
        "只输出符合格式的行，不要编号和解释。"
    )

    entries: list[tuple[str, str]] = []
    try:
        from ..asr.clean import prepare_display_text
        for hl in highlights[:20]:  # 每场最多处理 20 个切片，控成本
            text = prepare_display_text(hl.get("transcript") or "", cfg, max_chars=1500)
            if text == "（该段转写待回听确认）" or len(text) < 10:
                continue
            resp = requests.post(url, headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }, json={
                "model": model,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": text[:1500]},
                ],
                "temperature": 0.2,
            }, timeout=60)
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            for line in content.splitlines():
                line = line.strip().lstrip("-*0123456789. ")
                if "|" in line:
                    cat, _, body = line.partition("|")
                    cat = cat.strip()
                    body = body.strip()
                    if cat in ("逼单", "催付", "福利", "产品", "互动", "开场", "答疑", "其他") and body:
                        entries.append((cat, body))
    except Exception as e:
        log.warning("LLM 话术抽取失败，回退规则结果: %s", e)
    return entries
