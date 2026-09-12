"""分析流水线编排：转写 -> 高亮检测 + 话术入库 -> 复盘报告

各步骤可单独重跑（失败后置 status=failed，脚本可 --retry）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .config import local_epoch_ms, resolve
from .db import Store
from .asr.transcribe import transcribe_video, save_srt, save_json
from .highlight.detect import detect_highlights
from .talktrack.library import add_to_library
from .review.report import generate_stream_report

log = logging.getLogger("pipeline")

STEPS = ("transcribed", "analyzed", "reported")


def _video_path(stream) -> Path:
    return Path(stream["file_path"])


def _wav_path(stream) -> Path:
    return _video_path(stream).with_suffix(".wav")


def _try_reuse_brief(store: Store, stream_id: int, video: Path) -> list[tuple[int, int, str]] | None:
    """尝试复用简报/分段转写（已带全场偏移时间戳）。
    校验：转写覆盖时长与视频总时长偏差 < 60 秒（分片文件合并后已清理，不依赖分片存在）。
    返回 None 表示不可复用（无数据/偏差大 → 走完整转写）"""
    rows = store.get_brief_transcripts(stream_id)
    if not rows:
        return None
    sentences = [(r["start_ms"], r["end_ms"], r["text"]) for r in rows]
    total_ms = max((e for _, e, _ in sentences), default=0)
    try:
        import subprocess
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, timeout=30)
        video_ms = float(out.stdout.strip()) * 1000
        if abs(total_ms - video_ms) > 60000:
            return None  # 覆盖偏差过大，回退完整转写
    except Exception:
        return None
    log.info("场次 #%d 复用分段转写：%d 句", stream_id, len(sentences))
    return sentences


def transcribe_stream(cfg: dict, store: Store, stream_id: int) -> bool:
    # failed 先由 process_stream 按 failed_stage 回退；本阶段只领取 recorded。
    if not store.claim_stream(stream_id, ("recorded",), "transcribing"):
        return False
    stream = store.get_stream(stream_id)
    video = _video_path(stream)
    if not video.exists():
        store.set_stream_status(stream_id, "failed", error=f"录像文件不存在: {video}")
        return False
    ffmpeg = cfg["recorder"].get("ffmpeg", "ffmpeg")
    try:
        reused = False
        sentences = _try_reuse_brief(store, stream_id, video)
        if sentences is None:
            if (cfg.get("transcription", {}) or {}).get("enabled"):
                raise RuntimeError(
                    "统一转写覆盖与录像时长不一致；已禁止复盘自动调用 FunASR，请检查妙记任务")
            sentences, wav = transcribe_video(ffmpeg, video, cfg)
            save_srt(sentences, video.with_suffix(".srt"))
            save_json(sentences, video.with_suffix(".transcript.json"))
        else:
            reused = True
        store.save_transcripts(stream_id, sentences)  # 幂等：先清旧再写
        store.set_stream_status(stream_id, "transcribed")
        log.info("场次 #%d 转写完成：%d 句%s", stream_id, len(sentences),
                 "（复用简报转写）" if reused else "")
        return True
    except Exception as e:
        store.set_stream_failed(stream_id, "transcribe", f"转写失败: {e}")
        log.exception("场次 #%d 转写失败", stream_id)
        return False


def analyze_stream(cfg: dict, store: Store, stream_id: int) -> bool:
    if not store.claim_stream(stream_id, ("transcribed",), "analyzing"):
        return False
    stream = store.get_stream(stream_id)
    sentences = [(t["start_ms"], t["end_ms"], t["text"]) for t in store.get_transcripts(stream_id)]
    anchor_id = stream["anchor_id"]
    try:
        wav = _wav_path(stream)
        internal_signals = detect_highlights(sentences, wav if wav.exists() else None, cfg)
        unified = bool((cfg.get("transcription", {}) or {}).get("enabled"))
        display_sentences = store.get_persisted_display_corpus(stream_id)
        if unified:
            # 妙记或人工确认的 FunASR 原文直接入展示语料，不再调用 DeepSeek 补错字。
            display_sentences = list(sentences)
            store.save_display_transcripts(stream_id, display_sentences)
        elif display_sentences is None:
            from .asr.clean import prepare_display_sentences
            display_sentences = prepare_display_sentences(sentences, cfg)
            store.save_display_transcripts(stream_id, display_sentences)
        else:
            log.info("场次 #%d 复用已持久化展示语料：%d 句",
                     stream_id, len(display_sentences))
        from .highlight.quality import quality_library_entry, select_quality_highlights
        quality_cfg = cfg
        if unified:
            quality_cfg = dict(cfg)
            quality_cfg["llm"] = {}
        quality_highlights = select_quality_highlights(
            display_sentences, quality_cfg, feedback=store.get_highlight_feedback(),
            content_is_repaired=True)
        rows = [{
            "stream_id": stream_id,
            "anchor_id": anchor_id,
            "start_ms": h["start_ms"],
            "end_ms": h["end_ms"],
            "score": h["score"],
            "reasons": json.dumps(h["reasons"], ensure_ascii=False),
            "transcript": h["transcript"],
            "kind": "internal_signal",
        } for h in internal_signals]
        rows.extend({
            "stream_id": stream_id,
            "anchor_id": anchor_id,
            **h,
            "reasons": json.dumps(h["reasons"], ensure_ascii=False),
        } for h in quality_highlights)
        store.save_highlights(rows, stream_id=stream_id)

        # 话术库只接收精选榜；关键词/音量只是内部召回信号，
        # 不再把空泛召唤和背景对话大量灌入历史库。
        store.clear_stream_talktracks(stream_id)
        entries = [entry for h in quality_highlights
                   if (entry := quality_library_entry(h)) is not None]
        add_to_library(store, anchor_id, stream_id, entries, cfg)

        store.set_stream_status(stream_id, "analyzed")
        log.info("场次 #%d 分析完成：内部信号 %d，优质话术 %d 条",
                 stream_id, len(internal_signals), len(entries))
        return True
    except Exception as e:
        store.set_stream_failed(stream_id, "analyze", f"分析失败: {e}")
        log.exception("场次 #%d 分析失败", stream_id)
        return False


def review_stream(cfg: dict, store: Store, stream_id: int,
                  notify_card: bool = False) -> bool:
    if not store.claim_stream(stream_id, ("analyzed",), "reporting"):
        return False
    stream = store.get_stream(stream_id)
    try:
        # 下播后优先抓「场次分析」冻结行；尚未结算时才回退为本地时段序列/累计快照差值。
        # 用场次固化的 live_id（防自动切换后串到新场次数据）。
        from .metrics.qianniu import fetch_and_save, check_metrics_health
        live_id = (stream["live_id"] or (cfg.get("taobao", {}) or {}).get("live_id", ""))
        data_problems: list[str] = []
        if live_id:
            start_ms = local_epoch_ms(stream["started_at"])
            end_ms = local_epoch_ms(stream["ended_at"])
            metrics = fetch_and_save(cfg, store, stream_id, live_id,
                                     start_ms=start_ms, end_ms=end_ms)
            # 数据质量门禁：异常时报告明确标注，AI 不得把缺失/部分数据写成事实。
            data_problems = check_metrics_health(metrics, (stream["duration_sec"] or 0) / 60)
            if data_problems:
                log.warning("场次 #%d 经营数据校验异常: %s", stream_id, "；".join(data_problems))
            # 数据驱动高亮：以成交/点击/在线峰值为锚点抓峰值时段话术（用户需求 2026-08-03）
            if metrics:
                try:
                    from .highlight.peak import detect_data_peaks
                    sentences = store.get_display_sentences(stream_id)
                    peak_hls = detect_data_peaks(
                        metrics, sentences, recording_start_ms=start_ms,
                        categories=(cfg.get("highlight", {}) or {}).get("categories", {}),
                        recording_end_ms=end_ms,
                        duration_sec=stream["duration_sec"],
                        text_provenance="display_repaired",
                    )
                    store.delete_peak_highlights(stream_id)
                    if peak_hls:
                        store.append_highlights([{
                            "stream_id": stream_id,
                            "anchor_id": stream["anchor_id"],
                            "start_ms": h["start_ms"],
                            "end_ms": h["end_ms"],
                            "score": h["score"],
                            "reasons": json.dumps(h["reasons"], ensure_ascii=False),
                            "transcript": h["transcript"],
                            "kind": "data_association",
                            "peak_meta": h.get("peak_meta") or {},
                        } for h in peak_hls])
                        log.info("场次 #%d 数据关联高亮 %d 个", stream_id, len(peak_hls))
                except Exception:
                    log.exception("场次 #%d 数据高亮失败（不影响报告）", stream_id)

        generate_stream_report(cfg, store, stream_id, data_problems=data_problems)
        store.set_stream_status(stream_id, "reported")
        log.info("场次 #%d 复盘报告完成", stream_id)
        if notify_card:
            log.warning("场次 #%d 请求了技术碎片通知，第一阶段已禁用", stream_id)
        return True
    except Exception as e:
        store.set_stream_failed(stream_id, "report", f"报告失败: {e}")
        log.exception("场次 #%d 报告失败", stream_id)
        return False


def process_stream(cfg: dict, store: Store, stream_id: int,
                   notify_card: bool = False) -> bool:
    """从场次的当前状态续跑，而不是每次都从转写开始。"""
    while True:
        stream = store.get_stream(stream_id)
        if not stream:
            return False
        status = stream["status"]
        if status == "failed":
            resumed = store.resume_failed_stream(stream_id)
            if not resumed:
                log.warning("场次 #%d 失败阶段 %s 不可自动续跑",
                            stream_id, stream["failed_stage"] or "未记录")
                return False
            log.info("场次 #%d 从失败阶段 %s 回退为 %s 续跑",
                     stream_id, stream["failed_stage"], resumed)
            continue
        if status == "recorded":
            if not transcribe_stream(cfg, store, stream_id):
                return False
            continue
        if status == "transcribed":
            if not analyze_stream(cfg, store, stream_id):
                return False
            continue
        if status == "analyzed":
            return review_stream(cfg, store, stream_id, notify_card=notify_card)
        if status == "reported":
            return True
        if status in ("recording", "interrupted"):
            log.warning("场次 #%d 状态为 %s，跳过（录像不完整）", stream_id, status)
        return False


def retry_failed(cfg: dict, store: Store) -> None:
    """重试所有 failed 场次（脚本用）"""
    for s in store.find_streams("failed"):
        log.info("重试场次 #%d", s["id"])
        process_stream(cfg, store, s["id"])
