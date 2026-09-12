"""话术库：归一化去重 + 入库 + 频次统计

去重策略：
1. 先按归一化文本精确匹配（同主播同类别）
2. 精确未命中时，用 rapidfuzz 与同主播同类别已有话术做模糊比对（阈值可配），
   相似则视为同一话术（更新频次、保留更完整原文）
3. 全部未命中才新增
"""
from __future__ import annotations

import logging
import re

from rapidfuzz import fuzz

from ..db import Store
from ..asr.clean import numbers_equivalent

log = logging.getLogger("talktrack.library")

# 归一化：去掉标点/空白/emoji/语气词，统一小写（英文）
PUNCT_RE = re.compile("[\\s，。！？、；：\"\"''（）【】《》.,!?;:'\"()\\[\\]<>…~～·-]+")
EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]")
FILLER = ("嗯", "啊", "呀", "哦", "呃", "哈", "哎", "就是", "然后", "对吧", "对不对")

# 模糊比对范围上限（只跟最近 N 条比，控制性能）
FUZZY_WINDOW = 500


def normalize(text: str) -> str:
    t = EMOJI_RE.sub("", text or "")
    t = PUNCT_RE.sub("", t)
    for f in FILLER:
        t = t.replace(f, "")
    return t.lower()


def add_to_library(store: Store, anchor_id: int, stream_id: int,
                   entries: list[tuple[str, str]], cfg: dict) -> tuple[int, int]:
    """入库。返回 (新增数, 命中重复更新数)"""
    threshold = float(cfg.get("talktrack", {}).get("dedupe_threshold", 0.85))
    added = updated = 0

    for category, text in entries:
        text = text.strip()
        if len(text) < 4 or len(text) > 600:  # 保留完整句；异常超长段仍排除
            continue
        norm = normalize(text)
        if not norm:
            continue

        # 1) 精确匹配
        row = store.query(
            "SELECT id FROM talktracks WHERE anchor_id=? AND category=? AND norm_text=?",
            (anchor_id, category, norm),
        )
        if row:
            store.upsert_talktrack(
                anchor_id, stream_id, category, text, norm,
                text_provenance="display_repaired")
            updated += 1
            continue

        # 2) 模糊比对（同主播同类别已有话术）
        existing = store.query(
            "SELECT id, text, norm_text FROM talktracks WHERE anchor_id=? AND category=? "
            "ORDER BY last_seen DESC LIMIT ?",
            (anchor_id, category, FUZZY_WINDOW),
        )
        dup = False
        for e in existing:
            # 价格/功率/型号/链接号不同时绝不能只因文本相似就合并。
            # 允许已经过实测的 ASR 数字还原（如 十4→14）。
            if (numbers_equivalent(text, e["text"]) and
                    fuzz.ratio(norm, normalize(e["text"])) >= threshold * 100):
                # 归入真正命中的历史归一键，否则会以新 norm 再建一条，失去模糊去重意义。
                store.upsert_talktrack(
                    anchor_id, stream_id, category, text, e["norm_text"],
                    text_provenance="display_repaired")
                updated += 1
                dup = True
                break
        if dup:
            continue

        # 3) 新增
        store.upsert_talktrack(
            anchor_id, stream_id, category, text, norm,
            text_provenance="display_repaired")
        added += 1

    log.info("话术入库完成：新增 %d 条，重复更新 %d 条", added, updated)
    return added, updated
