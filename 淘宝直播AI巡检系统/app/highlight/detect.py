"""高亮切片生成：关键词信号 + 声学峰值融合评分

流程：
1. 关键词命中按 merge_window 聚合成候选切片（含类别标签）
2. 声学峰值窗口与切片重叠则加分，独立峰值窗口生成低分切片
3. 每个切片附上覆盖的转写文本（供话术抽取与复盘查看）
"""
from __future__ import annotations

import logging
from pathlib import Path

from .keywords import keyword_hits
from .acoustic import acoustic_peaks

log = logging.getLogger("highlight")


def detect_highlights(sentences: list[tuple[int, int, str]], wav_path: Path | None,
                      cfg: dict) -> list[dict]:
    hl_cfg = cfg.get("highlight", {})
    categories = hl_cfg.get("categories", {})
    merge_window_ms = int(hl_cfg.get("merge_window", 30)) * 1000
    max_duration_ms = int(hl_cfg.get("max_duration", 45)) * 1000

    # ---- 关键词聚合 ----
    hits = keyword_hits(sentences, categories)
    segments: list[dict] = []
    for h in sorted(hits, key=lambda x: x["start_ms"]):
        can_merge = (
            segments
            and h["start_ms"] <= segments[-1]["end_ms"] + merge_window_ms
            and max(segments[-1]["end_ms"], h["end_ms"]) - segments[-1]["start_ms"] <= max_duration_ms
        )
        if can_merge:
            seg = segments[-1]
            seg["end_ms"] = max(seg["end_ms"], h["end_ms"])
            seg["reasons"].append(h["category"])
            seg["keywords"].extend(h["keywords"])
            seg["hits"] += 1
        else:
            segments.append({
                "start_ms": h["start_ms"],
                "end_ms": h["end_ms"],
                "reasons": [h["category"]],
                "keywords": list(h["keywords"]),
                "hits": 1,
                "score": 1.0,
            })

    for seg in segments:
        # 去重原因标签
        seg["reasons"] = list(dict.fromkeys(seg["reasons"]))
        seg["keywords"] = list(dict.fromkeys(seg["keywords"]))
        # 命中越多分越高，封顶 5
        seg["score"] = min(5.0, 1.0 + 0.5 * (seg["hits"] - 1))

    # ---- 声学峰值融合 ----
    aco = acoustic_peaks(wav_path) if wav_path and wav_path.exists() else []
    used_peak_indices: set[int] = set()
    boosted_segments: set[int] = set()
    for peak_index, (s, e) in enumerate(aco):
        for segment_index, seg in enumerate(segments):
            if s <= seg["end_ms"] and e >= seg["start_ms"]:  # 有重叠
                if segment_index not in boosted_segments:
                    seg["reasons"].append("声学峰值")
                    seg["score"] = min(6.0, seg["score"] + 0.5)
                    boosted_segments.add(segment_index)
                used_peak_indices.add(peak_index)
                break
    # 独立的声学峰值窗口 -> 低分高亮
    for i, (s, e) in enumerate(aco):
        if i in used_peak_indices:
            continue
        if any(seg["start_ms"] <= e and seg["end_ms"] >= s for seg in segments):
            continue
        segments.append({
            "start_ms": s, "end_ms": e,
            "reasons": ["声学峰值"], "keywords": [], "hits": 0, "score": 0.8,
        })

    # ---- 附转写文本 ----
    for seg in segments:
        seg["reasons"] = list(dict.fromkeys(seg["reasons"]))
        seg["transcript"] = "\n".join(
            t for st, en, t in sentences if st <= seg["end_ms"] and en >= seg["start_ms"]
        )

    segments.sort(key=lambda x: -x["score"])
    log.info("高亮检测完成：%d 个切片", len(segments))
    return segments
