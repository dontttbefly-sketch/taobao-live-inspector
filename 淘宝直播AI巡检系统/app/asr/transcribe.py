"""FunASR 语音转写：SenseVoice + VAD + 标点 + 逐句时间戳。

模型懒加载为单例，长驻内存；转写输出 (start_ms, end_ms, text) 列表。
主 SenseVoice 在 Apple Silicon 上自动尝试 MPS，VAD/标点固定 CPU 保证长直播稳定。
SenseVoice 本身不带时间戳/标点，现实现为：FSMN-VAD 定位语音 → SenseVoice 转写
→ 相邻语音块合并 → CT-Transformer 恢复标点 → 按完整句重新分配时间戳。
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("asr")

# SenseVoice 单例（模型 + VAD + 标点各一份）
_sv_model = None
_sv_vad = None
_sv_punc = None
_sv_model_key = ""
_SV = "iic/SenseVoiceSmall"
_ASR_LOCK = threading.RLock()


def _device_auto() -> str:
    import torch
    if torch.backends.mps.is_available():
        return "mps"          # macOS Apple Silicon
    if torch.cuda.is_available():
        return "cuda"         # Windows/Linux NVIDIA 独显
    return "cpu"              # 纯 CPU（Windows 无独显时，转写较慢，建议夜间批量）


def _sv_models(cfg: dict):
    """SenseVoice + VAD + 标点单例（进程内各一份）"""
    global _sv_model, _sv_vad, _sv_punc, _sv_model_key
    asr_cfg = cfg.get("asr", {})
    vad_id = asr_cfg.get("vad_model", "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch")
    punc_id = asr_cfg.get("punc_model", "iic/punc_ct-transformer_cn-en-common-vocab471067-large")
    device = asr_cfg.get("device", "auto")
    if device == "auto":
        device = _device_auto()
    # FSMN-VAD/CT-Transformer 在 Apple MPS 上连续处理长直播会出现严重性能退化；
    # 实测同一 10 分钟音频 VAD：MPS 后期十几分钟，CPU 约 1 秒。主 ASR 保留
    # MPS 加速，轻量 VAD/标点固定 CPU，整体速度和长期稳定性都更好。
    vad_device = asr_cfg.get("vad_device", "cpu")
    punc_device = asr_cfg.get("punc_device", "cpu")
    if vad_device == "auto":
        vad_device = "cpu"
    if punc_device == "auto":
        punc_device = "cpu"
    key = f"{vad_id}|{punc_id}|{device}|{vad_device}|{punc_device}"
    if _sv_model is None or _sv_model_key != key:
        from funasr import AutoModel
        t0 = time.time()
        try:
            _sv_model = AutoModel(model=_SV, trust_remote_code=True,
                                  disable_update=True, device=device)
        except Exception as exc:
            if device == "cpu":
                raise
            log.warning("SenseVoice 使用 %s 失败（%s），主模型回退 CPU", device, exc)
            device = "cpu"
            _sv_model = AutoModel(model=_SV, trust_remote_code=True,
                                  disable_update=True, device=device)
        try:
            _sv_vad = AutoModel(model=vad_id, disable_update=True, device=vad_device)
        except Exception as exc:
            if vad_device == "cpu":
                raise
            log.warning("VAD 使用 %s 失败（%s），回退 CPU", vad_device, exc)
            vad_device = "cpu"
            _sv_vad = AutoModel(model=vad_id, disable_update=True, device="cpu")
        try:
            _sv_punc = AutoModel(model=punc_id, disable_update=True, device=punc_device)
        except Exception as exc:
            if punc_device == "cpu":
                raise
            log.warning("标点模型使用 %s 失败（%s），回退 CPU", punc_device, exc)
            punc_device = "cpu"
            _sv_punc = AutoModel(model=punc_id, disable_update=True, device="cpu")
        _sv_model_key = key
        log.info("SenseVoice + VAD + 标点模型加载完成，耗时 %.1fs (asr=%s, vad=%s, punc=%s)",
                 time.time() - t0, device, vad_device, punc_device)
    return _sv_model, _sv_vad, _sv_punc


def _sv_clean(r) -> str:
    """剥掉 SenseVoice 输出的 <|zh|><|HAPPY|> 等富文本标记，返回纯文本"""
    import re
    text = r.get("text", "") if isinstance(r, dict) else str(r)
    text = re.sub(r"<\|[^>]*\|>", "", text)
    return text.strip()


def _group_vad_texts(rows: list[tuple[int, int, str]], max_chars: int = 420,
                     max_duration_ms: int = 60000, max_gap_ms: int = 1800
                     ) -> list[tuple[int, int, str]]:
    """把 VAD 短块合成语义上下文，避免在一句话中间直接断开。"""
    groups: list[tuple[int, int, str]] = []
    for start, end, text in rows:
        if not groups:
            groups.append((start, end, text))
            continue
        gs, ge, gt = groups[-1]
        gap = max(0, start - ge)
        if gap <= max_gap_ms and len(gt) + len(text) <= max_chars and end - gs <= max_duration_ms:
            groups[-1] = (gs, end, gt + text)
        else:
            groups.append((start, end, text))
    return groups


def _punc_texts(texts: list[str], punc_model) -> list[str]:
    """批量恢复标点；模型异常时保留原文，不丢转写。"""
    if not texts:
        return []
    try:
        result = punc_model.generate(input=texts, disable_pbar=True)
        if not isinstance(result, list):
            result = [result]
        import re
        out = []
        for original, item in zip(texts, result):
            value = item.get("text", "") if isinstance(item, dict) else str(item)
            value = value.strip()
            # CT-Transformer 对已含少量标点的 SenseVoice 文本偶尔叠加标点。
            value = re.sub(r"([，。！？；,.!?;])\1+", r"\1", value)
            value = re.sub(r"^[，、；,;\s]+", "", value)
            value = re.sub(r"[，,]([。！？；.!?;])", r"\1", value)
            out.append(value or original)
        if len(out) == len(texts):
            return out
    except Exception as exc:
        log.warning("标点恢复失败，保留未标点文本: %s", exc)
    return texts


def _split_semantic(start_ms: int, end_ms: int, text: str) -> list[tuple[int, int, str]]:
    """按句末标点切完整句，并按字符占比映射到原音频时间范围。"""
    from ..transcript_segments import split_timed_text
    return split_timed_text(start_ms, end_ms, text)


def _transcribe_sensevoice(wav_path: Path, cfg: dict) -> list[tuple[int, int, str]]:
    """SenseVoice 转写：FSMN-VAD 分段 → 批量切片转写 → 段级时间戳。"""
    import wave

    import numpy as np

    sv, vad, punc = _sv_models(cfg)
    with wave.open(str(wav_path), "rb") as wf:
        sr = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    vres = vad.generate(input=str(wav_path), disable_pbar=True)
    segs = []
    if vres and isinstance(vres[0], dict):
        segs = vres[0].get("value") or []
    if not segs:
        r = sv.generate(input=str(wav_path), language="zh", use_itn=True, disable_pbar=True)
        txt = _sv_clean(r[0] if isinstance(r, list) else r)
        punctuated = _punc_texts([txt], punc)[0] if txt else ""
        return _split_semantic(0, int(len(audio) / sr * 1000), punctuated)

    inputs = [audio[int(s * sr / 1000):int(e * sr / 1000)] for s, e in segs]
    t0 = time.time()
    res = sv.generate(input=inputs, language="zh", use_itn=True, batch_size_s=300,
                      disable_pbar=True)
    log.info("SenseVoice 转写完成 %s：%d 段，耗时 %.1fs",
             wav_path.name, len(segs), time.time() - t0)
    vad_rows: list[tuple[int, int, str]] = []
    for (s, e), r in zip(segs, res):
        txt = _sv_clean(r)
        if txt:
            vad_rows.append((int(s), int(e), txt))
    groups = _group_vad_texts(vad_rows)
    restored = _punc_texts([x[2] for x in groups], punc)
    sents: list[tuple[int, int, str]] = []
    for (start, end, _), text in zip(groups, restored):
        sents.extend(_split_semantic(start, end, text))
    log.info("语义重组完成：VAD %d 段 → 上下文 %d 组 → 完整句 %d 句",
             len(vad_rows), len(groups), len(sents))
    return sents


def transcribe_wav(wav_path: Path, cfg: dict) -> list[tuple[int, int, str]]:
    """转写 wav，返回 [(start_ms, end_ms, text), ...]"""
    model_name = str((cfg.get("asr", {}) or {}).get("model", "sensevoice")).lower()
    if model_name != "sensevoice":
        raise ValueError(f"不支持的 ASR 模型 {model_name!r}；生产链路仅允许 sensevoice")
    # SenseVoice/MPS 与 VAD/标点模型均为单例且不是并发安全对象。简报和下播
    # 流水线共用线程池时必须排队，否则会争用约 3.3GB 模型内存并导致 MPS 退化/崩溃。
    with _ASR_LOCK:
        return _transcribe_sensevoice(wav_path, cfg)


def transcribe_video(ffmpeg: str, video_path: Path, cfg: dict,
                     wav_dir: Path | None = None) -> tuple[list[tuple[int, int, str]], Path]:
    """转写视频：先抽 16k 单声道 wav 再转写，返回 (句子列表, wav路径)"""
    from ..recorder.recorder import extract_audio

    wav_dir = wav_dir or video_path.parent
    wav_path = wav_dir / (video_path.stem + ".wav")
    extract_audio(ffmpeg, video_path, wav_path)
    sentences = transcribe_wav(wav_path, cfg)
    return sentences, wav_path


def save_srt(sentences: list[tuple[int, int, str]], out_path: Path) -> None:
    """导出 SRT 字幕（方便人工核对转写质量）"""
    def ts(ms: int) -> str:
        h, rem = divmod(max(ms, 0), 3600000)
        m, rem = divmod(rem, 60000)
        s, ms2 = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms2:03d}"

    lines = []
    for i, (start, end, text) in enumerate(sentences, 1):
        lines.append(f"{i}\n{ts(start)} --> {ts(end)}\n{text}\n")
    out_path.write_text("\n".join(lines), encoding="utf-8")


def save_json(sentences: list[tuple[int, int, str]], out_path: Path) -> None:
    out_path.write_text(
        json.dumps([{"start_ms": s, "end_ms": e, "text": t} for s, e, t in sentences],
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
