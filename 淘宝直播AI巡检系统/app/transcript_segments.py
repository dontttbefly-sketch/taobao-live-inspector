"""统一转写的完整句切分。

不依赖任何转写供应商；飞书妙记主链路与 FunASR 备胎共用。
"""
from __future__ import annotations

import re


def split_timed_text(start_ms: int, end_ms: int,
                     text: str) -> list[tuple[int, int, str]]:
    """只在明确句末标点处切分，按字符比例分配原时间窗。

    函数不改字、不补标点；只有一句或无句末标点时保持原段。
    """
    clean = " ".join(str(text or "").split()).strip()
    if not clean:
        return []
    pieces = [piece.strip() for piece in
              re.findall(r".+?(?:[。！？!?；;]+|$)", clean, re.S)
              if piece.strip()]
    if len(pieces) <= 1:
        return [(int(start_ms), int(end_ms), clean)]

    weights = [max(1, len(re.sub(r"\s+", "", piece))) for piece in pieces]
    total_weight = sum(weights)
    duration = max(1, int(end_ms) - int(start_ms))
    cursor = int(start_ms)
    consumed = 0
    rows: list[tuple[int, int, str]] = []
    for index, (piece, weight) in enumerate(zip(pieces, weights)):
        consumed += weight
        boundary = (int(end_ms) if index == len(pieces) - 1 else
                    int(start_ms) + round(duration * consumed / total_weight))
        boundary = max(cursor + 1, boundary)
        rows.append((cursor, boundary, piece))
        cursor = boundary
    return rows
