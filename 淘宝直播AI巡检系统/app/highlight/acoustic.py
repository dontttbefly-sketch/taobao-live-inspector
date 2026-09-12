"""声学特征：音量峰值检测（主播情绪/氛围高光时刻的辅助信号）"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

log = logging.getLogger("highlight.acoustic")

# 分析窗口（秒）
FRAME_SEC = 1.0
# 高出均值多少个标准差算峰值
ZSCORE = 1.5
# 峰值窗口最大合并间隔（秒）
MERGE_GAP_SEC = 5.0


def _rms_energy(wav_path: Path) -> np.ndarray:
    import librosa
    y, sr = librosa.load(str(wav_path), sr=16000, mono=True)
    if len(y) == 0:
        return np.zeros(0)
    frame = int(sr * FRAME_SEC)
    rms = librosa.feature.rms(y=y, frame_length=frame, hop_length=frame)[0]
    return rms


def acoustic_peaks(wav_path: Path, zscore: float = ZSCORE) -> list[tuple[int, int]]:
    """返回音量显著高于整场基线的连续窗口 [(start_ms, end_ms)]"""
    try:
        rms = _rms_energy(wav_path)
    except Exception as e:
        log.warning("声学分析失败（%s）：%s", wav_path.name, e)
        return []
    if rms.size < 4:
        return []

    mean, std = float(rms.mean()), float(rms.std())
    if std < 1e-6:
        return []
    mask = rms > mean + zscore * std

    # 把连续 True 帧合并为窗口
    windows: list[tuple[int, int]] = []
    start = None
    for i, on in enumerate(mask):
        if on and start is None:
            start = i
        elif not on and start is not None:
            windows.append((start, i))
            start = None
    if start is not None:
        windows.append((start, len(mask)))

    # 小间隙合并
    merged: list[tuple[int, int]] = []
    for s, e in windows:
        if merged and (s - merged[-1][1]) * FRAME_SEC <= MERGE_GAP_SEC:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    return [(int(s * FRAME_SEC * 1000), int(e * FRAME_SEC * 1000)) for s, e in merged]
