from __future__ import annotations

import json
import time
import hashlib
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable

from ..metrics.qianniu import SERIES_API, TOTAL_API
from ..recorder.discover import API as LIVE_LIST_API
from ..config import local_epoch_ms
from ..metrics.qianniu import fetch_metric_series
from ..recorder.mtop import MtopClient, activate_shared_cookie
from .config import write_taobao_cookie
from .provider import BrowserSessionProvider
from .signal import consume_auth_failure

log = logging.getLogger("taobao.session")


@dataclass(frozen=True)
class CandidateValidation:
    ok: bool
    failed_layer: str = ""


def _success(payload: dict) -> bool:
    ret = payload.get("ret", [""])
    value = ret[0] if isinstance(ret, list) and ret else str(ret or "")
    return str(value).startswith("SUCCESS")


def validate_candidate_cookie(cfg: dict, cookie: str, *, client_factory=MtopClient,
                              now_ms: int | None = None) -> CandidateValidation:
    taobao = cfg.get("taobao", {}) or {}
    live_id = str(taobao.get("live_id") or "")
    room_num = str(taobao.get("room_num") or "")
    client = client_factory(
        cookie=str(cookie), app_key=str(taobao.get("app_key") or "12574478"),
        user_agent=str(taobao.get("user_agent") or "") or None,
        referer="https://market.m.taobao.com/",
    )
    probe = client.probe(live_id=live_id, user_id="")
    if not str(probe.get("ret") or "").startswith("SUCCESS"):
        return CandidateValidation(False, "live_detail")

    end_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    totals = client.call(TOTAL_API, "1.0", {
        "liveId": live_id, "types": "totalStats", "timeType": 1,
        "searchType": "1", "startTime": end_ms - 600_000,
        "endTime": end_ms, "extParams": "{}",
    })
    data_list = (totals.get("data") or {}).get("dataList") or []
    if not _success(totals) or not any(
            item.get("type") == "totalStats" and item.get("data")
            for item in data_list if isinstance(item, dict)):
        return CandidateValidation(False, "total_stats")

    series = client.call(SERIES_API, "1.0", {
        "liveId": live_id, "types": "uv,deal,itemClick", "timeType": 1,
        "searchType": "2", "startTime": end_ms - 600_000,
        "endTime": end_ms, "extParams": "{}",
    })
    if not _success(series):
        return CandidateValidation(False, "minute_series")

    listing = client.call(LIVE_LIST_API, "1.0", {
        "roomNum": room_num, "pageNum": 1, "searchValue": "", "pageSize": 20,
    })
    items = (listing.get("data") or {}).get("data") or []
    if (not _success(listing) or not any(
            str(item.get("id") or "") == live_id for item in items
            if isinstance(item, dict))):
        return CandidateValidation(False, "live_list")
    return CandidateValidation(True)


def backfill_suppressed_brief_metrics(
        cfg: dict, store, job_keys: list[str], *,
        fetcher: Callable = fetch_metric_series,
        now: float | None = None) -> int:
    """补抓被抑制群卡的历史分钟趋势，与卡片投递状态分离。"""
    now = float(time.time() if now is None else now)
    completed = 0
    touched_streams: set[int] = set()
    for job_key in job_keys:
        job = store.get_transcription_job(job_key)
        if not job:
            continue
        existing_json = str(job["recovery_metrics_json"] or "")
        if existing_json not in ("", "{}"):
            # 上次已拿到趋势，但可能崩溃在 Markdown 原子落盘前。
            from ..review.report import write_recovery_metrics_markdown
            try:
                write_recovery_metrics_markdown(
                    cfg, store, int(job["stream_id"]))
            except Exception as exc:
                log.warning("历史趋势 Markdown 重试失败 stream=%s type=%s",
                            job["stream_id"], type(exc).__name__)
                store.update_transcription_job(
                    job_key, recovery_metrics_status="failed",
                    recovery_metrics_next_at=now + 300)
                continue
            store.update_transcription_job(
                job_key, recovery_metrics_status="ready",
                recovery_metrics_next_at=0)
            completed += 1
            continue
        stream = store.get_stream(int(job["stream_id"]))
        base_ms = local_epoch_ms(stream["started_at"] if stream else None)
        live_id = str(job["live_id"] or (stream["live_id"] if stream else "") or "")
        if base_ms is None or not live_id:
            store.update_transcription_job(
                job_key, recovery_metrics_status="failed",
                recovery_metrics_json="{}",
                recovery_metrics_next_at=now + 300)
            continue
        try:
            metrics = fetcher(
                cfg, live_id, search_type="2", time_type=1,
                start_ms=base_ms + int(job["window_start_ms"]),
                end_ms=base_ms + int(job["window_end_ms"]),
            )
        except Exception as exc:
            log.warning("历史分钟趋势补抓失败 job=%s type=%s",
                        job_key, type(exc).__name__)
            store.update_transcription_job(
                job_key, recovery_metrics_status="failed",
                recovery_metrics_json="{}",
                recovery_metrics_next_at=now + 300)
            continue
        if not metrics:
            store.update_transcription_job(
                job_key, recovery_metrics_status="failed",
                recovery_metrics_json="{}",
                recovery_metrics_next_at=now + 300)
            continue
        store.update_transcription_job(
            job_key, recovery_metrics_status="writing",
            recovery_metrics_next_at=0,
            recovery_metrics_json=json.dumps(
                metrics, ensure_ascii=False, separators=(",", ":"), default=str))
        touched_streams.add(int(job["stream_id"]))
    if touched_streams:
        from ..review.report import write_recovery_metrics_markdown
        for stream_id in touched_streams:
            try:
                write_recovery_metrics_markdown(cfg, store, stream_id)
            except Exception as exc:
                log.warning("历史趋势 Markdown 落盘失败 stream=%s type=%s",
                            stream_id, type(exc).__name__)
                for job_key in job_keys:
                    job = store.get_transcription_job(job_key)
                    if (job and int(job["stream_id"]) == stream_id
                            and str(job["recovery_metrics_status"]) == "writing"):
                        store.update_transcription_job(
                            job_key, recovery_metrics_status="failed",
                            recovery_metrics_next_at=now + 300)
                continue
            for job_key in job_keys:
                job = store.get_transcription_job(job_key)
                if (job and int(job["stream_id"]) == stream_id
                        and str(job["recovery_metrics_status"]) == "writing"):
                    store.update_transcription_job(
                        job_key, recovery_metrics_status="ready",
                        recovery_metrics_next_at=0)
                    completed += 1
    return completed


class SessionKeeper:
    def __init__(
            self, store, cfg: dict, provider: BrowserSessionProvider, *,
            config_path: Path | None = None,
            validator: Callable = validate_candidate_cookie,
            activate_client: Callable = activate_shared_cookie,
            on_client_activated: Callable | None = None,
            notify_required: Callable | None = None,
            notify_recovered: Callable | None = None,
            current_auth_epoch: Callable[[], int] | None = None,
            backfill_metrics: Callable = backfill_suppressed_brief_metrics,
            owner_id: str | None = None):
        if config_path is None:
            from ..config import CONFIG_PATH
            config_path = CONFIG_PATH
        self.store = store
        self.cfg = cfg
        self.provider = provider
        self.config_path = Path(config_path)
        self.validator = validator
        self.activate_client = activate_client
        self.on_client_activated = on_client_activated or (lambda _client: None)
        self.notify_required = notify_required or (lambda _payload: False)
        self.notify_recovered = notify_recovered or (lambda _payload: False)
        if current_auth_epoch is None:
            from ..recorder.mtop import current_auth_epoch as get_auth_epoch
            current_auth_epoch = get_auth_epoch
        self.current_auth_epoch = current_auth_epoch
        self.backfill_metrics = backfill_metrics
        self.owner_id = str(owner_id or uuid.uuid4().hex)

    def tick(self, *, now: float | None = None,
             force_recovery: bool = False) -> bool:
        now = float(time.time() if now is None else now)
        signal = consume_auth_failure()
        state = self.store.get_taobao_session_state()
        local_is_current = self._local_cookie_matches_state(state)
        if (signal is not None and local_is_current
                and int(signal.epoch) >= int(self.current_auth_epoch())):
            self.store.note_taobao_auth_failure(signal.kind, now=signal.occurred_at)
        state = self.store.get_taobao_session_state()
        if str(state["status"]) == "healthy":
            # 另一进程已激活新 Cookie：本进程的信号、补抓和
            # 恢复通知都属于旧会话，等新 watcher 接管。
            if not self._local_cookie_matches_state(state):
                return False
            # 另一进程的旧请求可能在恢复事务提交后才迟到 park；
            # healthy 状态下这些任务应交还当前会话，不再等下一次登录。
            self.store.resume_waiting_auth_briefs(now=now, limit=1000)
            self.store.wake_hourly_fact_captures_after_auth(now=now)
            self._run_due_backfills(now)
            if str(state["recovery_notice_status"]) == "pending":
                try:
                    payload = json.loads(str(state["recovery_notice_payload"] or "{}"))
                except (TypeError, ValueError):
                    payload = {}
                try:
                    delivered = bool(payload and self.notify_recovered(payload))
                except Exception as exc:
                    log.warning("淘宝恢复通知重试异常：type=%s", type(exc).__name__)
                    delivered = False
                if delivered:
                    self.store.mark_taobao_recovery_notice_sent()
            return False
        if not force_recovery and float(state["next_check_at"] or 0) > now:
            return False

        if not self.store.claim_taobao_session_recovery(
                self.owner_id, now=now, lease_sec=300,
                force=force_recovery):
            return False

        settings = self.cfg.get("taobao_session", {}) or {}
        check_interval = max(60, int(settings.get("check_interval_sec", 300)))
        reminder_interval = max(300, int(settings.get("reminder_interval_sec", 10800)))
        if not self.store.set_taobao_session_recovery_state(
                "auto_recovering", next_check_at=now + check_interval,
                owner=self.owner_id):
            return False

        # 业务请求偶发返回鉴权错误时，先复核当前进程正在使用的 Cookie。
        # Ego Lite 使用隔离任务空间；它没有商家 Cookie 只说明候选会话不可用，
        # 不能反过来证明当前淘宝会话已经失效。
        if not force_recovery:
            current_cookie = str(
                (self.cfg.get("taobao", {}) or {}).get("cookie") or "")
            if current_cookie:
                try:
                    current_validation = self.validator(self.cfg, current_cookie)
                except Exception as exc:
                    log.warning("淘宝当前登录态复核失败：type=%s", type(exc).__name__)
                else:
                    if current_validation.ok:
                        cookie_hash = hashlib.sha256(
                            current_cookie.encode("utf-8")).hexdigest()
                        if not self.store.mark_taobao_session_healthy(
                                cookie_hash, now=now, notice_payload={},
                                owner=self.owner_id):
                            return False
                        # 瞬时误判的撤销不是一次真实登录恢复，不发绿卡。
                        self.store.mark_taobao_recovery_notice_sent()
                        log.info("淘宝当前登录态复核通过，已撤销瞬时鉴权告警")
                        return True

        result = self.provider.read_session()
        if not self.store.is_taobao_session_recovery_owner(self.owner_id):
            log.info("淘宝登录恢复任务已被新 owner 接管，丢弃旧浏览器结果")
            return False
        if result.status != "ready" or not result.cookie:
            if not self.store.set_taobao_session_recovery_state(
                "user_login_required", next_check_at=now + check_interval,
                    error_class=result.error or result.status,
                    owner=self.owner_id):
                return False
            self._notify_required_if_due(now, reminder_interval)
            return False

        if not self.store.set_taobao_session_recovery_state(
                "validating", next_check_at=now + check_interval,
                owner=self.owner_id):
            return False
        try:
            validation = self.validator(self.cfg, result.cookie)
        except Exception as exc:
            log.warning("淘宝候选登录态验证失败：type=%s", type(exc).__name__)
            validation = CandidateValidation(False, type(exc).__name__)
        if not self.store.is_taobao_session_recovery_owner(self.owner_id):
            log.info("淘宝登录恢复任务已被新 owner 接管，丢弃旧验证结果")
            return False
        if not validation.ok:
            if not self.store.set_taobao_session_recovery_state(
                "user_login_required", next_check_at=now + check_interval,
                    error_class=validation.failed_layer,
                    owner=self.owner_id):
                return False
            self._notify_required_if_due(now, reminder_interval)
            return False

        old_cookie = str((self.cfg.get("taobao", {}) or {}).get("cookie") or "")
        try:
            if not self.store.is_taobao_session_recovery_owner(self.owner_id):
                return False
            write_taobao_cookie(self.config_path, result.cookie)
            self.cfg.setdefault("taobao", {})["cookie"] = result.cookie
            client = self.activate_client(self.cfg, result.cookie)
            self.on_client_activated(client)
        except Exception:
            self.cfg.setdefault("taobao", {})["cookie"] = old_cookie
            if self.store.is_taobao_session_recovery_owner(self.owner_id):
                try:
                    write_taobao_cookie(self.config_path, old_cookie)
                except Exception:
                    log.error("淘宝 Cookie 激活失败且旧配置回滚失败")
                self.store.set_taobao_session_recovery_state(
                    "user_login_required", next_check_at=now + check_interval,
                    error_class="activation_failed", owner=self.owner_id)
            return False

        if not self.store.is_taobao_session_recovery_owner(self.owner_id):
            log.info("淘宝登录恢复任务在激活后失去 owner，不再提交状态")
            return False

        state = self.store.get_taobao_session_state()
        outage_seconds = max(0, int(now - float(state["first_failed_at"] or now)))
        cookie_hash = hashlib.sha256(result.cookie.encode("utf-8")).hexdigest()
        short_outage_sec = max(60, int(settings.get("short_outage_sec", 7200)))
        resumed, suppressed = self.store.resolve_waiting_auth_briefs(
            outage_seconds=outage_seconds, now=now,
            short_outage_sec=short_outage_sec, limit=2,
            owner=self.owner_id)
        if not self.store.is_taobao_session_recovery_owner(self.owner_id):
            return False
        claimed = self.store.claim_recovery_metrics_jobs(
            now=now, limit=max(1, len(suppressed)), job_keys=suppressed)
        if claimed:
            try:
                self.backfill_metrics(
                    self.cfg, self.store,
                    [str(row["job_key"]) for row in claimed], now=now)
            except Exception:
                log.exception("登录恢复后历史趋势补抓任务异常")
        if not self.store.is_taobao_session_recovery_owner(self.owner_id):
            log.info("淘宝登录恢复任务在补抓期间失去 owner，不提交 healthy")
            return False
        backfilled = 0
        for key in suppressed:
            row = self.store.get_transcription_job(key)
            if row and str(row["recovery_metrics_status"] or "") == "ready":
                backfilled += 1
        payload = {
            "generation": int(state["generation"] or 0),
            "first_failed_at": float(state["first_failed_at"] or 0),
            "outage_seconds": outage_seconds,
            "resumed_briefs": len(resumed),
            "suppressed_briefs": len(suppressed),
            "backfilled_briefs": backfilled,
            "recovered_at": now,
        }
        if not self.store.mark_taobao_session_healthy(
                cookie_hash, now=now, notice_payload=payload,
                owner=self.owner_id):
            return False
        try:
            delivered = bool(self.notify_recovered(payload))
        except Exception as exc:
            log.warning("淘宝恢复通知投递异常：type=%s", type(exc).__name__)
            delivered = False
        if delivered:
            self.store.mark_taobao_recovery_notice_sent()
        log.info("淘宝登录态已恢复：cookie_len=%d fingerprint=%s",
                 len(result.cookie), cookie_hash[:10])
        return True

    def _local_cookie_matches_state(self, state) -> bool:
        expected = str(state["last_cookie_hash"] or "")
        if not expected:
            return True
        cookie = str((self.cfg.get("taobao", {}) or {}).get("cookie") or "")
        return hashlib.sha256(cookie.encode("utf-8")).hexdigest() == expected

    def _run_due_backfills(self, now: float) -> None:
        claimed = self.store.claim_recovery_metrics_jobs(now=now, limit=3)
        if not claimed:
            return
        try:
            self.backfill_metrics(
                self.cfg, self.store,
                [str(row["job_key"]) for row in claimed], now=now)
        except Exception:
            # 保留 running 租约；崩溃/异常后由下一轮过期接管。
            log.exception("历史趋势补抓 worker 异常")

    def _notify_required_if_due(self, now: float, reminder_interval: int) -> None:
        if not self.store.claim_taobao_auth_reminder(
                now=now, interval_sec=reminder_interval):
            return
        state = self.store.get_taobao_session_state()
        try:
            delivered = bool(self.notify_required(self._notice_payload(state)))
        except Exception as exc:
            log.warning("淘宝登录提醒投递异常：type=%s", type(exc).__name__)
            delivered = False
        if not delivered:
            self.store.release_taobao_auth_reminder(claimed_at=now)

    @staticmethod
    def _notice_payload(state) -> dict:
        return {
            "generation": int(state["generation"] or 0),
            "first_failed_at": float(state["first_failed_at"] or 0),
            "error_class": str(state["last_error_class"] or "authentication_required"),
            "reminder_slot": int(float(state["last_reminder_at"] or 0) // 3600),
        }
