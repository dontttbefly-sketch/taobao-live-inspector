"""AI 巡检主循环：开播监听 → 自动录制 → 小时简报 → 业务日报

- 每 poll_interval 秒轮询一次所有直播间（按 liveId 分组，同一直播间只探测一次）
- 开播 → 建录制会话（DB 记录 + ffmpeg 分片录制），场次按轮班表归属主播
- 直播中 → 定期刷新流地址，地址轮换自动切流；ffmpeg 意外退出自动续录
- 下播 → 停止录制并冻结尾段，完整业务日交给唯一日报任务
- 全程无人值守，不分班次

同一直播间多人轮播：主播在 config.yaml 的 anchors 里配置 shift 轮班表
（如 "09:00-12:00"，支持跨天 "22:00-02:00"），录制场次按开播时间归属主播，
第一阶段不自动生成技术碎片报告、旧话术库或轮换部分简报。
"""
from __future__ import annotations

import logging
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

from ..config import (SHANGHAI, load_config, local_epoch_ms, now_shanghai,
                      parse_local_datetime, resolve)
from ..runtime.locking import acquire_process_lock
from ..schedule import (_load_schedule, in_shift, load_feishu_anchor_schedule,
                        parse_shift, persist_weekly_schedule, schedule_slot_interval,
                        schedule_slot_is_active, scheduled_anchor_names,
                        scheduled_anchor_names_for_window)
from ..business_facts import (
    absolute_hour_window,
    business_interval_overlaps,
    business_operating_bounds,
    business_session_key,
    final_partial_window_key,
    formal_hourly_metrics_ready,
    initial_partial_window_key,
    plan_business_windows,
    shift_window_key,
)
from ..db import Store
from ..briefing import closed_parts, run_briefing
from .mtop import MtopAuthError, MtopError
from .recorder import MIN_VALID_PART_BYTES, Recorder, probe_ffmpeg, url_stem
from .timeline import (CoverageSlice, TimelineManifest, WindowCoverage,
                       formal_brief_allowed)
from .watchdog import (WatchdogSettings, create_watcher_heartbeat,
                       start_watchdog_process)

log = logging.getLogger("watcher")

# 历史补抓可接受的边界快照偏移。2026-08-13 由 120s 放宽到 300s：
# 排班轮换探测（poll_interval=60s）+ ffmpeg 封存（约 90～110s）让事实
# 批次稳定落在整点后 2～2.5 分钟，120s 容差曾使两天内 6 个小时窗口
# 永久停在 waiting_evidence。相邻小时边界快照首尾相接，日汇总守恒，
# 简报照常明示实际覆盖与缺口。
HISTORICAL_BOUNDARY_SNAPSHOT_TOLERANCE_MS = 300_000

# 拉流 UA：媒体 CDN 对默认 UA 可能 404/403，用 PC Chrome UA（实测可用）
RECORD_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def load_schedule(path: Path | str | None = None,
                  now: datetime | None = None) -> dict:
    """兼容旧调用方，并让 watcher.resolve 的既有 monkeypatch 继续生效。"""
    return _load_schedule(path, now, resolve)


def resolve_anchor_by_shift(anchors: list, now: datetime | None = None) -> int:
    """按轮班表选出当前应归属的主播 id；无匹配或未配班次时取第一个"""
    now = now or now_shanghai()
    for a in anchors:
        if in_shift(a["shift"] or "", now):
            return a["id"]
    return anchors[0]["id"]


def schedule_anchor_names(schedule: dict) -> set[str]:
    names: set[str] = set()
    for slots in (schedule or {}).values():
        if not isinstance(slots, list):
            continue
        for slot in slots:
            if isinstance(slot, dict) and str(slot.get("anchor") or "").strip():
                names.add(str(slot["anchor"]).strip())
    return names


def sync_anchor_roster(store: Store, cfg: dict, schedule: dict) -> set[str]:
    """当前名册 = 配置中启用的主播 ∪ 排班表主播。

    配置显式 ``enabled: false`` 的名称优先级最高；历史主播行保留
    供外键/报告查询，但不再参与当前轮询。
    """
    configured: dict[str, dict] = {}
    disabled: set[str] = set()
    for raw in cfg.get("anchors") or []:
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        configured[name] = raw
        if raw.get("enabled", True) is False:
            disabled.add(name)
        store.upsert_anchor(
            name, raw.get("taobao_user_id", ""), raw.get("live_id", ""),
            raw.get("shift", ""), enabled=name not in disabled,
        )

    active = (set(configured) | schedule_anchor_names(schedule)) - disabled
    existing = {str(row["name"]): row for row in store.query("SELECT * FROM anchors")}
    for name in sorted(active):
        if name not in existing:
            store.upsert_anchor(name, enabled=True)
    with store._write_lock:
        store.conn.execute("UPDATE anchors SET enabled=0")
        if active:
            placeholders = ",".join("?" for _ in active)
            store.conn.execute(
                f"UPDATE anchors SET enabled=1 WHERE name IN ({placeholders})",
                tuple(sorted(active)),
            )
        store.conn.commit()
    return active


def resolve_anchor_by_schedule(store, schedule: dict, now: datetime | None = None) -> int | None:
    """按排班表选出当前时刻的在播主播 id。
    跨天规则：凌晨 0-4 点属于前一天的晚班行。
    返回 None 表示当前时刻排班表无唯一在播主播（不强行归属）。"""
    now = now or now_shanghai()
    names = scheduled_anchor_names(schedule, now)
    name = names[0] if len(names) == 1 else ""
    if not name:
        return None
    row = store.query("SELECT id FROM anchors WHERE name=?", (name,))
    return row[0]["id"] if row else None


def _defer_current_hour_schedule_change(
        current: dict, incoming: dict, now: datetime) -> dict:
    """同步完成时，只保留已经开始的主播区间，不截断半小时交接。"""
    local = now.astimezone(SHANGHAI) if now.tzinfo else now.replace(tzinfo=SHANGHAI)
    business_day = local.date() - timedelta(days=1) if local.hour < 5 else local.date()
    day_key = f"{business_day.month}/{business_day.day}"
    current_slots = current.get(day_key) if isinstance(current, dict) else None
    held = [
        dict(slot) for slot in current_slots or []
        if isinstance(slot, dict) and schedule_slot_is_active(slot, local)
    ]
    if not held or day_key not in incoming:
        return incoming
    staged = {
        day: [dict(slot) for slot in slots if isinstance(slot, dict)]
        for day, slots in incoming.items()
        if isinstance(slots, list)
    }
    held_intervals = [
        interval for slot in held
        if (interval := schedule_slot_interval(slot)) is not None
    ]
    remaining: list[dict] = []
    for slot in staged.get(day_key, []):
        interval = schedule_slot_interval(slot)
        overlaps = interval is not None and any(
            max(interval[0], held_interval[0]) < min(interval[1], held_interval[1])
            for held_interval in held_intervals)
        if not overlaps:
            remaining.append(slot)

    def sort_key(slot: dict) -> tuple[int, int, int, str]:
        interval = schedule_slot_interval(slot)
        if interval is not None:
            return interval[0], interval[1], int(slot.get("hour", -1)), str(slot.get("anchor") or "")
        return 10 ** 9, 10 ** 9, -1, str(slot.get("anchor") or "")

    staged[day_key] = sorted(
        remaining + held,
        key=sort_key,
    )
    return staged


class Watcher:
    def __init__(self, cfg: dict, store: Store):
        self.cfg = cfg
        self.store = store
        t = cfg["taobao"]
        from .mtop import shared_client
        self.client = shared_client(cfg)
        rec = cfg["recorder"]
        self.ffmpeg = rec.get("ffmpeg", "ffmpeg")
        self.out_dir = resolve(rec.get("out_dir", "data/recordings"))
        self.recordings_dir = self.out_dir
        self.segment_seconds = int(rec.get("segment_seconds", 0))
        self.stream_stall_timeout = int(rec.get("stall_timeout_sec", 90))
        self.stream_retry_base = int(rec.get("retry_base_sec", 30))
        self.stream_retry_max = int(rec.get("retry_max_sec", 300))
        self.session_stale_timeout = int(rec.get("session_stale_timeout_sec", 900))
        self.poll_interval = int(t.get("poll_interval", 60))
        self.refresh_interval = int(t.get("stream_refresh_interval", 900))
        self.discovery_interval = int(t.get("live_discovery_interval", 180))
        self.alert_cooldown = int(t.get("alert_cooldown_sec", 3600))
        self.default_live_id = str(t.get("live_id", "") or "")
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="business")
        self.transcription_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="transcription")
        self.session_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="taobao-session")
        self.fact_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="hourly-facts")
        self.schedule_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="schedule")
        self._transcription_future = None
        self._session_future = None
        self._fact_future = None
        self._platform_review_inflight: set[str] = set()
        # room_key(live_id) -> session 状态
        self.sessions: dict[str, dict] = {}
        # 本轮详情探测失败的当前 liveId：先保留可续录分片，
        # 禁止立即做长时间合并而失去下一轮恢复机会。
        self._deferred_recovery_live_ids: set[str] = set()
        # 大录像 concat 只能在 daemon 恢复线程执行。它不参与录像控制，
        # 进程退出时也不等待；中断后仍由 recovering 状态和原分片重试。
        self._media_recovery_thread: threading.Thread | None = None
        self._media_recovery_stream_ids: set[int] = set()
        # 接口探测连续失败计数（触发告警用）
        self.probe_fails: dict[str, int] = {}
        # 限流（upstream_guard）连续失败计数，驱动轮询间隔退避
        self._guard_streaks: dict[str, int] = {}
        self._last_discovery_at: dict[str, float] = {}
        self._last_alert_at: dict[str, float] = {}
        self.offline_confirmations = max(2, int(t.get("offline_confirmations", 3)))
        self._last_health_check = 0.0
        self._last_fact_rescue = 0.0
        self._alert_retry_after_at: dict[str, float] = {}
        self._stop = False
        self._heartbeat = None
        # 主播上播排班表（小时级：每小时的在播主播），驱动轮换切场次
        self._schedule_file = rec.get("schedule_file")
        self.schedule = load_schedule(self._schedule_file)
        self._schedule_loaded_date = now_shanghai().date()
        self._empty_schedule_reads = 0
        source = rec.get("schedule_feishu")
        self._schedule_feishu = source if isinstance(source, dict) else {}
        self._schedule_sync_future = None
        self._last_schedule_sync_at = 0.0
        sync_anchor_roster(self.store, self.cfg, self.schedule)
        self.transcription = None
        if (self.cfg.get("transcription", {}) or {}).get("enabled"):
            from ..transcription.feishu import FeishuMinutesProvider
            from ..transcription.funasr import FunASRProvider
            from ..transcription.service import TranscriptionService
            self.transcription = TranscriptionService(
                self.store, self.cfg, FeishuMinutesProvider(self.cfg),
                FunASRProvider(self.cfg))
        self.taobao_session_keeper = None
        if (self.cfg.get("taobao_session", {}) or {}).get("enabled"):
            from ..notify.feishu import (notify_taobao_auth_recovered,
                                         notify_taobao_auth_required)
            from ..taobao_session.ego_lite import EgoLiteSessionProvider
            from ..taobao_session.keeper import SessionKeeper
            self.taobao_session_keeper = SessionKeeper(
                self.store, self.cfg, EgoLiteSessionProvider(self.cfg),
                on_client_activated=lambda client: setattr(self, "client", client),
                notify_required=lambda payload: notify_taobao_auth_required(
                    self.cfg, payload),
                notify_recovered=lambda payload: notify_taobao_auth_recovered(
                    self.cfg, payload),
            )

    def _persist_hour_window(self, stream_id: int, window_start_ms: int,
                             window_end_ms: int) -> tuple[str, str]:
        """Persist the absolute business/session identity for a transcription window."""
        stream = self.store.get_stream(int(stream_id))
        started_ms = local_epoch_ms(stream["started_at"] if stream else None)
        if started_ms is None:
            return "", ""
        start, end = absolute_hour_window(started_ms, int(window_start_ms))
        return self._persist_absolute_hour_window(
            int(stream_id), int(start.timestamp() * 1000),
            int(end.timestamp() * 1000),
            actual_start_ms=started_ms + int(window_start_ms),
            actual_end_ms=started_ms + int(window_end_ms),
        )

    def _persist_absolute_hour_window(
            self, stream_id: int, absolute_start_ms: int, absolute_end_ms: int,
            *, actual_start_ms: int, actual_end_ms: int,
            final_partial: bool = False) -> tuple[str, str]:
        """Persist one immutable schedule hour and its actual recorder slice."""
        stream = self.store.get_stream(int(stream_id))
        if stream is None:
            return "", ""
        start = datetime.fromtimestamp(int(absolute_start_ms) / 1000, SHANGHAI)
        end = datetime.fromtimestamp(int(absolute_end_ms) / 1000, SHANGHAI)
        session_key = business_session_key(start)
        names = scheduled_anchor_names_for_window(self.schedule, start, end)
        window_key = (final_partial_window_key(session_key, start, end, names)
                      if final_partial else
                      shift_window_key(session_key, start, names))
        self.store.upsert_business_session(session_key, status="collecting")
        self.store.add_business_session_source(
            session_key,
            live_id=str(stream["live_id"] or "") if stream else "",
            stream_id=int(stream_id),
        )
        anchor = self.store.get_anchor(int(stream["anchor_id"]))
        actual_slice = {
            "stream_id": int(stream_id),
            "anchor_id": int(stream["anchor_id"]),
            "anchor_name": str(anchor["name"] if anchor else ""),
            "live_id": str(stream["live_id"] or ""),
            "actual_start_ms": max(int(absolute_start_ms), int(actual_start_ms)),
            "actual_end_ms": min(int(absolute_end_ms), int(actual_end_ms)),
        }
        existing = self.store.query(
            "SELECT actual_slices_json FROM shift_windows WHERE shift_window_key=?",
            (window_key,),
        )
        slices: list[dict] = []
        if existing:
            try:
                loaded = json.loads(str(existing[0]["actual_slices_json"] or "[]"))
                slices = loaded if isinstance(loaded, list) else []
            except (TypeError, ValueError):
                slices = []
        marker = (
            actual_slice["stream_id"], actual_slice["anchor_id"],
            actual_slice["actual_start_ms"], actual_slice["actual_end_ms"],
        )
        if marker not in {
                (int(item.get("stream_id") or 0), int(item.get("anchor_id") or 0),
                 int(item.get("actual_start_ms") or 0),
                 int(item.get("actual_end_ms") or 0))
                for item in slices if isinstance(item, dict)}:
            slices.append(actual_slice)
        self.store.upsert_shift_window(
            window_key,
            business_session_key=session_key,
            window_start_ms=int(absolute_start_ms),
            window_end_ms=int(absolute_end_ms),
            scheduled_anchors=list(names),
            actual_slices=slices,
            status="collecting",
        )
        return session_key, window_key

    def _business_timeline_sources(
            self, room_key: str, sess: dict, stream_id: int, stream,
            session_key: str,
    ) -> tuple[list[tuple[dict, Path, TimelineManifest]], int, int]:
        """Load every persisted recorder ledger for one business day."""
        recording_start_ms = local_epoch_ms(stream["started_at"] if stream else None)
        if stream is None or recording_start_ms is None:
            return [], 0, 0
        rows = [dict(row) for row in self.store.query(
            """SELECT s.*,a.name anchor_name FROM streams s
               LEFT JOIN anchors a ON a.id=s.anchor_id
               WHERE s.business_session_key=? AND s.session_dir!=''
               ORDER BY s.started_at,s.id""", (str(session_key),))]
        if not any(int(row["id"]) == int(stream_id) for row in rows):
            row = dict(stream)
            anchor = self.store.get_anchor(int(stream["anchor_id"]))
            row["anchor_name"] = str(anchor["name"] if anchor else "")
            rows.append(row)

        sources: list[tuple[dict, Path, TimelineManifest]] = []
        latest_media_end_ms = 0
        earliest_stream_ms = recording_start_ms
        for row in rows:
            started_ms = local_epoch_ms(row.get("started_at"))
            if started_ms is not None:
                earliest_stream_ms = min(earliest_stream_ms, started_ms)
            raw_dir = str(row.get("session_dir") or "").strip()
            if not raw_dir:
                continue
            session_dir = Path(raw_dir).resolve(strict=False)
            recorder = sess.get("recorder")
            manifest = (
                getattr(recorder, "timeline", None)
                if int(row["id"]) == int(stream_id) else None)
            if manifest is None:
                try:
                    manifest = TimelineManifest.open_existing(session_dir)
                except (OSError, ValueError):
                    log.error("[%s] 录像时间线无法读取，正式产物保持等待", room_key)
                    continue
            if manifest is None:
                continue
            sources.append((row, session_dir, manifest))
            for item in manifest.parts:
                if item.state == "complete" and item.duration_ms > 0:
                    latest_media_end_ms = max(
                        latest_media_end_ms,
                        int(item.first_media_at_ms) + int(item.duration_ms),
                    )
        return sources, earliest_stream_ms, latest_media_end_ms

    @staticmethod
    def _merge_timeline_window(
            sources: list[tuple[dict, Path, TimelineManifest]],
            absolute_start_ms: int, absolute_end_ms: int,
    ) -> tuple[WindowCoverage, list[dict], list[tuple[int, int]]]:
        """Union real wall-clock slices without moving later media into a gap."""
        raw_slices: list[dict] = []
        timeline_incomplete = False
        for row, session_dir, manifest in sources:
            coverage = manifest.window_coverage(absolute_start_ms, absolute_end_ms)
            timeline_incomplete = (
                timeline_incomplete or coverage.timeline_state == "incomplete")
            for item in coverage.slices:
                path = (session_dir / item.relative_path).resolve(strict=False)
                try:
                    path.relative_to(session_dir)
                except ValueError:
                    continue
                if not path.is_file() or path.stat().st_size <= 0:
                    continue
                raw_slices.append({
                    "path": path,
                    "relative_path": item.relative_path,
                    "wall_start_ms": int(item.wall_start_ms),
                    "wall_end_ms": int(item.wall_end_ms),
                    "source_start_ms": int(item.source_start_ms),
                    "source_end_ms": int(item.source_end_ms),
                    "stream": row,
                })
        raw_slices.sort(key=lambda item: (
            item["wall_start_ms"], item["wall_end_ms"],
            int(item["stream"]["id"]), str(item["relative_path"])))

        merged: list[dict] = []
        gaps: list[tuple[int, int]] = []
        wall_cursor = int(absolute_start_ms)
        for raw in raw_slices:
            slice_start = max(
                int(absolute_start_ms), int(raw["wall_start_ms"]), wall_cursor)
            slice_end = min(int(absolute_end_ms), int(raw["wall_end_ms"]))
            if slice_end <= slice_start:
                continue
            if slice_start > wall_cursor:
                gaps.append((wall_cursor, slice_start))
            adjustment = slice_start - int(raw["wall_start_ms"])
            merged.append({
                **raw,
                "wall_start_ms": slice_start,
                "wall_end_ms": slice_end,
                "source_start_ms": int(raw["source_start_ms"]) + adjustment,
                "source_end_ms": (
                    int(raw["source_start_ms"]) + adjustment
                    + slice_end - slice_start),
            })
            wall_cursor = slice_end
        if wall_cursor < int(absolute_end_ms):
            gaps.append((wall_cursor, int(absolute_end_ms)))
        covered_ms = sum(
            int(item["wall_end_ms"]) - int(item["wall_start_ms"])
            for item in merged)
        coverage = WindowCoverage(
            window_start_ms=int(absolute_start_ms),
            window_end_ms=int(absolute_end_ms),
            slices=tuple(CoverageSlice(
                relative_path=(
                    f"{Path(str(item['stream'].get('session_dir') or '')).name}/"
                    f"{item['relative_path']}"),
                wall_start_ms=int(item["wall_start_ms"]),
                wall_end_ms=int(item["wall_end_ms"]),
                source_start_ms=int(item["source_start_ms"]),
                source_end_ms=int(item["source_end_ms"]),
            ) for item in merged),
            gaps=tuple(gaps),
            covered_ms=covered_ms,
            missing_ms=(int(absolute_end_ms) - int(absolute_start_ms) - covered_ms),
            timeline_state=("incomplete" if timeline_incomplete else "complete"),
        )
        return coverage, merged, gaps

    @staticmethod
    def _window_owner_stream(merged: list[dict],
                             scheduled_names: tuple[str, ...]) -> dict | None:
        """Pick the stream that actually owns most of one scheduled hour."""
        duration_by_stream: dict[int, int] = {}
        first_media_by_stream: dict[int, int] = {}
        streams: dict[int, dict] = {}
        for item in merged:
            source = item.get("stream")
            if not isinstance(source, dict) or source.get("id") is None:
                continue
            stream_id = int(source["id"])
            duration_by_stream[stream_id] = (
                duration_by_stream.get(stream_id, 0)
                + max(0, int(item["wall_end_ms"]) - int(item["wall_start_ms"])))
            first_media_by_stream[stream_id] = min(
                first_media_by_stream.get(stream_id, int(item["wall_start_ms"])),
                int(item["wall_start_ms"]),
            )
            streams[stream_id] = source
        if not streams:
            return None
        scheduled_names_set = set(scheduled_names)
        scheduled = {
            stream_id for stream_id, source in streams.items()
            if str(source.get("anchor_name") or "") in scheduled_names_set
        }
        candidates = scheduled or set(streams)
        owner_id = max(candidates, key=lambda stream_id: (
            duration_by_stream.get(stream_id, 0),
            -first_media_by_stream.get(stream_id, 0),
            -stream_id,
        ))
        return streams[owner_id]

    def _queue_completed_hour_windows(
            self, room_key: str, sess: dict, parts: list[Path]) -> int:
        """Queue closed clock hours from persisted wall-clock media evidence."""
        if getattr(self, "transcription", None) is None:
            return 0
        stream_id = int(sess["stream_id"])
        stream = self.store.get_stream(stream_id)
        recording_start_ms = local_epoch_ms(stream["started_at"] if stream else None)
        if stream is None or recording_start_ms is None:
            return 0
        current_key = self._business_session_for_stream(stream)
        if not current_key:
            return 0

        sources, earliest_stream_ms, latest_media_end_ms = (
            self._business_timeline_sources(
                room_key, sess, stream_id, stream, current_key))
        if not sources or latest_media_end_ms <= 0:
            return 0
        business_cfg = (getattr(self, "cfg", {}) or {}).get(
            "business_session", {}) or {}
        earliest_start = str(business_cfg.get("earliest_start") or "05:30")
        latest_end = str(business_cfg.get("latest_end") or "01:30")
        planned = plan_business_windows(
            current_key,
            observed_start_ms=earliest_stream_ms,
            observed_end_ms=latest_media_end_ms,
            earliest_start=earliest_start,
            latest_end=latest_end,
        )
        bound_start_ms, bound_end_ms = business_operating_bounds(
            current_key, earliest_start=earliest_start, latest_end=latest_end)
        self.store.upsert_business_session(
            current_key,
            planned_start=datetime.fromtimestamp(
                bound_start_ms / 1000, SHANGHAI).strftime("%Y-%m-%d %H:%M:%S"),
            planned_end=datetime.fromtimestamp(
                bound_end_ms / 1000, SHANGHAI).strftime("%Y-%m-%d %H:%M:%S"),
            status="collecting",
        )
        queued = 0
        for planned_window in planned:
            if str(planned_window["kind"]) != "hourly":
                continue
            absolute_start_ms = int(planned_window["window_start_ms"])
            absolute_end_ms = int(planned_window["window_end_ms"])
            coverage, merged, gaps = self._merge_timeline_window(
                sources, absolute_start_ms, absolute_end_ms)
            absolute_start = datetime.fromtimestamp(
                absolute_start_ms / 1000, SHANGHAI)
            absolute_end = datetime.fromtimestamp(
                absolute_end_ms / 1000, SHANGHAI)
            session_key = business_session_key(absolute_start)
            names = scheduled_anchor_names_for_window(
                self.schedule, absolute_start, absolute_end)
            window_key = shift_window_key(session_key, absolute_start, names)
            if coverage.timeline_state != "complete":
                log.warning(
                    "[%s] 排班小时 %s 时间线未闭合，暂不入队",
                    room_key, window_key)
                continue
            owner_stream = self._window_owner_stream(merged, names)
            owner_start_ms = local_epoch_ms(
                owner_stream.get("started_at") if owner_stream else None)
            if owner_stream is None or owner_start_ms is None:
                log.error("[%s] 排班小时 %s 无法确定录像归属场次",
                          room_key, window_key)
                continue
            owner_stream_id = int(owner_stream["id"])
            owner_live_id = str(owner_stream.get("live_id") or room_key)
            if self.store.query(
                    "SELECT 1 FROM transcription_jobs WHERE purpose='hourly' "
                    "AND shift_window_key=? LIMIT 1", (window_key,)):
                continue
            existing_artifact = self.store.get_hourly_artifact(window_key)
            existing_quality = (
                existing_artifact.get("quality") or {}
                if isinstance(existing_artifact, dict) else {})
            if (existing_quality.get("state") == "complete"
                    or existing_quality.get("stage") == "media_coverage_exceeded"):
                continue

            self.store.upsert_business_session(session_key, status="collecting")
            actual_slices: list[dict] = []
            for item in merged:
                source_stream = item["stream"]
                self.store.add_business_session_source(
                    session_key,
                    live_id=str(source_stream.get("live_id") or ""),
                    stream_id=int(source_stream["id"]),
                )
                actual_slices.append({
                    "kind": "media",
                    "stream_id": int(source_stream["id"]),
                    "anchor_id": int(source_stream["anchor_id"]),
                    "anchor_name": str(source_stream.get("anchor_name") or ""),
                    "live_id": str(source_stream.get("live_id") or ""),
                    "relative_path": str(item["relative_path"]),
                    "actual_start_ms": int(item["wall_start_ms"]),
                    "actual_end_ms": int(item["wall_end_ms"]),
                    "source_start_ms": int(item["source_start_ms"]),
                    "source_end_ms": int(item["source_end_ms"]),
                })
            actual_slices.extend({
                "kind": "gap", "actual_start_ms": start, "actual_end_ms": end,
            } for start, end in gaps)
            actual_slices.sort(key=lambda item: (
                int(item["actual_start_ms"]), 0 if item["kind"] == "media" else 1))
            self.store.upsert_shift_window(
                window_key,
                business_session_key=session_key,
                window_start_ms=absolute_start_ms,
                window_end_ms=absolute_end_ms,
                scheduled_anchors=list(names),
                actual_slices=actual_slices,
                status=("collecting" if formal_brief_allowed(coverage) else "partial"),
            )

            media_start_ms = absolute_start_ms - owner_start_ms
            media_end_ms = absolute_end_ms - owner_start_ms
            coverage_fact = coverage.to_fact()
            self._capture_hourly_fact_batch(
                stream_id=owner_stream_id,
                live_id=owner_live_id,
                business_session_key=session_key,
                shift_window_key=window_key,
                absolute_start_ms=absolute_start_ms,
                absolute_end_ms=absolute_end_ms,
                media_start_ms=media_start_ms,
                media_end_ms=media_end_ms,
                media_coverage=coverage_fact,
                brief_delivery_allowed=formal_brief_allowed(coverage),
            )
            if not formal_brief_allowed(coverage):
                log.warning(
                    "[%s] 排班小时 %s 录像缺口 %.1f 分钟，超过 20 分钟，"
                    "不发正式简报", room_key, window_key,
                    coverage.missing_ms / 60_000)
                continue

            layout: list[dict[str, object]] = []
            for item in merged:
                layout.append({
                    "kind": "media",
                    "path": str(item["path"]),
                    "source_start_ms": int(item["source_start_ms"]),
                    "duration_ms": int(item["wall_end_ms"])
                                   - int(item["wall_start_ms"]),
                    "output_start_ms": int(item["wall_start_ms"])
                                       - absolute_start_ms,
                })
            layout.extend({
                "kind": "gap",
                "duration_ms": end - start,
                "output_start_ms": start - absolute_start_ms,
            } for start, end in gaps)
            layout.sort(key=lambda item: int(item["output_start_ms"]))
            media_parts: list[Path] = []
            seen_paths: set[str] = set()
            for item in layout:
                path = str(item.get("path") or "")
                if item["kind"] == "media" and path not in seen_paths:
                    seen_paths.add(path)
                    media_parts.append(Path(path))
            key = self.transcription.queue_window(
                stream_id=owner_stream_id,
                live_id=owner_live_id,
                parts=media_parts,
                window_start_ms=media_start_ms,
                window_end_ms=media_end_ms,
                media_origin_ms=media_start_ms,
                media_layout=layout,
                media_coverage=coverage_fact,
                purpose="hourly",
                deadline_at=time.time() + int(
                    (self.cfg.get("transcription", {}) or {}).get(
                        "brief_wait_seconds", 600)),
                delivery_key=f"brief:{window_key}",
                business_session_key=session_key,
                shift_window_key=window_key,
                fallback_after_at=(
                    absolute_end_ms / 1000.0 + int(
                        (self.cfg.get("transcription", {}) or {}).get(
                            "fallback_after_seconds", 1800))),
            )
            sess["brief_job_key"] = key
            queued += 1
            log.info(
                "[%s] 绝对排班小时已排队：%s（实际录像 %.1f/60.0 分钟）",
                room_key, window_key, coverage.covered_ms / 60_000,
            )
        return queued

    def _queue_partial_business_window(
            self, room_key: str, sess: dict, parts: list[Path], *,
            boundary_kind: str) -> str:
        """Freeze the real opening/closing fragment for the daily only."""
        if boundary_kind not in {"initial", "final"}:
            raise ValueError("business boundary kind must be initial or final")
        if not parts or getattr(self, "transcription", None) is None:
            return ""
        current_stream_id = int(sess["stream_id"])
        current_stream = self.store.get_stream(current_stream_id)
        current_start_ms = local_epoch_ms(
            current_stream["started_at"] if current_stream else None)
        if current_stream is None or current_start_ms is None:
            return ""
        session_key = self._business_session_for_stream(current_stream)
        if not session_key:
            return ""
        sources, earliest_ms, observed_end_ms = self._business_timeline_sources(
            room_key, sess, current_stream_id, current_stream, session_key)
        if not sources or observed_end_ms <= 0:
            return ""
        business_cfg = (getattr(self, "cfg", {}) or {}).get(
            "business_session", {}) or {}
        earliest_start = str(business_cfg.get("earliest_start") or "05:30")
        latest_end = str(business_cfg.get("latest_end") or "01:30")
        planned = plan_business_windows(
            session_key,
            observed_start_ms=earliest_ms,
            observed_end_ms=observed_end_ms,
            earliest_start=earliest_start,
            latest_end=latest_end,
        )
        boundary_windows = [
            item for item in planned if item["kind"] == boundary_kind]
        if not boundary_windows:
            return ""
        boundary = (boundary_windows[0] if boundary_kind == "initial"
                    else boundary_windows[-1])
        absolute_start_ms = int(boundary["window_start_ms"])
        absolute_end_ms = int(boundary["window_end_ms"])
        # ffprobe/TS 时长常有亚秒级浮动；整点附近不另造一个
        # 几百毫秒的伪边界。完整整点小时已由上一步入队。
        if absolute_end_ms - absolute_start_ms < 1_000:
            return ""
        coverage, merged, gaps = self._merge_timeline_window(
            sources, absolute_start_ms, absolute_end_ms)
        if coverage.timeline_state != "complete" or not merged:
            log.warning("[%s] 经营日%s时间线尚未完整，日报继续等待",
                        room_key, "开头" if boundary_kind == "initial" else "尾段")
            return ""
        owner_item = merged[0] if boundary_kind == "initial" else merged[-1]
        owner_stream = owner_item["stream"]
        owner_stream_id = int(owner_stream["id"])
        owner_start_ms = local_epoch_ms(owner_stream.get("started_at"))
        if owner_start_ms is None:
            return ""
        media_start_ms = absolute_start_ms - owner_start_ms
        media_end_ms = absolute_end_ms - owner_start_ms
        start_dt = datetime.fromtimestamp(absolute_start_ms / 1000, SHANGHAI)
        end_dt = datetime.fromtimestamp(absolute_end_ms / 1000, SHANGHAI)
        names = scheduled_anchor_names_for_window(self.schedule, start_dt, end_dt)
        key_builder = (initial_partial_window_key
                       if boundary_kind == "initial"
                       else final_partial_window_key)
        window_key = key_builder(session_key, start_dt, end_dt, names)
        if self.store.query(
                "SELECT 1 FROM transcription_jobs WHERE shift_window_key=? LIMIT 1",
                (window_key,)):
            return window_key
        bound_start_ms, bound_end_ms = business_operating_bounds(
            session_key, earliest_start=earliest_start, latest_end=latest_end)
        self.store.upsert_business_session(
            session_key,
            planned_start=datetime.fromtimestamp(
                bound_start_ms / 1000, SHANGHAI).strftime("%Y-%m-%d %H:%M:%S"),
            planned_end=datetime.fromtimestamp(
                bound_end_ms / 1000, SHANGHAI).strftime("%Y-%m-%d %H:%M:%S"),
            status="collecting",
        )
        actual_slices: list[dict] = []
        for item in merged:
            source_stream = item["stream"]
            self.store.add_business_session_source(
                session_key,
                live_id=str(source_stream.get("live_id") or ""),
                stream_id=int(source_stream["id"]),
            )
            actual_slices.append({
                "kind": "media",
                "stream_id": int(source_stream["id"]),
                "anchor_id": int(source_stream["anchor_id"]),
                "anchor_name": str(source_stream.get("anchor_name") or ""),
                "live_id": str(source_stream.get("live_id") or ""),
                "relative_path": str(item["relative_path"]),
                "actual_start_ms": int(item["wall_start_ms"]),
                "actual_end_ms": int(item["wall_end_ms"]),
                "source_start_ms": int(item["source_start_ms"]),
                "source_end_ms": int(item["source_end_ms"]),
            })
        actual_slices.extend({
            "kind": "gap", "actual_start_ms": start, "actual_end_ms": gap_end,
        } for start, gap_end in gaps)
        actual_slices.sort(key=lambda item: (
            int(item["actual_start_ms"]), 0 if item["kind"] == "media" else 1))
        allowed = formal_brief_allowed(coverage)
        self.store.upsert_shift_window(
            window_key,
            business_session_key=session_key,
            window_start_ms=absolute_start_ms,
            window_end_ms=absolute_end_ms,
            scheduled_anchors=list(names),
            actual_slices=actual_slices,
            status="collecting" if allowed else "partial",
        )
        coverage_fact = coverage.to_fact()
        self._capture_hourly_fact_batch(
            stream_id=owner_stream_id,
            live_id=str(owner_stream.get("live_id") or room_key),
            business_session_key=session_key,
            shift_window_key=window_key,
            absolute_start_ms=absolute_start_ms,
            absolute_end_ms=absolute_end_ms,
            media_start_ms=media_start_ms,
            media_end_ms=media_end_ms,
            media_coverage=coverage_fact,
            brief_delivery_allowed=allowed,
        )
        if not allowed:
            log.warning(
                "[%s] 经营日%s录像缺口 %.1f 分钟，超过 20 分钟；"
                "不生成伪完整日报材料",
                room_key, "开头" if boundary_kind == "initial" else "尾段",
                coverage.missing_ms / 60_000)
            return ""
        layout: list[dict[str, object]] = [{
            "kind": "media",
            "path": str(item["path"]),
            "source_start_ms": int(item["source_start_ms"]),
            "duration_ms": int(item["wall_end_ms"]) - int(item["wall_start_ms"]),
            "output_start_ms": int(item["wall_start_ms"]) - absolute_start_ms,
        } for item in merged]
        layout.extend({
            "kind": "gap",
            "duration_ms": gap_end - start,
            "output_start_ms": start - absolute_start_ms,
        } for start, gap_end in gaps)
        layout.sort(key=lambda item: int(item["output_start_ms"]))
        media_parts: list[Path] = []
        seen_paths: set[str] = set()
        for item in layout:
            path = str(item.get("path") or "")
            if item["kind"] == "media" and path not in seen_paths:
                seen_paths.add(path)
                media_parts.append(Path(path))
        key = self.transcription.queue_window(
            stream_id=owner_stream_id,
            live_id=str(owner_stream.get("live_id") or room_key),
            parts=media_parts,
            window_start_ms=media_start_ms,
            window_end_ms=media_end_ms,
            media_origin_ms=media_start_ms,
            media_layout=layout,
            media_coverage=coverage_fact,
            purpose="daily_tail",
            business_session_key=session_key,
            shift_window_key=window_key,
        )
        log.info(
            "[%s] 经营日%s已入队：%s（实际录像 %.1f/%.1f 分钟，"
            "不发额外简报）",
            room_key, "开头" if boundary_kind == "initial" else "尾段",
            key, coverage.covered_ms / 60_000,
            (coverage.window_end_ms - coverage.window_start_ms) / 60_000,
        )
        return window_key

    def _queue_initial_partial_window(
            self, room_key: str, sess: dict, parts: list[Path]) -> str:
        return self._queue_partial_business_window(
            room_key, sess, parts, boundary_kind="initial")

    def _queue_final_partial_window(
            self, room_key: str, sess: dict, parts: list[Path]) -> str:
        return self._queue_partial_business_window(
            room_key, sess, parts, boundary_kind="final")

    def _historical_hour_boundary_snapshots(
            self, *, business_session_key: str, live_id: str,
            window_start_ms: int, window_end_ms: int,
    ) -> tuple[dict, dict] | None:
        """Return auditable start/end totals for a delayed hourly recovery.

        A current total read after an old hour has closed cannot be used as that
        hour's endpoint.  The only safe recovery source is a persisted screen
        snapshot captured close to each original clock boundary, including one
        written by the next technical stream after a rotation.
        """
        rows = self.store.query(
            """SELECT b.* FROM brief_snapshots b
               JOIN streams s ON s.id=b.stream_id
               WHERE s.business_session_key=? AND s.live_id=?
                 AND b.source!='' AND b.data_state='ok'
               ORDER BY b.id""",
            (str(business_session_key), str(live_id)),
        )
        snapshots: list[tuple[int, dict]] = []
        for row in rows:
            snapshot = dict(row)
            captured_ms = local_epoch_ms(snapshot.get("ts"))
            if captured_ms is None:
                continue
            snapshots.append((captured_ms, snapshot))

        def nearest(boundary_ms: int) -> dict | None:
            candidates = [
                (abs(captured_ms - int(boundary_ms)), captured_ms, snapshot)
                for captured_ms, snapshot in snapshots
                if abs(captured_ms - int(boundary_ms))
                <= HISTORICAL_BOUNDARY_SNAPSHOT_TOLERANCE_MS
            ]
            if not candidates:
                return None
            _distance, _captured_ms, snapshot = min(
                candidates, key=lambda item: (item[0], item[1]))
            result = dict(snapshot)
            result["fetched_at"] = str(result.get("ts") or "")
            return result

        start_snapshot = nearest(window_start_ms)
        end_snapshot = nearest(window_end_ms)
        if start_snapshot is None or end_snapshot is None:
            return None
        return start_snapshot, end_snapshot

    def _capture_hourly_fact_batch(
            self, *, stream_id: int, live_id: str,
            business_session_key: str, shift_window_key: str,
            absolute_start_ms: int, absolute_end_ms: int,
            media_start_ms: int, media_end_ms: int,
            media_coverage: dict | None = None,
            brief_delivery_allowed: bool = True,
            persist_on_failure: bool = True) -> dict:
        """Freeze the boundary metrics before remote transcription/AI can run late.

        ``persist_on_failure=False`` is the fact-rescue mode: a late re-capture
        that still cannot complete all ten metrics must not overwrite the
        already persisted artifact (which may hold usable minute trends)."""

        from ..business_facts import build_hourly_artifact
        from ..hourly_fact_aggregation import build_persisted_hourly_metrics

        metrics: dict = {
            "data_state": "unavailable",
            "data_issues": ["小时边界经营数据尚未捕获"],
        }
        metrics_text = "小时边界经营数据尚未捕获"
        # 新正式链路只消费在原整点/场次切换时已持久化的累计边界。
        # 旧 brief_snapshots 仅作迁移兼容，仍受同一 ±300s 容差约束。
        boundary_snapshots = self._historical_hour_boundary_snapshots(
            business_session_key=business_session_key,
            live_id=live_id,
            window_start_ms=absolute_start_ms,
            window_end_ms=absolute_end_ms,
        )
        try:
            metrics, metrics_text = build_persisted_hourly_metrics(
                self.cfg, self.store,
                business_session_key=str(business_session_key),
                shift_window_key=str(shift_window_key),
                window_start_ms=int(absolute_start_ms),
                window_end_ms=int(absolute_end_ms),
                fallback_live_id=str(live_id),
                fallback_stream_id=int(stream_id),
                legacy_boundaries=boundary_snapshots,
            )
        except MtopAuthError as exc:
            metrics = {
                "data_state": "unavailable",
                "data_issues": ["淘宝登录态失效，小时边界数据待恢复"],
            }
            metrics_text = metrics["data_issues"][0]
            log.warning("[%s] 小时边界数据因鉴权失败未冻结: %s",
                        live_id, str(exc)[:120])
        except Exception as exc:
            metrics = {
                "data_state": "unavailable",
                "data_issues": [f"小时边界数据捕获失败：{str(exc)[:160]}"],
            }
            metrics_text = metrics["data_issues"][0]
            log.warning("[%s] 小时边界数据捕获失败: %s",
                        live_id, str(exc)[:160])

        start = datetime.fromtimestamp(int(absolute_start_ms) / 1000, SHANGHAI)
        end = datetime.fromtimestamp(int(absolute_end_ms) / 1000, SHANGHAI)
        metrics["period_label"] = f"{start:%H:%M}–{end:%H:%M}"
        metrics["frozen_boundary"] = bool(metrics.get("frozen_boundary"))
        metrics["window_start_ms"] = int(absolute_start_ms)
        metrics["window_end_ms"] = int(absolute_end_ms)

        stream = self.store.get_stream(int(stream_id))
        anchor = self.store.get_anchor(int(stream["anchor_id"])) if stream else None
        artifact = build_hourly_artifact(
            business_session=business_session_key,
            shift_window=shift_window_key,
            window_start_ms=int(absolute_start_ms),
            window_end_ms=int(absolute_end_ms),
            identity={
                "stream_id": int(stream_id),
                "anchor_id": int(stream["anchor_id"]) if stream else 0,
                "anchor_name": str(anchor["name"] if anchor else ""),
                "live_id": str(live_id),
                "actual_media_start_ms": int(media_start_ms),
                "actual_media_end_ms": int(media_end_ms),
            },
            metrics=metrics,
            series=metrics.get("series") or {},
            presentation={"metrics_text": metrics_text},
            quality={
                "state": "partial",
                "stage": ("waiting_transcription_and_analysis"
                          if brief_delivery_allowed else "media_coverage_exceeded"),
                "brief_delivery_allowed": bool(brief_delivery_allowed),
                **({"media_coverage": dict(media_coverage)}
                   if media_coverage else {}),
                "metrics_frozen": metrics.get("data_state") == "ok",
                "data_state": metrics.get("data_state", "unavailable"),
            },
            sources=[{
                "kind": "hour_boundary_metrics",
                "source_intervals": list((metrics.get("fact_provenance") or {}).get(
                    "required_source_intervals") or []),
                "window_start_ms": int(absolute_start_ms),
                "window_end_ms": int(absolute_end_ms),
                "observed_at": str(metrics.get("fetched_at") or ""),
                "quality_state": str(metrics.get("data_state") or "unavailable"),
            }],
        )
        if persist_on_failure or formal_hourly_metrics_ready(artifact):
            self.store.save_hourly_artifact(shift_window_key, artifact)
        return metrics

    def _rescue_waiting_evidence_facts(self, *, now: float | None = None,
                                       limit: int = 1) -> int:
        """为因小时事实不完整而搁置的正式简报重抓边界事实。

        只碰 waiting_evidence 的正式小时：重抓成功写回的完整事实会由
        save_hourly_artifact 自动唤醒消费方并照常发送（含迟到补发）。
        失败时保留原 artifact（persist_on_failure=False），按持久化退避
        重试，重试上限由 store 维护，保证千牛接口低频。鉴权未恢复或
        经营日已封存的窗口不再消耗重抓次数。
        """
        now = float(time.time() if now is None else now)
        rescued = 0
        for row in self.store.claim_fact_rescue_jobs(now=now, limit=limit):
            job = dict(row)
            job_key = str(job["job_key"])
            try:
                session_state = self.store.get_taobao_session_state()
                if (session_state is None
                        or str(session_state["status"]) != "healthy"):
                    # 鉴权未恢复时不计数（claim 的软租约已排到 10 分钟后），
                    # 等登录恢复后下一轮再试。
                    continue
                shift_window_key = str(job.get("shift_window_key") or "")
                session_key = str(job.get("business_session_key") or "")
                if session_key and self.store.business_session_is_ended(session_key):
                    self.store.finish_fact_rescue(
                        job_key, ready=False, now=time.time(),
                        give_up_reason="等待完整小时经营事实；经营日已封存，不再补发")
                    log.warning("[%s] 经营日已封存，跳过小时事实重抓: %s",
                                session_key, job_key)
                    continue
                artifact = self.store.get_hourly_artifact(shift_window_key)
                if artifact is None:
                    self.store.finish_fact_rescue(
                        job_key, ready=False, now=time.time(),
                        give_up_reason="等待完整小时经营事实；缺少小时产物，重抓放弃")
                    continue
                identity = artifact.get("identity") or {}
                quality = artifact.get("quality") or {}
                absolute_start_ms = int(identity.get("window_start_ms") or 0)
                absolute_end_ms = int(identity.get("window_end_ms") or 0)
                if absolute_end_ms <= absolute_start_ms:
                    self.store.finish_fact_rescue(
                        job_key, ready=False, now=time.time(),
                        give_up_reason="等待完整小时经营事实；小时身份缺失，重抓放弃")
                    continue
                self._capture_hourly_fact_batch(
                    stream_id=int(job["stream_id"]),
                    live_id=str(job.get("live_id") or identity.get("live_id") or ""),
                    business_session_key=session_key,
                    shift_window_key=shift_window_key,
                    absolute_start_ms=absolute_start_ms,
                    absolute_end_ms=absolute_end_ms,
                    media_start_ms=int(identity.get(
                        "actual_media_start_ms", job.get("window_start_ms") or 0)),
                    media_end_ms=int(identity.get(
                        "actual_media_end_ms", job.get("window_end_ms") or 0)),
                    media_coverage=(json.loads(
                        job.get("media_coverage_json") or "{}") or None),
                    brief_delivery_allowed=bool(
                        quality.get("brief_delivery_allowed", True)),
                    persist_on_failure=False,
                )
                refreshed = self.store.get_hourly_artifact(shift_window_key)
                ready = formal_hourly_metrics_ready(refreshed)
                self.store.finish_fact_rescue(job_key, ready=ready, now=time.time())
                if ready:
                    rescued += 1
                    log.info("[%s] 小时事实重抓补齐，简报恢复发送: %s",
                             session_key, job_key)
                else:
                    log.info("[%s] 小时事实重抓仍未完整: %s", session_key, job_key)
            except Exception:
                log.exception("小时事实重抓异常（%s）", job_key)
                self.store.finish_fact_rescue(job_key, ready=False, now=time.time())
        return rescued

    def _converge_stale_business_observations(self, *, now: float | None = None) -> int:
        """关账被遗忘的过期观察并解锁对应日报。

        下播观察只在当前房间的生命周期事件里被检查；房间切到新 liveId
        后旧业务日可能永远停在 observing（2026-08-13 实测 20260812
        停 13 小时且日报 parked）。这里周期收敛：finalize_business_session
        自带状态/截止/在播流三重门禁，不会误关仍在直播的业务日。
        """
        now = float(time.time() if now is None else now)
        closed = 0
        rows = self.store.query(
            """SELECT business_session_key FROM business_sessions
               WHERE status='observing' AND observation_deadline>0
                 AND observation_deadline<=?
               ORDER BY business_session_key LIMIT 3""",
            (now,),
        )
        for row in rows:
            key = str(row["business_session_key"])
            job = self.store.query(
                "SELECT live_id FROM platform_review_jobs "
                "WHERE business_session_key=? LIMIT 1", (key,))
            live_id = str(job[0]["live_id"] or "") if job else ""
            if not live_id:
                # 正常下播观察期内日报任务本来尚未创建。若此时
                # watcher 重启或配置切到新 liveId，不能反过来依赖
                # platform_review_jobs 找日报身份。只接受同业务日且
                # 已持久化连续下播确认的场次，避免把探测异常当真下播。
                required = max(
                    2, int(getattr(self, "offline_confirmations", 3)))
                source = self.store.query(
                    """SELECT s.live_id,lifecycle.last_state,
                              lifecycle.ended_confirmations
                       FROM streams s
                       LEFT JOIN room_lifecycle_state lifecycle
                         ON lifecycle.live_id=s.live_id
                       WHERE s.business_session_key=? AND s.live_id!=''
                       ORDER BY s.started_at DESC,s.id DESC LIMIT 1""",
                    (key,),
                )
                latest = dict(source[0]) if source else {}
                confirmed = (
                    str(latest.get("last_state") or "") == "ended"
                    and int(latest.get("ended_confirmations") or 0) >= required
                )
                if confirmed:
                    live_id = str(latest.get("live_id") or "")
                else:
                    marked = self.store.mark_stale_observation_needs_attention(
                        key, now=now,
                        issue_code="STALE_OBSERVATION_WITHOUT_CONFIRMED_SOURCE",
                        evidence={
                            "candidate_live_id": str(
                                latest.get("live_id") or ""),
                            "last_state": str(
                                latest.get("last_state") or "missing"),
                            "ended_confirmations": int(
                                latest.get("ended_confirmations") or 0),
                            "required_confirmations": required,
                        },
                    )
                    if marked:
                        log.error(
                            "[%s] 过期观察缺少最新场次的可验证下播身份，"
                            "已停止自动日报并标记人工核验",
                            key,
                        )
                        self._send_alert(
                            "状态对账:STALE_OBSERVATION_WITHOUT_CONFIRMED_SOURCE",
                            "业务日状态无法安全自动恢复，已停止正式日报并等待人工核验",
                        )
                    continue
            if not live_id:
                continue
            wait_seconds = max(60, int(
                (getattr(self, "cfg", {}) or {}).get("review", {}).get(
                    "platform_wait_minutes", 30)) * 60)
            if self.store.finalize_business_session(
                    key, live_id=live_id, now=now,
                    settlement_wait_seconds=wait_seconds):
                closed += 1
                log.warning("[%s] 过期观察已自动关账并解锁日报", key)
        return closed

    def _maintenance_once(self, job_key: str, fn, *,
                          success_ttl_sec: int = 86400,
                          retry_sec: int = 3600) -> bool:
        """执行一个持久化维护任务；成功、失败和退避均跨 watcher 重启保留。"""
        if not self.store.claim_maintenance(job_key):
            return False
        try:
            fn()
        except Exception as exc:
            self.store.finish_maintenance(
                job_key, success=False, retry_sec=retry_sec, error=str(exc))
            log.exception("维护任务失败（%s）", job_key)
            return False
        self.store.finish_maintenance(
            job_key, success=True, success_ttl_sec=success_ttl_sec)
        return True

    def _run_daily_maintenance(self, today: str) -> None:
        """Run bounded local retention sweeps at most once per hour."""
        del today

        def cleanup_recordings() -> None:
            from ..cleanup import cleanup_old_recordings
            cleanup_old_recordings(self.cfg, self.store)

        self._maintenance_once(
            "cleanup:rolling", cleanup_recordings, success_ttl_sec=3600)

        def cleanup_transcription_media() -> None:
            from ..cleanup import cleanup_orphan_transcription_media
            cleanup_orphan_transcription_media(self.cfg, self.store)

        self._maintenance_once(
            "transcription-media:rolling", cleanup_transcription_media,
            success_ttl_sec=3600)

    def _refresh_schedule(self) -> None:
        """刷新小时排班；配置飞书来源时不阻塞录制主循环。"""
        source = getattr(self, "_schedule_feishu", {})
        if isinstance(source, dict) and source.get("url") and source.get("sheet_id"):
            latest_future = getattr(self, "_schedule_sync_future", None)
            if latest_future is not None and latest_future.done():
                self._schedule_sync_future = None
                try:
                    latest = latest_future.result()
                except Exception as exc:
                    log.warning("飞书主播排班同步失败，保留当前排班：%s", type(exc).__name__)
                else:
                    applied = _defer_current_hour_schedule_change(
                        getattr(self, "schedule", {}), latest, now_shanghai())
                    if applied != getattr(self, "schedule", {}):
                        self.schedule = applied
                        self._schedule_loaded_date = now_shanghai().date()
                        active = sync_anchor_roster(self.store, self.cfg, self.schedule)
                        log.info("飞书主播排班已重载，当前启用主播 %d 人", len(active))
                    try:
                        persist_weekly_schedule(
                            getattr(self, "schedule", {}), now=now_shanghai())
                    except Exception as persist_exc:
                        log.warning("飞书主播排班落盘失败：%s",
                                    type(persist_exc).__name__)
            if getattr(self, "_schedule_sync_future", None) is None:
                try:
                    interval = max(1, int(source.get("sync_seconds") or 300))
                except (TypeError, ValueError):
                    interval = 300
                now = time.time()
                last = float(getattr(self, "_last_schedule_sync_at", 0.0) or 0.0)
                pool = getattr(self, "schedule_pool", None)
                if pool is not None and now - last >= interval:
                    self._last_schedule_sync_at = now
                    self._schedule_sync_future = pool.submit(
                        load_feishu_anchor_schedule, dict(source))
            return

        # 未配置飞书来源时，保留原有本地 JSON 热重载行为。
        latest = load_schedule(self._schedule_file)
        today = now_shanghai().date()
        # 排班文件被编辑器原地覆盖时可能短暂为空/半截 JSON。连续三次都为空
        # 才接受“清空排班”，避免一次瞬态读取把全部排班主播停用。
        if not latest and self.schedule and today == self._schedule_loaded_date:
            self._empty_schedule_reads = getattr(self, "_empty_schedule_reads", 0) + 1
            if self._empty_schedule_reads < 3:
                log.warning("排班表本轮读取为空，保留上一版（%d/3）",
                            self._empty_schedule_reads)
                return
        else:
            self._empty_schedule_reads = 0
        if latest != self.schedule or today != self._schedule_loaded_date:
            self.schedule = latest
            self._schedule_loaded_date = today
            active = sync_anchor_roster(self.store, self.cfg, self.schedule)
            log.info("排班表已重载，当前启用主播 %d 人", len(active))

    def _tick_transcriptions(self) -> None:
        """在独立单线程推进上传/轮询；远端 processing 立即交还，不占流水线池。"""
        service = getattr(self, "transcription", None)
        if service is None:
            return
        future = getattr(self, "_transcription_future", None)
        if future is not None and not future.done():
            return
        if future is not None:
            try:
                future.result()
            except Exception:
                log.exception("转写后台循环异常")
        self._transcription_future = self.transcription_pool.submit(
            self._transcription_cycle)

    def _tick_taobao_session(self) -> None:
        """在独立单线程检查/恢复浏览器登录态，绝不阻塞录像循环。"""
        keeper = getattr(self, "taobao_session_keeper", None)
        if keeper is None:
            return
        future = getattr(self, "_session_future", None)
        if future is not None and not future.done():
            return
        if future is not None:
            try:
                # 2026-08-08 修复：主循环曾无限等待 Ego Lite 浏览器检查，
                # 浏览器/网络异常时主循环永久卡死（简报/轮询全部停摆）。
                # 加有界等待，超时后放弃本轮检查，主循环继续。
                future.result(timeout=45)
            except TimeoutError:
                log.warning("淘宝登录态检查超时（>45s），主循环继续，下一轮再查")
            except Exception:
                log.exception("淘宝登录态恢复任务异常")
        self._session_future = self.session_pool.submit(keeper.tick)

    def _transcription_cycle(self) -> None:
        import json
        service = self.transcription
        service.tick(limit=3)
        alert_now = time.time()
        blocked = self.store.query(
            """SELECT * FROM transcription_jobs
               WHERE ((remote_status='blocked'
                        AND alert_after_at<=?
                        AND ((alert_status='pending')
                             OR (alert_status='sent' AND last_alert_at<=?)))
                       OR (error LIKE '小时音频%' AND consumer_status<>'sent'
                           AND alert_after_at>0 AND alert_after_at<=?
                           AND (alert_status='pending'
                                OR (alert_status='sent' AND last_alert_at<=?))))
               ORDER BY last_alert_at,updated_at LIMIT 3""",
            (alert_now, alert_now - 1800, alert_now, alert_now - 1800),
        )
        retry_by_scope: dict[str, float] = {}
        for row in blocked:
            job = dict(row)
            scope = str(job.get("live_id") or "飞书妙记")
            if scope in retry_by_scope:
                self.store.update_transcription_job(
                    job["job_key"], alert_after_at=retry_by_scope[scope])
                continue
            if "小时音频" in str(job.get("error") or ""):
                message = (
                    "小时音频拼装持续失败，正式简报无法生成，请人工介入。任务："
                    f"{job['job_key']}；错误：{str(job.get('error') or '')[:120]}")
            else:
                message = (
                    "飞书妙记任务已阻塞，未自动登录或切换复盘备胎。请运行管理命令选择"
                    f"“重试飞书”或“确认使用FunASR”。任务：{job['job_key']}；"
                    f"类型：{job.get('error_class') or 'unknown'}")
            delivered = self._send_alert(
                scope,
                message,
                cooldown_sec=1800,
            )
            retry_state = getattr(self, "_alert_retry_after_at", {})
            retry_at = float(retry_state.get(scope, alert_now + 60))
            retry_by_scope[scope] = retry_at
            if delivered:
                self.store.update_transcription_job(
                    job["job_key"], alert_status="sent", last_alert_at=alert_now,
                    alert_after_at=retry_at)
            else:
                self.store.update_transcription_job(
                    job["job_key"], alert_after_at=retry_at)

        for row in self.store.claim_ready_brief_consumers(limit=2):
            job = dict(row)
            if str(job.get("purpose")) in {"hourly", "daily_tail"}:
                session_state = self.store.get_taobao_session_state()
                if str(session_state["status"]) != "healthy":
                    self.store.park_brief_consumer_for_auth(
                        job["job_key"], now=time.time())
                    continue
                expected_hash = str(session_state["last_cookie_hash"] or "")
                local_cookie = str((self.cfg.get("taobao", {}) or {}).get("cookie") or "")
                local_hash = hashlib.sha256(local_cookie.encode("utf-8")).hexdigest()
                if expected_hash and local_hash != expected_hash:
                    # 手工恢复已在另一进程激活新会话；旧 watcher
                    # 不得用内存中的旧 Cookie 抢回任务。
                    self.store.update_transcription_job(
                        job["job_key"], consumer_status="pending",
                        consumer_next_at=time.time() + 5,
                        error="waiting for watcher with current Taobao session")
                    continue
            result = service.result_for(
                job["job_key"], allow_fallback=True)
            if result is None:
                self.store.update_transcription_job(
                    job["job_key"], consumer_status="failed",
                    consumer_next_at=time.time() + 30)
                continue
            parts = [Path(item) for item in json.loads(job.get("media_manifest") or "[]")]
            media_coverage = json.loads(job.get("media_coverage_json") or "{}")
            if str(job.get("purpose")) == "review":
                shifted = [
                    (segment.start_ms + int(job["window_start_ms"]),
                     segment.end_ms + int(job["window_start_ms"]), segment.text)
                    for segment in result.segments if segment.text.strip()
                ]
                self.store.save_brief_transcripts(
                    int(job["stream_id"]), str(job["job_key"]), shifted)
                self.store.update_transcription_job(
                    job["job_key"], consumer_status="sent", sent_at=time.time())
                continue

            shifted = [
                (segment.start_ms + int(job["window_start_ms"]),
                 segment.end_ms + int(job["window_start_ms"]), segment.text)
                for segment in result.segments if segment.text.strip()
            ]
            self.store.save_brief_transcripts(
                int(job["stream_id"]), str(job["job_key"]), shifted)
            # 自动备胎已发过本小时简报后，飞书远端出稿只替换正式日报语料，
            # 绝不补发旧简报。
            if float(job.get("sent_at") or 0) > 0:
                self.store.update_transcription_job(
                    job["job_key"], consumer_status="sent")
                continue

            if str(job.get("purpose")) == "hourly":
                artifact = self.store.get_hourly_artifact(
                    str(job.get("shift_window_key") or ""))
                if (artifact is not None
                        and not formal_hourly_metrics_ready(artifact)):
                    self.store.park_brief_consumer_for_evidence(job["job_key"])
                    log.warning(
                        "小时经营事实未完整，任务已停止定时重建并等待真实事实写入："
                        "job=%s", job["job_key"],
                    )
                    continue

            stream = self.store.get_stream(int(job["stream_id"]))
            if not stream:
                self.store.update_transcription_job(
                    job["job_key"], consumer_status="failed",
                    consumer_next_at=time.time() + 300, error="场次不存在")
                continue
            stream_started_ms = local_epoch_ms(stream["started_at"])
            metric_end_ms = (stream_started_ms + int(job["window_end_ms"])
                             if stream_started_ms is not None else None)
            future = self.pool.submit(
                run_briefing, self.cfg, self.store, int(stream["anchor_id"]),
                int(job["stream_id"]), parts,
                delivery_key=str(job.get("delivery_key") or ""),
                transcription_result=result,
                window_start_ms=int(job["window_start_ms"]),
                window_end_ms=int(job["window_end_ms"]),
                job_key=str(job["job_key"]),
                metric_end_ms=metric_end_ms,
                business_session_key=str(job.get("business_session_key") or ""),
                shift_window_key=str(job.get("shift_window_key") or ""),
                historical_recovery=(str(job.get("error") or "")
                                     == "delayed_auth_recovery"),
                send_notification=(str(job.get("purpose")) == "hourly"),
                media_coverage=media_coverage,
            )

            def done(completed, *, item=job) -> None:
                try:
                    ok = bool(completed.result())
                except MtopAuthError as exc:
                    retired = self._handle_brief_auth_failure(item, exc)
                    log.warning(
                        "妙记简报%s job=%s",
                        "已交还给新登录态重试" if retired
                        else "已暂停等待淘宝登录恢复",
                        item["job_key"])
                    return
                except Exception:
                    ok = False
                    log.exception("妙记简报消费失败 job=%s", item["job_key"])
                # consumer_status 表示“逐字稿已消费”，不是卡片投递回执。
                # 正式分析失败可以按退避继续消费；投递回执不明则停在
                # 人工核验，不自动重发，也不阻塞下播日报等待。
                delivery = str(item.get("delivery_key") or "")
                delivery_state = ""
                if delivery:
                    from ..notify.feishu import delivery_status
                    delivery_state = delivery_status(delivery)
                if ok:
                    self.store.update_transcription_job(
                        item["job_key"], consumer_status="sent", sent_at=time.time(),
                        error="")
                elif delivery_state in {"sending", "delivery_unknown"}:
                    self.store.update_transcription_job(
                        item["job_key"], consumer_status="sent", sent_at=0,
                        error="简报投递结果待人工核验，已停止自动重发")
                else:
                    self.store.update_transcription_job(
                        item["job_key"], consumer_status="failed",
                        consumer_next_at=time.time() + 300,
                        error="简报构建或投递失败（逐字稿已入库）")
            future.add_done_callback(done)

    def _handle_brief_auth_failure(self, item: dict, exc: MtopAuthError) -> bool:
        """旧客户端迟到失败只重试；当前会话失败才等待人工登录。"""
        from .mtop import current_auth_epoch
        error_epoch = getattr(exc, "auth_epoch", None)
        state = self.store.get_taobao_session_state()
        expected_hash = str(state["last_cookie_hash"] or "")
        local_cookie = str((self.cfg.get("taobao", {}) or {}).get("cookie") or "")
        local_hash = hashlib.sha256(local_cookie.encode("utf-8")).hexdigest()
        retired = bool(expected_hash and expected_hash != local_hash)
        retired = retired or (error_epoch is not None
                              and int(error_epoch) < int(current_auth_epoch()))
        if retired:
            self.store.update_transcription_job(
                item["job_key"], consumer_status="pending",
                consumer_next_at=time.time() + 5,
                error="retired Taobao client failed; retry current session")
        else:
            self.store.park_brief_consumer_for_auth(
                item["job_key"], now=time.time())
        return retired

    # ---------- 直播间分组 ----------
    def _rooms(self) -> dict[str, list]:
        """把启用主播按直播间 liveId 分组（同一直播间只探测一次）"""
        rooms: dict[str, list] = {}
        for a in self.store.enabled_anchors():
            lid = str(a["live_id"] or "") or self.default_live_id or "default"
            rooms.setdefault(lid, []).append(a)
        return rooms

    def _recording_window_is_open(self, now: datetime | None = None) -> bool:
        """Only permit new recorder sessions inside the configured business window."""
        current = now or now_shanghai()
        business_cfg = (getattr(self, "cfg", {}) or {}).get(
            "business_session", {}) or {}
        earliest_start = str(business_cfg.get("earliest_start") or "05:30")
        latest_end = str(business_cfg.get("latest_end") or "01:30")
        session_key = business_session_key(current)
        start_ms, end_ms = business_operating_bounds(
            session_key,
            earliest_start=earliest_start,
            latest_end=latest_end,
        )
        current_ms = int(current.timestamp() * 1000)
        return start_ms <= current_ms < end_ms

    # ---------- 会话 ----------
    def _new_session(self, room_key: str, anchor_id: int, stream_id: int, url: str,
                     candidates: list[str] | None = None) -> None:
        anchor = self.store.get_anchor(anchor_id)
        name = anchor["name"] if anchor else f"anchor_{anchor_id}"
        base = f"{name}_{time.strftime('%Y%m%d_%H%M%S')}"
        recorder = Recorder(self.ffmpeg, self.out_dir, base,
                            segment_seconds=self.segment_seconds,
                            referer=self.cfg["taobao"].get("referer", "https://h5.m.taobao.com/"),
                            user_agent=RECORD_UA, candidates=candidates,
                            stall_timeout_sec=self.stream_stall_timeout,
                            retry_base_sec=self.stream_retry_base,
                            retry_max_sec=self.stream_retry_max,
                            process_started=lambda pid: self.store.set_recorder_pid(
                                stream_id, pid),
                            media_started=lambda media_url:
                                self.store.set_recorder_resume_url(
                                    stream_id, media_url))
        # 先持久化分片目录，再启动 ffmpeg；即使在 start 后立即
        # 掉电，下次启动也能找回已写入的 part。
        self.store.set_recording_runtime(
            stream_id, str(recorder.session_dir), resume_url=url)
        recorder.start(url)
        self.sessions[room_key] = {
            "stream_id": stream_id,
            "anchor_id": anchor_id,
            "recorder": recorder,
            "last_refresh": time.time(),
            "url": url,
            "last_stream_alert": 0.0,
            "offline_checks": 0,
        }
        # 经营累计基线由独立 hourly-facts 线程捕获。录像控制线程不能在
        # ffmpeg 启动后同步等待淘宝接口，否则整点/轮换时会拖住本地续录。
        log.info("[%s] 开播！开始录制 -> %s", name, base)

    def _recover_media_after_probe_failure(
            self, room_key: str, anchors: list) -> bool:
        """Resume a known live recording when the authenticated probe is down.

        The public detail response is used only to obtain current media URLs.
        It is never accepted as end-of-live evidence and does not close or
        finalize a business session.
        """
        if room_key in self.sessions:
            return False
        live_id = (str(getattr(self, "default_live_id", "") or "")
                   if room_key == "default" else str(room_key or ""))
        if not live_id:
            return False
        if not self._recording_window_is_open():
            log.info("[%s] 当前不在业务录制窗口，拒绝重新启动录像", room_key)
            return False

        rows = self.store.query(
            "SELECT * FROM streams "
            "WHERE live_id=? AND status IN ('recording','recovering') "
            "ORDER BY id DESC LIMIT 1",
            (live_id,),
        )
        # 后台线程已经取得该旧分片的封存所有权时，不能同时把同一目录
        # 接回直播写入；直接开一个新技术分片，业务日仍按同一 liveId 聚合。
        inflight_recovery_ids = set(getattr(
            self, "_media_recovery_stream_ids", set()) or set())
        if rows and int(rows[0]["id"]) in inflight_recovery_ids:
            rows = []
        persisted_url = (
            str(rows[0]["resume_url"] or "").strip() if rows else "")
        try:
            from .mtop import public_live_media_urls
            urls = list(public_live_media_urls(live_id))
        except Exception as exc:
            log.warning(
                "[%s] 匿名媒体恢复探测失败: %s",
                room_key, type(exc).__name__)
            urls = []
        if persisted_url and persisted_url not in urls:
            urls.append(persisted_url)
        if not urls:
            return False
        selected_url = _pick_url(urls, "")
        if not rows:
            anchor_id = resolve_anchor_by_schedule(
                self.store, getattr(self, "schedule", {}) or {})
            if anchor_id is None:
                if not anchors:
                    return False
                anchor_id = resolve_anchor_by_shift(anchors)
            stream_id = self.store.start_stream(
                int(anchor_id), live_id=live_id)
            try:
                self._new_session(
                    room_key, int(anchor_id), stream_id, selected_url,
                    candidates=urls)
            except Exception as exc:
                self.store.set_stream_status(
                    stream_id, "recovering",
                    error=f"新录像恢复启动失败: {type(exc).__name__}",
                )
                log.warning(
                    "[%s] 已取得媒体地址但新录像启动失败: %s",
                    room_key, type(exc).__name__)
                return False
            log.warning(
                "[%s] 鉴权探测不可用，已开始新录像会话",
                room_key)
            return True
        stream = rows[0]
        raw_dir = str(stream["session_dir"] or "").strip()
        try:
            session_dir = Path(raw_dir).resolve(strict=False)
            session_dir.relative_to(self.out_dir.resolve(strict=False))
            if not raw_dir or not session_dir.is_dir():
                return False
        except (OSError, ValueError):
            return False

        stream_id = int(stream["id"])
        anchor_id = int(stream["anchor_id"])
        recorder = Recorder(
            self.ffmpeg, session_dir.parent, session_dir.name,
            segment_seconds=self.segment_seconds,
            referer=self.cfg["taobao"].get(
                "referer", "https://h5.m.taobao.com/"),
            user_agent=RECORD_UA,
            candidates=urls,
            stall_timeout_sec=self.stream_stall_timeout,
            retry_base_sec=self.stream_retry_base,
            retry_max_sec=self.stream_retry_max,
            process_started=lambda pid: self.store.set_recorder_pid(
                stream_id, pid),
            media_started=lambda media_url:
                self.store.set_recorder_resume_url(stream_id, media_url),
        )
        try:
            self._terminate_orphan_recorder(
                stream["recorder_pid"], session_dir)
            self.store.set_recording_runtime(
                stream_id, str(session_dir), resume_url=selected_url)
            recorder.start(selected_url)
        except Exception as exc:
            self.store.set_recorder_pid(stream_id, None)
            self.store.set_stream_status(
                stream_id, "recovering",
                error=f"录像恢复启动失败: {type(exc).__name__}",
            )
            log.warning(
                "[%s] 已取得媒体地址但录像恢复失败: %s",
                room_key, type(exc).__name__)
            return False

        self.store.set_stream_status(stream_id, "recording")
        self.sessions[room_key] = {
            "stream_id": stream_id,
            "anchor_id": anchor_id,
            "recorder": recorder,
            "last_refresh": time.time(),
            "url": selected_url,
            "last_stream_alert": 0.0,
            "offline_checks": 0,
        }
        log.warning(
            "[%s] 鉴权探测不可用，已恢复原录像会话",
            room_key)
        return True

    def _maybe_stream_alert(self, room_key: str, sess: dict) -> bool:
        """流持续不可用（可能 liveId 已变化/主播未真正推流）：
        先用千牛列表接口自动发现当天场次并切换，找不到才推告警"""
        recorder = sess["recorder"]
        if recorder.fail_count < 3:
            return False
        # 距上次尝试 10 分钟以上才再次处理
        if time.time() - sess.get("last_stream_alert", 0) < 600:
            return False
        sess["last_stream_alert"] = time.time()

        room_num = (self.cfg.get("taobao", {}) or {}).get("room_num", "")
        if not room_num:
            self._send_alert(room_key, "流持续不可用且未配置 room_num，请检查 liveId")
            return False
        if self._discover_and_switch(room_key, sess, force=True):
            return True
        self._send_alert(room_key, "流持续不可用，未发现其他明确在播的场次，请检查推流状态")
        return False

    def _discover_and_switch(self, room_key: str, sess: dict | None,
                             force: bool = False,
                             accept_same_live: bool = False) -> bool:
        """低频查询直播列表；发现另一条明确在播的 liveId 时结束旧场并切换。"""
        room_num = (self.cfg.get("taobao", {}) or {}).get("room_num", "")
        if not room_num:
            return False
        if not self._recording_window_is_open():
            log.info("[%s] 当前不在业务录制窗口，忽略自动发现场次", room_key)
            return False
        now = time.time()
        if not force and now - self._last_discovery_at.get(room_key, 0) < self.discovery_interval:
            return False
        self._last_discovery_at[room_key] = now
        from ..recorder.discover import find_current_live, update_config_live_id
        try:
            item = find_current_live(self.cfg, room_num)
        except Exception as exc:
            log.warning("自动发现场次失败: %s", exc)
            return False
        new_id = str((item or {}).get("id") or "")
        if not new_id:
            return False
        if new_id == room_key:
            if not accept_same_live or sess is None:
                return False
            stream_id = int(sess.get("stream_id") or 0)
            # 详情接口说结束、场次列表却明确把同一 liveId 标为在播时，
            # 不能把矛盾状态当成真下播。恢复业务日收集态并立即解除
            # 录像退避；只有真实媒体增长才会清空 fail_count。
            self._record_lifecycle(room_key, "live", sess)
            sess["offline_checks"] = 0
            self._observe_live_stream(stream_id, room_key)
            recorder = sess["recorder"]
            recorder.rearm_after_live_signal()
            recorder.tick()
            log.warning(
                "[%s] 详情接口称已结束，但场次列表仍明确在播；"
                "取消终结观察并立即重试录像", room_key)
            return True
        log.info("[%s] 自动发现新的直播中场次 liveId=%s，切换", room_key, new_id)
        if sess is not None and not self._end_session(
                room_key, "检测到新场次，自动切换"):
            log.error(
                "[%s] 旧录像未确认停止，取消 liveId 切换以避免双录",
                room_key,
            )
            return False
        update_config_live_id(new_id)
        self.default_live_id = new_id
        self.cfg.setdefault("taobao", {})["live_id"] = new_id
        # anchors.live_id 非空时优先于全局配置，必须同步；空值继续继承全局 liveId。
        for anchor in self.store.enabled_anchors():
            if str(anchor["live_id"] or "") == room_key:
                self.store.execute("UPDATE anchors SET live_id=? WHERE id=?", (new_id, anchor["id"]))
        return True

    def _maybe_brief(self, room_key: str, sess: dict) -> None:
        """按绝对整点边界排队已闭合的小时简报。"""
        b = (getattr(self, "cfg", {}) or {}).get("briefing", {}) or {}
        if not b.get("enabled"):
            return
        # 统一转写生产链按绝对排班小时排队。上一小时 DeepSeek
        # 晚到也不得阻止下一
        # 小时录音/妙记先行入队。
        if getattr(self, "transcription", None) is None or self.store is None:
            log.error("[%s] 统一转写服务不可用，拒绝进入已删除的旧简报链路", room_key)
            return
        parts = closed_parts(
            sess["recorder"].session_dir,
            min_age=int(b.get("part_min_age", 120)),
        )
        self._queue_completed_hour_windows(room_key, sess, parts)

    def _end_session(self, room_key: str, reason: str,
                     capture_daily_tail: bool = False) -> bool:
        """快速停止拉流并保留分片；耗时合并由独立恢复任务处理。"""
        sess = self.sessions.get(room_key)
        if not sess:
            return True
        recorder: Recorder = sess["recorder"]
        stream_id = int(sess["stream_id"])
        anchor = self.store.get_anchor(sess["anchor_id"])
        name = anchor["name"] if anchor else f"anchor_{sess['anchor_id']}"
        log.info("[%s] 停止录制（%s）", name, reason)
        try:
            # 这里只终止当前 ffmpeg 并闭合 timeline；绝不在录像控制线程
            # 同步 concat 数小时录像，否则会饿死主心跳并触发看门狗重启。
            recorder.abort()
        except Exception as exc:
            log.exception("[%s] 停止当前拉流失败: %s", name, exc)
            return False

        self.sessions.pop(room_key, None)
        if not self.store.claim_stream(
                stream_id, ("recording",), "recovering"):
            current = self.store.get_stream(stream_id)
            if current is None or str(current["status"] or "") != "recovering":
                self.store.set_stream_status(
                    stream_id, "recovering", error="拉流已停止，等待异步封存")
        self.store.set_recorder_pid(stream_id, None)

        if getattr(self, "transcription", None) is not None:
            try:
                parts = [
                    part for part in sorted(recorder.session_dir.glob("part_*.ts"))
                    if part.stat().st_size >= MIN_VALID_PART_BYTES
                ]
                # 分片和 timeline 已闭合，可以立即排队正式小时/日报尾段；
                # 这些消费者不依赖最终大 mp4，没必要等待 concat。
                self._queue_completed_hour_windows(room_key, sess, parts)
                if capture_daily_tail:
                    self._queue_initial_partial_window(room_key, sess, parts)
                    self._queue_final_partial_window(room_key, sess, parts)
            except Exception:
                log.exception("[%s] 停录后小时窗口入队失败", name)

        log.info(
            "[%s] 场次 #%d 已停止拉流，分片等待后台封存；录像主循环不等待合并",
            name, stream_id,
        )
        return True

    def _business_session_for_stream(self, stream) -> str:
        if not stream:
            return ""
        key = str(stream["business_session_key"] or "").strip()
        started = str(stream["started_at"] or "")
        parsed = parse_local_datetime(started)
        if not key:
            key = business_session_key(parsed) if parsed else ""
        start_ms = local_epoch_ms(started)
        if not key or start_ms is None:
            return ""
        try:
            ended_at = str(stream["ended_at"] or "")
        except (IndexError, KeyError):
            ended_at = ""
        end_ms = local_epoch_ms(ended_at)
        try:
            duration_sec = float(stream["duration_sec"] or 0)
        except (IndexError, KeyError, TypeError, ValueError):
            duration_sec = 0
        duration_end_ms = start_ms + max(1, int(duration_sec * 1000))
        if end_ms is None:
            end_ms = max(
                duration_end_ms,
                int(time.time() * 1000),
            )
        elif end_ms <= start_ms:
            end_ms = duration_end_ms
        business_cfg = (getattr(self, "cfg", {}) or {}).get(
            "business_session", {}) or {}
        if not business_interval_overlaps(
                key, start_ms, end_ms,
                earliest_start=str(
                    business_cfg.get("earliest_start") or "05:30"),
                latest_end=str(
                    business_cfg.get("latest_end") or "01:30")):
            return ""
        return key

    def _observe_live_stream(self, stream_id: int, room_key: str) -> None:
        """Submit a verified live event when the Store supports aggregate state."""
        get_stream = getattr(self.store, "get_stream", None)
        observe_live = getattr(self.store, "observe_business_live", None)
        if not callable(get_stream) or not callable(observe_live):
            return
        stream = get_stream(int(stream_id))
        key = self._business_session_for_stream(stream)
        if not key:
            if stream and str(stream["business_session_key"] or "").strip():
                self.store.execute(
                    "UPDATE streams SET business_session_key='' WHERE id=?",
                    (int(stream_id),),
                )
            return
        if str(stream["business_session_key"] or "").strip() != key:
            self.store.execute(
                "UPDATE streams SET business_session_key=? WHERE id=?",
                (key, int(stream_id)),
            )
        observe_live(
            key,
            live_id=str((stream["live_id"] if stream else "") or room_key),
            stream_id=int(stream_id),
            now=time.time(),
        )
        active_sess = getattr(self, "sessions", {}).get(room_key)
        if isinstance(active_sess, dict):
            active_sess.pop("fact_source_closed", None)
        self._schedule_hourly_fact_source(room_key, {
            "stream_id": int(stream_id),
        })
        self._reconcile_runtime_state()

    def _schedule_hourly_fact_source(self, room_key: str, sess: dict) -> bool:
        """Persist known live identity and nearby boundaries without network I/O."""
        if bool(sess.get("fact_source_closed")):
            return False
        get_stream = getattr(getattr(self, "store", None), "get_stream", None)
        if not callable(get_stream):
            return False
        try:
            stream_id = int(sess.get("stream_id") or 0)
        except (TypeError, ValueError):
            return False
        if stream_id <= 0:
            return False
        stream = get_stream(stream_id)
        business_key = self._business_session_for_stream(stream)
        if not stream or not business_key:
            return False
        live_id = str(stream["live_id"] or room_key)
        if not live_id or live_id == "default":
            return False
        from ..hourly_fact_capture import schedule_active_source
        schedule_active_source(
            self.store,
            business_session_key=business_key,
            live_id=live_id,
            stream_id=stream_id,
            observed_at_ms=int(time.time() * 1000),
            source_started_at_ms=local_epoch_ms(stream["started_at"]),
        )
        return True

    def _schedule_hourly_fact_source_end(
            self, room_key: str, sess: dict) -> bool:
        """Freeze the final source boundary after an explicit ended quorum."""
        if bool(sess.get("fact_source_closed")):
            return False
        stream_id = int(sess.get("stream_id") or 0)
        stream = self.store.get_stream(stream_id) if stream_id > 0 else None
        business_key = self._business_session_for_stream(stream)
        if not stream or not business_key:
            return False
        live_id = str(stream["live_id"] or room_key)
        observed_ms = int(time.time() * 1000)
        from ..hourly_fact_capture import schedule_source_end
        job_key = schedule_source_end(
            self.store,
            business_session_key=business_key,
            live_id=live_id,
            stream_id=stream_id,
            # A stalled recorder is not evidence that the live source ended.
            # Only the lifecycle quorum that reached this method may close it.
            ended_at_ms=observed_ms,
            observed_at_ms=observed_ms,
        )
        if job_key is None:
            return False
        sess["fact_source_closed"] = True
        self._tick_hourly_fact_capture()
        return True

    def _tick_hourly_fact_capture(self) -> bool:
        """Submit at most one due fact read to the dedicated single worker."""
        pool = getattr(self, "fact_pool", None)
        if pool is None:
            return False
        checked_at = time.time()
        last_schedule = float(getattr(
            self, "_last_persisted_fact_schedule_at", 0.0) or 0.0)
        if checked_at - last_schedule >= 30:
            self._last_persisted_fact_schedule_at = checked_at
            try:
                from ..hourly_fact_capture import schedule_persisted_open_sources
                business_cfg = (getattr(self, "cfg", {}) or {}).get(
                    "business_session", {}) or {}
                schedule_persisted_open_sources(
                    self.store,
                    observed_at_ms=int(checked_at * 1000),
                    earliest_start=str(
                        business_cfg.get("earliest_start") or "05:30"),
                    latest_end=str(
                        business_cfg.get("latest_end") or "01:30"),
                )
            except Exception:
                # A malformed historical row must not stop the recorder or
                # prevent already-persisted due work from being claimed.
                log.exception("已持久化小时事实来源调度失败")
        future = getattr(self, "_fact_future", None)
        if future is not None:
            if not future.done():
                return False
            self._fact_future = None
            try:
                future.result()
            except Exception:
                log.exception("小时边界事实线程异常")
        from ..hourly_fact_capture import boundary_work_due, capture_due_boundary
        if not boundary_work_due(self.store, now=checked_at):
            return False
        self._fact_future = pool.submit(
            capture_due_boundary, self.cfg, self.store)
        return True

    def _queue_platform_review(self, live_id: str, *,
                               business_key: str = "") -> bool:
        """真结束观察完成后创建业务日唯一整场终结任务。"""
        triggered = time.time()
        cfg = getattr(self, "cfg", {}) or {}
        wait_min = max(1, int((cfg.get("review", {}) or {}).get(
            "platform_wait_minutes", 30)))
        business_key = str(business_key or "").strip()
        if not business_key:
            rows = self.store.query(
                "SELECT business_session_key FROM streams WHERE live_id=? "
                "AND business_session_key!='' ORDER BY started_at DESC LIMIT 1",
                (str(live_id),),
            )
            business_key = str(rows[0]["business_session_key"] or "") if rows else ""
        created = self.store.queue_platform_review(
            str(live_id), triggered, triggered + wait_min * 60,
            business_session_key=business_key)
        if created:
            log.info("[%s] 已创建业务日整场复盘任务 key=%s，最长等待官方数据 %d 分钟",
                     live_id, business_key or live_id, wait_min)
        return created

    def _retry_unconfirmed_business_ends(
            self, *, already_polled: set[str] | None = None) -> int:
        """录像都停了还要再问淘宝。确认下播后才恢复日报，接口报错只重试。"""
        skipped = set(already_polled or ())
        skipped.update(self.sessions)
        probed = 0
        lister = getattr(self.store, "list_unconfirmed_end_probe_targets", None)
        if not callable(lister):
            return 0
        for row in lister(limit=1):
            live_id = str(row.get("live_id") or "")
            if not live_id or live_id in skipped:
                continue
            try:
                self._poll_room(live_id, [], allow_group_alert=False)
            except Exception:
                log.exception("过期业务日补探测失败 liveId=%s", live_id)
            probed += 1
        return probed

    def _record_lifecycle(self, room_key: str, state: str, sess: dict | None = None) -> int:
        """按真实 liveId 持久化生命周期，确保下播确认跨重启且严格连续。"""
        live_id = str(room_key)
        if sess:
            stream = self.store.get_stream(int(sess.get("stream_id") or 0))
            live_id = str((stream["live_id"] if stream else "") or live_id)
        elif room_key == "default":
            live_id = str(getattr(self, "default_live_id", "") or room_key)
        return self.store.record_lifecycle_state(live_id, state)

    def _submit_due_platform_reviews(self) -> int:
        """把到期的持久化终结任务交给后台线程，主轮询不被网络/LLM 阻塞。"""
        from ..review.platform import process_platform_review_job

        submitted = 0
        for job in self.store.claim_due_platform_reviews(time.time(), limit=1):
            live_id = str(job["live_id"])
            if live_id in self._platform_review_inflight:
                continue
            self._platform_review_inflight.add(live_id)
            try:
                future = self.pool.submit(
                    process_platform_review_job, self.cfg, self.store, live_id)
            except Exception as exc:
                self._platform_review_inflight.discard(live_id)
                self.store.update_platform_review_job(
                    live_id, "waiting", next_attempt_at=time.time() + 60,
                    error=str(exc))
                continue

            def _done(done, *, key=live_id) -> None:
                self._platform_review_inflight.discard(key)
                try:
                    done.result()
                except Exception as exc:
                    log.exception("平台整场复盘任务异常 liveId=%s", key)
                    self.store.update_platform_review_job(
                        key, "waiting", next_attempt_at=time.time() + 300,
                        error=str(exc))

            future.add_done_callback(_done)
            submitted += 1
        return submitted

    # ---------- 轮询 ----------
    @staticmethod
    def _probe_failure_cause(detail: str) -> str:
        """Map unstable upstream text to a small, non-sensitive cause family."""
        text = str(detail or "").upper()
        if any(token in text for token in (
                "SESSION_EXPIRED", "USER_VALIDATE", "TOKEN", "登录状态",
                "AUTHENTICATION")):
            return "authentication"
        if any(token in text for token in ("RGV", "TRAFFIC_LIMIT", "风控")):
            return "upstream_guard"
        if any(token in text for token in (
                "TIMEOUT", "CONNECTION", "CONNECT", "DNS", "NETWORK",
                "网络请求失败", "连接失败")):
            return "transport"
        if any(token in text for token in (
                "响应非 JSON", "JSONDECODE", "SCHEMA", "CONTRACT")):
            return "response_contract"
        if "FAIL_" in text:
            return "upstream_response"
        return "generic"

    def _probe_guard_has_fresh_media(self, room_key: str) -> bool:
        """Treat RGV as non-actionable while the active recording is healthy."""
        sessions = getattr(self, "sessions", None)
        if not isinstance(sessions, dict):
            return False
        sess = sessions.get(room_key)
        if not isinstance(sess, dict):
            return False
        media_age_reader = getattr(sess.get("recorder"),
                                   "seconds_since_media", None)
        if not callable(media_age_reader):
            return False
        try:
            media_age = float(media_age_reader())
            stall_after = max(
                1.0, float(getattr(self, "stream_stall_timeout", 90)))
        except (TypeError, ValueError, OverflowError):
            return False
        return 0.0 <= media_age < stall_after

    def _current_poll_interval(self) -> int:
        """限流退避：连续 upstream_guard 失败时拉长整轮轮询间隔。

        被 mtop 网关限流时保持 60s 节奏只会每分钟撞一次墙；退避给风控
        留冷却窗口，恢复后由 _note_probe_success 降回基础间隔。
        """
        streaks = getattr(self, "_guard_streaks", None)
        streak = (max(streaks.values(), default=0)
                  if isinstance(streaks, dict) else 0)
        base = int(getattr(self, "poll_interval", 60) or 60)
        if streak < 3:
            return base
        return min(300, base * (2 ** min(streak - 2, 3)))

    def _note_probe_success(self, room_key: str) -> None:
        """探测成功：清零失败/限流计数并关闭未决事故。"""
        self.probe_fails.pop(room_key, None)
        streaks = getattr(self, "_guard_streaks", None)
        backed_off = (isinstance(streaks, dict)
                      and streaks.pop(room_key, 0) >= 3)
        base = int(getattr(self, "poll_interval", 60) or 60)
        if backed_off and self._current_poll_interval() == base:
            log.info("[%s] 探测已恢复，轮询间隔降回 %ds",
                     room_key, base)
        self._resolve_probe_incident(room_key)

    def _probe_fail(
            self, room_key: str, detail: str, *,
            allow_group_alert: bool = True,
    ) -> int:
        """同一直播间连续失败只算一个事故，成功恢复后才允许再次告警。"""
        self.probe_fails[room_key] = self.probe_fails.get(room_key, 0) + 1
        n = self.probe_fails[room_key]
        cause = self._probe_failure_cause(detail)
        if cause == "upstream_guard":
            streaks = getattr(self, "_guard_streaks", None)
            if not isinstance(streaks, dict):
                streaks = {}
                self._guard_streaks = streaks
            before = self._current_poll_interval()
            streaks[room_key] = streaks.get(room_key, 0) + 1
            after = self._current_poll_interval()
            if after != before:
                log.info("[%s] 探测连续被限流，轮询间隔退避至 %ds",
                         room_key, after)
        cause_state = getattr(self, "_probe_alert_state", None)
        if not isinstance(cause_state, dict):
            cause_state = {}
            self._probe_alert_state = cause_state
        # cause 只保留作诊断标签。上游常在同一故障期间交替返回限流、
        # 鉴权和网络文案；若按文案拆事故，会在群里制造告警风暴。
        cause_state[room_key] = (cause, n)
        if n >= 3:
            if not allow_group_alert:
                # 过期业务日的下播确认只是后台收敛工作，不是当前
                # 直播健康信号。它可以继续退避和记录，但不得创建群告警；
                # 当前录像真正停滞时，仍由正常直播间探测链独立告警。
                self._resolve_probe_incident(room_key)
                return n
            if (cause == "upstream_guard"
                    and self._probe_guard_has_fresh_media(room_key)):
                # 详情接口限流不是业务中断证据。录像仍持续增长时只保留
                # 调用方的本地日志，并关闭旧版本可能留下的群告警事故；
                # 一旦媒体超过停滞阈值，后续探测会重新开启并发送新事故。
                self._resolve_probe_incident(room_key)
                return n
            incident_key = f"PROBE_FAILURE:{room_key}"
            if n == 3:
                # 兼容旧版本按 cause 拆出的事故键。若本次连续故障已经
                # 发过卡，合并键继承投递时间，升级本身不能再制造一张卡。
                store = getattr(self, "store", None)
                query = getattr(store, "query", None)
                observe = getattr(store, "runtime_incident_notification_due", None)
                mark = getattr(store, "mark_runtime_issue_notified", None)
                resolve_family = getattr(
                    store, "resolve_runtime_issue_family", None)
                if all(callable(item) for item in (
                        query, observe, mark, resolve_family)):
                    legacy = [
                        row for row in query(
                            "SELECT issue_key,last_notified_at FROM runtime_issues "
                            "WHERE status='open' AND issue_key<>?",
                            (incident_key,),
                        )
                        if str(row["issue_key"] or "").startswith(
                            incident_key + ":")
                    ]
                    notified_at = max(
                        (float(row["last_notified_at"] or 0) for row in legacy),
                        default=0.0,
                    )
                    if notified_at > 0:
                        observe(
                            incident_key, now=time.time(),
                            payload={"scope": str(room_key)},
                        )
                        mark(incident_key, notified_at=notified_at)
                        resolve_family(
                            incident_key, now=time.time(),
                            except_key=incident_key,
                        )
            self._send_alert(
                room_key, detail,
                incident_key=incident_key,
            )
        return n

    def _resolve_probe_incident(self, room_key: str) -> bool:
        cause_state = getattr(self, "_probe_alert_state", None)
        if isinstance(cause_state, dict):
            cause_state.pop(room_key, None)
        store = getattr(self, "store", None)
        family_resolver = getattr(store, "resolve_runtime_issue_family", None)
        if callable(family_resolver):
            return bool(family_resolver(
                f"PROBE_FAILURE:{room_key}", now=time.time()))
        resolver = getattr(store, "resolve_runtime_issue", None)
        return bool(callable(resolver) and resolver(
            f"PROBE_FAILURE:{room_key}", now=time.time()))

    def _send_alert(self, room_key: str, detail: str, *,
                    cooldown_sec: int | None = None,
                    incident_key: str = "") -> bool:
        """推送接口异常告警卡片到群"""
        now = time.time()
        cooldown = self.alert_cooldown if cooldown_sec is None else max(0, int(cooldown_sec))
        retry_state = getattr(self, "_alert_retry_after_at", None)
        if not isinstance(retry_state, dict):
            retry_state = {}
            self._alert_retry_after_at = retry_state
        issue_key = str(incident_key or f"ALERT_COOLDOWN:{room_key}")
        incident_reader = getattr(
            getattr(self, "store", None),
            "runtime_incident_notification_due", None)
        due_reader = getattr(
            getattr(self, "store", None),
            "runtime_issue_notification_due", None)
        incident_started_at = now
        if incident_key and callable(incident_reader):
            due, incident_started_at = incident_reader(
                issue_key,
                now=now,
                payload={"scope": str(room_key)},
            )
            retry_at = now + cooldown
        elif callable(due_reader):
            due, retry_at = due_reader(
                issue_key,
                now=now,
                cooldown_seconds=cooldown,
                # Only the stable scope is persisted. ``detail`` can contain
                # upstream error material and remains in the rotating log/card.
                payload={"scope": str(room_key)},
            )
        else:
            due = now - self._last_alert_at.get(room_key, 0) >= cooldown
            retry_at = (
                now if due
                else self._last_alert_at.get(room_key, 0) + cooldown
            )
        if not due:
            retry_state[room_key] = max(now + 1, float(retry_at))
            log.warning("[%s] 告警处于冷却期，本次仅记录日志", room_key)
            return False
        nf = (self.cfg.get("notify", {}) or {}).get("feishu", {}) or {}
        chat_id = nf.get("chat_id", "")
        if not chat_id:
            retry_state[room_key] = now + 300
            log.error("巡检接口异常（未配置告警群）: %s %s", room_key, detail)
            return False
        try:
            from ..notify.feishu import (build_alert_card,
                                         remote_idempotency_key,
                                         safe_delivery_error_fields, send_card)
            delivery_scope = hashlib.sha256(issue_key.encode("utf-8")).hexdigest()
            if incident_key:
                delivery_identity = (
                    f"runtime-incident:{delivery_scope}:"
                    f"{int(float(incident_started_at) * 1000)}")
            else:
                bucket_seconds = max(1, cooldown)
                bucket = int(now // bucket_seconds)
                delivery_identity = f"runtime-alert:{delivery_scope}:{bucket}"
            send_card(
                self.cfg, chat_id,
                build_alert_card(room_key, detail, failure_count=3),
                idempotency_key=remote_idempotency_key(
                    delivery_identity),
            )
            self._last_alert_at[room_key] = now
            retry_state[room_key] = now + cooldown
            acknowledge = getattr(
                getattr(self, "store", None), "mark_runtime_issue_notified", None)
            if callable(acknowledge):
                try:
                    acknowledge(issue_key, notified_at=now)
                except Exception:
                    log.exception("告警投递成功但持久化冷却失败")
            return True
        except Exception as e:
            retry_state[room_key] = now + 60
            stage, error_type = safe_delivery_error_fields(e)
            log.error("告警推送失败: stage=%s error_type=%s",
                      stage, error_type)
            return False

    def _poll_room(
            self, room_key: str, anchors: list, *,
            allow_group_alert: bool = True,
    ) -> None:
        # 小时边界由绝对时钟驱动，不能依赖本轮详情接口恰好返回在播。
        # 若整点遇到详情状态抖动，仍须先冻结当时经营事实并排队录像窗口。
        sess = self.sessions.get(room_key)
        if sess:
            self._schedule_hourly_fact_source(room_key, sess)
            self._tick_hourly_fact_capture()
            self._maybe_brief(room_key, sess)
        try:
            info = self.client.probe(live_id=room_key if room_key != "default" else "",
                                     user_id="")
        except MtopError as e:
            # 接口异常（风控/网络）：保持现有录制继续跑，避免中断
            log.warning("[%s] 直播状态探测失败: %s", room_key, e)
            if allow_group_alert:
                failures = self._probe_fail(room_key, str(e))
            else:
                failures = self._probe_fail(
                    room_key, str(e), allow_group_alert=False,
                )
            sess = self.sessions.get(room_key)
            self._record_lifecycle(room_key, "error", sess)
            if sess:
                sess["recorder"].tick()  # 仅保证 ffmpeg 存活
                if self._maybe_stream_alert(room_key, sess):
                    return
                if (failures >= self.offline_confirmations and
                        sess["recorder"].seconds_since_media() >= self.session_stale_timeout):
                    if not self._discover_and_switch(room_key, sess, force=True):
                        mins = int(sess["recorder"].seconds_since_media() // 60)
                        self._end_session(
                            room_key,
                            f"媒体连续 {mins} 分钟无增长且状态接口持续失败，熔断收尾",
                        )
            else:
                if not self._recover_media_after_probe_failure(
                        room_key, anchors):
                    live_id = (
                        str(getattr(self, "default_live_id", "") or "")
                        if room_key == "default" else str(room_key))
                    if live_id:
                        self._deferred_recovery_live_ids.add(live_id)
            return

        # 接口返回异常（API 不存在/业务错误）也计入探测失败
        ret = str(info.get("ret", ""))
        if "SUCCESS" not in ret:
            log.warning("[%s] 探测返回异常: %s", room_key, ret)
            if allow_group_alert:
                self._probe_fail(room_key, ret)
            else:
                self._probe_fail(
                    room_key, ret, allow_group_alert=False,
                )
            # 业务异常/接口契约变更不是下播证据。已在录制的
            # ffmpeg 继续存活，下一轮重试，offline_checks 不变。
            sess = self.sessions.get(room_key)
            self._record_lifecycle(room_key, "error", sess)
            if sess:
                sess["recorder"].tick()
                self._maybe_stream_alert(room_key, sess)
            else:
                if not self._recover_media_after_probe_failure(
                        room_key, anchors):
                    live_id = (
                        str(getattr(self, "default_live_id", "") or "")
                        if room_key == "default" else str(room_key))
                    if live_id:
                        self._deferred_recovery_live_ids.add(live_id)
            return
        else:
            self._note_probe_success(room_key)

        is_live = info["is_live"]
        lifecycle = str(info.get("lifecycle") or "unknown")
        urls = info["stream_urls"]
        sess = self.sessions.get(room_key)

        if is_live and urls:
            if sess is None and not self._recording_window_is_open():
                log.info("[%s] 当前不在业务录制窗口，忽略在播信号，不启动新录像",
                         room_key)
                return
            self._record_lifecycle(room_key, "live", sess)
            if sess:
                self._observe_live_stream(
                    int(sess.get("stream_id") or 0), room_key)
            url = _pick_url(urls, sess["url"] if sess else "")
            if sess is None:
                # 排班优先，无排班匹配时回退轮班表
                anchor_id = resolve_anchor_by_schedule(self.store, self.schedule) \
                    or resolve_anchor_by_shift(anchors)
                # 固化开播时的 liveId（不可变），防自动切换后旧场次串到新场次数据
                stream_id = self.store.start_stream(anchor_id, live_id=room_key if room_key != "default" else self.default_live_id)
                self._observe_live_stream(stream_id, room_key)
                self._new_session(room_key, anchor_id, stream_id, url, candidates=urls)
            else:
                # 排班轮换检测：当前小时在播主播变了 → 封存旧分片并开新分片。
                next_anchor = resolve_anchor_by_schedule(self.store, self.schedule)
                if next_anchor and next_anchor != sess["anchor_id"]:
                    if not self._recording_window_is_open():
                        log.info("[%s] 当前不在业务录制窗口，保留现有录像直到自然收尾",
                                 room_key)
                        return
                    # 只封存旧主播的活动分片。绝对整点小时会跨技术碎片聚合，
                    # 轮换本身不生成额外简报或技术碎片报告。
                    if not self._end_session(
                            room_key, "主播轮换（排班切场次）"):
                        log.error(
                            "[%s] 旧录像未确认停止，取消本轮主播切换以避免双录",
                            room_key,
                        )
                        return
                    anchor_id = next_anchor
                    stream_id = self.store.start_stream(
                        anchor_id,
                        live_id=room_key if room_key != "default" else self.default_live_id)
                    self._observe_live_stream(stream_id, room_key)
                    self._new_session(room_key, anchor_id, stream_id, url, candidates=urls)
                    return
                sess["offline_checks"] = 0
                sess["recorder"].update_candidates(urls)
                # 定期刷新流地址（地址轮换自动切流）
                if time.time() - sess["last_refresh"] >= self.refresh_interval:
                    sess["last_refresh"] = time.time()
                    sess["recorder"].tick(url, candidates=urls)
                    sess["url"] = url
                else:
                    sess["recorder"].tick(candidates=urls)  # 保证 ffmpeg 存活
                if self._maybe_stream_alert(room_key, sess):
                    return
        elif lifecycle == "ended":
            n = self._record_lifecycle(room_key, "ended", sess)
            if sess:
                sess["offline_checks"] = n
                if n >= self.offline_confirmations:
                    if self._discover_and_switch(
                            room_key, sess, force=True,
                            accept_same_live=True):
                        return
                    stream = self.store.get_stream(int(sess["stream_id"]))
                    live_id = str((stream["live_id"] if stream else "") or room_key)
                    business_key = self._business_session_for_stream(stream)
                    if not business_key:
                        self._end_session(
                            room_key,
                            f"连续 {n} 次明确状态确认；场次在业务时间外，独立收尾",
                        )
                        self._discover_and_switch(room_key, None, force=True)
                        return
                    self._schedule_hourly_fact_source_end(room_key, sess)
                    self.store.add_business_session_source(
                        business_key, live_id=live_id, stream_id=int(sess["stream_id"]))
                    observation_deadline = self.store.begin_business_observation(
                        business_key, now=time.time(), observe_seconds=600)
                    if not self.store.business_observation_due(
                            business_key, now=time.time()):
                        # 04 终稿：连续明确下播只是观察起点；10 分钟内重播/新 liveId
                        # 会取消观察，不能提前终结业务日。
                        sess["recorder"].tick()
                        log.warning(
                            "[%s] 连续 %d 次明确下播，开始业务日 10 分钟观察，截止 %.0f",
                            room_key, n, observation_deadline)
                        return
                    # 先完成本地录像收尾，再由同一聚合事务确认业务日结束并解锁日报。
                    # 如果合并/状态回写失败而 stream 仍是 recording，聚合门禁会拒绝终结。
                    self._end_session(
                        room_key, f"连续 {n} 次明确状态确认且观察 10 分钟无重播",
                        capture_daily_tail=True)
                    wait_seconds = max(60, int(
                        (getattr(self, "cfg", {}) or {}).get("review", {}).get(
                            "platform_wait_minutes", 30)) * 60)
                    finalized = self.store.finalize_business_session(
                        business_key, live_id=live_id, now=time.time(),
                        settlement_wait_seconds=wait_seconds)
                    if not finalized:
                        log.error(
                            "[%s] 本地录像尚未安全收尾，业务日保持观察态且日报不解锁",
                            room_key)
                    self._reconcile_runtime_state()
                    self._discover_and_switch(room_key, None, force=True)
                else:
                    # 单次状态抖动不结束场次，录制进程继续存活。
                    sess["recorder"].tick()
                    log.warning("[%s] 第 %d/%d 次返回未开播，继续录制等待复核",
                                room_key, n, self.offline_confirmations)
            else:
                live_id = str(getattr(self, "default_live_id", "") or room_key) \
                    if room_key == "default" else str(room_key)
                if n >= self.offline_confirmations:
                    rows = self.store.query(
                        "SELECT * FROM streams WHERE live_id=? AND file_path!='' "
                        "ORDER BY started_at DESC LIMIT 1", (live_id,))
                    if rows:
                        stream = rows[0]
                        business_key = self._business_session_for_stream(stream)
                        if not business_key:
                            self._discover_and_switch(room_key, None)
                            return
                        self.store.add_business_session_source(
                            business_key, live_id=live_id, stream_id=int(stream["id"]))
                        self.store.begin_business_observation(
                            business_key, now=time.time(), observe_seconds=600)
                        if self.store.business_observation_due(
                                business_key, now=time.time()):
                            wait_seconds = max(60, int(
                                (getattr(self, "cfg", {}) or {}).get(
                                    "review", {}).get(
                                        "platform_wait_minutes", 30)) * 60)
                            self.store.finalize_business_session(
                                business_key, live_id=live_id, now=time.time(),
                                settlement_wait_seconds=wait_seconds)
                            self._reconcile_runtime_state()
                self._discover_and_switch(room_key, None)
        else:
            # 状态字段未知，或明确在播但暂时没拿到流地址：都不是下播证据。
            self._record_lifecycle(
                room_key, "live" if lifecycle == "live" else "unknown", sess)
            if sess:
                sess["recorder"].tick()
            else:
                self._discover_and_switch(room_key, None)
            log.warning("[%s] 生命周期状态%s且无可用直播流，下轮重试",
                        room_key, lifecycle)

    def run(self) -> None:
        if not probe_ffmpeg(self.ffmpeg):
            log.error("未找到 ffmpeg（%s），请先安装：brew install ffmpeg", self.ffmpeg)
            return
        rooms = self._rooms()
        if not rooms:
            log.warning("没有任何启用中的主播，请在 config.yaml 中配置 anchors")
        log.info("AI 巡检启动：%d 个直播间，轮询间隔 %ds，流刷新间隔 %ds",
                 len(rooms), self.poll_interval, self.refresh_interval)
        self._reconcile_runtime_state()
        recovered = self.store.recover_abandoned_analysis_leases(now=time.time())
        if any(recovered.values()):
            log.warning(
                "重启后已立即释放旧执行租约：简报=%d，DeepSeek=%d",
                recovered["brief_consumers"], recovered["intelligence_jobs"],
            )
        while not self._stop:
            self._beat_main_loop()
            started = time.time()
            self._tick_taobao_session()
            # liveId 自动发现、主播配置变更后无需重启循环即可生效。
            self._refresh_schedule()
            rooms = self._rooms()
            self._deferred_recovery_live_ids = set()
            for room_key, anchors in rooms.items():
                if self._stop:
                    break
                try:
                    self._poll_room(room_key, anchors)
                except Exception:
                    log.exception("轮询异常（%s）", room_key)
            try:
                self._retry_unconfirmed_business_ends(
                    already_polled=set(rooms))
            except Exception:
                log.exception("过期业务日补探测异常")
            # 先恢复当前拉流，再封存历史分片。旧录像合并可能需要数分钟，
            # 绝不能因此阻塞新录像启动。
            self._tick_media_recovery()
            self._tick_transcriptions()
            self._tick_taobao_session()
            self._submit_due_platform_reviews()
            # 每日自动清理/经营数据/冻结回填；任务表保证跨重启不重复、失败可退避重试。
            today = now_shanghai().strftime("%Y-%m-%d")
            self._run_daily_maintenance(today)
            # 小时事实补齐重抓：5 分钟一轮，只碰 waiting_evidence 的正式小时
            self._last_fact_rescue = getattr(self, "_last_fact_rescue", 0.0)
            if time.time() - self._last_fact_rescue >= 300:
                self._last_fact_rescue = time.time()
                try:
                    self._rescue_waiting_evidence_facts()
                    self._converge_stale_business_observations()
                except Exception:
                    log.exception("小时事实重抓/关账收敛循环异常")
            # 每小时健康检查：磁盘水位/待分析积压/录制会话卡死
            if time.time() - self._last_health_check >= 3600:
                self._last_health_check = time.time()
                try:
                    self._health_check()
                except Exception:
                    log.exception("健康检查失败")
            # 细粒度睡眠：随时响应停止信号，避免把剩余轮询间隔睡完
            last_recorder_tick = 0.0
            while not self._stop and time.time() - started < self._current_poll_interval():
                self._beat_main_loop()
                # 状态 API 仍按分钟低频请求；ffmpeg 本地存活检查每 5 秒，分片写满后及时续录，
                # 避免等待下一次 API 轮询造成最长约 60 秒的录制空洞。
                if time.time() - last_recorder_tick >= 5:
                    last_recorder_tick = time.time()
                    for sess in list(self.sessions.values()):
                        try:
                            sess["recorder"].tick()
                        except Exception:
                            log.exception("录制进程本地续跑检查失败")
                self._tick_transcriptions()
                self._tick_taobao_session()
                self._tick_hourly_fact_capture()
                time.sleep(1)

    def _beat_main_loop(self) -> None:
        """Publish liveness only from the sole recorder-control thread."""
        heartbeat = getattr(self, "_heartbeat", None)
        if heartbeat is None:
            return
        try:
            heartbeat.beat()
        except Exception as exc:
            raise RuntimeError(
                f"watchdog heartbeat update failed: {type(exc).__name__}"
            ) from exc

    def _persistent_job_health_alerts(
            self, *, now: float | None = None,
            stale_running_seconds: int = 7_200) -> list[str]:
        """Inspect the persisted queues that determine brief/report delivery."""
        checked_at = float(time.time() if now is None else now)
        checked_local = datetime.fromtimestamp(checked_at, SHANGHAI)
        business_start = checked_local.replace(
            hour=5, minute=0, second=0, microsecond=0)
        if checked_local < business_start:
            business_start -= timedelta(days=1)
        active_since = business_start.strftime("%Y-%m-%d %H:%M:%S")
        alerts: list[str] = []

        retrying_intelligence = int(self.store.query(
            """SELECT COUNT(*) n FROM intelligence_jobs j
               INNER JOIN streams s ON s.id=j.stream_id
               WHERE j.status NOT IN ('ready','blocked')
                 AND j.error<>'' AND s.started_at>=?""",
            (active_since,),
        )[0]["n"])
        if retrying_intelligence:
            alerts.append(
                f"有 {retrying_intelligence} 个 DeepSeek 小时分析正在等待重试，"
                "正式简报尚未生成")

        blocked_intelligence = int(self.store.query(
            """SELECT COUNT(*) n FROM intelligence_jobs j
               INNER JOIN streams s ON s.id=j.stream_id
               WHERE j.status='blocked'
                 AND s.started_at>=?""",
            (active_since,),
        )[0]["n"])
        if blocked_intelligence:
            alerts.append(
                f"有 {blocked_intelligence} 个历史 DeepSeek 任务停在不完整终态，"
                "对应简报或日报需恢复")

        failed_reports = int(self.store.query(
            "SELECT COUNT(*) n FROM platform_review_jobs WHERE status='failed'"
        )[0]["n"])
        if failed_reports:
            alerts.append(f"有 {failed_reports} 个日报任务已失败，冻结载荷未投递")

        failed_maintenance = int(self.store.query(
            """SELECT COUNT(*) n FROM maintenance_jobs
               WHERE status='failed' AND job_key LIKE 'cleanup:%'"""
        )[0]["n"])
        if failed_maintenance:
            alerts.append(f"有 {failed_maintenance} 个维护任务处于失败退避")
        stale_maintenance = int(self.store.query(
            """SELECT COUNT(*) n FROM maintenance_jobs
               WHERE status='running' AND job_key LIKE 'cleanup:%'
                 AND last_attempt<?""",
            (checked_at - max(60, int(stale_running_seconds)),),
        )[0]["n"])
        if stale_maintenance:
            alerts.append(
                f"有 {stale_maintenance} 个维护任务长时间停在 running，"
                "可能已中断")
        return alerts

    def _reconcile_runtime_state(self, *, now: float | None = None) -> list:
        """Converge safe lifecycle contradictions and alert on ambiguous ones."""
        from ..runtime.invariants import reconcile_business_sessions

        checked_at = float(time.time() if now is None else now)
        issues = reconcile_business_sessions(self.store, now=checked_at)
        for issue in issues:
            if issue.repair == "needs_attention":
                self._send_alert(
                    f"状态对账:{issue.code}",
                    f"业务日 {issue.business_session_key} 状态无法安全自动修复，"
                    "已停止正式日报并等待人工核验",
                )
            else:
                log.warning(
                    "业务日状态已自动收敛 code=%s key=%s repair=%s",
                    issue.code, issue.business_session_key, issue.repair)
        return issues

    def _health_check(self) -> None:
        """健康检查：磁盘水位、待分析积压、录制会话异常。异常推告警卡片"""
        self._reconcile_runtime_state()
        alerts: list[str] = self._persistent_job_health_alerts()
        if getattr(self, "transcription", None) is not None:
            blocked = self.store.query(
                "SELECT COUNT(*) n FROM transcription_jobs WHERE status='blocked'")[0]["n"]
            cleanup_failed = self.store.query(
                "SELECT COUNT(*) n FROM transcription_jobs WHERE cleanup_status='failed'")[0]["n"]
            stale = self.store.query(
                """SELECT COUNT(*) n FROM transcription_jobs
                   WHERE status='processing'
                     AND updated_at < datetime('now','localtime','-2 hours')""")[0]["n"]
            if blocked:
                alerts.append(f"有 {blocked} 个妙记任务处于 blocked，等待人工选择处理方式")
            if stale:
                alerts.append(f"有 {stale} 个妙记任务已处理超过2小时，仍会继续后台轮询")
            if cleanup_failed:
                alerts.append(f"有 {cleanup_failed} 个妙记云盘源文件删除失败，正在独立退避重试")
        try:
            from ..notify.feishu import ambiguous_deliveries
            ambiguous = ambiguous_deliveries()
            if ambiguous:
                alerts.append(
                    f"有 {len(ambiguous)} 条飞书发送结果不确定，已暂停自动重发以避免重复；"
                    f"请人工核验 notify_state（示例：{ambiguous[0]}）")
        except Exception as exc:
            log.warning("飞书投递台账检查异常: %s", exc)
        # 1) 磁盘水位（录像目录所在分区 < 10GB 告警）
        try:
            import shutil
            du = shutil.disk_usage(self.recordings_dir)
            free_gb = du.free / 1024 ** 3
            if free_gb < 10:
                alerts.append(f"磁盘剩余仅 {free_gb:.1f}GB（录像目录 {self.recordings_dir}），可能很快写满")
        except Exception:
            pass
        # 2) 录制会话异常：正在录但 ffmpeg 进程已死（tick 会续拉起，这里提示长时间无新分片）
        try:
            now = time.time()
            for key, sess in self.sessions.items():
                sess_dir = Path(sess["recorder"].session_dir)
                parts = list(sess_dir.glob("part_*.ts"))
                if parts:
                    newest = max(p.stat().st_mtime for p in parts)
                    if now - newest > 900:  # 15 分钟无新分片
                        alerts.append(f"直播间 {key} 已 {int((now-newest)//60)} 分钟未产生新分片，可能录制卡死")
        except Exception:
            pass
        # 3) 在播数据源契约：每小时低频核验 totalStats 关键字段仍存在。
        if self.sessions:
            try:
                from ..metrics.qianniu import fetch_live_totals
                for live_id in self.sessions:
                    totals = fetch_live_totals(self.cfg, live_id)
                    missing = [label for key, label in (
                        ("online_uv", "当前在线"), ("max_online_uv", "最高在线"),
                        ("viewer_uv", "观看人数"), ("pay_amt", "成交金额"),
                        ("buyer_cnt", "成交人数"), ("item_qty", "成交件数"),
                    ) if key not in totals]
                    if missing:
                        alerts.append(f"直播间 {live_id} 实时数据缺字段：{'、'.join(missing)}")
            except Exception as exc:
                alerts.append(f"实时经营数据源健康检查失败：{exc}")
        if alerts:
            self._send_alert("健康检查", "；".join(alerts))
        else:
            log.info("健康检查通过：磁盘/积压/录制均正常")

    @staticmethod
    def _terminate_orphan_recorder(pid: int | None, session_dir: Path) -> bool:
        """只终止命令行明确包含该分片目录的孤儿 ffmpeg。"""
        if not pid or int(pid) <= 1:
            return False
        import subprocess
        try:
            check = subprocess.run(
                ["ps", "-p", str(int(pid)), "-o", "command="],
                capture_output=True, text=True, timeout=5,
            )
            command = check.stdout.strip()
            if check.returncode != 0 or "ffmpeg" not in command or str(session_dir) not in command:
                return False
            os.killpg(int(pid), signal.SIGTERM)
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    os.kill(int(pid), 0)
                except OSError:
                    return True
                time.sleep(0.1)
            os.killpg(int(pid), signal.SIGKILL)
            return True
        except (OSError, ValueError):
            return False

    def _quarantine_stale_recording_owners(self) -> int:
        """停掉不属于当前会话的旧 ffmpeg，并只把状态降为待封存。"""
        active_ids = {
            int(sess["stream_id"]) for sess in self.sessions.values()
        }
        if not active_ids:
            return 0
        changed = 0
        rows = self.store.query(
            "SELECT id,live_id,recorder_pid,session_dir FROM streams "
            "WHERE status='recording' ORDER BY id")
        for row in rows:
            stream_id = int(row["id"])
            if stream_id in active_ids:
                continue
            pid = int(row["recorder_pid"] or 0)
            raw_dir = str(row["session_dir"] or "")
            safe_dir: Path | None = None
            try:
                candidate = Path(raw_dir).resolve(strict=False)
                candidate.relative_to(self.out_dir.resolve(strict=False))
                safe_dir = candidate
            except (OSError, ValueError):
                pass

            stopped = False
            if pid > 1 and safe_dir is not None:
                stopped = bool(self._terminate_orphan_recorder(pid, safe_dir))
            if not stopped and pid > 1:
                try:
                    os.kill(pid, 0)
                except OSError:
                    stopped = True
            else:
                stopped = True
            if not stopped:
                log.error(
                    "场次 #%d 的旧录像进程身份无法安全确认，保留 recording 等待核验",
                    stream_id,
                )
                continue
            if self.store.claim_stream(
                    stream_id, ("recording",), "recovering"):
                self.store.set_recorder_pid(stream_id, None)
                if (safe_dir is not None
                        and self._session_recorder_is_dead(
                            pid=None, session_dir=safe_dir)
                        and self._finalize_abandoned_timeline(safe_dir)):
                    self.store.request_hour_window_rescan(
                        live_id=str(row["live_id"] or ""),
                        stream_id=stream_id)
                changed += 1
                log.warning(
                    "场次 #%d 的旧录像主人已退出，时间线已封口，分片延后后台封存",
                    stream_id,
                )
        return changed

    def _session_recorder_is_dead(
            self, *, pid: int | None, session_dir: Path | None) -> bool:
        """封口前必须证明旧录像进程已死；查不清就当它还活着。"""
        if session_dir is None:
            return False
        live_pid = int(pid or 0)
        if live_pid > 1:
            try:
                os.kill(live_pid, 0)
                return False
            except OSError:
                pass
        checker = getattr(self, "_ffmpeg_owns_session_dir", None)
        if not callable(checker):
            return False
        try:
            return not bool(checker(session_dir))
        except Exception:
            return False

    def _ffmpeg_owns_session_dir(self, session_dir: Path) -> bool:
        needle = str(Path(session_dir).resolve(strict=False))
        if not needle:
            return True
        try:
            output = subprocess.check_output(
                ["ps", "-ax", "-o", "pid=,command="],
                text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return True
        return any(
            "ffmpeg" in line and needle in line
            for line in output.splitlines())

    def _finalize_abandoned_timeline(self, session_dir: Path) -> bool:
        """停录后立刻封口时间线，不合并大 mp4。简报只认时间线，不能干等封存。"""
        try:
            manifest = TimelineManifest.open_existing(session_dir)
            if manifest is None:
                return False
            closed = manifest.finalize_unfinished_parts()
            if closed:
                log.warning(
                    "已封口 %d 个半截分片：%s",
                    len(closed), session_dir.name)
            return True
        except Exception:
            log.exception("封口半截时间线失败：%s", session_dir.name)
            return False

    def _finalize_abandoned_recovering_timelines(self) -> int:
        """只封口已确认死亡的 recovering 场次，避免误封仍在写的分片。"""
        active_ids = {
            int(sess["stream_id"]) for sess in self.sessions.values()
        }
        closed = 0
        rows = self.store.query(
            "SELECT id,live_id,session_dir,recorder_pid FROM streams "
            "WHERE status='recovering' ORDER BY id")
        for row in rows:
            stream_id = int(row["id"])
            if stream_id in active_ids:
                continue
            raw_dir = str(row["session_dir"] or "").strip()
            if not raw_dir:
                continue
            try:
                session_dir = Path(raw_dir).resolve(strict=False)
                session_dir.relative_to(self.out_dir.resolve(strict=False))
            except (OSError, ValueError):
                continue
            if not self._session_recorder_is_dead(
                    pid=row["recorder_pid"], session_dir=session_dir):
                log.warning(
                    "场次 #%d 旧录像进程未确认死亡，拒绝封口时间线",
                    stream_id)
                continue
            if self._finalize_abandoned_timeline(session_dir):
                self.store.request_hour_window_rescan(
                    live_id=str(row["live_id"] or ""),
                    stream_id=stream_id)
                closed += 1
        return closed

    def _drain_hour_window_rescans(self) -> int:
        """封口成功后的持久化重扫：直播中和下播后都能补漏掉的小时。"""
        if getattr(self, "transcription", None) is None or self.store is None:
            return 0
        queued = 0
        for row in self.store.claim_hour_window_rescans(limit=5):
            job_key = str(row["job_key"])
            try:
                stream = self.store.get_stream(int(row["stream_id"]))
                if stream is None:
                    self.store.finish_hour_window_rescan(job_key, success=True)
                    continue
                queued += int(self._rescan_hours_for_stream(stream) or 0)
                self.store.finish_hour_window_rescan(job_key, success=True)
            except Exception as exc:
                log.exception("小时窗口重扫失败: %s", job_key)
                self.store.finish_hour_window_rescan(
                    job_key, success=False, error=str(exc))
        return queued

    def _rescan_hours_for_stream(self, stream) -> int:
        # get_stream 返回 sqlite3.Row，统一直接转 dict 再取字段。
        stream = dict(stream)
        room_key = str(stream.get("live_id") or "")
        if not room_key:
            return 0
        sess = self.sessions.get(room_key)
        if sess is None:
            raw_dir = str(stream.get("session_dir") or "").strip()
            if not raw_dir:
                return 0
            session_dir = Path(raw_dir)
            parts = [
                part for part in sorted(session_dir.glob("part_*.ts"))
                if part.stat().st_size >= MIN_VALID_PART_BYTES
            ]
            sess = {
                "stream_id": int(stream["id"]),
                "anchor_id": int(stream["anchor_id"] or 0),
                "recorder": type("RecoveredRecorder", (), {
                    "session_dir": session_dir,
                    "timeline": TimelineManifest.open_existing(session_dir),
                })(),
            }
        else:
            parts = closed_parts(sess["recorder"].session_dir, min_age=0)
        return self._queue_completed_hour_windows(room_key, sess, parts)

    def _tick_media_recovery(self) -> None:
        """有直播时只封口已死亡场次的时间线；大 mp4 合并仍延后。"""
        if self.sessions:
            self._quarantine_stale_recording_owners()
            self._finalize_abandoned_recovering_timelines()
            self._drain_hour_window_rescans()
            return
        self._drain_hour_window_rescans()
        running = getattr(self, "_media_recovery_thread", None)
        if running is not None and running.is_alive():
            return

        deferred_live_ids = set(getattr(
            self, "_deferred_recovery_live_ids", set()) or set())
        rows = self.store.query(
            "SELECT id,live_id FROM streams "
            "WHERE status IN ('recording','recovering') ORDER BY id")
        stream_ids = {
            int(row["id"]) for row in rows
            if str(row["live_id"] or "") not in deferred_live_ids
        }
        if not stream_ids:
            return

        self._media_recovery_stream_ids = set(stream_ids)

        def recover() -> None:
            try:
                self._recover_interrupted(stream_ids=stream_ids)
            except Exception:
                log.exception("后台录像分片封存任务异常")
            finally:
                inflight = getattr(self, "_media_recovery_stream_ids", set())
                inflight.difference_update(stream_ids)

        thread = threading.Thread(
            target=recover,
            name="media-recovery",
            daemon=True,
        )
        self._media_recovery_thread = thread
        thread.start()

    def _recover_interrupted(
            self, *, stream_ids: set[int] | None = None) -> None:
        """封存崩溃前已写入的分片；当前拉流已在本轮程序中先启动。"""
        selected_ids = set(stream_ids or ())
        active_ids = {sess["stream_id"] for sess in self.sessions.values()}
        deferred_live_ids = set(getattr(
            self, "_deferred_recovery_live_ids", set()) or set())
        rows = self.store.query(
            "SELECT * FROM streams WHERE status IN ('recording','recovering') "
            "ORDER BY id")
        for s in rows:
            if selected_ids and int(s["id"]) not in selected_ids:
                continue
            if s["id"] in active_ids:
                continue
            if str(s["live_id"] or "") in deferred_live_ids:
                continue
            if str(s["status"] or "") == "recording" and not self.store.claim_stream(
                    int(s["id"]), ("recording",), "recovering"):
                continue
            raw_dir = str(s["session_dir"] or "")
            try:
                session_dir = Path(raw_dir).resolve(strict=False)
                session_dir.relative_to(self.out_dir.resolve(strict=False))
                parts = [p for p in sorted(session_dir.glob("part_*.ts"))
                         if p.stat().st_size >= MIN_VALID_PART_BYTES]
            except (OSError, ValueError):
                parts = []
                session_dir = Path(raw_dir) if raw_dir else self.out_dir / "__missing__"
            if parts:
                try:
                    self._terminate_orphan_recorder(s["recorder_pid"], session_dir)
                    self.store.set_recorder_pid(int(s["id"]), None)
                    last_part_mtime = max(p.stat().st_mtime for p in parts)
                    recorder = Recorder(self.ffmpeg, session_dir.parent, session_dir.name)
                    recorder.last_media_at = last_part_mtime
                    unified = getattr(self, "transcription", None) is not None
                    final = recorder.stop(cleanup_parts=False) if unified else recorder.stop()
                    duration = _probe_duration(self.ffmpeg, final)
                    ended = datetime.fromtimestamp(
                        last_part_mtime, SHANGHAI,
                    ).strftime("%Y-%m-%d %H:%M:%S")
                    self.store.finish_stream(
                        s["id"], file_path=str(final), duration_sec=duration,
                        status="recorded", ended_at=ended,
                        error="异常重启后已从分片恢复",
                    )
                    self.store.request_hour_window_rescan(
                        live_id=str(s["live_id"] or ""),
                        stream_id=int(s["id"]))
                    log.warning(
                        "场次 #%d 已从 %d 个分片恢复并封存；只保留真实时间线证据",
                        s["id"], len(parts),
                    )
                    continue
                except Exception as exc:
                    log.exception("场次 #%d 分片恢复失败", s["id"])
                    # 保留 recovering，下一轮/下一次重启可继续接管，
                    # 不把一次合并失败变成永久死任务。
                    self.store.set_stream_status(
                        int(s["id"]), "recovering",
                        error=f"分片恢复失败: {exc}")
                    continue
            else:
                error = "程序重启时无可恢复录制分片"
            self.store.finish_stream(s["id"], status="interrupted", error=error)

    def _suspend_session_for_restart(self, room_key: str) -> None:
        """维护重启只停止当前媒体并保留分片，不同步合并长录像。"""
        sess = self.sessions.pop(room_key, None)
        if not sess:
            return
        stream_id = int(sess["stream_id"])
        try:
            sess["recorder"].abort()
        except Exception:
            # 留在 recording，新进程仍可根据 PID+分片接管。
            log.exception("场次 #%d 维护重启停录失败", stream_id)
            return
        if self.store.claim_stream(stream_id, ("recording",), "recovering"):
            self.store.set_recorder_pid(stream_id, None)
            log.info("场次 #%d 已停录并交由新进程封存分片", stream_id)


    def request_stop(self) -> None:
        """信号处理器只设标志；不在信号上下文等待 ASR/网络任务。"""
        log.info("收到停止信号，将在当前安全点收尾")
        self._stop = True

    def close(self) -> None:
        """维护退出只停媒体；持久化后台任务由下一进程继续接管。"""
        heartbeat = getattr(self, "_heartbeat", None)
        if heartbeat is not None:
            try:
                heartbeat.mark_stopping()
            except Exception:
                log.exception("看门狗停止标记写入失败")
        for key in list(self.sessions.keys()):
            try:
                # 维护重启不等于真下播；快速停录后由新进程封存，
                # 避免长录像合并阻塞下一个录像进程。
                self._suspend_session_for_restart(key)
            except Exception:
                log.exception("收尾失败 room=%s", key)
        # 所有任务均以 SQLite 状态为真相源。维护切换不能等待网络线程，
        # 否则 launchd 的有界停机窗口会被 Mtop/妙记/DeepSeek 请求耗尽。
        self.transcription_pool.shutdown(wait=False, cancel_futures=True)
        self.session_pool.shutdown(wait=False, cancel_futures=True)
        schedule_pool = getattr(self, "schedule_pool", None)
        if schedule_pool is not None:
            schedule_pool.shutdown(wait=False, cancel_futures=True)
        fact_pool = getattr(self, "fact_pool", None)
        if fact_pool is not None:
            fact_pool.shutdown(wait=False, cancel_futures=True)
        self.pool.shutdown(wait=False, cancel_futures=True)

    # 保留旧调用面，但不再在此处执行耗时收尾。
    stop = request_stop


def _pick_url(urls: list[str], current: str) -> str:
    """优先保留当前流主干，只刷新签名参数；主干消失时才换候选。"""
    current_stem = url_stem(current) if current else ""
    for u in urls:
        if current_stem and url_stem(u) == current_stem:
            return u
    return urls[0]


def _probe_duration(ffmpeg: str, video: Path) -> float:
    import subprocess
    try:
        out = subprocess.run(
            [ffmpeg.replace("ffmpeg", "ffprobe") if ffmpeg.endswith("ffmpeg") else "ffprobe",
             "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, timeout=30,
        )
        return float(out.stdout.strip())
    except Exception:
        return 0.0


_acquire_singleton = acquire_process_lock


def _enable_external_watchdog(cfg: dict, watcher: Watcher):
    """Arm the process-external watchdog after the watcher owns its lock."""
    settings = WatchdogSettings.from_config(cfg)
    if not settings.enabled:
        return None
    runtime_dir = resolve(cfg["paths"]["db"]).parent
    heartbeat = create_watcher_heartbeat(settings, runtime_dir)
    watcher._heartbeat = heartbeat
    try:
        return start_watchdog_process(settings, heartbeat)
    except Exception:
        try:
            heartbeat.mark_stopping()
        except Exception:
            pass
        watcher._heartbeat = None
        raise


def _deployed_commit() -> str:
    """Return the immutable local code identity without exposing configuration."""
    import subprocess

    root = Path(__file__).resolve().parents[2]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root,
            capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    value = result.stdout.strip().lower()
    return value if re.fullmatch(r"[0-9a-f]{40}", value) else "unknown"


def main() -> None:
    import logging.handlers
    os.chdir(Path(__file__).resolve().parents[2])
    os.umask(0o077)
    log_dir = Path("data/logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    # 日志轮转：5MB × 5 个文件，防长期运行日志无限膨胀
    _rfh = logging.handlers.RotatingFileHandler(
        log_dir / "watcher.log", maxBytes=5 * 1024 * 1024, backupCount=5,
        encoding="utf-8")
    handlers: list[logging.Handler] = [_rfh]
    # launchd 已把 stdout/stderr 指向同一 watcher.log；后台进程再挂 StreamHandler 会每行写两次。
    # 仅在人工前台终端运行时同时输出到屏幕。
    if sys.stderr.isatty():
        handlers.insert(0, logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=handlers,
        force=True,
    )
    log.info("生产代码提交：%s", _deployed_commit())
    cfg = load_config()
    from ..config import ensure_dirs
    ensure_dirs(cfg)
    # 单实例锁：已有实例在跑则直接退出（避免双实例各自开录/重复提交）
    lock_file = _acquire_singleton(Path(resolve(cfg["paths"]["db"]).parent) / "watcher.lock")
    if lock_file is None:
        lock_path = Path(resolve(cfg["paths"]["db"]).parent) / "watcher.lock"
        log.warning("检测到已有 watcher 实例在运行（锁 %s），本实例退出", lock_path)
        sys.exit(0)
    log.info("单实例锁已获取：%s", Path(resolve(cfg["paths"]["db"]).parent) / "watcher.lock")
    store = Store(resolve(cfg["paths"]["db"]))
    watcher = Watcher(cfg, store)
    signal.signal(signal.SIGINT, lambda *_: watcher.request_stop())
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, lambda *_: watcher.request_stop())
        except (ValueError, OSError):
            pass  # Windows 上 SIGTERM 不可注册
    try:
        _enable_external_watchdog(cfg, watcher)
        watcher.run()
    finally:
        watcher.close()
        # close() 不等待仍在退出的网络线程；此处不能抢先关闭它们共用的
        # SQLite 连接。进程退出时由 SQLite/操作系统统一释放句柄。


if __name__ == "__main__":
    main()
