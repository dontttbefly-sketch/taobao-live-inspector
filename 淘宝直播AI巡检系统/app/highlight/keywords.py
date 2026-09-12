"""关键词规则：按配置词典匹配转写句子，产出高亮信号"""
from __future__ import annotations

import logging

log = logging.getLogger("highlight")


def keyword_hits(sentences: list[tuple[int, int, str]], categories: dict[str, list[str]],
                 ) -> list[dict]:
    """句子列表 -> 关键词命中信号（保留完整原句供简报直接引用）。"""
    hits: list[dict] = []
    for start, end, text in sentences:
        for cat, words in categories.items():
            found = [w for w in words if w and w in text]
            if found:
                hits.append({
                    "start_ms": start,
                    "end_ms": end,
                    "category": cat,
                    "keywords": found,
                    "text": text,
                })
    return hits
