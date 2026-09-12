"""SQLite 存储层：anchors / streams / transcripts / highlights / talktracks / reviews"""
from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from .asr.clean import DISPLAY_REPAIR_VERSION, OUTWARD_REVIEW_PLACEHOLDER
from .business_facts import (
    business_session_key as build_business_session_key,
    formal_hourly_metrics_ready,
)
from .config import SHANGHAI, load_config, resolve

INTELLIGENCE_TRANSITIONS = {
    # 小时简报先完整分析、后提取可选证据；整场复盘继续复用原有证据优先路径。
    "queued": {"analyzing_evidence", "designing_actions", "blocked"},
    "analyzing_evidence": {"validating_evidence", "blocked"},
    "validating_evidence": {"designing_actions", "ready", "blocked"},
    "designing_actions": {"validating_actions", "blocked"},
    "validating_actions": {"analyzing_evidence", "ready", "blocked"},
}
INTELLIGENCE_TERMINAL_STATUSES = frozenset((
    "ready", "blocked",
))

# 事实重抓永久放弃的时间哨兵：next_at 设为极远未来，claim 永不再领取，
# 避免对无法补齐的窗口无限调用千牛接口。
FACT_RESCUE_GIVE_UP_AT = 1e15

# These cumulative fields are the minimum needed to derive all screen-boundary
# metrics in the fixed ten-item hourly contract. Zero is valid; NULL is not.
HOURLY_FACT_BOUNDARY_FIELDS = (
    "viewer_uv", "pay_amt", "buyer_cnt", "atn_uv", "refund_amt",
    "stay_time_pu",
)


def _intelligence_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _load_intelligence_json(value: object, default: object) -> object:
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError):
        raise ValueError("invalid persisted intelligence JSON")


SCHEMA = """
CREATE TABLE IF NOT EXISTS anchors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    taobao_user_id TEXT DEFAULT '',
    live_id TEXT DEFAULT '',
    room_url TEXT DEFAULT '',
    -- 轮班时间表（同一直播间多人轮播时用于归属场次）："09:00-12:00"，支持跨天 "22:00-02:00"
    shift TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS streams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    anchor_id INTEGER REFERENCES anchors(id),
    started_at TEXT,
    ended_at TEXT,
    file_path TEXT DEFAULT '',
    duration_sec REAL DEFAULT 0,
    status TEXT DEFAULT 'recording',
    -- status: recording -> recovering -> recorded -> transcribing -> transcribed -> analyzing -> analyzed -> reporting -> reported
    --         或 failed / interrupted
    -- 中间态（transcribing/analyzing/reporting）表示"已被某线程领取处理中"，原子领取防重复处理
    error TEXT DEFAULT '',
    failed_stage TEXT DEFAULT '', -- recording/transcribe/analyze/report/notify
    live_id TEXT DEFAULT '',   -- 开播时保存的 liveId（不可变，防自动切换后旧场次串新数据）
    business_session_key TEXT DEFAULT '', -- 业务直播日，跨 liveId 合并正式复盘
    session_dir TEXT DEFAULT '', -- 录制分片目录，用于崩溃后恢复
    recorder_pid INTEGER,        -- 当前 ffmpeg PID，恢复时只终止命令行匹配的孤儿进程
    resume_url TEXT DEFAULT '',  -- 私有运行线索；仅用于重启续录，严禁记日志/回复
    transcribed_at TEXT,
    analyzed_at TEXT,
    reported_at TEXT,
    updated_at TEXT DEFAULT (datetime('now','localtime')),
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id INTEGER NOT NULL REFERENCES streams(id),
    start_ms INTEGER,
    end_ms INTEGER,
    text TEXT,
    display_text TEXT DEFAULT '',
    display_state TEXT DEFAULT 'pending',
    text_provenance TEXT DEFAULT 'raw_asr',
    source_hash TEXT DEFAULT '',
    repair_version TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS highlights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id INTEGER NOT NULL REFERENCES streams(id),
    anchor_id INTEGER REFERENCES anchors(id),
    start_ms INTEGER,
    end_ms INTEGER,
    score REAL DEFAULT 0,
    reasons TEXT DEFAULT '[]',      -- JSON: ["逼单", "声学峰值"]
    transcript TEXT DEFAULT '',
    kind TEXT DEFAULT 'internal_signal', -- internal_signal/data_association/quality
    peak_meta TEXT DEFAULT '{}',    -- JSON: 可审计峰值及时间关联元数据
    quality_meta TEXT DEFAULT '{}', -- JSON: 话术质量类别/分数/入选依据
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS talktracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    anchor_id INTEGER NOT NULL REFERENCES anchors(id),
    stream_id INTEGER REFERENCES streams(id),
    category TEXT DEFAULT '其他',
    text TEXT,
    text_provenance TEXT DEFAULT 'raw_asr',
    norm_text TEXT,
    use_count INTEGER DEFAULT 1,
    first_seen TEXT,
    last_seen TEXT,
    UNIQUE(anchor_id, norm_text, category)
);

-- 每场话术出现记录：同一场同一归一话术只计一次，使重跑分析保持幂等。
CREATE TABLE IF NOT EXISTS talktrack_occurrences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id INTEGER NOT NULL REFERENCES streams(id),
    anchor_id INTEGER NOT NULL REFERENCES anchors(id),
    category TEXT NOT NULL,
    norm_text TEXT NOT NULL,
    text TEXT DEFAULT '',
    text_provenance TEXT DEFAULT 'raw_asr',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(stream_id, category, norm_text)
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id INTEGER NOT NULL REFERENCES streams(id),
    report_path TEXT DEFAULT '',
    ai_review TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

-- 直播中简报数据快照（每期一次，用于下一期做时段对比）
CREATE TABLE IF NOT EXISTS brief_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id INTEGER NOT NULL REFERENCES streams(id),
    ts TEXT DEFAULT '',
    snapshot_kind TEXT DEFAULT 'brief',
    source TEXT DEFAULT '',
    data_state TEXT DEFAULT '',
    online_uv INTEGER,
    max_online_uv INTEGER,
    viewer_uv INTEGER,
    viewer_pv INTEGER,
    visitor_total INTEGER,
    ipv_total INTEGER,
    pay_amt REAL,
    buyer_cnt INTEGER,
    order_cnt INTEGER,
    item_qty INTEGER,
    pay_byr_rate REAL,
    ipv_uv_rate REAL,
    atn_uv INTEGER,
    comment_uv INTEGER,
    favor_uv INTEGER,
    share_uv INTEGER,
    heat_score INTEGER,
    refund_amt REAL,
    stay_time_pu REAL,
    raw TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_brief_snap_stream ON brief_snapshots(stream_id);

-- 直播中简报的分片转写（带全场偏移时间戳）：下播复盘时复用，避免重复 ASR
CREATE TABLE IF NOT EXISTS brief_transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id INTEGER NOT NULL REFERENCES streams(id),
    part_name TEXT DEFAULT '',      -- part_050.ts
    start_ms INTEGER DEFAULT 0,     -- 全场偏移后时间戳
    end_ms INTEGER DEFAULT 0,
    text TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_brief_tr_stream ON brief_transcripts(stream_id, part_name);

-- 统一转写任务：妙记主链路与 FunASR 备胎共享同一持久化状态机。
CREATE TABLE IF NOT EXISTS transcription_jobs (
    job_key TEXT PRIMARY KEY,
    stream_id INTEGER NOT NULL REFERENCES streams(id),
    live_id TEXT DEFAULT '',
    purpose TEXT NOT NULL DEFAULT 'hourly',
    window_start_ms INTEGER NOT NULL,
    window_end_ms INTEGER NOT NULL,
    media_manifest TEXT NOT NULL DEFAULT '[]',
    media_hash TEXT NOT NULL DEFAULT '',
    media_origin_ms INTEGER NOT NULL DEFAULT 0,
    media_layout_json TEXT NOT NULL DEFAULT '[]',
    media_coverage_json TEXT NOT NULL DEFAULT '{}',
    media_path TEXT DEFAULT '',
    business_session_key TEXT DEFAULT '',
    shift_window_key TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    remote_status TEXT NOT NULL DEFAULT 'queued',
    deadline_at REAL,
    next_poll_at REAL NOT NULL DEFAULT 0,
    lease_until REAL NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    drive_file_token TEXT DEFAULT '',
    minute_token TEXT DEFAULT '',
    minute_url TEXT DEFAULT '',
    note_id TEXT DEFAULT '',
    note_doc_token TEXT DEFAULT '',
    result_json TEXT DEFAULT '',
    fallback_json TEXT DEFAULT '',
    review_fallback_approved INTEGER NOT NULL DEFAULT 0,
    error_class TEXT DEFAULT '',
    error TEXT DEFAULT '',
    cleanup_status TEXT NOT NULL DEFAULT 'pending',
    cleanup_attempts INTEGER NOT NULL DEFAULT 0,
    cleanup_next_at REAL NOT NULL DEFAULT 0,
    cleaned_at REAL NOT NULL DEFAULT 0,
    consumer_status TEXT NOT NULL DEFAULT 'pending',
    consumer_attempts INTEGER NOT NULL DEFAULT 0,
    consumer_next_at REAL NOT NULL DEFAULT 0,
    delivery_key TEXT DEFAULT '',
    sent_at REAL NOT NULL DEFAULT 0,
    alert_status TEXT NOT NULL DEFAULT 'pending',
    alert_after_at REAL NOT NULL DEFAULT 0,
    last_alert_at REAL NOT NULL DEFAULT 0,
    fallback_after_at REAL NOT NULL DEFAULT 0,
    recovery_metrics_status TEXT NOT NULL DEFAULT '',
    recovery_metrics_json TEXT NOT NULL DEFAULT '',
    recovery_metrics_attempts INTEGER NOT NULL DEFAULT 0,
    recovery_metrics_next_at REAL NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_transcription_jobs_due
    ON transcription_jobs(status, next_poll_at, lease_until);
CREATE INDEX IF NOT EXISTS idx_transcription_jobs_stream
    ON transcription_jobs(stream_id, window_start_ms);

-- 排班轮换简报任务：旧主播最后一个分片的 ASR/飞书发送可跨 watcher 重启续跑。
CREATE TABLE IF NOT EXISTS rotation_brief_jobs (
    stream_id INTEGER PRIMARY KEY REFERENCES streams(id),
    -- waiting/running/sent/failed/delivery_unknown；末者必须人工核验，不可自动领取
    status TEXT NOT NULL DEFAULT 'waiting',
    triggered_at REAL NOT NULL,
    next_attempt_at REAL NOT NULL,
    lease_until REAL NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT DEFAULT '',
    sent_at REAL NOT NULL DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_rotation_brief_due
    ON rotation_brief_jobs(status, next_attempt_at);

-- 淘宝浏览器授权状态：单行、跨重启，严禁保存 Cookie 原文。
CREATE TABLE IF NOT EXISTS taobao_session_state (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
    status TEXT NOT NULL DEFAULT 'healthy',
    first_failed_at REAL NOT NULL DEFAULT 0,
    last_error_class TEXT DEFAULT '',
    last_reminder_at REAL NOT NULL DEFAULT 0,
    next_check_at REAL NOT NULL DEFAULT 0,
    generation INTEGER NOT NULL DEFAULT 0,
    last_cookie_hash TEXT DEFAULT '',
    last_validated_at REAL NOT NULL DEFAULT 0,
    recovery_notice_status TEXT NOT NULL DEFAULT 'sent',
    recovery_notice_payload TEXT NOT NULL DEFAULT '{}',
    recovery_owner TEXT NOT NULL DEFAULT '',
    recovery_lease_until REAL NOT NULL DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
INSERT OR IGNORE INTO taobao_session_state(singleton_id) VALUES(1);

-- 平台场次总账（冻结数据按 liveId 唯一）：淘宝把一天直播算作一个平台场次，
-- 本地可能因轮换/重启拆成多个 streams；整场总账只存这里，拆分场次不重复写入。
CREATE TABLE IF NOT EXISTS platform_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    live_id TEXT NOT NULL UNIQUE,
    content_id TEXT DEFAULT '',
    started_at TEXT DEFAULT '',
    ended_at TEXT DEFAULT '',
    pay_amt REAL,
    order_cnt INTEGER,
    buyer_cnt INTEGER,
    viewer_uv INTEGER,
    viewer_pv INTEGER,
    max_online_uv INTEGER,
    item_qty INTEGER,
    raw TEXT DEFAULT '{}',
    fetched_at TEXT DEFAULT (datetime('now','localtime')),
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

-- 平台场次内的主播经营指标：同一主播多次上下钟先按 liveId 归并，再供整场复盘使用。
CREATE TABLE IF NOT EXISTS platform_anchor_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    live_id TEXT NOT NULL,
    anchor_id INTEGER NOT NULL REFERENCES anchors(id),
    daibo_id TEXT DEFAULT '',
    daibo_name TEXT DEFAULT '',
    segment_count INTEGER DEFAULT 0,
    on_air_duration_sec REAL,
    work_up_at TEXT DEFAULT '',
    work_down_at TEXT DEFAULT '',
    look_uv REAL, look_pv REAL,
    pay_amt REAL, pay_byr_cnt REAL, pay_ord_cnt REAL, pay_itm_qty REAL,
    cvr_pay REAL, atv REAL,
    ipv_uv REAL, ipv REAL, ctr_itm REAL,
    cart_uv REAL, cart_pv REAL, cart_itm_qty REAL,
    atn_uv REAL, cmt_uv REAL, cmt_pv REAL,
    shr_uv REAL, shr_pv REAL, fvr_uv REAL, fvr_pv REAL,
    sns_uv REAL, sns_pv REAL, rfd_amt REAL,
    look_uv_segment_sum REAL, pay_byr_cnt_segment_sum REAL,
    ipv_uv_segment_sum REAL, cart_uv_segment_sum REAL,
    atn_uv_segment_sum REAL, cmt_uv_segment_sum REAL,
    shr_uv_segment_sum REAL, fvr_uv_segment_sum REAL,
    sns_uv_segment_sum REAL,
    aggregation_note TEXT DEFAULT '',
    source TEXT DEFAULT '',
    data_state TEXT DEFAULT '',
    data_issues TEXT DEFAULT '[]',
    raw TEXT DEFAULT '{}',
    fetched_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(live_id, anchor_id)
);
CREATE INDEX IF NOT EXISTS idx_platform_anchor_live
    ON platform_anchor_metrics(live_id, anchor_id);

-- 平台整场正式复盘任务：只有明确下播确认路径可以创建；business_session_key
-- 保证同一业务直播日跨多个技术 liveId 只发一张卡。
CREATE TABLE IF NOT EXISTS platform_review_jobs (
    live_id TEXT PRIMARY KEY,
    business_session_key TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'waiting',
    triggered_at REAL NOT NULL,
    deadline_at REAL NOT NULL,
    next_attempt_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_until REAL NOT NULL DEFAULT 0,
    settlement_reminded_at REAL NOT NULL DEFAULT 0,
    report_path TEXT DEFAULT '',
    error TEXT DEFAULT '',
    sent_at REAL NOT NULL DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_platform_review_due
    ON platform_review_jobs(status, next_attempt_at);

-- 旧库可能已存在需人工核验的重复日报行，不能为建唯一索引
-- 在启动时破坏性删除。触发器先阻止任何新重复；无历史冲突的库
-- 还会在迁移阶段建立部分唯一索引。
CREATE TRIGGER IF NOT EXISTS prevent_duplicate_business_daily_insert
BEFORE INSERT ON platform_review_jobs
WHEN NEW.business_session_key!='' AND EXISTS (
    SELECT 1 FROM platform_review_jobs
    WHERE business_session_key=NEW.business_session_key
)
BEGIN
    SELECT RAISE(ABORT, 'duplicate business daily identity');
END;

CREATE TRIGGER IF NOT EXISTS prevent_duplicate_business_daily_update
BEFORE UPDATE OF business_session_key ON platform_review_jobs
WHEN NEW.business_session_key!='' AND EXISTS (
    SELECT 1 FROM platform_review_jobs
    WHERE business_session_key=NEW.business_session_key
      AND live_id!=OLD.live_id
)
BEGIN
    SELECT RAISE(ABORT, 'duplicate business daily identity');
END;

-- 正式复盘冻结载荷：发送失败/进程重启时复用同一份 AI 结果，避免重复生成后漂移。
CREATE TABLE IF NOT EXISTS platform_reviews (
    live_id TEXT PRIMARY KEY,
    data_state TEXT DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}',
    report_path TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);

-- 直播生命周期确认：明确结束必须跨重启连续出现，任何非 ended 状态都会清零。
CREATE TABLE IF NOT EXISTS room_lifecycle_state (
    live_id TEXT PRIMARY KEY,
    last_state TEXT NOT NULL DEFAULT 'unknown',
    ended_confirmations INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);

-- 守护进程维护任务状态：跨重启保留成功/失败/退避，避免每日任务反复执行或失败后失联。
CREATE TABLE IF NOT EXISTS maintenance_jobs (
    job_key TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt REAL NOT NULL DEFAULT 0,
    last_success REAL NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL DEFAULT 0,
    error TEXT DEFAULT ''
);

-- 人工对高亮话术的评价，由飞书多维表格同步回来；只存反馈，不存敏感凭据。
CREATE TABLE IF NOT EXISTS highlight_feedback (
    feedback_key TEXT PRIMARY KEY,
    text_hash TEXT NOT NULL DEFAULT '',
    rating TEXT NOT NULL DEFAULT '',
    note TEXT DEFAULT '',
    synced_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_highlight_feedback_text ON highlight_feedback(text_hash);

CREATE TABLE IF NOT EXISTS intelligence_jobs (
    job_key TEXT PRIMARY KEY, task_type TEXT NOT NULL, stream_id INTEGER,
    live_id TEXT NOT NULL DEFAULT '', anchor_id INTEGER,
    window_start_ms INTEGER NOT NULL DEFAULT 0,
    window_end_ms INTEGER NOT NULL DEFAULT 0,
    input_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
    deadline_at REAL NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL DEFAULT 0,
    lease_until REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
    model TEXT NOT NULL DEFAULT '', prompt_version TEXT NOT NULL DEFAULT '',
    fallback_reason TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_intelligence_due
    ON intelligence_jobs(status,next_attempt_at,lease_until);

CREATE TABLE IF NOT EXISTS intelligence_artifacts (
    job_key TEXT PRIMARY KEY REFERENCES intelligence_jobs(job_key),
    context_snapshot_json TEXT NOT NULL DEFAULT '{}',
    raw_evidence_json TEXT NOT NULL DEFAULT '{}',
    validated_evidence_json TEXT NOT NULL DEFAULT '{}',
    raw_actions_json TEXT NOT NULL DEFAULT '{}',
    validated_result_json TEXT NOT NULL DEFAULT '{}',
    validated_result_hash TEXT NOT NULL DEFAULT '',
    rejected_json TEXT NOT NULL DEFAULT '[]', latency_ms INTEGER NOT NULL DEFAULT 0,
    usage_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);

-- 业务直播日高于淘宝技术 liveId，覆盖轮换、重启和多个技术场次。
CREATE TABLE IF NOT EXISTS business_sessions (
    business_session_key TEXT PRIMARY KEY,
    planned_start TEXT DEFAULT '',
    planned_end TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'collecting',
    source_live_ids TEXT NOT NULL DEFAULT '[]',
    source_stream_ids TEXT NOT NULL DEFAULT '[]',
    ended_confirmations INTEGER NOT NULL DEFAULT 0,
    observation_deadline REAL NOT NULL DEFAULT 0,
    last_live_at REAL NOT NULL DEFAULT 0,
    observation_started_at REAL NOT NULL DEFAULT 0,
    observation_completed_at REAL NOT NULL DEFAULT 0,
    ended_at REAL NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);

-- 经营事实来源区间独立于录像切片；录音缺口不能被误判为当时没有直播。
CREATE TABLE IF NOT EXISTS business_fact_source_intervals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_session_key TEXT NOT NULL REFERENCES business_sessions(business_session_key),
    live_id TEXT NOT NULL,
    stream_id INTEGER,
    started_at_ms INTEGER NOT NULL,
    ended_at_ms INTEGER NOT NULL DEFAULT 0,
    last_observed_at_ms INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    start_evidence TEXT NOT NULL DEFAULT 'verified_live',
    end_evidence TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(business_session_key,live_id,started_at_ms)
);
CREATE UNIQUE INDEX IF NOT EXISTS uidx_business_fact_source_open
    ON business_fact_source_intervals(business_session_key)
    WHERE status='open';
CREATE INDEX IF NOT EXISTS idx_business_fact_source_window
    ON business_fact_source_intervals(
        business_session_key,started_at_ms,ended_at_ms);

-- 整点/场次切换的累计事实捕获任务；软租约保证崩溃后可继续领取。
CREATE TABLE IF NOT EXISTS hourly_fact_capture_jobs (
    job_key TEXT PRIMARY KEY,
    business_session_key TEXT NOT NULL REFERENCES business_sessions(business_session_key),
    boundary_kind TEXT NOT NULL,
    nominal_boundary_ms INTEGER NOT NULL,
    live_id TEXT NOT NULL,
    stream_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    expires_at REAL NOT NULL DEFAULT 0,
    lease_until REAL NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(business_session_key,boundary_kind,nominal_boundary_ms,live_id)
);
CREATE INDEX IF NOT EXISTS idx_hourly_fact_capture_due
    ON hourly_fact_capture_jobs(status,next_attempt_at,lease_until);

-- 每个任务只保存距名义边界最近的一份有效累计事实；失败响应永不覆盖。
CREATE TABLE IF NOT EXISTS hourly_fact_boundaries (
    job_key TEXT PRIMARY KEY REFERENCES hourly_fact_capture_jobs(job_key),
    business_session_key TEXT NOT NULL,
    boundary_kind TEXT NOT NULL,
    nominal_boundary_ms INTEGER NOT NULL,
    live_id TEXT NOT NULL,
    stream_id INTEGER,
    captured_at_ms INTEGER NOT NULL,
    distance_ms INTEGER NOT NULL,
    source TEXT NOT NULL,
    data_state TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_hourly_fact_boundary_lookup
    ON hourly_fact_boundaries(
        business_session_key,live_id,nominal_boundary_ms,boundary_kind);

-- 不可变绝对排班窗口；实际主播切片和窗口级数据分别保留。
CREATE TABLE IF NOT EXISTS shift_windows (
    shift_window_key TEXT PRIMARY KEY,
    business_session_key TEXT NOT NULL REFERENCES business_sessions(business_session_key),
    window_start_ms INTEGER NOT NULL,
    window_end_ms INTEGER NOT NULL,
    scheduled_anchors_json TEXT NOT NULL DEFAULT '[]',
    actual_slices_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'scheduled',
    artifact_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_shift_windows_session
    ON shift_windows(business_session_key, window_start_ms);

-- 同一冻结小时事实供简报、报告和 Base 复用；完整产物拒绝被残缺批次覆盖。
CREATE TABLE IF NOT EXISTS hourly_artifacts (
    shift_window_key TEXT PRIMARY KEY REFERENCES shift_windows(shift_window_key),
    business_session_key TEXT NOT NULL,
    artifact_hash TEXT NOT NULL,
    quality_state TEXT NOT NULL DEFAULT 'partial',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_hourly_artifacts_session
    ON hourly_artifacts(business_session_key);

-- 运行时不变条件与告警冷却。只保存脱敏证据，不保存任何认证信息。
CREATE TABLE IF NOT EXISTS runtime_issues (
    issue_key TEXT PRIMARY KEY,
    issue_code TEXT NOT NULL,
    business_session_key TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    repair_action TEXT NOT NULL DEFAULT '',
    occurrences INTEGER NOT NULL DEFAULT 1,
    first_seen_at REAL NOT NULL DEFAULT 0,
    last_seen_at REAL NOT NULL DEFAULT 0,
    last_notified_at REAL NOT NULL DEFAULT 0,
    resolved_at REAL NOT NULL DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_runtime_issues_status
    ON runtime_issues(status,last_seen_at);

CREATE INDEX IF NOT EXISTS idx_streams_anchor ON streams(anchor_id);
CREATE INDEX IF NOT EXISTS idx_transcripts_by_stream ON transcripts(stream_id);
CREATE INDEX IF NOT EXISTS idx_highlights_stream ON highlights(stream_id);
CREATE INDEX IF NOT EXISTS idx_talktracks_anchor ON talktracks(anchor_id);
CREATE INDEX IF NOT EXISTS idx_tt_occ_stream ON talktrack_occurrences(stream_id);
"""


def now_str() -> str:
    return datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # SQLite defaults this to OFF per connection. Keep historical orphan
        # rows visible for explicit repair, but reject every new orphan write.
        self.conn.execute("PRAGMA foreign_keys=ON")
        # WAL：读写不互斥 + 崩溃安全；busy_timeout：并发写等待而非立刻报错
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=8000")
        self.conn.executescript(SCHEMA)
        artifact_cols = [
            r["name"] for r in self.conn.execute(
                "PRAGMA table_info(intelligence_artifacts)")]
        if "context_snapshot_json" not in artifact_cols:
            self.conn.execute(
                "ALTER TABLE intelligence_artifacts "
                "ADD COLUMN context_snapshot_json TEXT NOT NULL DEFAULT '{}'")
        if "validated_result_hash" not in artifact_cols:
            self.conn.execute(
                "ALTER TABLE intelligence_artifacts "
                "ADD COLUMN validated_result_hash TEXT NOT NULL DEFAULT ''")
        # 迁移：旧库补 live_id 列 + reviews 唯一约束（先清重复行再建唯一索引）
        self._migrate()
        self.conn.commit()
        self._write_lock = threading.Lock()  # 单写入队列，防并发写同一连接

    def _migrate(self) -> None:
        cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(streams)")]
        if "live_id" not in cols:
            self.conn.execute("ALTER TABLE streams ADD COLUMN live_id TEXT DEFAULT ''")
        for name, decl in (
            ("transcribed_at", "TEXT"), ("analyzed_at", "TEXT"),
            ("reported_at", "TEXT"),
            ("updated_at", "TEXT DEFAULT ''"),
            ("failed_stage", "TEXT DEFAULT ''"),
            ("session_dir", "TEXT DEFAULT ''"),
            ("recorder_pid", "INTEGER"),
            ("resume_url", "TEXT DEFAULT ''"),
            ("business_session_key", "TEXT DEFAULT ''"),
        ):
            if name not in cols:
                self.conn.execute(f"ALTER TABLE streams ADD COLUMN {name} {decl}")
        transcript_cols = [
            r["name"] for r in self.conn.execute("PRAGMA table_info(transcripts)")]
        for name, decl in (
            ("display_text", "TEXT DEFAULT ''"),
            ("display_state", "TEXT DEFAULT 'pending'"),
            ("text_provenance", "TEXT DEFAULT 'raw_asr'"),
            ("source_hash", "TEXT DEFAULT ''"),
            ("repair_version", "TEXT DEFAULT ''"),
        ):
            if name not in transcript_cols:
                self.conn.execute(f"ALTER TABLE transcripts ADD COLUMN {name} {decl}")
        job_cols = [
            r["name"] for r in self.conn.execute("PRAGMA table_info(transcription_jobs)")]
        for name, decl in (
            ("review_fallback_approved", "INTEGER NOT NULL DEFAULT 0"),
            ("media_origin_ms", "INTEGER NOT NULL DEFAULT 0"),
            ("media_layout_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("media_coverage_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("business_session_key", "TEXT DEFAULT ''"),
            ("shift_window_key", "TEXT DEFAULT ''"),
            ("alert_after_at", "REAL NOT NULL DEFAULT 0"),
            ("last_alert_at", "REAL NOT NULL DEFAULT 0"),
            ("fallback_after_at", "REAL NOT NULL DEFAULT 0"),
            ("recovery_metrics_status", "TEXT NOT NULL DEFAULT ''"),
            ("recovery_metrics_json", "TEXT NOT NULL DEFAULT ''"),
            ("recovery_metrics_attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("recovery_metrics_next_at", "REAL NOT NULL DEFAULT 0"),
            ("fact_rescue_attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("fact_rescue_next_at", "REAL NOT NULL DEFAULT 0"),
        ):
            if name not in job_cols:
                self.conn.execute(
                    f"ALTER TABLE transcription_jobs ADD COLUMN {name} {decl}")
        platform_review_cols = [
            r["name"] for r in self.conn.execute(
                "PRAGMA table_info(platform_review_jobs)")]
        if "settlement_reminded_at" not in platform_review_cols:
            self.conn.execute(
                "ALTER TABLE platform_review_jobs ADD COLUMN "
                "settlement_reminded_at REAL NOT NULL DEFAULT 0")
        if "business_session_key" not in platform_review_cols:
            self.conn.execute(
                "ALTER TABLE platform_review_jobs ADD COLUMN "
                "business_session_key TEXT DEFAULT ''")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_platform_review_business_session "
            "ON platform_review_jobs(business_session_key,status)")
        duplicate_business_daily = self.conn.execute(
            """SELECT 1 FROM platform_review_jobs
               WHERE business_session_key!=''
               GROUP BY business_session_key HAVING COUNT(*)>1 LIMIT 1"""
        ).fetchone()
        if duplicate_business_daily is None:
            self.conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS
                       uidx_platform_review_business_session
                   ON platform_review_jobs(business_session_key)
                   WHERE business_session_key!=''"""
            )
        business_session_cols = [
            r["name"] for r in self.conn.execute(
                "PRAGMA table_info(business_sessions)")]
        for name in (
                "last_live_at", "observation_started_at",
                "observation_completed_at", "ended_at"):
            if name not in business_session_cols:
                self.conn.execute(
                    f"ALTER TABLE business_sessions ADD COLUMN {name} "
                    "REAL NOT NULL DEFAULT 0")
        session_cols = [
            r["name"] for r in self.conn.execute("PRAGMA table_info(taobao_session_state)")]
        if "recovery_notice_payload" not in session_cols:
            self.conn.execute(
                "ALTER TABLE taobao_session_state ADD COLUMN "
                "recovery_notice_payload TEXT NOT NULL DEFAULT '{}'")
        for name, decl in (
            ("recovery_owner", "TEXT NOT NULL DEFAULT ''"),
            ("recovery_lease_until", "REAL NOT NULL DEFAULT 0"),
        ):
            if name not in session_cols:
                self.conn.execute(
                    f"ALTER TABLE taobao_session_state ADD COLUMN {name} {decl}")
        for table in ("talktracks", "talktrack_occurrences"):
            talktrack_cols = [
                r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")]
            if "text_provenance" not in talktrack_cols:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN text_provenance "
                    "TEXT DEFAULT 'raw_asr'")
        # 高亮结构化峰值元数据（旧高亮保持 '{}'，调用方可无感读取）。
        highlight_cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(highlights)")]
        if "peak_meta" not in highlight_cols:
            self.conn.execute("ALTER TABLE highlights ADD COLUMN peak_meta TEXT DEFAULT '{}'")
        if "kind" not in highlight_cols:
            self.conn.execute(
                "ALTER TABLE highlights ADD COLUMN kind TEXT DEFAULT 'internal_signal'")
        if "quality_meta" not in highlight_cols:
            self.conn.execute(
                "ALTER TABLE highlights ADD COLUMN quality_meta TEXT DEFAULT '{}'")
        self.conn.execute(
            """UPDATE highlights SET kind='data_association'
               WHERE peak_meta NOT IN ('','{}') AND kind='internal_signal'""")
        # 旧快照只有少量且把缺失值写成 0；补齐可信快照字段，新逻辑只读取带 source 的行。
        snap_cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(brief_snapshots)")]
        for name, decl in (
            ("snapshot_kind", "TEXT DEFAULT 'brief'"),
            ("source", "TEXT DEFAULT ''"), ("data_state", "TEXT DEFAULT ''"),
            ("online_uv", "INTEGER"), ("max_online_uv", "INTEGER"),
            ("viewer_uv", "INTEGER"), ("viewer_pv", "INTEGER"),
            ("buyer_cnt", "INTEGER"), ("item_qty", "INTEGER"),
            ("pay_amt", "REAL"), ("pay_byr_rate", "REAL"),
            ("ipv_uv_rate", "REAL"), ("atn_uv", "INTEGER"),
            ("comment_uv", "INTEGER"), ("favor_uv", "INTEGER"),
            ("share_uv", "INTEGER"), ("raw", "TEXT DEFAULT '{}'"),
            ("refund_amt", "REAL"), ("stay_time_pu", "REAL"),
        ):
            if name not in snap_cols:
                self.conn.execute(f"ALTER TABLE brief_snapshots ADD COLUMN {name} {decl}")
        # 清理 reviews 重复行（并发下可能出现同场次多行），保留每组最早一行
        self.conn.execute(
            "DELETE FROM reviews WHERE id NOT IN (SELECT MIN(id) FROM reviews GROUP BY stream_id)")
        try:
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_reviews_stream ON reviews(stream_id)")
        except sqlite3.OperationalError:
            pass
        # 转写/高亮去重索引（防并发重复插入）。旧版本错误地把普通索引
        # 和唯一索引使用了同一个名字，SQLite 会静默保留普通索引；先清理
        # 旧索引，再建稳定的查询索引和真正的唯一约束。
        self.conn.execute("DROP INDEX IF EXISTS idx_transcripts_stream")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_transcripts_by_stream ON transcripts(stream_id)")
        try:
            self.conn.execute(
                "DELETE FROM transcripts WHERE id NOT IN ("
                "SELECT MIN(id) FROM transcripts GROUP BY stream_id, start_ms, end_ms, text)")
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_transcripts_unique "
                "ON transcripts(stream_id, start_ms, end_ms, text)")
        except sqlite3.OperationalError:
            pass

    # ---------- 通用 ----------
    def query(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        cur = self.conn.execute(sql, args)
        return cur.fetchall()

    def execute(self, sql: str, args: tuple = ()) -> int:
        with self._write_lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.lastrowid

    def executemany(self, sql: str, args: list) -> int:
        with self._write_lock:
            cur = self.conn.executemany(sql, args)
            self.conn.commit()
            return cur.rowcount

    # ---------- 业务直播日与小时冻结事实 ----------
    @staticmethod
    def _hourly_fact_capture_job_key(
            business_session_key: str, boundary_kind: str,
            nominal_boundary_ms: int, live_id: str) -> str:
        identity = "\x1f".join((
            str(business_session_key), str(boundary_kind),
            str(int(nominal_boundary_ms)), str(live_id),
        ))
        return "hour-fact:" + hashlib.sha256(
            identity.encode("utf-8")).hexdigest()

    def ensure_hourly_fact_capture_job(
            self, *, business_session_key: str, boundary_kind: str,
            nominal_boundary_ms: int, live_id: str, stream_id: int | None,
            next_attempt_at: float, expires_at: float) -> str:
        """Create one stable boundary task without re-arming terminal work."""
        session_key = str(business_session_key or "").strip()
        kind = str(boundary_kind or "").strip()
        live = str(live_id or "").strip()
        if not session_key or kind not in {"clock", "live_start", "live_end"} or not live:
            raise ValueError("valid business session, boundary kind and live_id are required")
        nominal_ms = int(nominal_boundary_ms)
        if nominal_ms <= 0:
            raise ValueError("nominal_boundary_ms must be positive")
        due_at = float(next_attempt_at)
        expiry = float(expires_at)
        if expiry < due_at:
            raise ValueError("capture job expires before its first attempt")
        job_key = self._hourly_fact_capture_job_key(
            session_key, kind, nominal_ms, live)
        with self._write_lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO business_sessions(business_session_key) VALUES(?)",
                (session_key,),
            )
            self.conn.execute(
                """INSERT INTO hourly_fact_capture_jobs(
                       job_key,business_session_key,boundary_kind,
                       nominal_boundary_ms,live_id,stream_id,next_attempt_at,expires_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(job_key) DO UPDATE SET
                       stream_id=COALESCE(hourly_fact_capture_jobs.stream_id,
                                          excluded.stream_id),
                       next_attempt_at=CASE
                           WHEN hourly_fact_capture_jobs.status IN ('ready','exhausted')
                                OR hourly_fact_capture_jobs.attempts>0
                           THEN hourly_fact_capture_jobs.next_attempt_at
                           ELSE MIN(hourly_fact_capture_jobs.next_attempt_at,
                                    excluded.next_attempt_at) END,
                       expires_at=MAX(hourly_fact_capture_jobs.expires_at,
                                      excluded.expires_at),
                       updated_at=datetime('now','localtime')""",
                (job_key, session_key, kind, nominal_ms, live, stream_id,
                 due_at, expiry),
            )
            self.conn.commit()
        return job_key

    def get_hourly_fact_capture_job(self, job_key: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM hourly_fact_capture_jobs WHERE job_key=?",
            (str(job_key),),
        ).fetchone()
        return dict(row) if row is not None else None

    def claim_hourly_fact_capture_jobs(
            self, *, now: float, limit: int = 1,
            lease_sec: int = 60) -> list[sqlite3.Row]:
        """Atomically claim due work, including leases abandoned by a restart."""
        checked_at = float(now)
        lease_until = checked_at + max(1, int(lease_sec))
        claimed: list[str] = []
        with self._write_lock:
            rows = self.conn.execute(
                """SELECT job_key FROM hourly_fact_capture_jobs
                   WHERE ((status='pending') OR
                          (status='running' AND lease_until<=?))
                     AND next_attempt_at<=? AND expires_at>=?
                   ORDER BY next_attempt_at,nominal_boundary_ms LIMIT ?""",
                (checked_at, checked_at, checked_at, max(0, int(limit))),
            ).fetchall()
            for row in rows:
                key = str(row["job_key"])
                cursor = self.conn.execute(
                    """UPDATE hourly_fact_capture_jobs
                       SET status='running',lease_until=?,
                           updated_at=datetime('now','localtime')
                       WHERE job_key=? AND next_attempt_at<=? AND expires_at>=?
                         AND (status='pending' OR
                              (status='running' AND lease_until<=?))""",
                    (lease_until, key, checked_at, checked_at, checked_at),
                )
                if cursor.rowcount:
                    claimed.append(key)
            self.conn.commit()
        if not claimed:
            return []
        placeholders = ",".join("?" for _ in claimed)
        return self.query(
            f"SELECT * FROM hourly_fact_capture_jobs WHERE job_key IN ({placeholders}) "
            "ORDER BY next_attempt_at,nominal_boundary_ms",
            tuple(claimed),
        )

    def save_hourly_fact_boundary(
            self, job_key: str, snapshot: dict,
            *, captured_at_ms: int, tolerance_ms: int = 300_000) -> bool:
        """Keep only the closest valid screen snapshot for one boundary."""
        if not isinstance(snapshot, dict):
            return False
        source = str(snapshot.get("source") or "")
        state = str(snapshot.get("data_state") or "")
        if (source != "screen.totalStats" or state != "ok"
                or any(snapshot.get(name) is None
                       for name in HOURLY_FACT_BOUNDARY_FIELDS)):
            return False
        captured_ms = int(captured_at_ms)
        key = str(job_key)
        with self._write_lock:
            job = self.conn.execute(
                "SELECT * FROM hourly_fact_capture_jobs WHERE job_key=?", (key,),
            ).fetchone()
            if job is None:
                return False
            distance_ms = abs(captured_ms - int(job["nominal_boundary_ms"]))
            if distance_ms > max(0, int(tolerance_ms)):
                return False
            existing = self.conn.execute(
                "SELECT distance_ms FROM hourly_fact_boundaries WHERE job_key=?",
                (key,),
            ).fetchone()
            if existing is not None and int(existing["distance_ms"]) <= distance_ms:
                return False
            encoded = _intelligence_json(snapshot)
            self.conn.execute(
                """INSERT INTO hourly_fact_boundaries(
                       job_key,business_session_key,boundary_kind,
                       nominal_boundary_ms,live_id,stream_id,captured_at_ms,
                       distance_ms,source,data_state,snapshot_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(job_key) DO UPDATE SET
                       stream_id=excluded.stream_id,
                       captured_at_ms=excluded.captured_at_ms,
                       distance_ms=excluded.distance_ms,
                       source=excluded.source,data_state=excluded.data_state,
                       snapshot_json=excluded.snapshot_json,
                       updated_at=datetime('now','localtime')""",
                (key, str(job["business_session_key"]),
                 str(job["boundary_kind"]), int(job["nominal_boundary_ms"]),
                 str(job["live_id"]), job["stream_id"], captured_ms,
                 distance_ms, source, state, encoded),
            )
            self.conn.commit()
        return True

    def finish_hourly_fact_capture_attempt(
            self, job_key: str, *, now: float, success: bool,
            error: str = "", recoverable_until_expiry: bool = False) -> str:
        """Advance one claimed request without ever extending its truth window."""
        checked_at = float(now)
        key = str(job_key)
        retry_offsets = (0.0, 120.0, 240.0)
        with self._write_lock:
            job = self.conn.execute(
                "SELECT * FROM hourly_fact_capture_jobs WHERE job_key=?", (key,),
            ).fetchone()
            if job is None:
                raise KeyError(key)
            nominal = int(job["nominal_boundary_ms"]) / 1000.0
            expiry = float(job["expires_at"])
            has_boundary = self.conn.execute(
                "SELECT 1 FROM hourly_fact_boundaries WHERE job_key=?", (key,),
            ).fetchone() is not None
            status = "pending"
            next_attempt = 0.0
            if success and checked_at >= nominal:
                status = "ready"
            elif success:
                next_attempt = nominal
            else:
                later = [
                    nominal + offset for offset in retry_offsets
                    if nominal + offset > checked_at + 0.001
                    and nominal + offset <= expiry
                ]
                if later:
                    next_attempt = min(later)
                elif (recoverable_until_expiry and not has_boundary
                      and checked_at < expiry):
                    # Park just beyond expiry so only an explicit auth-recovery
                    # event can wake it; the expiry sweeper will otherwise
                    # converge it without issuing a late current-total read.
                    next_attempt = expiry + 1.0
                else:
                    status = "ready" if has_boundary else "exhausted"
            self.conn.execute(
                """UPDATE hourly_fact_capture_jobs
                   SET status=?,attempts=attempts+1,next_attempt_at=?,lease_until=0,
                       last_error=?,updated_at=datetime('now','localtime')
                   WHERE job_key=?""",
                (status, next_attempt,
                 "" if success else str(error or "boundary capture failed")[:500],
                 key),
            )
            self.conn.commit()
        return status

    def _wake_hourly_fact_captures_after_auth_locked(self, *, now: float) -> int:
        checked_at = float(now)
        return self.conn.execute(
            """UPDATE hourly_fact_capture_jobs
               SET next_attempt_at=?,lease_until=0,
                   updated_at=datetime('now','localtime')
               WHERE status='pending'
                 AND last_error='Taobao authentication unavailable'
                 AND expires_at>=? AND next_attempt_at>?""",
            (checked_at, checked_at, checked_at),
        ).rowcount

    def wake_hourly_fact_captures_after_auth(self, *, now: float) -> int:
        """Wake truth-window boundary reads after a validated session recovery."""
        with self._write_lock:
            changed = self._wake_hourly_fact_captures_after_auth_locked(now=now)
            self.conn.commit()
        return changed

    def finalize_expired_hourly_fact_capture_jobs(self, *, now: float) -> int:
        """Converge jobs after +5m without issuing a late remote request."""
        checked_at = float(now)
        with self._write_lock:
            rows = self.conn.execute(
                """SELECT j.job_key,
                          CASE WHEN b.job_key IS NULL THEN 'exhausted' ELSE 'ready' END status
                   FROM hourly_fact_capture_jobs j
                   LEFT JOIN hourly_fact_boundaries b ON b.job_key=j.job_key
                   WHERE j.status IN ('pending','running') AND j.expires_at<?""",
                (checked_at,),
            ).fetchall()
            for row in rows:
                self.conn.execute(
                    """UPDATE hourly_fact_capture_jobs
                       SET status=?,next_attempt_at=0,lease_until=0,
                           last_error=CASE WHEN ?='exhausted'
                               THEN 'truth window expired without a valid boundary'
                               ELSE last_error END,
                           updated_at=datetime('now','localtime')
                       WHERE job_key=?""",
                    (str(row["status"]), str(row["status"]), str(row["job_key"])),
                )
            self.conn.commit()
        return len(rows)

    def get_hourly_fact_boundary(self, job_key: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM hourly_fact_boundaries WHERE job_key=?",
            (str(job_key),),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["snapshot"] = _load_intelligence_json(
            result.pop("snapshot_json"), {})
        return result

    def find_hourly_fact_boundary(
            self, *, business_session_key: str, live_id: str,
            nominal_boundary_ms: int,
            boundary_kinds: tuple[str, ...] = ("clock", "live_start", "live_end"),
    ) -> dict | None:
        """Return the closest valid persisted fact among allowed boundary roles."""
        kinds = tuple(str(kind) for kind in boundary_kinds if str(kind))
        if not kinds:
            return None
        placeholders = ",".join("?" for _ in kinds)
        rows = self.query(
            f"""SELECT * FROM hourly_fact_boundaries
                WHERE business_session_key=? AND live_id=?
                  AND nominal_boundary_ms=?
                  AND boundary_kind IN ({placeholders})
                ORDER BY distance_ms,captured_at_ms,job_key LIMIT 1""",
            (str(business_session_key), str(live_id),
             int(nominal_boundary_ms), *kinds),
        )
        if not rows:
            return None
        result = dict(rows[0])
        result["snapshot"] = _load_intelligence_json(
            result.pop("snapshot_json"), {})
        return result

    def observe_business_fact_source(
            self, business_session_key: str, *, live_id: str,
            stream_id: int | None, observed_at_ms: int,
            source_started_at_ms: int | None = None) -> dict:
        """Persist the single verified live source and atomically close a handoff."""
        session_key = str(business_session_key or "").strip()
        live = str(live_id or "").strip()
        observed_ms = int(observed_at_ms)
        source_start_ms = min(
            observed_ms,
            int(source_started_at_ms)
            if source_started_at_ms is not None else observed_ms,
        )
        if not session_key or not live or source_start_ms <= 0:
            raise ValueError("valid business session, live_id and observation are required")
        closed: dict | None = None
        opened: dict | None = None
        with self._write_lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO business_sessions(business_session_key) VALUES(?)",
                (session_key,),
            )
            current = self.conn.execute(
                """SELECT * FROM business_fact_source_intervals
                   WHERE business_session_key=? AND status='open' LIMIT 1""",
                (session_key,),
            ).fetchone()
            if current is not None and str(current["live_id"]) == live:
                self.conn.execute(
                    """UPDATE business_fact_source_intervals
                       SET last_observed_at_ms=MAX(last_observed_at_ms,?),
                           stream_id=COALESCE(stream_id,?),
                           updated_at=datetime('now','localtime') WHERE id=?""",
                    (observed_ms, stream_id, int(current["id"])),
                )
            else:
                if current is not None:
                    ended_ms = max(int(current["started_at_ms"]), observed_ms)
                    self.conn.execute(
                        """UPDATE business_fact_source_intervals
                           SET ended_at_ms=?,last_observed_at_ms=MAX(
                               last_observed_at_ms,?),status='closed',
                               end_evidence='verified_live_switch',
                               updated_at=datetime('now','localtime') WHERE id=?""",
                        (ended_ms, observed_ms, int(current["id"])),
                    )
                    closed = dict(current)
                    closed.update({
                        "ended_at_ms": ended_ms,
                        "last_observed_at_ms": max(
                            int(current["last_observed_at_ms"]), observed_ms),
                        "status": "closed",
                        "end_evidence": "verified_live_switch",
                    })
                prior = self.conn.execute(
                    """SELECT * FROM business_fact_source_intervals
                       WHERE business_session_key=? AND live_id=?
                         AND started_at_ms=? LIMIT 1""",
                    (session_key, live, source_start_ms),
                ).fetchone()
                if prior is not None:
                    # A same-live replay during the end-observation window uses
                    # the original stream start. Reopen that cumulative scope
                    # instead of inserting the same durable identity twice.
                    self.conn.execute(
                        """UPDATE business_fact_source_intervals
                           SET ended_at_ms=0,status='open',end_evidence='',
                               last_observed_at_ms=MAX(last_observed_at_ms,?),
                               stream_id=COALESCE(stream_id,?),
                               updated_at=datetime('now','localtime') WHERE id=?""",
                        (observed_ms, stream_id, int(prior["id"])),
                    )
                else:
                    cursor = self.conn.execute(
                        """INSERT INTO business_fact_source_intervals(
                               business_session_key,live_id,stream_id,started_at_ms,
                               last_observed_at_ms)
                           VALUES(?,?,?,?,?)""",
                        (session_key, live, stream_id, source_start_ms, observed_ms),
                    )
                    opened_row = self.conn.execute(
                        "SELECT * FROM business_fact_source_intervals WHERE id=?",
                        (int(cursor.lastrowid),),
                    ).fetchone()
                    opened = dict(opened_row) if opened_row is not None else None
            self.conn.commit()
        return {"closed": closed, "opened": opened}

    def business_fact_sources_for_window(
            self, business_session_key: str, *,
            window_start_ms: int, window_end_ms: int) -> list[dict]:
        rows = self.query(
            """SELECT * FROM business_fact_source_intervals
               WHERE business_session_key=? AND started_at_ms<?
                 AND (ended_at_ms=0 OR ended_at_ms>?)
               ORDER BY started_at_ms,id""",
            (str(business_session_key), int(window_end_ms), int(window_start_ms)),
        )
        return [dict(row) for row in rows]

    def close_business_fact_source(
            self, business_session_key: str, *, live_id: str,
            ended_at_ms: int, end_evidence: str = "verified_end") -> dict | None:
        """Close the currently open matching source exactly once."""
        session_key = str(business_session_key or "").strip()
        live = str(live_id or "").strip()
        ended_ms = int(ended_at_ms)
        with self._write_lock:
            row = self.conn.execute(
                """SELECT * FROM business_fact_source_intervals
                   WHERE business_session_key=? AND live_id=? AND status='open'
                   LIMIT 1""",
                (session_key, live),
            ).fetchone()
            if row is None:
                return None
            ended_ms = max(int(row["started_at_ms"]), ended_ms)
            self.conn.execute(
                """UPDATE business_fact_source_intervals
                   SET ended_at_ms=?,last_observed_at_ms=MAX(
                       last_observed_at_ms,?),status='closed',end_evidence=?,
                       updated_at=datetime('now','localtime') WHERE id=?""",
                (ended_ms, ended_ms, str(end_evidence), int(row["id"])),
            )
            self.conn.commit()
        result = dict(row)
        result.update({
            "ended_at_ms": ended_ms,
            "last_observed_at_ms": max(
                int(row["last_observed_at_ms"]), ended_ms),
            "status": "closed",
            "end_evidence": str(end_evidence),
        })
        return result

    def _wake_platform_review_for_session_locked(
            self, business_session_key: str, *, now: float | None = None) -> int:
        """Re-arm a daily parked on local evidence; caller holds ``_write_lock``."""
        key = str(business_session_key or "").strip()
        if not key:
            return 0
        due_at = float(time.time() if now is None else now)
        cursor = self.conn.execute(
            """UPDATE platform_review_jobs
               SET status='waiting',next_attempt_at=?,lease_until=0,
                   error='本地经营材料已更新，重新检查日报完整性',
                   updated_at=datetime('now','localtime')
               WHERE business_session_key=? AND status='waiting_evidence'""",
            (due_at, key),
        )
        return int(cursor.rowcount)

    def save_hourly_artifact(self, shift_window_key: str, payload: dict) -> str:
        """Persist one shared hourly payload without downgrading a frozen fact."""
        if not isinstance(payload, dict):
            raise ValueError("hourly artifact must be an object")
        identity = payload.get("identity") or {}
        session_key = str(identity.get("business_session_key") or "")
        if not session_key:
            raise ValueError("hourly artifact requires business_session_key")
        quality = payload.get("quality") or {}
        quality_state = str(quality.get("state") or "partial")
        digest = str(payload.get("artifact_hash") or "")
        if not digest:
            from .business_facts import artifact_hash
            payload = dict(payload)
            payload["artifact_hash"] = artifact_hash(payload)
            digest = payload["artifact_hash"]
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":"))
        with self._write_lock:
            existing = self.conn.execute(
                "SELECT quality_state,artifact_hash FROM hourly_artifacts "
                "WHERE shift_window_key=?", (str(shift_window_key),),
            ).fetchone()
            if existing is not None and str(existing["quality_state"]) == "complete":
                if quality_state != "complete":
                    raise ValueError("incomplete batch cannot replace complete artifact")
                if str(existing["artifact_hash"]) != digest:
                    raise ValueError("complete hourly artifact is frozen")
                return digest
            self.conn.execute(
                """INSERT INTO hourly_artifacts(
                       shift_window_key,business_session_key,artifact_hash,
                       quality_state,payload_json)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(shift_window_key) DO UPDATE SET
                       business_session_key=excluded.business_session_key,
                       artifact_hash=excluded.artifact_hash,
                       quality_state=excluded.quality_state,
                       payload_json=excluded.payload_json,
                       updated_at=datetime('now','localtime')""",
                (str(shift_window_key), session_key, digest, quality_state, encoded),
            )
            if formal_hourly_metrics_ready(payload):
                self.conn.execute(
                    """UPDATE transcription_jobs
                       SET consumer_status='pending',consumer_next_at=?,lease_until=0,
                           error='',updated_at=datetime('now','localtime')
                       WHERE purpose='hourly' AND shift_window_key=?
                         AND consumer_status='waiting_evidence'""",
                    (time.time(), str(shift_window_key)),
                )
            if quality_state == "complete":
                self._wake_platform_review_for_session_locked(session_key)
            self.conn.commit()
        return digest

    def get_hourly_artifact(self, shift_window_key: str) -> dict | None:
        row = self.conn.execute(
            "SELECT payload_json FROM hourly_artifacts WHERE shift_window_key=?",
            (str(shift_window_key),),
        ).fetchone()
        if row is None:
            return None
        return json.loads(str(row["payload_json"] or "{}"))

    def upsert_business_session(self, business_session_key: str, *,
                                planned_start: str = "", planned_end: str = "",
                                status: str = "collecting") -> None:
        with self._write_lock:
            self.conn.execute(
                """INSERT INTO business_sessions(
                       business_session_key,planned_start,planned_end,status)
                   VALUES(?,?,?,?)
                   ON CONFLICT(business_session_key) DO UPDATE SET
                       planned_start=CASE WHEN excluded.planned_start='' THEN business_sessions.planned_start ELSE excluded.planned_start END,
                       planned_end=CASE WHEN excluded.planned_end='' THEN business_sessions.planned_end ELSE excluded.planned_end END,
                       status=CASE WHEN business_sessions.status IN ('observing','ended')
                                  AND excluded.status='collecting'
                                  THEN business_sessions.status ELSE excluded.status END,
                       updated_at=datetime('now','localtime')""",
                (str(business_session_key), str(planned_start), str(planned_end), str(status)),
            )
            self.conn.commit()

    def add_business_session_source(self, business_session_key: str, *,
                                    live_id: str = "", stream_id: int | None = None) -> None:
        """幂等登记业务日包含的技术 liveId/本地碎片。"""
        key = str(business_session_key or "").strip()
        if not key:
            return
        with self._write_lock:
            self._add_business_session_source_locked(
                key, live_id=live_id, stream_id=stream_id)
            self.conn.commit()

    def _add_business_session_source_locked(
            self, business_session_key: str, *, live_id: str = "",
            stream_id: int | None = None) -> None:
        """Update source sets without committing; caller must hold ``_write_lock``."""
        key = str(business_session_key or "").strip()
        row = self.conn.execute(
            "SELECT source_live_ids,source_stream_ids FROM business_sessions "
            "WHERE business_session_key=?", (key,)).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO business_sessions(business_session_key) VALUES(?)", (key,))
            row = {"source_live_ids": "[]", "source_stream_ids": "[]"}
        live_ids = _load_intelligence_json(row["source_live_ids"] or "[]", [])
        stream_ids = _load_intelligence_json(row["source_stream_ids"] or "[]", [])
        if not isinstance(live_ids, list):
            live_ids = []
        if not isinstance(stream_ids, list):
            stream_ids = []
        live = str(live_id or "").strip()
        if live and live not in {str(item) for item in live_ids}:
            live_ids.append(live)
        if stream_id is not None and int(stream_id) not in {
                int(item) for item in stream_ids}:
            stream_ids.append(int(stream_id))
        self.conn.execute(
            """UPDATE business_sessions SET source_live_ids=?,source_stream_ids=?,
               updated_at=datetime('now','localtime') WHERE business_session_key=?""",
            (_intelligence_json(live_ids), _intelligence_json(stream_ids), key),
        )

    def _persist_runtime_issue_locked(
            self, issue_key: str, issue_code: str, business_session_key: str, *,
            now: float, evidence: dict, repair_action: str = "") -> None:
        encoded = _intelligence_json(evidence)
        self.conn.execute(
            """INSERT INTO runtime_issues(
                   issue_key,issue_code,business_session_key,status,evidence_json,
                   repair_action,first_seen_at,last_seen_at)
               VALUES(?,?,?,'open',?,?,?,?)
               ON CONFLICT(issue_key) DO UPDATE SET
                   status='open',evidence_json=excluded.evidence_json,
                   repair_action=excluded.repair_action,
                   occurrences=CASE WHEN runtime_issues.status='resolved'
                       THEN 1 ELSE runtime_issues.occurrences+1 END,
                   first_seen_at=CASE WHEN runtime_issues.status='resolved'
                       THEN excluded.first_seen_at ELSE runtime_issues.first_seen_at END,
                   last_notified_at=CASE WHEN runtime_issues.status='resolved'
                       THEN 0 ELSE runtime_issues.last_notified_at END,
                   last_seen_at=excluded.last_seen_at,resolved_at=0,
                   updated_at=datetime('now','localtime')""",
            (str(issue_key), str(issue_code), str(business_session_key), encoded,
             str(repair_action), float(now), float(now)),
        )

    def observe_business_live(
            self, business_session_key: str, *, live_id: str = "",
            stream_id: int | None = None, now: float | None = None) -> str:
        """Apply one verified live event to the whole business-session aggregate."""
        key = str(business_session_key or "").strip()
        if not key:
            raise ValueError("business_session_key is required")
        observed_at = float(time.time() if now is None else now)
        with self._write_lock:
            sent = self.conn.execute(
                """SELECT live_id FROM platform_review_jobs
                   WHERE business_session_key=? AND status IN ('sent','partial_sent')
                   LIMIT 1""", (key,),
            ).fetchone()
            target = "needs_attention" if sent is not None else "collecting"
            self.conn.execute(
                """INSERT INTO business_sessions(
                       business_session_key,status,observation_deadline,ended_confirmations,
                       last_live_at,observation_started_at,observation_completed_at,ended_at)
                   VALUES(?,?,0,0,?,0,0,0)
                   ON CONFLICT(business_session_key) DO UPDATE SET
                       status=excluded.status,observation_deadline=0,
                       ended_confirmations=0,last_live_at=excluded.last_live_at,
                       observation_started_at=0,observation_completed_at=0,ended_at=0,
                       updated_at=datetime('now','localtime')""",
                (key, target, observed_at),
            )
            self._add_business_session_source_locked(
                key, live_id=str(live_id or ""), stream_id=stream_id)
            if sent is None:
                self.conn.execute(
                    """UPDATE platform_review_jobs
                       SET status='parked',next_attempt_at=0,lease_until=0,
                           error='business session resumed',
                           updated_at=datetime('now','localtime')
                       WHERE business_session_key=?
                         AND status IN (
                             'waiting','waiting_evidence','running','failed')""",
                    (key,),
                )
            else:
                issue_key = f"REPLAY_AFTER_DAILY_SENT:{key}"
                self._persist_runtime_issue_locked(
                    issue_key, "REPLAY_AFTER_DAILY_SENT", key,
                    now=observed_at,
                    evidence={
                        "business_session_key": key,
                        "sent_live_id": str(sent["live_id"] or ""),
                        "observed_live_id": str(live_id or ""),
                        "stream_id": int(stream_id) if stream_id is not None else None,
                    },
                )
            self.conn.commit()
        return target

    def get_business_session(self, business_session_key: str):
        return self.conn.execute(
            "SELECT * FROM business_sessions WHERE business_session_key=?",
            (str(business_session_key),),
        ).fetchone()

    def begin_business_observation(self, business_session_key: str, *,
                                   now: float | None = None,
                                   observe_seconds: int = 600) -> float:
        """在明确下播后开始持久化观察，防止短暂重播/切换被误判为真结束。"""
        now = float(time.time() if now is None else now)
        key = str(business_session_key or "").strip()
        deadline = now + max(1, int(observe_seconds))
        with self._write_lock:
            row = self.conn.execute(
                "SELECT observation_deadline,status FROM business_sessions "
                "WHERE business_session_key=?", (key,)).fetchone()
            if row is None:
                self.conn.execute(
                    """INSERT INTO business_sessions(
                           business_session_key,status,observation_deadline,
                           observation_started_at,observation_completed_at)
                       VALUES(?,'observing',?,?,0)""", (key, deadline, now))
            elif str(row["status"] or "") == "ended":
                return float(row["observation_deadline"] or 0)
            else:
                old_deadline = float(row["observation_deadline"] or 0)
                # 同一组连续下播探测只延续原观察窗口，不能每分钟重新计时。
                deadline = old_deadline or deadline
                self.conn.execute(
                    """UPDATE business_sessions SET status='observing',
                       observation_deadline=?,observation_started_at=CASE
                           WHEN observation_started_at>0 THEN observation_started_at ELSE ? END,
                       observation_completed_at=0,ended_at=0,
                       updated_at=datetime('now','localtime')
                       WHERE business_session_key=?""",
                    (deadline, now, key),
                )
            self.conn.commit()
        self.resolve_runtime_issue(
            f"STALE_COLLECTING_WITHOUT_RECORDING:{key}", now=now)
        return deadline

    def list_unconfirmed_end_probe_targets(self, *, limit: int = 1) -> list:
        """Business days whose recordings stopped but Taobao end is unconfirmed."""
        rows = self.conn.execute(
            """SELECT b.business_session_key, b.status, s.live_id
               FROM business_sessions b
               JOIN streams s ON s.business_session_key=b.business_session_key
                AND s.id=(
                    SELECT s2.id FROM streams s2
                    WHERE s2.business_session_key=b.business_session_key
                      AND s2.live_id!=''
                    ORDER BY s2.started_at DESC,s2.id DESC LIMIT 1)
               WHERE (
                    b.status='collecting'
                    OR (
                        b.status='needs_attention'
                        AND EXISTS (
                            SELECT 1 FROM runtime_issues i
                            WHERE i.issue_key=? || b.business_session_key
                              AND i.status='open')
                        AND NOT EXISTS (
                            SELECT 1 FROM runtime_issues i2
                            WHERE i2.business_session_key=b.business_session_key
                              AND i2.status='open'
                              AND i2.issue_code NOT IN (
                                  'STALE_COLLECTING_WITHOUT_RECORDING'))
                    )
               )
               AND NOT EXISTS (
                    SELECT 1 FROM streams r
                    WHERE r.business_session_key=b.business_session_key
                      AND r.status IN ('recording','recovering'))
               AND NOT EXISTS (
                    SELECT 1 FROM platform_review_jobs j
                    WHERE j.business_session_key=b.business_session_key
                      AND j.status IN (
                          'sent','partial_sent','waiting',
                          'waiting_evidence','running'))
               ORDER BY b.business_session_key LIMIT ?""",
            ("STALE_COLLECTING_WITHOUT_RECORDING:", max(1, int(limit))),
        ).fetchall()
        return [dict(row) for row in rows]

    def cancel_business_observation(self, business_session_key: str) -> None:
        """真实在播信号出现时恢复业务日，包括已误终结的同日新场。"""
        key = str(business_session_key or "").strip()
        if not key:
            return
        with self._write_lock:
            self.conn.execute(
                """UPDATE business_sessions SET status='collecting',observation_deadline=0,
                   ended_confirmations=0,updated_at=datetime('now','localtime')
                   WHERE business_session_key=? AND status IN ('observing','ended')""", (key,))
            self.conn.commit()

    def business_observation_due(self, business_session_key: str, *,
                                 now: float | None = None) -> bool:
        now = float(time.time() if now is None else now)
        row = self.get_business_session(business_session_key)
        return bool(row and str(row["status"] or "") == "observing"
                    and float(row["observation_deadline"] or 0) <= now)

    def mark_business_session_ended(self, business_session_key: str) -> None:
        key = str(business_session_key or "").strip()
        if not key:
            return
        with self._write_lock:
            self.conn.execute(
                """UPDATE business_sessions SET status='ended',observation_deadline=0,
                   observation_completed_at=CASE WHEN observation_completed_at>0
                       THEN observation_completed_at ELSE ? END,
                   ended_at=CASE WHEN ended_at>0 THEN ended_at ELSE ? END,
                   updated_at=datetime('now','localtime') WHERE business_session_key=?""",
                (time.time(), time.time(), key),
            )
            self.conn.commit()

    def mark_stale_observation_needs_attention(
            self, business_session_key: str, *, now: float,
            issue_code: str, evidence: dict) -> bool:
        """Stop an unsafe stale-observation recovery without closing the day."""
        key = str(business_session_key or "").strip()
        code = str(issue_code or "").strip()
        if not key or not code:
            raise ValueError("business_session_key and issue_code are required")
        checked_at = float(now)
        with self._write_lock, self.conn:
            return self._mark_due_observation_attention_locked(
                key, now=checked_at, issue_code=code, evidence=evidence,
            )

    def _mark_due_observation_attention_locked(
            self, business_session_key: str, *, now: float,
            issue_code: str, evidence: dict) -> bool:
        """Guard and persist one unsafe due observation; caller owns transaction."""
        key = str(business_session_key)
        checked_at = float(now)
        cursor = self.conn.execute(
            """UPDATE business_sessions SET status='needs_attention',
                   updated_at=datetime('now','localtime')
               WHERE business_session_key=? AND status='observing'
                 AND observation_deadline>0 AND observation_deadline<=?
                 AND NOT EXISTS (
                     SELECT 1 FROM streams s
                     WHERE s.business_session_key=business_sessions.business_session_key
                       AND s.status IN ('recording','recovering'))""",
            (key, checked_at),
        )
        if not cursor.rowcount:
            return False
        self.conn.execute(
            """UPDATE platform_review_jobs SET status='parked',next_attempt_at=0,
                   lease_until=0,error='business session needs attention',
                   updated_at=datetime('now','localtime')
               WHERE business_session_key=?
                 AND status IN ('waiting','waiting_evidence','running','failed')""",
            (key,),
        )
        code = str(issue_code)
        self._persist_runtime_issue_locked(
            f"{code}:{key}", code, key, now=checked_at,
            evidence=dict(evidence or {}), repair_action="needs_attention",
        )
        return True

    def finalize_business_session(
            self, business_session_key: str, *, live_id: str,
            now: float | None = None,
            settlement_wait_seconds: int = 1800) -> bool:
        """End one due observation and re-arm exactly one daily job atomically."""
        key = str(business_session_key or "").strip()
        target_live_id = str(live_id or "").strip()
        if not key:
            raise ValueError("business_session_key is required")
        ended_at = float(time.time() if now is None else now)
        with self._write_lock, self.conn:
            session = self.conn.execute(
                "SELECT status,observation_deadline FROM business_sessions "
                "WHERE business_session_key=?", (key,),
            ).fetchone()
            active = self.conn.execute(
                """SELECT 1 FROM streams WHERE business_session_key=?
                   AND status IN ('recording','recovering') LIMIT 1""", (key,),
            ).fetchone()
            if (session is None or str(session["status"] or "") != "observing"
                    or float(session["observation_deadline"] or 0) <= 0
                    or float(session["observation_deadline"] or 0) > ended_at
                    or active is not None):
                return False
            job = self.conn.execute(
                "SELECT live_id,status FROM platform_review_jobs "
                "WHERE business_session_key=? LIMIT 1", (key,),
            ).fetchone()
            persisted_live_id = str(job["live_id"] or "").strip() if job else ""
            if ((job is None and not target_live_id)
                    or (job is not None and not persisted_live_id)):
                self._mark_due_observation_attention_locked(
                    key, now=ended_at,
                    issue_code="DAILY_JOB_IDENTITY_MISSING",
                    evidence={
                        "business_session_key": key,
                        "requested_live_id_present": bool(target_live_id),
                        "persisted_live_id_present": bool(persisted_live_id),
                        "existing_job": job is not None,
                    },
                )
                return False
            identity_conflict = (
                self.conn.execute(
                    """SELECT business_session_key,status
                       FROM platform_review_jobs WHERE live_id=? LIMIT 1""",
                    (target_live_id,),
                ).fetchone()
                if job is None and target_live_id else None
            )
            if (identity_conflict is not None
                    and str(identity_conflict["business_session_key"] or "") != key):
                self._mark_due_observation_attention_locked(
                    key, now=ended_at,
                    issue_code="DAILY_JOB_IDENTITY_CONFLICT",
                    evidence={
                        "business_session_key": key,
                        "live_id": target_live_id,
                        "conflicting_business_session_key": str(
                            identity_conflict["business_session_key"] or ""),
                        "conflicting_status": str(
                            identity_conflict["status"] or ""),
                    },
                )
                return False
            if job is not None and str(job["status"] or "") in {
                    "sent", "partial_sent", "delivery_unknown"}:
                self._mark_due_observation_attention_locked(
                    key, now=ended_at,
                    issue_code="REPLAY_AFTER_DAILY_SENT",
                    evidence={
                        "business_session_key": key,
                        "daily_status": str(job["status"] or ""),
                        "live_id": str(live_id or ""),
                    },
                )
                return False
            closed = self.conn.execute(
                """UPDATE business_sessions SET status='ended',observation_deadline=0,
                   observation_completed_at=?,ended_at=?,
                   updated_at=datetime('now','localtime')
                   WHERE business_session_key=? AND status='observing'
                     AND observation_deadline>0 AND observation_deadline<=?
                     AND NOT EXISTS (
                         SELECT 1 FROM streams s
                         WHERE s.business_session_key=business_sessions.business_session_key
                           AND s.status IN ('recording','recovering'))""",
                (ended_at, ended_at, key, ended_at),
            )
            if not closed.rowcount:
                return False
            deadline_at = ended_at + max(1, int(settlement_wait_seconds))
            if job is None:
                try:
                    self.conn.execute(
                        """INSERT INTO platform_review_jobs(
                               live_id,business_session_key,status,triggered_at,
                               deadline_at,next_attempt_at)
                           VALUES(?,?,'waiting',?,?,?)""",
                        (target_live_id, key, ended_at, deadline_at, ended_at),
                    )
                except sqlite3.IntegrityError as exc:
                    message = str(exc)
                    identity_collision = (
                        "duplicate business daily identity" in message
                        or "platform_review_jobs.business_session_key" in message
                        or "platform_review_jobs.live_id" in message
                    )
                    if not identity_collision:
                        raise
                    # 回滚上面的 ended 转移；下一轮会读到竞态
                    # 胜出的任务，并按同业务日或身份冲突正常收敛。
                    self.conn.rollback()
                    return False
            else:
                self.conn.execute(
                    """UPDATE platform_review_jobs SET status='waiting',triggered_at=?,
                       deadline_at=?,next_attempt_at=?,lease_until=0,attempts=0,error='',
                       settlement_reminded_at=0,
                       updated_at=datetime('now','localtime') WHERE live_id=?""",
                    (ended_at, deadline_at, ended_at, str(job["live_id"])),
                )
        return True

    def should_notify_runtime_issue(
            self, issue_key: str, *, now: float, cooldown_seconds: int,
            payload: dict) -> bool:
        """Persist an issue observation and atomically claim its alert cooldown."""
        key = str(issue_key or "").strip()
        if not key:
            raise ValueError("issue_key is required")
        checked_at = float(now)
        cooldown = max(0, int(cooldown_seconds))
        issue_code, _, business_key = key.partition(":")
        with self._write_lock:
            row = self.conn.execute(
                "SELECT last_notified_at FROM runtime_issues WHERE issue_key=?",
                (key,),
            ).fetchone()
            self._persist_runtime_issue_locked(
                key, issue_code or "RUNTIME_ISSUE", business_key,
                now=checked_at, evidence=dict(payload or {}),
            )
            last_notified = float(row["last_notified_at"] or 0) if row else 0.0
            notify = row is None or checked_at - last_notified >= cooldown
            if notify:
                self.conn.execute(
                    "UPDATE runtime_issues SET last_notified_at=? WHERE issue_key=?",
                    (checked_at, key),
                )
            self.conn.commit()
        return notify

    def runtime_issue_notification_due(
            self, issue_key: str, *, now: float, cooldown_seconds: int,
            payload: dict) -> tuple[bool, float]:
        """Persist an observation without claiming delivery success.

        The caller must acknowledge an accepted delivery separately.  This
        prevents a transport error from consuming the persistent cooldown.
        """
        key = str(issue_key or "").strip()
        if not key:
            raise ValueError("issue_key is required")
        checked_at = float(now)
        cooldown = max(0, int(cooldown_seconds))
        issue_code, _, business_key = key.partition(":")
        with self._write_lock:
            row = self.conn.execute(
                "SELECT last_notified_at FROM runtime_issues WHERE issue_key=?",
                (key,),
            ).fetchone()
            self._persist_runtime_issue_locked(
                key, issue_code or "RUNTIME_ISSUE", business_key,
                now=checked_at, evidence=dict(payload or {}),
            )
            last_notified = float(row["last_notified_at"] or 0) if row else 0.0
            due = last_notified <= 0 or checked_at - last_notified >= cooldown
            retry_at = checked_at if due else last_notified + cooldown
            self.conn.commit()
        return due, retry_at

    def runtime_incident_notification_due(
            self, issue_key: str, *, now: float,
            payload: dict) -> tuple[bool, float]:
        """Observe one incident; notify once until it is explicitly resolved."""
        key = str(issue_key or "").strip()
        if not key:
            raise ValueError("issue_key is required")
        checked_at = float(now)
        issue_code, _, business_key = key.partition(":")
        with self._write_lock:
            previous = self.conn.execute(
                """SELECT status,last_notified_at,first_seen_at
                   FROM runtime_issues WHERE issue_key=?""",
                (key,),
            ).fetchone()
            self._persist_runtime_issue_locked(
                key, issue_code or "RUNTIME_INCIDENT", business_key,
                now=checked_at, evidence=dict(payload or {}),
            )
            current = self.conn.execute(
                "SELECT first_seen_at FROM runtime_issues WHERE issue_key=?",
                (key,),
            ).fetchone()
            due = (
                previous is None
                or str(previous["status"]) == "resolved"
                or float(previous["last_notified_at"] or 0) <= 0
            )
            started_at = float(current["first_seen_at"] or checked_at)
            self.conn.commit()
        return due, started_at

    def resolve_runtime_issue(
            self, issue_key: str, *, now: float | None = None) -> bool:
        """Close an open incident so a later recurrence gets a new identity."""
        key = str(issue_key or "").strip()
        if not key:
            return False
        resolved_at = float(time.time() if now is None else now)
        with self._write_lock:
            changed = self.conn.execute(
                """UPDATE runtime_issues
                   SET status='resolved',resolved_at=?,last_seen_at=MAX(
                       last_seen_at,?),updated_at=datetime('now','localtime')
                   WHERE issue_key=? AND status='open'""",
                (resolved_at, resolved_at, key),
            ).rowcount
            self.conn.commit()
        return changed == 1

    def resolve_runtime_issue_family(
            self, family_key: str, *, now: float | None = None,
            except_key: str = "") -> int:
        """Close one exact incident key and all safe ``key:*`` variants."""
        base = str(family_key or "").strip().rstrip(":")
        if not base:
            return 0
        resolved_at = float(time.time() if now is None else now)
        child_prefix = base + ":"
        keep = str(except_key or "").strip()
        with self._write_lock:
            changed = self.conn.execute(
                """UPDATE runtime_issues
                   SET status='resolved',resolved_at=?,last_seen_at=MAX(
                       last_seen_at,?),updated_at=datetime('now','localtime')
                   WHERE status='open'
                     AND (issue_key=? OR substr(issue_key,1,length(?))=?)
                     AND (?='' OR issue_key<>?)""",
                (resolved_at, resolved_at, base, child_prefix, child_prefix,
                 keep, keep),
            ).rowcount
            self.conn.commit()
        return int(changed)

    def mark_runtime_issue_notified(
            self, issue_key: str, *, notified_at: float) -> bool:
        key = str(issue_key or "").strip()
        if not key:
            raise ValueError("issue_key is required")
        delivered_at = float(notified_at)
        with self._write_lock:
            changed = self.conn.execute(
                "UPDATE runtime_issues SET last_notified_at="
                "MAX(last_notified_at,?),updated_at=datetime('now','localtime') "
                "WHERE issue_key=?",
                (delivered_at, key),
            ).rowcount
            self.conn.commit()
        return changed == 1

    def upsert_shift_window(self, shift_window_key: str, *,
                            business_session_key: str, window_start_ms: int,
                            window_end_ms: int, scheduled_anchors: list[str] | None = None,
                            actual_slices: list[dict] | None = None,
                            status: str = "scheduled") -> None:
        with self._write_lock:
            self.conn.execute(
                """INSERT INTO shift_windows(
                       shift_window_key,business_session_key,window_start_ms,window_end_ms,
                       scheduled_anchors_json,actual_slices_json,status)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(shift_window_key) DO UPDATE SET
                       scheduled_anchors_json=excluded.scheduled_anchors_json,
                       actual_slices_json=excluded.actual_slices_json,
                       status=excluded.status,
                       updated_at=datetime('now','localtime')""",
                (str(shift_window_key), str(business_session_key), int(window_start_ms),
                 int(window_end_ms), json.dumps(scheduled_anchors or [], ensure_ascii=False),
                 json.dumps(actual_slices or [], ensure_ascii=False), str(status)),
            )
            self.conn.commit()

    # ---------- 智能闭环任务（跨重启真相源） ----------
    @staticmethod
    def _intelligence_job_key(task_type: str, stream_id: int | None,
                              live_id: str, anchor_id: int | None,
                              window_start_ms: int, window_end_ms: int,
                              prompt_version: str = "") -> str:
        version = hashlib.sha256(prompt_version.encode("utf-8")).hexdigest()[:16]
        if task_type == "hourly":
            return (
                f"hourly:{int(stream_id or 0)}:{live_id}:{int(anchor_id or 0)}:"
                f"{int(window_start_ms)}:{int(window_end_ms)}:prompt:{version}"
            )
        if task_type == "platform":
            return f"platform:{live_id}:prompt:{version}"
        if stream_id is not None:
            return f"{task_type}:{int(stream_id)}:{int(window_start_ms)}"
        if live_id:
            return f"{task_type}:{live_id}:{int(window_start_ms)}"
        return f"{task_type}:{int(window_start_ms)}"

    def ensure_intelligence_job(
            self, *, task_type: str, stream_id: int | None = None, live_id: str = "",
            anchor_id: int | None = None, window_start_ms: int = 0, window_end_ms: int = 0,
            input_hash: str, deadline_at: float = 0, next_attempt_at: float | None = None,
            model: str = "", prompt_version: str = "") -> str:
        """Create an immutable task identity including its prompt version."""
        key = self._intelligence_job_key(
            task_type, stream_id, str(live_id), anchor_id, window_start_ms, window_end_ms,
            str(prompt_version),
        )
        due_at = 0.0 if next_attempt_at is None else float(next_attempt_at)
        with self._write_lock:
            self.conn.execute(
                """INSERT OR IGNORE INTO intelligence_jobs(
                       job_key,task_type,stream_id,live_id,anchor_id,window_start_ms,
                       window_end_ms,input_hash,deadline_at,next_attempt_at,model,prompt_version)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (key, str(task_type), stream_id, str(live_id), anchor_id,
                 int(window_start_ms), int(window_end_ms), str(input_hash),
                 float(deadline_at), due_at, str(model), str(prompt_version)),
            )
            self.conn.commit()
        return key

    def get_intelligence_job(self, job_key: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM intelligence_jobs WHERE job_key=?", (str(job_key),),
        ).fetchone()
        return None if row is None else dict(row)

    def recover_abandoned_analysis_leases(
            self, *, now: float | None = None) -> dict[str, int]:
        """Release work owned by the previous watcher after singleton takeover.

        The caller must already own the process singleton.  Frozen transcripts,
        intelligence context, attempts, and delivery ledgers stay intact; only
        local execution leases are released so the new process can resume now.
        """
        recovered_at = float(time.time() if now is None else now)
        updated_at = now_str()
        with self._write_lock:
            consumers = self.conn.execute(
                """UPDATE transcription_jobs
                   SET consumer_status='failed',consumer_next_at=?,lease_until=0,
                       error=CASE WHEN error='' THEN
                           'watcher restart reclaimed unfinished brief'
                           ELSE error END,updated_at=?
                   WHERE consumer_status='running'""",
                (recovered_at, updated_at),
            ).rowcount
            intelligence = self.conn.execute(
                """UPDATE intelligence_jobs SET lease_until=0,updated_at=?
                   WHERE lease_until>0 AND status IN (
                       'queued','analyzing_evidence','validating_evidence',
                       'designing_actions','validating_actions')""",
                (updated_at,),
            ).rowcount
            self.conn.commit()
        return {
            "brief_consumers": int(consumers),
            "intelligence_jobs": int(intelligence),
        }

    def _require_intelligence_job(self, job_key: str) -> None:
        row = self.conn.execute(
            "SELECT 1 FROM intelligence_jobs WHERE job_key=?", (str(job_key),),
        ).fetchone()
        if row is None:
            raise sqlite3.IntegrityError("intelligence job does not exist")

    def claim_intelligence_job(self, job_key: str, *, now: float | None = None,
                               lease_sec: int = 300) -> dict | None:
        """以单条条件 UPDATE 领取到期非终态任务，attempts 作为栅栏。"""
        now = float(time.time() if now is None else now)
        with self._write_lock:
            cursor = self.conn.execute(
                """UPDATE intelligence_jobs
                   SET lease_until=?,attempts=attempts+1,error='',
                       updated_at=datetime('now','localtime')
                   WHERE job_key=? AND status IN (
                       'queued','analyzing_evidence','validating_evidence',
                       'designing_actions','validating_actions')
                     AND lease_until<=? AND next_attempt_at<=?""",
                (now + max(1, int(lease_sec)), str(job_key), now, now),
            )
            self.conn.commit()
            if not cursor.rowcount:
                return None
            return self.get_intelligence_job(job_key)

    def defer_intelligence_job_retry(
            self, job_key: str, *, claim_generation: int, now: float,
            retry_at: float, deadline_at: float, error: str) -> bool:
        """Release one failed attempt without turning the formal analysis terminal.

        The generation CAS is sufficient even when a provider returns after its
        lease: a newer claimant has already incremented ``attempts`` and wins.
        """
        now = float(now)
        with self._write_lock:
            cursor = self.conn.execute(
                """UPDATE intelligence_jobs
                   SET status='queued',lease_until=0,next_attempt_at=?,deadline_at=?,error=?,
                       fallback_reason='',updated_at=datetime('now','localtime')
                   WHERE job_key=? AND attempts=?
                     AND status IN (
                         'queued','analyzing_evidence','validating_evidence',
                         'designing_actions','validating_actions')""",
                (
                    max(now + 1.0, float(retry_at)),
                    max(float(deadline_at), float(retry_at) + 1.0),
                    str(error)[:500], str(job_key), int(claim_generation),
                ),
            )
            if cursor.rowcount:
                self.conn.execute(
                    """UPDATE intelligence_artifacts
                       SET raw_evidence_json='{}',validated_evidence_json='{}',
                           raw_actions_json='{}',validated_result_json='{}',
                           validated_result_hash='',rejected_json='[]',latency_ms=0,
                           usage_json='{}',updated_at=datetime('now','localtime')
                       WHERE job_key=?""",
                    (str(job_key),),
                )
            self.conn.commit()
            return cursor.rowcount > 0

    def advance_intelligence_job(self, job_key: str, status: str, *,
                                 claim_generation: int, now: float,
                                 error: str = "", fallback_reason: str = "",
                                 next_attempt_at: float | None = None) -> bool:
        """只允许持有当前未过期 generation 的执行者推进状态机。"""
        target = str(status)
        now = float(now)
        with self._write_lock:
            row = self.conn.execute(
                "SELECT status FROM intelligence_jobs WHERE job_key=?", (str(job_key),),
            ).fetchone()
            if row is None:
                return False
            current = str(row["status"])
            if current in INTELLIGENCE_TERMINAL_STATUSES:
                raise ValueError(f"terminal intelligence job cannot transition: {current}")
            if target not in INTELLIGENCE_TRANSITIONS.get(current, set()):
                raise ValueError(
                    f"invalid intelligence job transition: {current} -> {target}")
            cursor = self.conn.execute(
                """UPDATE intelligence_jobs
                   SET status=?,lease_until=CASE WHEN ? THEN 0 ELSE lease_until END,
                       error=?,fallback_reason=?,
                       next_attempt_at=COALESCE(?,next_attempt_at),
                       updated_at=datetime('now','localtime')
                   WHERE job_key=? AND status=? AND attempts=? AND lease_until>?""",
                (target, int(target in INTELLIGENCE_TERMINAL_STATUSES), str(error),
                 str(fallback_reason), next_attempt_at, str(job_key), current,
                 int(claim_generation), now),
            )
            self.conn.commit()
            return cursor.rowcount > 0

    def save_intelligence_artifact(
            self, job_key: str, *, context_snapshot: object | None = None,
            raw_evidence: object | None = None,
            validated_evidence: object | None = None, raw_actions: object | None = None,
            validated_result: object | None = None, rejected: object | None = None,
            latency_ms: int | None = None, usage: object | None = None,
            claim_generation: int, now: float) -> bool:
        """只允许当前未过期 generation 写阶段产物，拒绝迟到执行者覆盖。"""
        now = float(now)
        encoded = {
            "context_snapshot_json": (
                None if context_snapshot is None else _intelligence_json(context_snapshot)),
            "raw_evidence_json": None if raw_evidence is None else _intelligence_json(raw_evidence),
            "validated_evidence_json": (
                None if validated_evidence is None else _intelligence_json(validated_evidence)),
            "raw_actions_json": None if raw_actions is None else _intelligence_json(raw_actions),
            "validated_result_json": (
                None if validated_result is None else _intelligence_json(validated_result)),
            "rejected_json": None if rejected is None else _intelligence_json(rejected),
            "usage_json": None if usage is None else _intelligence_json(usage),
        }
        if validated_result is not None:
            from .intelligence.integrity import validated_result_hash
            encoded["validated_result_hash"] = validated_result_hash(validated_result)
        assignments = [f"{name}=?" for name, value in encoded.items() if value is not None]
        values = [value for value in encoded.values() if value is not None]
        if latency_ms is not None:
            assignments.append("latency_ms=?")
            values.append(int(latency_ms))
        with self._write_lock:
            self._require_intelligence_job(job_key)
            inserted = self.conn.execute(
                """INSERT OR IGNORE INTO intelligence_artifacts(job_key)
                   SELECT job_key FROM intelligence_jobs
                   WHERE job_key=? AND attempts=? AND lease_until>?""",
                (str(job_key), int(claim_generation), now),
            )
            if assignments:
                cursor = self.conn.execute(
                    "UPDATE intelligence_artifacts SET " + ",".join(assignments)
                    + """,updated_at=datetime('now','localtime') WHERE job_key=?
                       AND EXISTS (SELECT 1 FROM intelligence_jobs
                                   WHERE job_key=? AND attempts=? AND lease_until>?)""",
                    (*values, str(job_key), str(job_key), int(claim_generation), now),
                )
                saved = cursor.rowcount > 0
            else:
                saved = inserted.rowcount > 0
            self.conn.commit()
            return saved

    def get_intelligence_artifact(self, job_key: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM intelligence_artifacts WHERE job_key=?", (str(job_key),),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        for field, default in (
            ("context_snapshot", {}),
            ("raw_evidence", {}), ("validated_evidence", {}), ("raw_actions", {}),
            ("validated_result", {}), ("rejected", []), ("usage", {}),
        ):
            result[field] = _load_intelligence_json(result.pop(f"{field}_json"), default)
        if result["validated_result"]:
            from .intelligence.integrity import validated_result_integrity_matches
            if not validated_result_integrity_matches(
                    result["validated_result"], result.get("validated_result_hash")):
                raise ValueError("validated intelligence result integrity mismatch")
        return result

    # ---------- 守护进程维护任务（跨重启退避） ----------
    def claim_maintenance(self, job_key: str, *, stale_sec: int = 3600) -> bool:
        """原子领取到期任务；运行中任务只有超过 stale_sec 才能被新进程接管。"""
        now = time.time()
        with self._write_lock:
            row = self.conn.execute(
                "SELECT * FROM maintenance_jobs WHERE job_key=?", (job_key,)).fetchone()
            if row:
                if float(row["next_attempt"] or 0) > now:
                    return False
                if (row["status"] == "running" and
                        now - float(row["last_attempt"] or 0) < stale_sec):
                    return False
                cur = self.conn.execute(
                    """UPDATE maintenance_jobs SET status='running',attempts=attempts+1,
                       last_attempt=?,next_attempt=?,error='' WHERE job_key=?
                       AND next_attempt<=?""",
                    (now, now + stale_sec, job_key, now),
                )
            else:
                cur = self.conn.execute(
                    """INSERT INTO maintenance_jobs(
                           job_key,status,attempts,last_attempt,next_attempt)
                       VALUES(?,'running',1,?,?)""",
                    (job_key, now, now + stale_sec),
                )
            self.conn.commit()
            return cur.rowcount > 0

    def finish_maintenance(self, job_key: str, *, success: bool,
                           success_ttl_sec: int = 86400,
                           retry_sec: int = 3600, error: str = "") -> None:
        now = time.time()
        with self._write_lock:
            if success:
                self.conn.execute(
                    """UPDATE maintenance_jobs SET status='success',attempts=0,
                       last_success=?,next_attempt=?,error='' WHERE job_key=?""",
                    (now, now + max(1, success_ttl_sec), job_key),
                )
            else:
                self.conn.execute(
                    """UPDATE maintenance_jobs SET status='failed',next_attempt=?,error=?
                       WHERE job_key=?""",
                    (now + max(1, retry_sec), str(error)[:500], job_key),
                )
            self.conn.commit()

    def request_hour_window_rescan(self, *, live_id: str, stream_id: int) -> str:
        """Persist that a sealed stream should re-scan closed clock hours."""
        room = str(live_id or "").strip()
        sid = int(stream_id)
        if not room or sid <= 0:
            return ""
        job_key = f"hour-rescan:{room}:{sid}"
        with self._write_lock:
            self.conn.execute(
                """INSERT INTO maintenance_jobs(
                       job_key,status,attempts,last_attempt,last_success,
                       next_attempt,error)
                   VALUES(?, 'pending', 0, 0, 0, 0, '')
                   ON CONFLICT(job_key) DO UPDATE SET
                       status='pending', next_attempt=0, error=''""",
                (job_key,),
            )
            self.conn.commit()
        return job_key

    def claim_hour_window_rescans(
            self, *, now: float | None = None, limit: int = 5) -> list:
        checked_at = float(time.time() if now is None else now)
        claimed: list[dict] = []
        with self._write_lock:
            rows = self.conn.execute(
                """SELECT job_key FROM maintenance_jobs
                   WHERE job_key LIKE 'hour-rescan:%'
                     AND status IN ('pending', 'failed')
                     AND next_attempt<=?
                   ORDER BY job_key LIMIT ?""",
                (checked_at, max(1, int(limit))),
            ).fetchall()
            for row in rows:
                job_key = str(row["job_key"])
                cursor = self.conn.execute(
                    """UPDATE maintenance_jobs SET status='running',
                           attempts=attempts+1,last_attempt=?,next_attempt=?
                       WHERE job_key=? AND status IN ('pending', 'failed')""",
                    (checked_at, checked_at + 60, job_key),
                )
                if not cursor.rowcount:
                    continue
                try:
                    _prefix, live_id, stream_id = job_key.split(":", 2)
                except ValueError:
                    continue
                claimed.append({
                    "job_key": job_key,
                    "live_id": live_id,
                    "stream_id": int(stream_id),
                })
            self.conn.commit()
        return claimed

    def finish_hour_window_rescan(
            self, job_key: str, *, success: bool, error: str = "") -> None:
        self.finish_maintenance(
            job_key, success=success, success_ttl_sec=86400,
            retry_sec=30, error=error)

    # ---------- 排班轮换简报任务（stream 级、跨重启） ----------
    def queue_rotation_brief(self, stream_id: int, triggered_at: float | None = None) -> bool:
        """为轮换前旧主播创建唯一简报任务；重复调用不重置已领取的任务。"""
        now = float(time.time() if triggered_at is None else triggered_at)
        with self._write_lock:
            cursor = self.conn.execute(
                """INSERT OR IGNORE INTO rotation_brief_jobs(
                       stream_id,status,triggered_at,next_attempt_at)
                   VALUES(?,'waiting',?,?)""",
                (int(stream_id), now, now),
            )
            self.conn.commit()
            return cursor.rowcount > 0

    def get_rotation_brief_job(self, stream_id: int):
        return self.conn.execute(
            "SELECT * FROM rotation_brief_jobs WHERE stream_id=?", (int(stream_id),)
        ).fetchone()

    def claim_rotation_brief(self, stream_id: int, now: float | None = None,
                             *, lease_sec: int = 900):
        """按 stream_id 原子领取刚创建的简报任务，供轮换路径立即后台执行。"""
        now = float(time.time() if now is None else now)
        stream_id = int(stream_id)
        with self._write_lock:
            cursor = self.conn.execute(
                """UPDATE rotation_brief_jobs
                   SET status='running',attempts=attempts+1,lease_until=?,
                       error='',updated_at=datetime('now','localtime')
                   WHERE stream_id=? AND ((status='waiting' AND next_attempt_at<=?)
                       OR (status='running' AND lease_until<=?))""",
                (now + max(60, int(lease_sec)), stream_id, now, now),
            )
            self.conn.commit()
            if not cursor.rowcount:
                return None
            return self.get_rotation_brief_job(stream_id)

    def claim_due_rotation_briefs(self, now: float | None = None, *, limit: int = 1,
                                  lease_sec: int = 900) -> list[sqlite3.Row]:
        """原子领取待投递简报；崩溃遗留的 running 在租约到期后可恢复。"""
        now = float(time.time() if now is None else now)
        claimed: list[int] = []
        with self._write_lock:
            rows = self.conn.execute(
                """SELECT stream_id FROM rotation_brief_jobs
                   WHERE ((status='waiting' AND next_attempt_at<=?)
                          OR (status='running' AND lease_until<=?))
                   ORDER BY next_attempt_at,triggered_at LIMIT ?""",
                (now, now, max(0, int(limit))),
            ).fetchall()
            for row in rows:
                stream_id = int(row["stream_id"])
                cursor = self.conn.execute(
                    """UPDATE rotation_brief_jobs
                       SET status='running',attempts=attempts+1,lease_until=?,
                           error='',updated_at=datetime('now','localtime')
                       WHERE stream_id=? AND ((status='waiting' AND next_attempt_at<=?)
                           OR (status='running' AND lease_until<=?))""",
                    (now + max(60, int(lease_sec)), stream_id, now, now),
                )
                if cursor.rowcount:
                    claimed.append(stream_id)
            self.conn.commit()
            if not claimed:
                return []
            placeholders = ",".join("?" for _ in claimed)
            return self.conn.execute(
                f"SELECT * FROM rotation_brief_jobs WHERE stream_id IN ({placeholders}) "
                "ORDER BY next_attempt_at,triggered_at", tuple(claimed),
            ).fetchall()

    def renew_rotation_brief_lease(self, stream_id: int, now: float | None = None,
                                   *, lease_sec: int = 900) -> bool:
        """为本进程仍在执行的轮换简报续租，不增加尝试次数。"""
        now = float(time.time() if now is None else now)
        with self._write_lock:
            cursor = self.conn.execute(
                """UPDATE rotation_brief_jobs
                   SET lease_until=MAX(lease_until,?),
                       updated_at=datetime('now','localtime')
                   WHERE stream_id=? AND status='running'""",
                (now + max(60, int(lease_sec)), int(stream_id)),
            )
            self.conn.commit()
            return cursor.rowcount > 0

    def update_rotation_brief_job(self, stream_id: int, status: str, *,
                                  next_attempt_at: float | None = None,
                                  error: str = "") -> None:
        sent_at = time.time() if status == "sent" else 0
        with self._write_lock:
            self.conn.execute(
                """UPDATE rotation_brief_jobs
                   SET status=?,next_attempt_at=COALESCE(?,next_attempt_at),lease_until=0,
                       error=?,sent_at=CASE WHEN ?>0 THEN ? ELSE sent_at END,
                       updated_at=datetime('now','localtime')
                   WHERE stream_id=?""",
                (status, next_attempt_at, str(error)[:500], sent_at, sent_at, int(stream_id)),
            )
            self.conn.commit()

    def release_running_rotation_briefs(self, now: float | None = None) -> int:
        """受控退出时交还简报租约，重启后的 watcher 可立即续跑。"""
        now = float(time.time() if now is None else now)
        with self._write_lock:
            cursor = self.conn.execute(
                """UPDATE rotation_brief_jobs
                   SET status='waiting',next_attempt_at=?,lease_until=0,
                       error='watcher 重启前交还任务',updated_at=datetime('now','localtime')
                   WHERE status='running'""",
                (now,),
            )
            self.conn.commit()
            return cursor.rowcount

    # ---------- 淘宝浏览器授权恢复（单行、跨重启） ----------
    def get_taobao_session_state(self):
        return self.conn.execute(
            "SELECT * FROM taobao_session_state WHERE singleton_id=1").fetchone()

    def note_taobao_auth_failure(self, error_class: str,
                                 *, now: float | None = None):
        now = float(time.time() if now is None else now)
        with self._write_lock:
            row = self.get_taobao_session_state()
            if str(row["status"]) == "healthy":
                self.conn.execute(
                    """UPDATE taobao_session_state
                       SET status='auto_recovering',first_failed_at=?,last_error_class=?,
                           last_reminder_at=0,next_check_at=?,generation=generation+1,
                           recovery_notice_status='pending',
                           recovery_owner='',recovery_lease_until=0,
                           updated_at=datetime('now','localtime') WHERE singleton_id=1""",
                    (now, str(error_class), now),
                )
            else:
                self.conn.execute(
                    """UPDATE taobao_session_state SET last_error_class=?,
                           updated_at=datetime('now','localtime') WHERE singleton_id=1""",
                    (str(error_class),),
                )
            self.conn.commit()
            return self.get_taobao_session_state()

    def claim_taobao_auth_reminder(self, *, now: float | None = None,
                                   interval_sec: int = 10800) -> bool:
        now = float(time.time() if now is None else now)
        with self._write_lock:
            cursor = self.conn.execute(
                """UPDATE taobao_session_state SET last_reminder_at=?,
                       updated_at=datetime('now','localtime')
                   WHERE singleton_id=1 AND status!='healthy'
                     AND (last_reminder_at=0 OR last_reminder_at<=?)""",
                (now, now - max(1, int(interval_sec))),
            )
            self.conn.commit()
            return cursor.rowcount > 0

    def release_taobao_auth_reminder(self, *, claimed_at: float) -> bool:
        """投递明确失败时交还提醒槽；不覆盖其他线程已发送的新时间。"""
        with self._write_lock:
            cursor = self.conn.execute(
                """UPDATE taobao_session_state SET last_reminder_at=0,
                       updated_at=datetime('now','localtime')
                   WHERE singleton_id=1 AND status!='healthy' AND last_reminder_at=?""",
                (float(claimed_at),),
            )
            self.conn.commit()
            return cursor.rowcount > 0

    def set_taobao_session_recovery_state(self, status: str, *, next_check_at: float,
                                          error_class: str = "",
                                          owner: str | None = None) -> bool:
        with self._write_lock:
            owner_sql = " AND recovery_owner=?" if owner is not None else ""
            args: list = [str(status), float(next_check_at),
                          str(error_class), str(error_class)]
            if owner is not None:
                args.append(str(owner))
            cursor = self.conn.execute(
                """UPDATE taobao_session_state SET status=?,next_check_at=?,
                       last_error_class=CASE WHEN ?!='' THEN ? ELSE last_error_class END,
                       updated_at=datetime('now','localtime') WHERE singleton_id=1"""
                + owner_sql,
                tuple(args),
            )
            self.conn.commit()
            return cursor.rowcount > 0

    def claim_taobao_session_recovery(
            self, owner: str, *, now: float | None = None,
            lease_sec: int = 120, force: bool = False) -> bool:
        """跨进程恢复租约；手工候选可 force 接管旧自动任务。"""
        now = float(time.time() if now is None else now)
        owner = str(owner)
        with self._write_lock:
            if force:
                cursor = self.conn.execute(
                    """UPDATE taobao_session_state SET recovery_owner=?,
                           recovery_lease_until=?,updated_at=datetime('now','localtime')
                       WHERE singleton_id=1 AND status!='healthy'""",
                    (owner, now + max(30, int(lease_sec))),
                )
            else:
                cursor = self.conn.execute(
                    """UPDATE taobao_session_state SET recovery_owner=?,
                           recovery_lease_until=?,updated_at=datetime('now','localtime')
                       WHERE singleton_id=1 AND status!='healthy'
                         AND (recovery_owner='' OR recovery_owner=?
                              OR recovery_lease_until<=?)""",
                    (owner, now + max(30, int(lease_sec)), owner, now),
                )
            self.conn.commit()
            return cursor.rowcount > 0

    def is_taobao_session_recovery_owner(self, owner: str) -> bool:
        row = self.get_taobao_session_state()
        return (str(row["status"]) != "healthy"
                and str(row["recovery_owner"] or "") == str(owner))

    def mark_taobao_session_healthy(self, cookie_hash: str,
                                    *, now: float | None = None,
                                    notice_payload: dict | None = None,
                                    owner: str | None = None) -> bool:
        now = float(time.time() if now is None else now)
        with self._write_lock:
            owner_sql = " AND recovery_owner=?" if owner is not None else ""
            args: list = [str(cookie_hash), now,
                          json.dumps(notice_payload or {}, ensure_ascii=False)]
            if owner is not None:
                args.append(str(owner))
            cursor = self.conn.execute(
                """UPDATE taobao_session_state SET status='healthy',next_check_at=0,
                       last_cookie_hash=?,last_validated_at=?,last_error_class='',
                       recovery_notice_status='pending',recovery_notice_payload=?,
                       recovery_owner='',recovery_lease_until=0,
                       updated_at=datetime('now','localtime') WHERE singleton_id=1"""
                + owner_sql,
                tuple(args),
            )
            # 与 healthy 同一事务交还 resolve 之后才被旧进程 park
            # 的竞态任务；事务提交后再迟到的由 healthy tick 自愈。
            if cursor.rowcount:
                self.conn.execute(
                    """UPDATE transcription_jobs SET consumer_status='pending',
                           consumer_next_at=?,lease_until=0,
                           error='resumed after Taobao session activation',
                           updated_at=datetime('now','localtime')
                       WHERE purpose IN ('hourly','daily_tail')
                         AND consumer_status='waiting_auth'""",
                    (now,),
                )
                self._wake_hourly_fact_captures_after_auth_locked(now=now)
            self.conn.commit()
            return cursor.rowcount > 0

    def mark_taobao_recovery_notice_sent(self) -> None:
        with self._write_lock:
            self.conn.execute(
                """UPDATE taobao_session_state SET recovery_notice_status='sent',
                       updated_at=datetime('now','localtime') WHERE singleton_id=1""")
            self.conn.commit()

    def park_brief_consumer_for_auth(self, job_key: str,
                                     *, now: float | None = None) -> None:
        now = float(time.time() if now is None else now)
        with self._write_lock:
            self.conn.execute(
                """UPDATE transcription_jobs
                   SET consumer_status='waiting_auth',consumer_next_at=?,lease_until=0,
                       error='waiting for Taobao authentication',
                       updated_at=datetime('now','localtime')
                   WHERE job_key=? AND purpose IN ('hourly','daily_tail')""",
                (now, str(job_key)),
            )
            self.conn.commit()

    def park_brief_consumer_for_evidence(self, job_key: str) -> None:
        """Park a formal brief until its frozen hourly facts are actually replaced."""
        with self._write_lock:
            self.conn.execute(
                """UPDATE transcription_jobs
                   SET consumer_status='waiting_evidence',consumer_next_at=0,
                       lease_until=0,error='等待完整小时经营事实；真实事实写入后自动恢复',
                       updated_at=datetime('now','localtime')
                   WHERE job_key=? AND purpose='hourly'""",
                (str(job_key),),
            )
            self.conn.commit()

    def claim_fact_rescue_jobs(self, now: float | None = None, *, limit: int = 1,
                               lease_sec: int = 600) -> list[sqlite3.Row]:
        """Claim waiting_evidence formal hours that deserve one fact re-capture.

        A soft lease on ``fact_rescue_next_at`` stops the same hour being
        re-fetched every loop and keeps 千牛 requests low-frequency.
        """
        now = float(time.time() if now is None else now)
        with self._write_lock:
            rows = self.conn.execute(
                """SELECT job_key FROM transcription_jobs
                   WHERE purpose='hourly' AND consumer_status='waiting_evidence'
                     AND fact_rescue_next_at<=?
                   ORDER BY window_start_ms LIMIT ?""",
                (now, max(0, int(limit))),
            ).fetchall()
            keys: list[str] = []
            for row in rows:
                key = str(row["job_key"])
                cursor = self.conn.execute(
                    """UPDATE transcription_jobs SET fact_rescue_next_at=?
                       WHERE job_key=? AND fact_rescue_next_at<=?""",
                    (now + max(60, int(lease_sec)), key, now),
                )
                if cursor.rowcount:
                    keys.append(key)
            self.conn.commit()
        return [self.get_transcription_job(key) for key in keys]

    def finish_fact_rescue(self, job_key: str, *, ready: bool,
                           now: float | None = None,
                           give_up_reason: str = "") -> None:
        """Record one fact re-capture attempt and schedule the next one.

        Successful captures are woken by ``save_hourly_artifact``; this only
        manages the retry cadence for hours whose facts stay incomplete.
        ``give_up_reason`` permanently stops re-capture (bounded 千牛 usage).
        """
        now = float(time.time() if now is None else now)
        backoff = (600, 1800, 7200, 21600)
        with self._write_lock:
            if ready:
                self.conn.execute(
                    """UPDATE transcription_jobs
                       SET fact_rescue_attempts=fact_rescue_attempts+1,
                           fact_rescue_next_at=0,
                           updated_at=datetime('now','localtime')
                       WHERE job_key=?""",
                    (str(job_key),),
                )
            elif give_up_reason:
                self.conn.execute(
                    """UPDATE transcription_jobs
                       SET fact_rescue_attempts=fact_rescue_attempts+1,
                           fact_rescue_next_at=?,error=?,
                           updated_at=datetime('now','localtime')
                       WHERE job_key=?""",
                    (FACT_RESCUE_GIVE_UP_AT, str(give_up_reason), str(job_key)),
                )
            else:
                row = self.conn.execute(
                    "SELECT fact_rescue_attempts FROM transcription_jobs "
                    "WHERE job_key=?", (str(job_key),),
                ).fetchone()
                if row is None:
                    self.conn.commit()
                    return
                attempts = int(row["fact_rescue_attempts"]) + 1
                if attempts > len(backoff):
                    self.conn.execute(
                        """UPDATE transcription_jobs
                           SET fact_rescue_attempts=?,fact_rescue_next_at=?,
                               error=?,updated_at=datetime('now','localtime')
                           WHERE job_key=?""",
                        (attempts, FACT_RESCUE_GIVE_UP_AT,
                         f"等待完整小时经营事实；事实重抓 {attempts} 次后放弃",
                         str(job_key)),
                    )
                else:
                    self.conn.execute(
                        """UPDATE transcription_jobs
                           SET fact_rescue_attempts=?,fact_rescue_next_at=?,
                               updated_at=datetime('now','localtime')
                           WHERE job_key=?""",
                        (attempts, now + float(backoff[attempts - 1]),
                         str(job_key)),
                    )
            self.conn.commit()

    def business_session_is_ended(self, business_session_key: str) -> bool:
        """Return whether a business live day has been finalized."""
        rows = self.query(
            "SELECT status FROM business_sessions WHERE business_session_key=?",
            (str(business_session_key or ""),),
        )
        return bool(rows and str(rows[0]["status"]) == "ended")

    def resume_waiting_auth_briefs(self, *, now: float | None = None,
                                   limit: int = 2) -> list[str]:
        now = float(time.time() if now is None else now)
        with self._write_lock:
            rows = self.conn.execute(
                """SELECT job_key FROM transcription_jobs
                   WHERE purpose IN ('hourly','daily_tail')
                     AND consumer_status='waiting_auth'
                   ORDER BY window_end_ms DESC LIMIT ?""",
                (max(0, int(limit)),),
            ).fetchall()
            keys = [str(row["job_key"]) for row in reversed(rows)]
            for key in keys:
                self.conn.execute(
                    """UPDATE transcription_jobs SET consumer_status='pending',
                           consumer_next_at=?,lease_until=0,error='',
                           updated_at=datetime('now','localtime') WHERE job_key=?""",
                    (now, key),
                )
            self.conn.commit()
            return keys

    def resolve_waiting_auth_briefs(
            self, *, outage_seconds: int, now: float | None = None,
            short_outage_sec: int = 7200, limit: int = 2,
            owner: str | None = None,
    ) -> tuple[list[str], list[str]]:
        """短中断最多恢复 limit 张；其余只保留产物、不补发群卡。"""
        now = float(time.time() if now is None else now)
        with self._write_lock:
            if owner is not None:
                state = self.conn.execute(
                    "SELECT status,recovery_owner FROM taobao_session_state "
                    "WHERE singleton_id=1").fetchone()
                if (not state or str(state["status"]) == "healthy"
                        or str(state["recovery_owner"] or "") != str(owner)):
                    return [], []
            rows = self.conn.execute(
                """SELECT job_key,purpose FROM transcription_jobs
                   WHERE purpose IN ('hourly','daily_tail')
                     AND consumer_status='waiting_auth'
                   ORDER BY window_end_ms""").fetchall()
            hourly_keys = [str(row["job_key"]) for row in rows
                           if str(row["purpose"]) == "hourly"]
            daily_keys = [str(row["job_key"]) for row in rows
                          if str(row["purpose"]) == "daily_tail"]
            resumed_hourly = (hourly_keys[-max(0, int(limit)):] if
                       int(outage_seconds) <= int(short_outage_sec) and limit > 0 else [])
            # 尾段不发群卡，所以不受“最多补发 N 张”限制；
            # 它必须恢复消费，否则日报会永久缺最后一段。
            resumed = [*daily_keys, *resumed_hourly]
            resumed_set = set(resumed)
            suppressed = [key for key in hourly_keys if key not in resumed_set]
            for key in resumed:
                self.conn.execute(
                    """UPDATE transcription_jobs SET consumer_status='pending',
                           consumer_next_at=?,lease_until=0,error='delayed_auth_recovery',
                           updated_at=datetime('now','localtime') WHERE job_key=?""",
                    (now, key),
                )
            for key in suppressed:
                self.conn.execute(
                    """UPDATE transcription_jobs SET consumer_status='sent',sent_at=?,
                           consumer_next_at=0,lease_until=0,
                           recovery_metrics_status='pending',
                           recovery_metrics_next_at=?,
                           error='group card suppressed after authentication outage',
                           updated_at=datetime('now','localtime') WHERE job_key=?""",
                    (now, now, key),
                )
            self.conn.commit()
            return resumed, suppressed

    # ---------- 平台整场复盘任务（业务日级、跨重启；保留 liveId 兼容入口） ----------
    def queue_platform_review(self, live_id: str, triggered_at: float,
                              deadline_at: float,
                              business_session_key: str = "") -> bool:
        """只创建一次正式复盘任务；同一业务日的多个 liveId 共用一张正式卡。"""
        live_id = str(live_id or "").strip()
        if not live_id:
            return False
        session_key = str(business_session_key or "").strip()
        with self._write_lock:
            if session_key:
                existing = self.conn.execute(
                    "SELECT live_id FROM platform_review_jobs WHERE business_session_key=? "
                    "LIMIT 1", (session_key,)).fetchone()
                if existing is not None:
                    return False
            try:
                cursor = self.conn.execute(
                    """INSERT OR IGNORE INTO platform_review_jobs(
                           live_id,business_session_key,status,triggered_at,
                           deadline_at,next_attempt_at)
                       VALUES(?,?,'waiting',?,?,?)""",
                    (live_id, session_key, float(triggered_at),
                     float(deadline_at), float(triggered_at)),
                )
            except sqlite3.IntegrityError as exc:
                self.conn.rollback()
                if "duplicate business daily identity" in str(exc):
                    return False
                raise
            self.conn.commit()
            return cursor.rowcount > 0

    def get_platform_review_job(self, live_id: str):
        return self.conn.execute(
            "SELECT * FROM platform_review_jobs WHERE live_id=?",
            (str(live_id),),
        ).fetchone()

    def business_review_gate(
            self, business_session_key: str) -> tuple[bool, str]:
        """Return whether formal daily work may make external calls."""
        key = str(business_session_key or "").strip()
        if not key:
            return False, "missing_business_session_key"
        session = self.conn.execute(
            "SELECT status,observation_completed_at FROM business_sessions "
            "WHERE business_session_key=?", (key,),
        ).fetchone()
        if session is None:
            return False, "business_session_missing"
        status = str(session["status"] or "")
        if status != "ended":
            return False, f"business_session_{status or 'unknown'}"
        if float(session["observation_completed_at"] or 0) <= 0:
            return False, "business_observation_incomplete"
        active = self.conn.execute(
            """SELECT 1 FROM streams WHERE business_session_key=?
               AND status IN ('recording','recovering') LIMIT 1""", (key,),
        ).fetchone()
        if active is not None:
            return False, "business_session_recording"
        return True, "ready"

    def claim_due_platform_reviews(self, now: float | None = None, *, limit: int = 1,
                                   lease_sec: int = 3600) -> list[sqlite3.Row]:
        """原子领取到期任务；崩溃遗留 running 在租约过期后可被接管。"""
        now = float(time.time() if now is None else now)
        claimed: list[str] = []
        with self._write_lock:
            rows = self.conn.execute(
                """SELECT j.live_id FROM platform_review_jobs j
                   INNER JOIN business_sessions b
                     ON b.business_session_key=j.business_session_key
                   WHERE ((j.status='waiting' AND j.next_attempt_at<=?)
                          OR (j.status='running' AND j.lease_until<=?))
                     AND b.status='ended' AND b.observation_completed_at>0
                     AND NOT EXISTS (
                         SELECT 1 FROM streams s
                         WHERE s.business_session_key=j.business_session_key
                           AND s.status IN ('recording','recovering'))
                   ORDER BY j.next_attempt_at,j.triggered_at LIMIT ?""",
                (now, now, max(0, int(limit))),
            ).fetchall()
            for row in rows:
                live_id = str(row["live_id"])
                cursor = self.conn.execute(
                    """UPDATE platform_review_jobs
                       SET status='running',attempts=attempts+1,lease_until=?,
                           error='',updated_at=datetime('now','localtime')
                       WHERE live_id=? AND ((status='waiting' AND next_attempt_at<=?)
                           OR (status='running' AND lease_until<=?))
                         AND EXISTS (
                             SELECT 1 FROM business_sessions b
                             WHERE b.business_session_key=platform_review_jobs.business_session_key
                               AND b.status='ended'
                               AND b.observation_completed_at>0)
                         AND NOT EXISTS (
                             SELECT 1 FROM streams s
                             WHERE s.business_session_key=platform_review_jobs.business_session_key
                               AND s.status IN ('recording','recovering'))""",
                    (now + max(60, int(lease_sec)), live_id, now, now),
                )
                if cursor.rowcount:
                    claimed.append(live_id)
            self.conn.commit()
            if not claimed:
                return []
            placeholders = ",".join("?" for _ in claimed)
            return self.conn.execute(
                f"SELECT * FROM platform_review_jobs WHERE live_id IN ({placeholders}) "
                "ORDER BY next_attempt_at,triggered_at", tuple(claimed),
            ).fetchall()

    def update_platform_review_job(self, live_id: str, status: str, *,
                                   next_attempt_at: float | None = None,
                                   error: str = "", report_path: str = "",
                                   settlement_reminded_at: float | None = None) -> None:
        sent_at = time.time() if status in {"sent", "partial_sent"} else 0
        with self._write_lock:
            self.conn.execute(
                """UPDATE platform_review_jobs SET status=?,next_attempt_at=COALESCE(?,next_attempt_at),
                   lease_until=0,error=?,report_path=CASE WHEN ?!='' THEN ? ELSE report_path END,
                   sent_at=CASE WHEN ?>0 THEN ? ELSE sent_at END,
                   settlement_reminded_at=COALESCE(?,settlement_reminded_at),
                   updated_at=datetime('now','localtime') WHERE live_id=?""",
                (status, next_attempt_at, str(error)[:500], report_path, report_path,
                 sent_at, sent_at, settlement_reminded_at, str(live_id)),
            )
            self.conn.commit()

    def save_platform_review(self, live_id: str, payload: dict, *,
                             data_state: str, report_path: str = "") -> None:
        encoded = json.dumps(payload, ensure_ascii=False)
        with self._write_lock:
            self.conn.execute(
                """INSERT INTO platform_reviews(live_id,data_state,payload,report_path)
                   VALUES(?,?,?,?) ON CONFLICT(live_id) DO UPDATE SET
                   data_state=excluded.data_state,payload=excluded.payload,
                   report_path=excluded.report_path,
                   updated_at=datetime('now','localtime')""",
                (str(live_id), str(data_state), encoded, str(report_path)),
            )
            self.conn.commit()

    def get_platform_review(self, live_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM platform_reviews WHERE live_id=?", (str(live_id),),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        try:
            result["payload"] = json.loads(result.get("payload") or "{}")
        except (TypeError, ValueError):
            result["payload"] = {}
        return result

    def record_lifecycle_state(self, live_id: str, state: str) -> int:
        """持久化严格连续的下播确认；网络/未知/在播都会中断计数。"""
        live_id = str(live_id or "").strip()
        state = str(state or "unknown").strip().lower()
        if not live_id:
            return 0
        with self._write_lock:
            old = self.conn.execute(
                "SELECT last_state,ended_confirmations FROM room_lifecycle_state "
                "WHERE live_id=?", (live_id,),
            ).fetchone()
            confirmations = (
                int(old["ended_confirmations"] or 0) + 1
                if state == "ended" and old and old["last_state"] == "ended"
                else (1 if state == "ended" else 0)
            )
            self.conn.execute(
                """INSERT INTO room_lifecycle_state(live_id,last_state,ended_confirmations)
                   VALUES(?,?,?) ON CONFLICT(live_id) DO UPDATE SET
                   last_state=excluded.last_state,
                   ended_confirmations=excluded.ended_confirmations,
                   updated_at=datetime('now','localtime')""",
                (live_id, state, confirmations),
            )
            self.conn.commit()
        return confirmations

    def save_highlight_feedback(self, rows: list[dict]) -> int:
        """幂等同步飞书人工评价；同一反馈键始终保留最新状态。"""
        if not rows:
            return 0
        values = [(
            str(row.get("feedback_key") or ""), str(row.get("text_hash") or ""),
            str(row.get("rating") or ""), str(row.get("note") or ""), now_str(),
        ) for row in rows if row.get("feedback_key")]
        if not values:
            return 0
        with self._write_lock:
            self.conn.executemany(
                """INSERT INTO highlight_feedback(
                       feedback_key,text_hash,rating,note,synced_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(feedback_key) DO UPDATE SET
                       text_hash=excluded.text_hash,rating=excluded.rating,
                       note=excluded.note,synced_at=excluded.synced_at""",
                values,
            )
            self.conn.commit()
        return len(values)

    def get_highlight_feedback(self) -> dict[str, dict]:
        """按话术哈希返回反馈；误判优先于一般/好，避免坏样本继续入选。"""
        priority = {"误判": 3, "一般": 2, "好": 1, "待评": 0, "": 0}
        result: dict[str, dict] = {}
        for row in self.query("SELECT * FROM highlight_feedback ORDER BY synced_at"):
            key = str(row["text_hash"] or "")
            if not key:
                continue
            old = result.get(key)
            if old is None or priority.get(str(row["rating"] or ""), 0) >= priority.get(
                    str(old.get("rating") or ""), 0):
                result[key] = dict(row)
        return result

    # ---------- 流水线原子领取（防重复处理） ----------
    def claim_stream(self, stream_id: int, from_statuses: tuple[str, ...],
                     to_status: str) -> bool:
        """原子领取场次：仅当当前状态在 from_statuses 中时置为 to_status。
        返回 True=领取成功（唯一处理者）；False=已被处理/状态不符（调用方应跳过）。
        并发安全：UPDATE ... WHERE status IN (...)，SQLite 行锁保证只有一个线程成功。"""
        ph = ",".join("?" * len(from_statuses))
        with self._write_lock:
            cur = self.conn.execute(
                f"UPDATE streams SET status=?, error='', failed_stage='', updated_at=? "
                f"WHERE id=? AND status IN ({ph})",
                (to_status, now_str(), stream_id, *from_statuses))
            self.conn.commit()
            return cur.rowcount > 0

    # ---------- anchors ----------
    def _ensure_columns(self) -> None:
        """轻量迁移：旧库补 shift 列"""
        cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(anchors)")]
        if "shift" not in cols:
            self.conn.execute("ALTER TABLE anchors ADD COLUMN shift TEXT DEFAULT ''")
            self.conn.commit()

    def upsert_anchor(self, name: str, taobao_user_id: str = "", live_id: str = "",
                      shift: str = "", enabled: bool = True) -> int:
        self._ensure_columns()
        row = self.conn.execute(
            "SELECT id FROM anchors WHERE name=?", (name,)
        ).fetchone()
        if row:
            self.conn.execute(
                "UPDATE anchors SET taobao_user_id=?, live_id=?, shift=?, enabled=? WHERE id=?",
                (taobao_user_id, live_id, shift, int(bool(enabled)), row["id"]),
            )
            self.conn.commit()
            return row["id"]
        return self.execute(
            "INSERT INTO anchors(name, taobao_user_id, live_id, shift, enabled) VALUES(?,?,?,?,?)",
            (name, taobao_user_id, live_id, shift, int(bool(enabled))),
        )

    def get_anchor(self, anchor_id: int):
        return self.conn.execute(
            "SELECT * FROM anchors WHERE id=?", (anchor_id,)
        ).fetchone()

    def enabled_anchors(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM anchors WHERE enabled=1")

    # ---------- streams ----------
    def start_stream(self, anchor_id: int, live_id: str = "",
                     business_session_key: str = "") -> int:
        started_at = now_str()
        session_key = str(business_session_key or "").strip()
        if not session_key:
            parsed = datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=SHANGHAI)
            session_key = build_business_session_key(parsed)
        return self.execute(
            "INSERT INTO streams(anchor_id, started_at, status, live_id, business_session_key) "
            "VALUES(?,?, 'recording', ?, ?)",
            (anchor_id, started_at, live_id, session_key),
        )

    def set_recording_runtime(
            self, stream_id: int, session_dir: str,
            recorder_pid: int | None = None,
            *, resume_url: str | None = None) -> None:
        """持久化可恢复的录制线索。PID/媒体地址可随分片更新。"""
        with self._write_lock:
            if resume_url is None:
                self.conn.execute(
                    "UPDATE streams SET session_dir=?,recorder_pid=?,updated_at=? "
                    "WHERE id=?",
                    (session_dir, recorder_pid, now_str(), stream_id),
                )
            else:
                self.conn.execute(
                    "UPDATE streams SET session_dir=?,recorder_pid=?,resume_url=?,"
                    "updated_at=? WHERE id=?",
                    (session_dir, recorder_pid, str(resume_url), now_str(),
                     stream_id),
                )
            self.conn.commit()

    def set_recorder_resume_url(self, stream_id: int, resume_url: str) -> None:
        """更新私有续录地址；调用方不得输出地址原文。"""
        with self._write_lock:
            self.conn.execute(
                "UPDATE streams SET resume_url=?,updated_at=? WHERE id=?",
                (str(resume_url or ""), now_str(), stream_id),
            )
            self.conn.commit()

    def set_recorder_pid(self, stream_id: int, recorder_pid: int | None) -> None:
        with self._write_lock:
            self.conn.execute(
                "UPDATE streams SET recorder_pid=?,updated_at=? WHERE id=?",
                (recorder_pid, now_str(), stream_id),
            )
            self.conn.commit()

    def finish_stream(self, stream_id: int, file_path: str = "", duration_sec: float = 0.0,
                      status: str = "recorded", error: str = "",
                      ended_at: str = "") -> None:
        """结束真实录制。

        ended_at 只在这里首次落盘，后续转写/分析/报告必须调用 set_stream_status，
        防止处理耗时被误写成直播结束时间。
        """
        ended = ended_at or now_str()
        with self._write_lock:
            self.conn.execute(
                """UPDATE streams SET ended_at=COALESCE(ended_at, ?), status=?, error=?,
                   failed_stage=CASE WHEN ?='failed' THEN failed_stage ELSE '' END,
                   recorder_pid=NULL,resume_url='',
                   updated_at=?, file_path=COALESCE(NULLIF(?, ''), file_path),
                   duration_sec=CASE WHEN ? > 0 THEN ? ELSE duration_sec END
                   WHERE id=?""",
                (ended, status, error, status, ended, file_path,
                 duration_sec, duration_sec, stream_id),
            )
            row = self.conn.execute(
                """SELECT business_session_key,status,file_path,duration_sec
                   FROM streams WHERE id=?""", (int(stream_id),),
            ).fetchone()
            if (row is not None
                    and str(row["status"] or "") not in {
                        "recording", "recovering", "interrupted", "failed"}
                    and bool(str(row["file_path"] or "").strip())
                    and float(row["duration_sec"] or 0) > 0):
                self._wake_platform_review_for_session_locked(
                    str(row["business_session_key"] or ""))
            self.conn.commit()

    def set_stream_failed(self, stream_id: int, stage: str, error: str) -> None:
        """记录出错阶段，使 failed 能从正确前置状态续跑。"""
        if stage not in {"recording", "transcribe", "analyze", "report", "notify"}:
            raise ValueError(f"未知失败阶段: {stage}")
        with self._write_lock:
            self.conn.execute(
                "UPDATE streams SET status='failed',failed_stage=?,error=?,updated_at=? WHERE id=?",
                (stage, error, now_str(), stream_id),
            )
            self.conn.commit()

    def resume_failed_stream(self, stream_id: int) -> str | None:
        """原子把 failed 回退到出错阶段的前置状态。"""
        row = self.get_stream(stream_id)
        if not row or row["status"] != "failed":
            return None
        previous = {
            "transcribe": "recorded",
            "analyze": "transcribed",
            "report": "analyzed",
        }.get(row["failed_stage"] or "")
        if not previous:
            return None
        with self._write_lock:
            cur = self.conn.execute(
                """UPDATE streams SET status=?,error='',failed_stage='',updated_at=?
                   WHERE id=? AND status='failed' AND failed_stage=?""",
                (previous, now_str(), stream_id, row["failed_stage"]),
            )
            self.conn.commit()
        return previous if cur.rowcount else None

    def set_stream_status(self, stream_id: int, status: str, error: str = "") -> None:
        """更新处理状态，不触碰真实下播时间；完成阶段各写独立时间戳。"""
        ts_col = {
            "transcribed": "transcribed_at",
            "analyzed": "analyzed_at",
            "reported": "reported_at",
        }.get(status)
        now = now_str()
        with self._write_lock:
            if ts_col:
                self.conn.execute(
                    f"UPDATE streams SET status=?, error=?, failed_stage='', "
                    f"{ts_col}=?, updated_at=? WHERE id=?",
                    (status, error, now, now, stream_id),
                )
            else:
                self.conn.execute(
                    "UPDATE streams SET status=?, error=?, failed_stage='', updated_at=? WHERE id=?",
                    (status, error, now, stream_id),
                )
            self.conn.commit()

    def get_stream(self, stream_id: int):
        return self.conn.execute(
            "SELECT * FROM streams WHERE id=?", (stream_id,)
        ).fetchone()

    def day_seq(self, stream_id: int) -> int:
        """该场次在开播日期内是第几场（按开播时间排序，跨天场次按开播日归属）。
        仅用于展示；全局自增 id 仍是唯一标识，不参与重置。"""
        row = self.query("SELECT started_at FROM streams WHERE id=?", (stream_id,))
        if not row or not row[0]["started_at"]:
            return 0
        started = row[0]["started_at"]
        r = self.query(
            "SELECT COUNT(*) c FROM streams WHERE date(started_at)=date(?) AND started_at <= ?",
            (started, started))
        return int(r[0]["c"]) if r else 0

    def find_streams(self, status: str | None = None) -> list[sqlite3.Row]:
        if status is None:
            return self.query("SELECT * FROM streams ORDER BY id DESC")
        return self.query("SELECT * FROM streams WHERE status=? ORDER BY id DESC", (status,))

    # ---------- transcripts ----------
    def save_transcripts(self, stream_id: int, sentences: list[tuple[int, int, str]]) -> int:
        # 幂等：先清场次旧数据再写入（重复处理不产生重复行，配合唯一索引双保险）
        unique_sentences: list[tuple[int, int, str]] = []
        seen: set[tuple[int, int, str]] = set()
        for start_ms, end_ms, text in sentences:
            item = (int(start_ms), int(end_ms), str(text))
            if item not in seen:
                seen.add(item)
                unique_sentences.append(item)
        with self._write_lock:
            self.conn.execute("DELETE FROM transcripts WHERE stream_id=?", (stream_id,))
            cur = self.conn.executemany(
                "INSERT INTO transcripts(stream_id, start_ms, end_ms, text) VALUES(?,?,?,?)",
                [(stream_id, s, e, t) for s, e, t in unique_sentences],
            )
            self.conn.commit()
            return cur.rowcount

    def get_transcripts(self, stream_id: int) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM transcripts WHERE stream_id=? ORDER BY start_ms", (stream_id,)
        )

    def save_display_transcripts(
            self, stream_id: int, sentences: list[tuple[int, int, str]],
            *, repair_version: str = DISPLAY_REPAIR_VERSION,
    ) -> int:
        """将 analyze 阶段的一次批量修复结果与原始 ASR 对齐持久化。"""
        rows = self.get_transcripts(int(stream_id))
        if len(rows) != len(sentences):
            raise ValueError("展示转写与原始转写数量不一致")
        updates: list[tuple[str, str, str, str, str, int]] = []
        for row, (start_ms, end_ms, body) in zip(rows, sentences):
            if (int(row["start_ms"] or 0), int(row["end_ms"] or 0)) != (
                    int(start_ms), int(end_ms)):
                raise ValueError("展示转写与原始转写时间轴不一致")
            display = str(body or "").strip()
            repaired = bool(display and display != "【待回听确认】")
            updates.append((
                display if repaired else "",
                "repaired" if repaired else "failed",
                "display_repaired" if repaired else "raw_asr",
                hashlib.sha256(str(row["text"] or "").encode("utf-8")).hexdigest(),
                str(repair_version or DISPLAY_REPAIR_VERSION),
                int(row["id"]),
            ))
        with self._write_lock:
            self.conn.executemany(
                """UPDATE transcripts SET display_text=?,display_state=?,
                   text_provenance=?,source_hash=?,repair_version=? WHERE id=?""",
                updates,
            )
            self.conn.commit()
        return len(updates)

    def get_display_sentences(self, stream_id: int) -> list[tuple[int, int, str]]:
        """只返回与当前 raw ASR 哈希匹配的已修复展示语料。"""
        sentences: list[tuple[int, int, str]] = []
        for row in self.get_transcripts(int(stream_id)):
            source_hash = hashlib.sha256(
                str(row["text"] or "").encode("utf-8")).hexdigest()
            if (str(row["display_state"] or "") != "repaired"
                    or str(row["text_provenance"] or "") != "display_repaired"
                    or str(row["source_hash"] or "") != source_hash
                    or str(row["repair_version"] or "") != DISPLAY_REPAIR_VERSION):
                continue
            sentences.append((
                int(row["start_ms"] or 0), int(row["end_ms"] or 0),
                str(row["display_text"] or ""),
            ))
        return sentences

    def get_persisted_display_corpus(
            self, stream_id: int,
    ) -> list[tuple[int, int, str]] | None:
        """读取完整、同版本且与当前 raw 哈希匹配的批量修复结果。

        与正式展示读取不同，失败行恢复为占位符，供 analyze 下游稳定复用；
        任一行缺失/过期都返回 ``None``，由 analyze 重新做一次完整批量修复。
        """
        rows = self.get_transcripts(int(stream_id))
        if not rows:
            return None
        corpus: list[tuple[int, int, str]] = []
        for row in rows:
            source_hash = hashlib.sha256(
                str(row["text"] or "").encode("utf-8")).hexdigest()
            state = str(row["display_state"] or "")
            provenance = str(row["text_provenance"] or "")
            if (str(row["source_hash"] or "") != source_hash
                    or str(row["repair_version"] or "") != DISPLAY_REPAIR_VERSION):
                return None
            if state == "repaired" and provenance == "display_repaired":
                body = str(row["display_text"] or "").strip()
                if not body:
                    return None
            elif state == "failed" and provenance == "raw_asr":
                body = OUTWARD_REVIEW_PLACEHOLDER
            else:
                return None
            corpus.append((
                int(row["start_ms"] or 0), int(row["end_ms"] or 0), body,
            ))
        return corpus

    # ---------- highlights ----------
    @staticmethod
    def _highlight_row(highlight: dict) -> dict:
        row = dict(highlight)
        for name in ("peak_meta", "quality_meta"):
            value = row.get(name) or {}
            row[name] = (value if isinstance(value, str)
                         else json.dumps(value, ensure_ascii=False, allow_nan=False))
        row.setdefault("kind", "internal_signal")
        return row

    def save_highlights(self, highlights: list[dict], stream_id: int | None = None) -> int:
        # 幂等：先清场次旧高亮再写入
        if not highlights:
            if stream_id is not None:
                self.execute("DELETE FROM highlights WHERE stream_id=?", (stream_id,))
            return 0
        with self._write_lock:
            self.conn.execute(
                "DELETE FROM highlights WHERE stream_id=?",
                (highlights[0]["stream_id"],))
            cur = self.conn.executemany(
                """INSERT INTO highlights(stream_id, anchor_id, start_ms, end_ms, score,
                   reasons, transcript, kind, peak_meta, quality_meta)
                   VALUES(:stream_id, :anchor_id, :start_ms, :end_ms, :score, :reasons,
                          :transcript, :kind, :peak_meta, :quality_meta)""",
                [self._highlight_row(highlight) for highlight in highlights],
            )
            self.conn.commit()
            return cur.rowcount

    def append_highlights(self, highlights: list[dict]) -> int:
        """追加高亮（不清旧）：供数据驱动高亮在分析阶段之后补充"""
        if not highlights:
            return 0
        with self._write_lock:
            cur = self.conn.executemany(
                """INSERT INTO highlights(stream_id, anchor_id, start_ms, end_ms, score,
                   reasons, transcript, kind, peak_meta, quality_meta)
                   VALUES(:stream_id, :anchor_id, :start_ms, :end_ms, :score, :reasons,
                          :transcript, :kind, :peak_meta, :quality_meta)""",
                [self._highlight_row(highlight) for highlight in highlights],
            )
            self.conn.commit()
            return cur.rowcount

    def delete_peak_highlights(self, stream_id: int) -> int:
        """删除本场旧的数据峰值高亮，保留关键词/声学高亮，支持报告阶段幂等重跑。"""
        with self._write_lock:
            cur = self.conn.execute(
                """DELETE FROM highlights
                   WHERE stream_id=? AND (
                       kind='data_association' OR peak_meta != '{}' OR reasons LIKE '%成交峰值%'
                       OR reasons LIKE '%点击峰值%' OR reasons LIKE '%在线峰值%'
                   )""",
                (stream_id,),
            )
            self.conn.commit()
            return cur.rowcount

    def get_highlights(self, stream_id: int) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM highlights WHERE stream_id=? ORDER BY score DESC", (stream_id,)
        )

    # ---------- talktracks ----------
    def upsert_talktrack(self, anchor_id: int, stream_id: int, category: str,
                         text: str, norm_text: str, *,
                         text_provenance: str = "raw_asr") -> tuple[int, bool]:
        """返回 (id, 是否新增到历史库)。同一场重复抽取只记录一次，不累计频次。"""
        now = now_str()
        with self._write_lock:
            occ = self.conn.execute(
                """INSERT OR IGNORE INTO talktrack_occurrences
                   (stream_id,anchor_id,category,norm_text,text,text_provenance,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (stream_id, anchor_id, category, norm_text, text,
                 text_provenance, now),
            )
            row = self.conn.execute(
                """SELECT id,text,text_provenance FROM talktracks
                   WHERE anchor_id=? AND norm_text=? AND category=?""",
                (anchor_id, norm_text, category),
            ).fetchone()
            if occ.rowcount == 0:
                self.conn.commit()
                return (row["id"] if row else 0), False
            if row:
                old_provenance = str(row["text_provenance"] or "raw_asr")
                if (text_provenance == "display_repaired"
                        and old_provenance != "display_repaired"):
                    best, best_provenance = text, text_provenance
                elif (old_provenance == "display_repaired"
                      and text_provenance != "display_repaired"):
                    best, best_provenance = row["text"], old_provenance
                elif len(row["text"] or "") >= len(text):
                    best, best_provenance = row["text"], old_provenance
                else:
                    best, best_provenance = text, text_provenance
                count = self.conn.execute(
                    """SELECT COUNT(*) c FROM talktrack_occurrences
                       WHERE anchor_id=? AND norm_text=? AND category=?""",
                    (anchor_id, norm_text, category),
                ).fetchone()["c"]
                self.conn.execute(
                    """UPDATE talktracks SET text=?,text_provenance=?,use_count=?,
                       last_seen=?,stream_id=? WHERE id=?""",
                    (best, best_provenance, count, now, stream_id, row["id"]),
                )
                self.conn.commit()
                return row["id"], False
            cur = self.conn.execute(
                """INSERT INTO talktracks(anchor_id,stream_id,category,text,norm_text,
                   text_provenance,use_count,first_seen,last_seen)
                   VALUES(?,?,?,?,?,?,1,?,?)""",
                (anchor_id, stream_id, category, text, norm_text,
                 text_provenance, now, now),
            )
            self.conn.commit()
            return cur.lastrowid, True

    def clear_stream_talktracks(self, stream_id: int) -> None:
        """清除本场旧话术出现并重算历史聚合，供分析安全重跑。"""
        with self._write_lock:
            affected = self.conn.execute(
                """SELECT DISTINCT anchor_id,category,norm_text FROM talktrack_occurrences
                   WHERE stream_id=?""", (stream_id,)).fetchall()
            self.conn.execute("DELETE FROM talktrack_occurrences WHERE stream_id=?", (stream_id,))
            for item in affected:
                latest = self.conn.execute(
                    """SELECT stream_id,text,text_provenance,created_at
                       FROM talktrack_occurrences
                       WHERE anchor_id=? AND category=? AND norm_text=?
                       ORDER BY created_at DESC,id DESC LIMIT 1""",
                    (item["anchor_id"], item["category"], item["norm_text"]),
                ).fetchone()
                if latest:
                    count = self.conn.execute(
                        """SELECT COUNT(*) c FROM talktrack_occurrences
                           WHERE anchor_id=? AND category=? AND norm_text=?""",
                        (item["anchor_id"], item["category"], item["norm_text"]),
                    ).fetchone()["c"]
                    self.conn.execute(
                        """UPDATE talktracks SET stream_id=?,text=?,text_provenance=?,
                           use_count=?,last_seen=?
                           WHERE anchor_id=? AND category=? AND norm_text=?""",
                        (latest["stream_id"], latest["text"], latest["text_provenance"],
                         count, latest["created_at"],
                         item["anchor_id"], item["category"], item["norm_text"]),
                    )
                else:
                    self.conn.execute(
                        "DELETE FROM talktracks WHERE anchor_id=? AND category=? AND norm_text=?",
                        (item["anchor_id"], item["category"], item["norm_text"]),
                    )
            self.conn.commit()

    def get_talktracks(self, anchor_id: int | None = None, category: str | None = None,
                       since: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM talktracks WHERE 1=1"
        args: list = []
        if anchor_id is not None:
            sql += " AND anchor_id=?"
            args.append(anchor_id)
        if category:
            sql += " AND category=?"
            args.append(category)
        if since:
            sql += " AND last_seen>=?"
            args.append(since)
        sql += " ORDER BY use_count DESC, last_seen DESC"
        return self.query(sql, tuple(args))

    # ---------- brief_transcripts（简报分片转写，下播复盘复用） ----------
    def save_brief_transcripts(self, stream_id: int, part_name: str,
                               sentences: list[tuple[int, int, str]]) -> int:
        with self._write_lock:
            self.conn.execute(
                "DELETE FROM brief_transcripts WHERE stream_id=? AND part_name=?",
                (stream_id, part_name))
            cur = self.conn.executemany(
                """INSERT INTO brief_transcripts(stream_id, part_name, start_ms, end_ms, text)
                   VALUES(?,?,?,?,?)""",
                [(stream_id, part_name, s, e, t) for s, e, t in sentences],
            )
            self.conn.commit()
            return cur.rowcount

    def get_brief_transcripts(self, stream_id: int) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM brief_transcripts WHERE stream_id=? ORDER BY start_ms",
            (stream_id,))

    # ---------- 统一转写任务（妙记主链路 + FunASR 备胎） ----------
    def queue_transcription_job(self, *, job_key: str, stream_id: int, live_id: str,
                                purpose: str, window_start_ms: int,
                                window_end_ms: int, media_manifest: list[str],
                                media_hash: str, deadline_at: float | None,
                                media_origin_ms: int = 0,
                                media_layout: list[dict] | None = None,
                                media_coverage: dict | None = None,
                                media_path: str = "", business_session_key: str = "",
                                shift_window_key: str = "",
                                fallback_after_at: float | None = None) -> bool:
        with self._write_lock:
            cursor = self.conn.execute(
                """INSERT OR IGNORE INTO transcription_jobs(
                       job_key,stream_id,live_id,purpose,window_start_ms,window_end_ms,
                       media_manifest,media_hash,media_origin_ms,media_layout_json,
                       media_coverage_json,media_path,business_session_key,shift_window_key,
                       deadline_at,fallback_after_at,next_poll_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                (str(job_key), int(stream_id), str(live_id or ""), str(purpose),
                 int(window_start_ms), int(window_end_ms),
                 json.dumps(media_manifest, ensure_ascii=False), str(media_hash),
                 int(media_origin_ms),
                 json.dumps(media_layout or [], ensure_ascii=False, allow_nan=False,
                            sort_keys=True, separators=(",", ":")),
                 json.dumps(media_coverage or {}, ensure_ascii=False, allow_nan=False,
                            sort_keys=True, separators=(",", ":")),
                 str(media_path or ""),
                 str(business_session_key or ""),
                 str(shift_window_key or ""), deadline_at,
                 float(fallback_after_at or 0)),
            )
            self.conn.commit()
            return cursor.rowcount > 0

    def get_transcription_job(self, job_key: str):
        return self.conn.execute(
            "SELECT * FROM transcription_jobs WHERE job_key=?", (str(job_key),)
        ).fetchone()

    def get_stream_transcription_jobs(self, stream_id: int) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM transcription_jobs WHERE stream_id=? ORDER BY window_start_ms",
            (int(stream_id),),
        )

    def claim_due_transcription_jobs(self, now: float | None = None, *, limit: int = 3,
                                     lease_sec: int = 120) -> list[sqlite3.Row]:
        """短租约领取；processing/fallback_ready 仍可轮询远端，不占工作线程。"""
        now = float(time.time() if now is None else now)
        claimed: list[str] = []
        with self._write_lock:
            rows = self.conn.execute(
                """SELECT job_key FROM transcription_jobs
                   WHERE (status IN ('queued','media_ready','uploaded','processing','fallback_running')
                          OR (status='fallback_ready' AND remote_status!='blocked'))
                     AND next_poll_at<=? AND lease_until<=?
                   ORDER BY next_poll_at,created_at LIMIT ?""",
                (now, now, max(0, int(limit))),
            ).fetchall()
            for row in rows:
                key = str(row["job_key"])
                cursor = self.conn.execute(
                    """UPDATE transcription_jobs SET lease_until=?,attempts=attempts+1,
                       updated_at=datetime('now','localtime')
                       WHERE job_key=? AND next_poll_at<=? AND lease_until<=?
                       AND (status IN ('queued','media_ready','uploaded','processing','fallback_running')
                            OR (status='fallback_ready' AND remote_status!='blocked'))""",
                    (now + max(10, int(lease_sec)), key, now, now),
                )
                if cursor.rowcount:
                    claimed.append(key)
            self.conn.commit()
        return [self.get_transcription_job(key) for key in claimed]

    def update_transcription_job(self, job_key: str, *, status: str | None = None,
                                 remote_status: str | None = None,
                                 next_poll_at: float | None = None,
                                 result_json: str | None = None,
                                 fallback_json: str | None = None,
                                 error_class: str | None = None,
                                 error: str | None = None, **fields) -> None:
        allowed = {
            "media_path", "drive_file_token", "minute_token", "minute_url",
            "note_id", "note_doc_token", "cleanup_status", "cleanup_next_at",
            "cleanup_attempts", "cleaned_at",
            "consumer_status", "consumer_next_at", "consumer_attempts",
            "delivery_key", "sent_at",
            "alert_status", "review_fallback_approved",
            "alert_after_at", "last_alert_at",
            "recovery_metrics_status", "recovery_metrics_json",
            "recovery_metrics_attempts", "recovery_metrics_next_at",
        }
        values = {
            "status": status, "remote_status": remote_status,
            "next_poll_at": next_poll_at, "result_json": result_json,
            "fallback_json": fallback_json, "error_class": error_class,
            "error": error,
            **{key: value for key, value in fields.items() if key in allowed},
        }
        sets, args = [], []
        for key, value in values.items():
            if value is not None:
                sets.append(f"{key}=?")
                args.append(value)
        sets.extend(["lease_until=0", "updated_at=datetime('now','localtime')"])
        with self._write_lock:
            previous = self.conn.execute(
                """SELECT status,purpose,business_session_key,
                          review_fallback_approved
                   FROM transcription_jobs WHERE job_key=?""",
                (str(job_key),),
            ).fetchone()
            self.conn.execute(
                f"UPDATE transcription_jobs SET {','.join(sets)} WHERE job_key=?",
                (*args, str(job_key)),
            )
            terminal_transition = bool(
                previous is not None
                and status in {"ready", "fallback_ready"}
                and str(previous["status"] or "") != str(status)
            )
            fallback_approved = fields.get("review_fallback_approved")
            approval_transition = bool(
                previous is not None
                and fallback_approved is not None
                and int(fallback_approved or 0) == 1
                and int(previous["review_fallback_approved"] or 0) == 0
            )
            if ((terminal_transition or approval_transition)
                    and str(previous["purpose"] or "") in {"hourly", "daily_tail"}):
                self._wake_platform_review_for_session_locked(
                    str(previous["business_session_key"] or ""))
            self.conn.commit()

    def reconcile_transcription_job_media(
            self, job_key: str, *, media_layout_json: str,
            media_coverage_json: str, media_hash: str) -> bool:
        """Update local-only media evidence without releasing its claim lease."""
        with self._write_lock:
            changed = self.conn.execute(
                """UPDATE transcription_jobs
                   SET media_layout_json=?,media_coverage_json=?,media_hash=?,
                       updated_at=datetime('now','localtime')
                   WHERE job_key=? AND status IN ('queued','processing')
                     AND remote_status='queued' AND media_path=''
                     AND drive_file_token='' AND minute_token=''
                     AND result_json='' AND fallback_json=''""",
                (
                    str(media_layout_json), str(media_coverage_json),
                    str(media_hash), str(job_key),
                ),
            ).rowcount
            self.conn.commit()
        return changed == 1

    def claim_recovery_metrics_jobs(
            self, *, now: float | None = None, limit: int = 3,
            lease_sec: int = 120, job_keys: list[str] | None = None,
    ) -> list[sqlite3.Row]:
        """原子领取被抑制群卡的历史趋势补抓，崩溃后可接管。"""
        now = float(time.time() if now is None else now)
        key_filter = ""
        args: list = [now, now]
        if job_keys is not None:
            clean = [str(key) for key in job_keys if str(key)]
            if not clean:
                return []
            key_filter = f" AND job_key IN ({','.join('?' for _ in clean)})"
            args.extend(clean)
        args.append(max(0, int(limit)))
        claimed: list[str] = []
        with self._write_lock:
            rows = self.conn.execute(
                """SELECT job_key FROM transcription_jobs
                   WHERE recovery_metrics_status IN ('pending','failed','running','writing')
                     AND recovery_metrics_next_at<=? AND lease_until<=?"""
                + key_filter +
                " ORDER BY recovery_metrics_next_at,window_end_ms LIMIT ?",
                tuple(args),
            ).fetchall()
            for row in rows:
                key = str(row["job_key"])
                cursor = self.conn.execute(
                    """UPDATE transcription_jobs
                       SET recovery_metrics_status='running',
                           recovery_metrics_attempts=recovery_metrics_attempts+1,
                           lease_until=?,updated_at=datetime('now','localtime')
                       WHERE job_key=? AND recovery_metrics_next_at<=? AND lease_until<=?
                         AND recovery_metrics_status IN ('pending','failed','running','writing')""",
                    (now + max(10, int(lease_sec)), key, now, now),
                )
                if cursor.rowcount:
                    claimed.append(key)
            self.conn.commit()
        return [self.get_transcription_job(key) for key in claimed]

    def claim_ready_brief_consumers(self, now: float | None = None, *, limit: int = 2,
                                    lease_sec: int = 900) -> list[sqlite3.Row]:
        now = float(time.time() if now is None else now)
        keys: list[str] = []
        with self._write_lock:
            rows = self.conn.execute(
                """SELECT job_key FROM transcription_jobs
                   WHERE ((purpose IN ('hourly','daily_tail') AND (
                              (status='ready' AND result_json!='')
                              OR (status='fallback_ready'
                                  AND (result_json!='' OR fallback_json!='')
                                  AND (purpose='hourly' OR review_fallback_approved=1))
                              OR (purpose='hourly' AND status='processing' AND result_json!=''
                                  AND deadline_at IS NOT NULL AND deadline_at<=?)
                          ))
                          OR (purpose='review' AND status IN ('ready','fallback_ready')))
                     AND (consumer_status IN ('pending','failed')
                          OR (consumer_status='running' AND lease_until<=?))
                     AND consumer_next_at<=? AND lease_until<=?
                   ORDER BY window_start_ms LIMIT ?""",
                (now, now, now, now, max(0, int(limit))),
            ).fetchall()
            for row in rows:
                key = str(row["job_key"])
                cursor = self.conn.execute(
                    """UPDATE transcription_jobs SET consumer_status='running',
                       consumer_attempts=consumer_attempts+1,lease_until=?,
                       updated_at=datetime('now','localtime')
                       WHERE job_key=?
                         AND (consumer_status IN ('pending','failed')
                              OR (consumer_status='running' AND lease_until<=?))
                         AND consumer_next_at<=? AND lease_until<=?""",
                    (now + max(60, int(lease_sec)), key, now, now, now),
                )
                if cursor.rowcount:
                    keys.append(key)
            self.conn.commit()
        return [self.get_transcription_job(key) for key in keys]

    def claim_due_transcription_cleanups(self, now: float | None = None, *, limit: int = 3,
                                         lease_sec: int = 120) -> list[sqlite3.Row]:
        now = float(time.time() if now is None else now)
        keys: list[str] = []
        with self._write_lock:
            rows = self.conn.execute(
                """SELECT job_key FROM transcription_jobs
                   WHERE status='ready' AND result_json!='' AND drive_file_token!=''
                     AND (cleanup_status IN ('pending','failed')
                          OR (cleanup_status='running' AND lease_until<=?))
                     AND cleanup_next_at<=? AND lease_until<=?
                   ORDER BY cleanup_next_at,created_at LIMIT ?""",
                (now, now, now, max(0, int(limit))),
            ).fetchall()
            for row in rows:
                key = str(row["job_key"])
                cursor = self.conn.execute(
                    """UPDATE transcription_jobs SET cleanup_status='running',
                       cleanup_attempts=cleanup_attempts+1,lease_until=?,
                       updated_at=datetime('now','localtime')
                       WHERE job_key=?
                         AND (cleanup_status IN ('pending','failed')
                              OR (cleanup_status='running' AND lease_until<=?))
                         AND cleanup_next_at<=? AND lease_until<=?""",
                    (now + max(10, int(lease_sec)), key, now, now, now),
                )
                if cursor.rowcount:
                    keys.append(key)
            self.conn.commit()
        return [self.get_transcription_job(key) for key in keys]

    # ---------- reviews ----------
    def _ensure_review_columns(self) -> None:
        cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(reviews)")]
        if "ai_review" not in cols:
            self.conn.execute("ALTER TABLE reviews ADD COLUMN ai_review TEXT DEFAULT ''")
            self.conn.commit()

    def save_review(self, stream_id: int, report_path: str, ai_review: str = "") -> int:
        self._ensure_review_columns()
        # 幂等：配合唯一索引 idx_reviews_stream，同场次只保留一行（重复处理不产生重复推送记录）
        with self._write_lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO reviews(stream_id, report_path, ai_review)
                   VALUES(?,?,?)""",
                (stream_id, report_path, ai_review),
            )
            self.conn.commit()
        return stream_id

    def get_review(self, stream_id: int):
        return self.conn.execute(
            "SELECT * FROM reviews WHERE stream_id=?", (stream_id,)
        ).fetchone()


# 供脚本使用的快速实例（懒加载）
_db: Store | None = None


def get_store() -> Store:
    global _db
    if _db is None:
        cfg = load_config()
        _db = Store(resolve(cfg["paths"]["db"]))
    return _db
