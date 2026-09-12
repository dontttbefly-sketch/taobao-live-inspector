from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .models import TranscriptionResult
from .media import (canonical_media_layout, filter_segments_to_coverage,
                    media_manifest_hash, reconcile_media_layout_to_audio)

log = logging.getLogger("transcription.service")


class TranscriptionService:
    """持久化、非阻塞的统一转写调度器。

    Provider 的 ``advance(job, now)`` 每次只推进一个小状态并立即返回；远端
    processing 通过 ``next_poll_at`` 交还 watcher，不占线程等待。
    """

    def __init__(self, store, cfg: dict, feishu_provider, fallback_provider):
        self.store = store
        self.cfg = cfg
        self.feishu = feishu_provider
        self.fallback = fallback_provider
        transcription_cfg = (cfg.get("transcription", {}) or {}) if isinstance(cfg, dict) else {}
        raw_funasr_enabled = transcription_cfg.get("funasr_enabled", True)
        if type(raw_funasr_enabled) is not bool:
            raise ValueError("transcription.funasr_enabled must be boolean")
        self.funasr_enabled = raw_funasr_enabled
        # FunASR 只是显式开关下的历史兼容能力；关闭时只等待妙记。
        self.fallback_after_seconds = max(
            1800, int(transcription_cfg.get("fallback_after_seconds", 1800)))

    def queue_window(self, *, stream_id: int, live_id: str, parts: list[Path],
                     window_start_ms: int, window_end_ms: int,
                     purpose: str = "hourly", deadline_at: float | None = None,
                     delivery_key: str = "", business_session_key: str = "",
                     shift_window_key: str = "",
                     media_origin_ms: int | None = None,
                     fallback_after_at: float | None = None,
                     media_layout: list[dict[str, object]] | None = None,
                     media_coverage: dict[str, object] | None = None) -> str:
        duration_ms = int(window_end_ms) - int(window_start_ms)
        layout = canonical_media_layout(
            media_layout, window_duration_ms=duration_ms) if media_layout else []
        if media_coverage is not None and not isinstance(media_coverage, dict):
            raise ValueError("media_coverage must be an object")
        coverage = json.loads(json.dumps(
            media_coverage or {}, ensure_ascii=False, allow_nan=False,
            sort_keys=True, separators=(",", ":")))
        digest = media_manifest_hash(parts, layout)
        key = f"tx:{int(stream_id)}:{int(window_start_ms)}:{int(window_end_ms)}:{digest[:20]}"
        self.store.queue_transcription_job(
            job_key=key, stream_id=int(stream_id), live_id=str(live_id or ""),
            purpose=str(purpose), window_start_ms=int(window_start_ms),
            window_end_ms=int(window_end_ms), media_manifest=[str(path.resolve()) for path in parts],
            media_hash=digest, deadline_at=deadline_at,
            # Existing callers pass a manifest that starts exactly at the
            # window.  Strict wall-clock windows may include the preceding
            # overlap part and provide its true origin explicitly.
            media_origin_ms=(int(window_start_ms) if media_origin_ms is None
                             else int(media_origin_ms)),
            media_layout=layout, media_coverage=coverage,
            business_session_key=str(business_session_key or ""),
            shift_window_key=str(shift_window_key or ""),
            fallback_after_at=fallback_after_at,
        )
        resolved_delivery = str(delivery_key or (f"brief:{key}" if purpose == "hourly" else ""))
        if resolved_delivery:
            self.store.update_transcription_job(key, delivery_key=resolved_delivery)
        return key

    @staticmethod
    def _alert_after(now: float, error_class: str = "", error: str = "") -> float:
        detail = f"{error_class} {error}".upper()
        # 鉴权失效需要立即提醒；普通网络/妙记故障静默 10 分钟后再提醒。
        if error_class.lower() in {"auth", "authorization"} or any(
                marker in detail for marker in ("SESSION_EXPIRED", "USER_VALIDATE", "授权")):
            return float(now)
        return float(now) + 600

    def tick(self, *, now: float | None = None, limit: int = 3) -> list[str]:
        now = float(time.time() if now is None else now)
        touched: list[str] = []
        for row in self.store.claim_due_transcription_jobs(now, limit=limit):
            job = dict(row)
            key = str(job["job_key"])
            try:
                media_layout = json.loads(job.get("media_layout_json") or "[]")
                local_media_only = (
                    str(job.get("remote_status") or "queued") == "queued"
                    and not any(str(job.get(field) or "") for field in (
                        "media_path", "drive_file_token", "minute_token",
                        "result_json", "fallback_json",
                    ))
                )
                if media_layout and local_media_only:
                    duration_ms = (
                        int(job.get("window_end_ms") or 0)
                        - int(job.get("window_start_ms") or 0)
                    )
                    reconciled, coverage = reconcile_media_layout_to_audio(
                        (self.cfg.get("recorder", {}) or {}).get(
                            "ffmpeg", "ffmpeg"),
                        media_layout,
                        json.loads(job.get("media_coverage_json") or "{}"),
                        window_duration_ms=duration_ms,
                    )
                    if (reconciled != media_layout or coverage != json.loads(
                            job.get("media_coverage_json") or "{}")):
                        encoded_layout = json.dumps(
                            reconciled, ensure_ascii=False, allow_nan=False,
                            sort_keys=True, separators=(",", ":"))
                        encoded_coverage = json.dumps(
                            coverage, ensure_ascii=False, allow_nan=False,
                            sort_keys=True, separators=(",", ":"))
                        parts = [Path(item) for item in json.loads(
                            job.get("media_manifest") or "[]")]
                        if not self.store.reconcile_transcription_job_media(
                            key,
                            media_layout_json=encoded_layout,
                            media_coverage_json=encoded_coverage,
                            media_hash=media_manifest_hash(parts, reconciled),
                        ):
                            raise RuntimeError("小时音频任务状态已变化")
                        job = dict(self.store.get_transcription_job(key))
                if str(job.get("status")) == "fallback_running":
                    if not self.funasr_enabled:
                        self.store.update_transcription_job(
                            key, status="processing", next_poll_at=now,
                            review_fallback_approved=0,
                        )
                        job = dict(self.store.get_transcription_job(key))
                    else:
                        fallback_result = self.fallback.transcribe(job)
                        self.store.update_transcription_job(
                            key, status="fallback_ready",
                            fallback_json=fallback_result.to_json(),
                            next_poll_at=now + 30,
                        )
                        touched.append(key)
                        continue
                if (str(job.get("remote_status")) == "blocked"
                        and str(job.get("purpose")) == "hourly"):
                    if job.get("result_json"):
                        # 逐字稿已经可用时简报不需要本地重转；智能纪要阻塞继续
                        # 保留告警，正式复盘仍等待人工“重试飞书/确认备胎”。
                        self.store.update_transcription_job(
                            key, status="processing", next_poll_at=now + 3600)
                        touched.append(key)
                        continue
                    fallback_deadline = self._fallback_deadline(job)
                    if not self.funasr_enabled or now < fallback_deadline:
                        self.store.update_transcription_job(
                            key, status="processing", next_poll_at=(
                                now + 3600 if not self.funasr_enabled
                                else fallback_deadline
                            ))
                        touched.append(key)
                        continue
                    self.store.update_transcription_job(
                        key, status="fallback_running", next_poll_at=now)
                    fallback_result = self.fallback.transcribe(job)
                    self.store.update_transcription_job(
                        key, status="fallback_ready", fallback_json=fallback_result.to_json(),
                        next_poll_at=now + 3600, review_fallback_approved=0)
                    touched.append(key)
                    continue
                outcome = dict(self.feishu.advance(job, now) or {})
                remote = str(outcome.get("status") or job.get("remote_status") or "processing")
                if remote == "blocked":
                    if str(job.get("purpose")) == "hourly":
                        if job.get("result_json"):
                            self.store.update_transcription_job(
                                key, status="processing", remote_status="blocked",
                                error_class=str(outcome.get("error_class") or "non_retryable"),
                                error=str(outcome.get("error") or "飞书妙记发生不可恢复错误")[:500],
                                alert_status="pending", alert_after_at=self._alert_after(
                                    now, str(outcome.get("error_class") or ""),
                                    str(outcome.get("error") or "")),
                                next_poll_at=now + 3600,
                            )
                            touched.append(key)
                            continue
                        fallback_deadline = self._fallback_deadline(job)
                        if not self.funasr_enabled or now < fallback_deadline:
                            # 简报备胎严格在排班结束后 30 分钟且远端明确 blocked
                            # 才启动；远端硬错误仍立即告警，
                            # 但不能让正式复盘把自动备胎当成人工确认结果。
                            self.store.update_transcription_job(
                                key, status="processing", remote_status="blocked",
                                error_class=str(outcome.get("error_class") or "non_retryable"),
                                error=str(outcome.get("error") or "飞书妙记发生不可恢复错误")[:500],
                                alert_status="pending", alert_after_at=self._alert_after(
                                    now, str(outcome.get("error_class") or ""),
                                    str(outcome.get("error") or "")),
                                next_poll_at=(
                                    now + 3600 if not self.funasr_enabled
                                    else fallback_deadline
                                ),
                            )
                            touched.append(key)
                            continue
                        self.store.update_transcription_job(
                            key, status="fallback_running", remote_status="blocked",
                            error_class=str(outcome.get("error_class") or "non_retryable"),
                            error=str(outcome.get("error") or "飞书妙记发生不可恢复错误")[:500],
                            alert_status="pending", alert_after_at=self._alert_after(
                                now, str(outcome.get("error_class") or ""),
                                str(outcome.get("error") or "")),
                            next_poll_at=now,
                        )
                        try:
                            fallback_result = self.fallback.transcribe(
                                dict(self.store.get_transcription_job(key)))
                        except Exception as exc:
                            self.store.update_transcription_job(
                                key, status="blocked", remote_status="blocked",
                                error_class="fallback_failed", error=str(exc)[:500],
                                alert_status="pending", next_poll_at=now + 300)
                            touched.append(key)
                            continue
                        self.store.update_transcription_job(
                            key, status="fallback_ready", remote_status="blocked",
                            fallback_json=fallback_result.to_json(), next_poll_at=now + 3600,
                            review_fallback_approved=0)
                        touched.append(key)
                        continue
                    self.store.update_transcription_job(
                        key, status="blocked", remote_status="blocked",
                        error_class=str(outcome.get("error_class") or "non_retryable"),
                        error=str(outcome.get("error") or "飞书妙记发生不可恢复错误")[:500],
                        alert_status="pending", alert_after_at=self._alert_after(
                            now, str(outcome.get("error_class") or ""),
                            str(outcome.get("error") or "")),
                    )
                    touched.append(key)
                    continue
                result = outcome.get("result")
                if isinstance(result, TranscriptionResult):
                    transcript_only = result.quality_status == "transcript_ready"
                    reconsume_remote = bool(
                        not transcript_only and job.get("fallback_json")
                        and str(job.get("consumer_status")) == "sent")
                    self.store.update_transcription_job(
                        key, status="processing" if transcript_only else "ready",
                        remote_status="processing" if transcript_only else "ready",
                        result_json=result.to_json(),
                        next_poll_at=(now + 30 if transcript_only else now),
                        error_class="", error="", **self._remote_fields(outcome),
                        **({"consumer_status": "pending", "consumer_next_at": now}
                           if reconsume_remote else {}),
                    )
                    touched.append(key)
                    continue

                current_status = str(job.get("status") or "queued")
                status = current_status if current_status == "fallback_ready" else remote
                self.store.update_transcription_job(
                    key, status=status, remote_status=remote,
                    next_poll_at=float(outcome.get("next_poll_at") or now + 30),
                    error_class=str(outcome.get("error_class") or ""),
                    error=str(outcome.get("error") or "")[:500],
                    **self._remote_fields(outcome),
                )
                refreshed = dict(self.store.get_transcription_job(key))
                if self._brief_fallback_due(refreshed, now):
                    self.store.update_transcription_job(
                        key, status="fallback_running", next_poll_at=now)
                    fallback_result = self.fallback.transcribe(refreshed)
                    self.store.update_transcription_job(
                        key, status="fallback_ready", fallback_json=fallback_result.to_json(),
                        next_poll_at=float(outcome.get("next_poll_at") or now + 30),
                    )
                touched.append(key)
            except Exception as exc:
                delay = min(900, 15 * (2 ** min(6, int(job.get("attempts") or 1))))
                remote_state = str(job.get("remote_status") or "processing")
                blocked = remote_state == "blocked"
                media_build_failure = "小时音频" in str(exc)
                # 本地拼装失败静默重试会漏发简报（2026-08-13 实测数小时无人知），
                # 重试两次后武装告警，由 watcher 的告警循环按冷却推送。
                arm_alert = blocked or (
                    media_build_failure and int(job.get("attempts") or 0) >= 2)
                self.store.update_transcription_job(
                    key, status=("blocked" if blocked else
                                 ("fallback_ready" if job.get("fallback_json") else "processing")),
                    remote_status=remote_state, next_poll_at=now + delay,
                    error_class=("fallback_failed" if str(job.get("status")) == "fallback_running"
                                 else "transient"),
                    error=str(exc)[:500],
                    **({"alert_status": "pending", "alert_after_at": self._alert_after(
                        now, "transient", str(exc))} if arm_alert else {}),
                )
                log.warning("妙记任务推进失败 job=%s type=%s", key, type(exc).__name__)
                touched.append(key)
        self._run_cleanups(now, limit)
        return touched

    def _run_cleanups(self, now: float, limit: int) -> None:
        cleanup = getattr(self.feishu, "cleanup_source", None)
        if not callable(cleanup):
            return
        for row in self.store.claim_due_transcription_cleanups(now, limit=limit):
            job = dict(row)
            key = str(job["job_key"])
            try:
                cleanup(str(job.get("drive_file_token") or ""))
                self.store.update_transcription_job(
                    key, cleanup_status="deleted", cleaned_at=now,
                    cleanup_next_at=0, error="",
                )
                self._cleanup_local_media(job)
            except Exception as exc:
                delay = min(3600, 60 * (2 ** min(6, int(job.get("cleanup_attempts") or 1))))
                self.store.update_transcription_job(
                    key, cleanup_status="failed", cleanup_next_at=now + delay,
                    error=str(exc)[:500],
                )
                log.warning("妙记源文件清理失败 job=%s type=%s", key, type(exc).__name__)

    @staticmethod
    def _cleanup_local_media(job: dict) -> None:
        path = Path(str(job.get("media_path") or ""))
        if not path.exists():
            return
        root = Path(__file__).resolve().parents[2] / "data" / "transcription_media"
        try:
            path.resolve().relative_to(root.resolve())
            path.unlink(missing_ok=True)
        except (OSError, ValueError):
            # 预检/人工传入文件不属于自动媒体目录，永不删除。
            return

    @staticmethod
    def _remote_fields(outcome: dict) -> dict:
        keys = ("media_path", "drive_file_token", "minute_token", "minute_url",
                "note_id", "note_doc_token", "cleanup_status", "cleanup_next_at",
                "cleanup_attempts", "cleaned_at")
        return {key: outcome[key] for key in keys if outcome.get(key) is not None}

    def _fallback_deadline(self, job: dict) -> float:
        if float(job.get("fallback_after_at") or 0) > 0:
            return float(job["fallback_after_at"])
        deadline = job.get("deadline_at")
        if deadline is None:
            return float("inf")
        brief_wait = max(0, int(((self.cfg.get("transcription", {}) or {})
                                 if isinstance(self.cfg, dict) else {})
                                .get("brief_wait_seconds", 600)))
        # deadline_at 是简报等待边界；还原任务创建时刻，再加 30 分钟总门槛。
        return float(deadline) - brief_wait + self.fallback_after_seconds

    def _brief_fallback_due(self, job: dict, now: float) -> bool:
        deadline = self._fallback_deadline(job)
        return (self.funasr_enabled
                and str(job.get("purpose")) == "hourly"
                and str(job.get("remote_status") or "") in {"processing", "blocked"}
                and now >= float(deadline) and not job.get("fallback_json")
                and not job.get("result_json"))

    def result_for(self, job_key: str, *, allow_fallback: bool = False) -> TranscriptionResult | None:
        row = self.store.get_transcription_job(job_key)
        if not row:
            return None
        job = dict(row)
        result = TranscriptionResult.from_json(job.get("result_json"))
        if result is None and allow_fallback:
            result = TranscriptionResult.from_json(job.get("fallback_json"))
        if result is not None:
            coverage = json.loads(job.get("media_coverage_json") or "{}")
            segments, removed = filter_segments_to_coverage(
                result.segments, coverage)
            if removed:
                return TranscriptionResult(
                    provider=result.provider,
                    segments=segments,
                    smart=result.smart,
                    quality_status=result.quality_status,
                    quality_issues=[
                        *result.quality_issues,
                        f"{removed} 句话与无录音时段重叠，已排除",
                    ],
                )
            return result
        return None

    def resolve_blocked(self, job_key: str, action: str, *, now: float | None = None) -> None:
        now = float(time.time() if now is None else now)
        row = self.store.get_transcription_job(job_key)
        if not row or not (
                str(row["status"]) == "blocked" or str(row["remote_status"]) == "blocked"):
            raise ValueError("任务不存在或未处于 blocked")
        current = dict(row)
        if action == "retry_feishu":
            if current.get("minute_token"):
                restart = "processing"
            elif current.get("drive_file_token"):
                restart = "uploaded"
            elif current.get("media_path"):
                restart = "media_ready"
            else:
                restart = "queued"
            self.store.update_transcription_job(
                job_key, status=restart, remote_status=restart, next_poll_at=now,
                error_class="", error="", alert_status="pending",
                review_fallback_approved=0,
            )
            return
        if action == "use_funasr":
            if not self.funasr_enabled:
                raise ValueError("FunASR 已禁用")
            existing = TranscriptionResult.from_json(current.get("fallback_json"))
            if existing is not None:
                self.store.update_transcription_job(
                    job_key, status="fallback_ready", review_fallback_approved=1,
                    next_poll_at=now + 3600, error_class="", error="")
                return
            self.store.update_transcription_job(
                job_key, status="fallback_running", consumer_status="pending",
                next_poll_at=now, error_class="", error="", review_fallback_approved=1)
            result = self.fallback.transcribe(current)
            self.store.update_transcription_job(
                job_key, status="fallback_ready", fallback_json=result.to_json(),
                next_poll_at=now + 3600, error_class="", error="",
                consumer_status="pending", review_fallback_approved=1,
            )
            return
        raise ValueError("action 必须是 retry_feishu 或 use_funasr")
