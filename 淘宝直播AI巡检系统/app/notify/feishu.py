"""飞书通知：群卡片推送 + 多维表格统计

实现方式：复用本机已授权的 lark-cli（机器人/用户身份，无需额外凭证）。
- 群卡片：每场直播复盘摘要推送到指定群（interactive card）
- 多维表格：每场直播的结构化数据自动追加一行（场次/主播/时长/高亮/话术类别分布）
- 多维表格首次自动创建，token 存 data/notify_state.json；任何失败只记日志，不阻断流水线

配置（config.yaml -> notify.feishu）：
  enabled: false
  chat_id: ""            # 推送群 oc_xxx；留空不推送卡片
"""
from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import subprocess
import threading
from datetime import datetime
from copy import deepcopy
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from ..config import SHANGHAI, now_shanghai, resolve
from ..runtime.locking import exclusive_file_lock

log = logging.getLogger("notify.feishu")

STATE_FILE = "data/notify_state.json"
PLATFORM_OUTWARD_SCHEMA_VERSION = "platform-outward-v1"

_notify_lock = threading.Lock()
_state_write_lock = threading.Lock()


def _safe_platform_ai_review(text: str) -> str:
    """冻结前只保留通过当前展示语义红旗门禁的 AI 正文。"""
    body = str(text or "").strip()
    if not body:
        return ""
    from ..asr.clean import has_display_semantic_red_flag
    return "" if has_display_semantic_red_flag(body) else body


def freeze_platform_review_payload(summary: dict) -> dict:
    """生成可持久重放的平台复盘载荷，并显式标记外发 schema。"""
    frozen = deepcopy(summary if isinstance(summary, dict) else {})
    frozen["outward_schema_version"] = PLATFORM_OUTWARD_SCHEMA_VERSION
    # 官方总账和主播指标在冻结时进入独立命名空间。
    # display_metrics/主播 metrics 随后只从该命名空间回填，
    # 模型输出中同名或伪造的经营字段永远不会成为展示数据。
    official_anchors: dict[str, dict] = {}
    for anchor in frozen.get("anchors") or []:
        if not isinstance(anchor, dict):
            continue
        anchor_id = str(int(anchor.get("anchor_id") or 0))
        official_anchors[anchor_id] = deepcopy(anchor.get("metrics") or {})
    frozen["official_metrics"] = {
        "platform": deepcopy(frozen.get("display_metrics") or {}),
        "anchors": official_anchors,
    }
    frozen["display_metrics"] = deepcopy(frozen["official_metrics"]["platform"])
    for anchor in frozen.get("anchors") or []:
        if isinstance(anchor, dict):
            anchor_id = str(int(anchor.get("anchor_id") or 0))
            anchor["metrics"] = deepcopy(
                frozen["official_metrics"]["anchors"].get(anchor_id, {}))
            anchor["ai_review"] = _safe_platform_ai_review(
                anchor.get("ai_review") or "")
    if "platform_intelligence" in frozen:
        from ..intelligence.platform import (
            build_platform_intelligence_binding,
            sanitize_platform_intelligence_payload,
        )
        frozen["platform_intelligence"] = sanitize_platform_intelligence_payload(
            frozen.get("platform_intelligence"))
        try:
            frozen["platform_intelligence_binding"] = (
                build_platform_intelligence_binding(
                    frozen.get("live_id"),
                    frozen.get("platform_intelligence_input_hash"),
                    frozen["platform_intelligence"],
                ))
        except (TypeError, ValueError):
            frozen.pop("platform_intelligence_binding", None)
    return frozen


def _read_state_file(path: Path) -> dict:
    if path.exists():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {"_state_file_corrupt": True}
        except Exception:
            return {"_state_file_corrupt": True}
    return {}


@contextmanager
def _state_file_lock(path: Path):
    """串行跨进程状态写入，避免多个通知流程相互覆盖。"""
    with _state_write_lock:
        lock_path = path.with_name(path.name + ".lock")
        with exclusive_file_lock(lock_path):
            yield


def _merge_state(newer: dict, pending: dict) -> dict:
    """将旧快照的改动合并到磁盘上的更新状态，保留未知/新增分区。"""
    merged = dict(newer)
    for key, value in pending.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge_state(current, value)
        elif isinstance(current, list) and isinstance(value, list):
            merged[key] = list(dict.fromkeys([*current, *value]))
        else:
            merged[key] = value
    return merged


def _load_state() -> dict:
    p = Path(resolve(STATE_FILE))
    return _read_state_file(p)


def _save_state(state: dict) -> None:
    p = Path(resolve(STATE_FILE))
    p.parent.mkdir(parents=True, exist_ok=True)
    with _state_file_lock(p):
        current = _read_state_file(p)
        if current.get("_state_file_corrupt") is True:
            raise RuntimeError("通知投递状态文件损坏，已停止覆盖与自动重发")
        merged = _merge_state(current, state)
        tmp = p.with_name(f".{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.chmod(0o600)
            os.replace(tmp, p)
        finally:
            if tmp.exists():
                tmp.unlink()


def _mark_notified(stream_id: int, kind: str = "card") -> None:
    with _notify_lock:
        st = _load_state()
        key = "notified_card_streams" if kind == "card" else "notified_bitable_streams"
        values = st.setdefault(key, [])
        if stream_id not in values:
            values.append(stream_id)
        pending = st.get("pending_card_streams" if kind == "card" else "pending_bitable_streams")
        if isinstance(pending, dict):
            pending.pop(str(stream_id), None)
        _save_state(st)


MAX_NOTIFY_RETRY_ATTEMPTS = 5  # 失败通知自动重试上限；超限停止，保留记录待人工处理


class DeliveryUnknownError(RuntimeError):
    """请求可能已被远端接收，但本地无法确认回执。"""

    _CATEGORIES = {
        "transport", "receipt", "transport_timeout", "transport_process",
        "transport_decode", "transport_error",
    }

    def __init__(self, category: str = "transport", *,
                 error_type: str = "DeliveryUnknownError"):
        safe_category = (category if category in self._CATEGORIES
                         else "transport_error")
        safe_type = (str(error_type) if re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]{0,79}", str(error_type or ""))
            else "Exception")
        self.category = safe_category
        self.stage = "receipt" if safe_category == "receipt" else "transport"
        self.error_type = safe_type
        super().__init__(safe_category)


class ExplicitRemoteRejection(RuntimeError):
    """远端明确声明未接受请求；这是唯一允许自动重试的投递异常。"""

    def __init__(self):
        super().__init__("remote_rejected")


def safe_delivery_error_fields(exc: BaseException) -> tuple[str, str]:
    """返回可写入日志的固定投递分类与异常类型。"""
    if isinstance(exc, DeliveryUnknownError):
        return exc.category, exc.error_type
    if isinstance(exc, ExplicitRemoteRejection):
        return "remote_rejection", "ExplicitRemoteRejection"
    error_type = type(exc).__name__
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", error_type):
        error_type = "Exception"
    return "transport_error", error_type


def remote_idempotency_key(delivery_key: str) -> str:
    """生成 lark-cli/Feishu 最长 50 字符的稳定远端 UUID。"""
    digest = hashlib.sha256(str(delivery_key or "").encode("utf-8")).hexdigest()
    return f"card-{digest[:45]}"


def _serialize_card(card: dict) -> str:
    return json.dumps(
        card, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def frozen_delivery_payload(delivery_key: str) -> str:
    """返回首次尝试前冻结的原始 JSON 字符串，不解析也不重新序列化。"""
    state = _load_state()
    deliveries = state.get("deliveries") or {}
    row = deliveries.get(str(delivery_key)) if isinstance(deliveries, dict) else None
    return str((row or {}).get("payload_json") or "") if isinstance(row, dict) else ""


def frozen_delivery_card(delivery_key: str) -> dict | None:
    """返回首次远端尝试前冻结的卡片，供明确失败原样重放。"""
    payload = frozen_delivery_payload(delivery_key)
    if not payload:
        return None
    try:
        card = json.loads(str(payload))
    except (TypeError, ValueError):
        return None
    return card if isinstance(card, dict) else None


def _delivery_begin(
        key: str, *, remote_key: str = "", payload_hash: str = "",
        target_hash: str = "", payload_json: str = "",
) -> bool:
    """原子登记一次发送尝试。

    上次若停在 sending，说明进程可能在飞书成功响应后、落本地状态前崩溃；
    此时不自动重发，宁可告警人工核验，也不在群里制造重复卡片。
    """
    with _notify_lock:
        state = _load_state()
        if state.get("_state_file_corrupt") is True:
            return False
        deliveries = state.setdefault("deliveries", {})
        old = deliveries.get(key) or {}
        if (old.get("payload_hash") and payload_hash
                and old.get("payload_hash") != payload_hash):
            raise ValueError("相同投递键对应的卡片载荷已变更，拒绝自动重发")
        if (old.get("target_hash") and target_hash
                and old.get("target_hash") != target_hash):
            raise ValueError("相同投递键的目标会话已变更，拒绝自动重发")
        if old.get("status") in {"sending", "delivery_unknown", "sent"}:
            return False
        attempts = int(old.get("attempts", 0))
        if attempts >= MAX_NOTIFY_RETRY_ATTEMPTS:
            return False
        deliveries[key] = {
            **old,
            "status": "sending",
            "attempts": attempts + 1,
            "remote_idempotency_key": remote_key or old.get(
                "remote_idempotency_key") or remote_idempotency_key(key),
            "payload_hash": payload_hash or old.get("payload_hash") or "",
            "payload_json": payload_json or old.get("payload_json") or "",
            "target_hash": target_hash or old.get("target_hash") or "",
            "updated_at": now_shanghai().strftime("%Y-%m-%d %H:%M:%S"),
        }
        _save_state(state)
    return True


def _delivery_finish(
        key: str, *, status: str = "sent", error_stage: str = "",
        error_type: str = "",
        remote_message_id: str = "",
) -> None:
    if status not in {"sent", "failed", "delivery_unknown"}:
        raise ValueError(f"unsupported delivery status: {status}")
    with _notify_lock:
        state = _load_state()
        deliveries = state.setdefault("deliveries", {})
        old = deliveries.get(key) or {}
        safe_stage = (error_stage if error_stage in {
            "", "transport", "receipt", "remote_rejection"} else "transport")
        safe_type = (str(error_type) if re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]{0,79}", str(error_type or "")) else "Exception")
        safe_error = (f"{safe_stage}:{safe_type}"
                      if safe_stage and safe_type else "")
        deliveries[key] = {
            **old,
            "status": status,
            "error": safe_error,
            "error_stage": safe_stage,
            "error_type": safe_type if safe_stage else "",
            "remote_message_id": remote_message_id or old.get(
                "remote_message_id") or "",
            "updated_at": now_shanghai().strftime("%Y-%m-%d %H:%M:%S"),
        }
        _save_state(state)


def delivery_status(key: str) -> str:
    """读取持久化投递状态，供跨重启任务判断是否可以安全重试。"""
    state = _load_state()
    if state.get("_state_file_corrupt") is True:
        return "delivery_unknown"
    deliveries = state.get("deliveries") or {}
    row = deliveries.get(str(key)) if isinstance(deliveries, dict) else None
    return str((row or {}).get("status") or "") if isinstance(row, dict) else ""


def send_card_once(cfg: dict, chat_id: str, card: dict, delivery_key: str) -> bool:
    """本地状态+飞书 UUID 双层幂等；歧义回执永不自动重发。"""
    key = str(delivery_key or "").strip()
    if not key:
        raise ValueError("delivery_key 不能为空")
    payload = frozen_delivery_payload(key)
    if payload:
        try:
            frozen = json.loads(payload)
        except (TypeError, ValueError):
            frozen = None
        if isinstance(frozen, dict):
            card = frozen
    else:
        payload = _serialize_card(card)
    remote_key = remote_idempotency_key(key)
    payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    target_hash = hashlib.sha256(str(chat_id).encode("utf-8")).hexdigest()
    if not _delivery_begin(
            key, remote_key=remote_key, payload_hash=payload_hash,
            target_hash=target_hash, payload_json=payload):
        status = delivery_status(key)
        if status != "sent":
            log.warning("卡片投递状态待核验，跳过自动重发：%s (%s)", key, status)
        return status == "sent"
    try:
        result = send_card(
            cfg, chat_id, card, idempotency_key=remote_key,
            serialized_content=payload)
    except ExplicitRemoteRejection as exc:
        _delivery_finish(
            key, status="failed", error_stage="remote_rejection",
            error_type=type(exc).__name__)
        log.warning(
            "飞书明确拒绝请求，可原样重试：stage=remote_rejection "
            "error_type=%s", type(exc).__name__)
        raise
    except Exception as exc:
        stage = exc.stage if isinstance(exc, DeliveryUnknownError) else "transport"
        error_type = (exc.error_type if isinstance(exc, DeliveryUnknownError)
                      else type(exc).__name__)
        _delivery_finish(
            key, status="delivery_unknown", error_stage=stage,
            error_type=error_type)
        log.error(
            "飞书卡片投递回执不确定，已停止自动重发：stage=%s "
            "error_type=%s", stage, error_type)
        return False
    message_id = str(_find_value(result, ("message_id", "messageId")) or "") \
        if isinstance(result, dict) else ""
    _delivery_finish(key, status="sent", remote_message_id=message_id)
    return True


def ambiguous_deliveries() -> list[str]:
    """返回发送结果不确定的幂等键，供健康检查告警。"""
    state = _load_state()
    if state.get("_state_file_corrupt") is True:
        return ["notify-ledger:corrupt"]
    deliveries = state.get("deliveries") or {}
    if not isinstance(deliveries, dict):
        return []
    return sorted(key for key, row in deliveries.items()
                  if isinstance(row, dict)
                  and row.get("status") in {"sending", "delivery_unknown"})


def _mark_pending(stream_id: int, kind: str, error: str) -> None:
    """记录通知失败，供 watcher 健康检查低频自动重试。"""
    with _notify_lock:
        st = _load_state()
        key = "pending_card_streams" if kind == "card" else "pending_bitable_streams"
        pending = st.get(key)
        if not isinstance(pending, dict):
            pending = st[key] = {}
        old = pending.get(str(stream_id)) if isinstance(pending, dict) else None
        pending[str(stream_id)] = {
            "attempts": int((old or {}).get("attempts", 0)) + 1,
            "error": str(error)[:300],
        }
        _save_state(st)


def _already_notified(stream_id: int, kind: str = "card") -> bool:
    with _notify_lock:
        st = _load_state()
        key = "notified_card_streams" if kind == "card" else "notified_bitable_streams"
        # 兼容旧状态：notified_streams 表示旧逻辑已完成卡片+多维表格。
        return (stream_id in st.get(key, []) or
                stream_id in st.get("notified_streams", []))


def retry_pending_notifications(cfg: dict, store, limit: int = 10) -> int:
    """重试状态文件中失败的卡片/多维表格通知，返回本轮尝试数。

    单场次累计重试超过 MAX_NOTIFY_RETRY_ATTEMPTS 次仍失败则停止自动重试，
    保留记录待人工处理（防止永远推不出去的旧卡迟到大爆发）。
    """
    state = _load_state()
    card = state.get("pending_card_streams") or {}
    bitable = state.get("pending_bitable_streams") or {}
    if not isinstance(card, dict):
        card = {}
    if not isinstance(bitable, dict):
        bitable = {}
    # v3 起本地 stream 永不发送正式卡。升级前遗留的碎片卡失败项只归档，
    # 防止它先占用 review:{liveId} 幂等键，反过来吞掉真正的整场卡。
    if card:
        with _notify_lock:
            latest = _load_state()
            retired = latest.setdefault("retired_fragment_card_streams", {})
            retired.update(card)
            latest["pending_card_streams"] = {}
            _save_state(latest)
        log.warning("已停用 %d 条旧版碎片复盘卡重试；正式复盘仅由 liveId 终结器发送", len(card))
        card = {}
    ids: set[int] = set()
    for sid in (*card.keys(), *bitable.keys()):
        sid_str = str(sid)
        if not sid_str.isdigit():
            continue
        attempts = max(
            int((card.get(sid) or {}).get("attempts", 0)),
            int((bitable.get(sid) or {}).get("attempts", 0)),
        )
        if attempts > MAX_NOTIFY_RETRY_ATTEMPTS:
            log.warning("场次 #%s 通知已重试 %d 次仍失败，停止自动重试，保留记录待人工处理",
                        sid_str, attempts)
            continue
        ids.add(int(sid))
    ids = sorted(ids)[:max(0, int(limit))]
    attempted = 0
    for stream_id in ids:
        try:
            notify_stream(cfg, store, stream_id, notify_card=False)
            attempted += 1
        except Exception:
            log.exception("通知重试异常：场次 #%d", stream_id)
    return attempted

# 多维表格字段定义（首次建表用）
BITABLE_NAME = "淘宝直播AI巡检统计"
TABLE_NAME = "直播场次"
FIELDS = [
    {"name": "场次", "type": "text"},
    {"name": "主播", "type": "text"},
    {"name": "日期", "type": "text"},
    {"name": "开始时间", "type": "text"},
    {"name": "结束时间", "type": "text"},
    {"name": "时长分钟", "type": "number"},
    {"name": "转写句数", "type": "number"},
    {"name": "转写字数", "type": "number"},
    {"name": "高亮数", "type": "number"},
    {"name": "新增话术", "type": "number"},
    {"name": "开场", "type": "number"},
    {"name": "逼单", "type": "number"},
    {"name": "催付", "type": "number"},
    {"name": "福利", "type": "number"},
    {"name": "互动", "type": "number"},
    {"name": "产品", "type": "number"},
    {"name": "答疑", "type": "number"},
    {"name": "录像路径", "type": "text"},
]
CATEGORIES = ("开场", "逼单", "催付", "福利", "互动", "产品", "答疑")

FEEDBACK_TABLE_NAME = "高亮话术反馈"
FEEDBACK_FIELDS = [
    {"name": "唯一键", "type": "text"},
    {"name": "平台场次", "type": "text"},
    {"name": "本地场次", "type": "text"},
    {"name": "主播", "type": "text"},
    {"name": "时间", "type": "text"},
    {"name": "类型", "type": "select", "multiple": False,
     "options": [{"name": "数据关联"}, {"name": "优质话术"}]},
    {"name": "类别", "type": "text"},
    {"name": "质量分", "type": "number"},
    {"name": "原话", "type": "text"},
    {"name": "入选依据", "type": "text"},
    {"name": "数据证据", "type": "text"},
    {"name": "人工评价", "type": "select", "multiple": False,
     "options": [{"name": "待评"}, {"name": "好"}, {"name": "一般"}, {"name": "误判"}]},
    {"name": "反馈备注", "type": "text"},
]


# ---------- lark-cli 封装 ----------
def _run(args: list[str], timeout: int = 60) -> dict:
    """执行 lark-cli，返回解析后的 JSON"""
    try:
        from ..lark_cli import LarkCliError, run_lark_cli
        return run_lark_cli(
            args, cwd=Path(__file__).resolve().parents[2], timeout=timeout)
    except LarkCliError as exc:
        if exc.retryable:
            if exc.error_class == "transport_timeout":
                raise DeliveryUnknownError(
                    "transport_timeout", error_type="TimeoutExpired") from None
            raise DeliveryUnknownError(
                "receipt" if exc.error_class == "protocol" else "transport") from None
        raise ExplicitRemoteRejection() from None


def _find_value(obj, keys: tuple[str, ...]):
    if isinstance(obj, dict):
        for key in keys:
            value = obj.get(key)
            if value not in (None, ""):
                return value
        for value in obj.values():
            found = _find_value(value, keys)
            if found not in (None, ""):
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_value(value, keys)
            if found not in (None, ""):
                return found
    return None


def _field_names(payload: dict) -> set[str]:
    names: set[str] = set()

    def walk(obj) -> None:
        if isinstance(obj, dict):
            if ("field_id" in obj or "type" in obj) and obj.get("name"):
                names.add(str(obj["name"]))
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(payload)
    return names


def _verify_writable_fields(token: str, table_id: str, required: set[str]) -> None:
    payload = _run([
        "base", "+field-list", "--as", "user", "--base-token", token,
        "--table-id", table_id, "--format", "json", "--limit", "200",
    ])
    actual = _field_names(payload)
    missing = sorted(required - actual)
    if missing:
        raise RuntimeError("多维表格缺少可写字段：" + "、".join(missing))


# ---------- 多维表格 ----------
def ensure_bitable(cfg: dict) -> tuple[str, str]:
    """确保多维表格存在，返回 (app_token, table_id)；不存在则自动创建"""
    state = _load_state()
    token = state.get("bitable_app_token", "")
    table_id = state.get("bitable_table_id", "")
    if token and table_id:
        return token, table_id

    r = _run(["base", "+base-create", "--as", "user",
              "--name", BITABLE_NAME,
              "--table-name", TABLE_NAME,
              "--fields", json.dumps(FIELDS, ensure_ascii=False),
              "--time-zone", "Asia/Shanghai"])
    data = r.get("data") or r
    base = data.get("base") or {}
    token = str(base.get("base_token") or data.get("app_token") or data.get("token") or "")
    # 首个表的 table_id（建表响应里的 table 或 default_table）
    tbl = data.get("table") or data.get("default_table") or {}
    table_id = str(tbl.get("table_id") or tbl.get("id") or "")
    if not token or not table_id:
        raise RuntimeError(f"创建多维表格失败，响应: {json.dumps(r, ensure_ascii=False)[:400]}")

    state["bitable_app_token"] = token
    state["bitable_table_id"] = table_id
    _save_state(state)
    log.info("已创建多维表格: %s（标识已写入本机状态文件）", BITABLE_NAME)
    return token, table_id


def append_stream_record(cfg: dict, summary: dict) -> None:
    """把一场直播统计按 日期+开始时间 幂等写入多维表格。"""
    token, table_id = ensure_bitable(cfg)
    fields: dict = {
        # 本地库清理后自增 ID 会重置；日期+开始时刻保证多维表格里的展示键仍唯一。
        "场次": f"第 {summary.get('day_seq', 0)} 场",
        "主播": summary["anchor_name"],
        "日期": summary.get("date", ""),
        "开始时间": summary.get("started_at", ""),
        "结束时间": summary.get("ended_at", ""),
        "时长分钟": round(summary.get("duration_min", 0), 1),
        "转写句数": summary.get("sentence_count", 0),
        "转写字数": summary.get("char_count", 0),
        "高亮数": summary.get("highlight_count", 0),
        "新增话术": summary.get("new_talktracks", 0),
        "录像路径": summary.get("video_path", ""),
    }
    for cat in CATEGORIES:
        fields[cat] = summary.get("categories", {}).get(cat, 0)

    _verify_writable_fields(token, table_id, set(fields))
    # 崩溃可能发生在飞书已成功、本地 notified 状态尚未落盘之间；先查精确
    # 业务键再带 record_id 更新，可消除这类远端重复行。
    started_at = str(fields.get("开始时间") or "")
    record_id = ""
    if started_at:
        result = _run([
            "base", "+record-search", "--as", "user", "--base-token", token,
            "--table-id", table_id, "--keyword", started_at,
            "--search-field", "开始时间", "--field-id", "日期",
            "--field-id", "开始时间", "--format", "json", "--limit", "10",
        ])
        for row in _records_from_payload(result):
            remote = row.get("fields") or {}
            if (str(remote.get("开始时间") or "") == started_at and
                    str(remote.get("日期") or "") == str(fields.get("日期") or "")):
                record_id = str(row.get("record_id") or row.get("id") or "")
                break
    args = [
        "base", "+record-upsert", "--as", "user", "--base-token", token,
        "--table-id", table_id, "--json", json.dumps(fields, ensure_ascii=False),
        "--format", "json",
    ]
    if record_id:
        args.extend(["--record-id", record_id])
    _run(args)
    log.info("场次 #%s 已写入多维表格", summary["stream_id"])


def _feedback_enabled(cfg: dict) -> bool:
    nf = (cfg.get("notify", {}) or {}).get("feishu", {}) or {}
    feedback = nf.get("highlight_feedback") or {}
    return bool(feedback.get("enabled")) if isinstance(feedback, dict) else bool(feedback)


def ensure_feedback_table(cfg: dict) -> tuple[str, str]:
    """显式开启 highlight_feedback 后创建/复用反馈表。默认关闭，不触碰线上。"""
    if not _feedback_enabled(cfg):
        raise RuntimeError("高亮话术反馈表未启用")
    state = _load_state()
    token, _stats_table = ensure_bitable(cfg)
    table_id = str(state.get("highlight_feedback_table_id") or "")
    if not table_id:
        result = _run([
            "base", "+table-create", "--as", "user", "--base-token", token,
            "--name", FEEDBACK_TABLE_NAME,
            "--fields", json.dumps(FEEDBACK_FIELDS, ensure_ascii=False),
            "--format", "json",
        ])
        table_id = str(_find_value(result, ("table_id",)) or "")
        if not table_id:
            raise RuntimeError(
                "创建高亮反馈表失败：" + json.dumps(result, ensure_ascii=False)[:300])
        with _notify_lock:
            state = _load_state()
            state["highlight_feedback_table_id"] = table_id
            _save_state(state)
    _verify_writable_fields(token, table_id, {row["name"] for row in FEEDBACK_FIELDS})
    return token, table_id


def _records_from_payload(payload: dict) -> list[dict]:
    """兼容 lark-cli 不同 envelope，抽取带 record_id/fields 的记录。"""
    records: list[dict] = []

    def walk(obj) -> None:
        if isinstance(obj, dict):
            if (obj.get("record_id") or obj.get("id")) and isinstance(obj.get("fields"), dict):
                records.append(obj)
                return
            # record-list / record-search 的一种 CLI 响应是列名、record_id 和
            # 行值分离的二维数组；还原后才能按业务键执行幂等更新。
            columns = obj.get("fields")
            record_ids = obj.get("record_id_list")
            rows = obj.get("data")
            if (isinstance(columns, list) and isinstance(record_ids, list)
                    and isinstance(rows, list)):
                names = [str(name) for name in columns]
                for record_id, values in zip(record_ids, rows):
                    if isinstance(values, list):
                        records.append({
                            "record_id": str(record_id),
                            "fields": dict(zip(names, values)),
                        })
                return
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(payload)
    return records


def _find_feedback_record(token: str, table_id: str, feedback_key: str) -> str:
    result = _run([
        "base", "+record-search", "--as", "user", "--base-token", token,
        "--table-id", table_id, "--keyword", feedback_key,
        "--search-field", "唯一键", "--field-id", "唯一键",
        "--format", "json", "--limit", "10",
    ])
    for row in _records_from_payload(result):
        if str((row.get("fields") or {}).get("唯一键") or "") == feedback_key:
            return str(row.get("record_id") or row.get("id") or "")
    return ""


def build_feedback_records(cfg: dict, summary: dict) -> list[tuple[str, dict]]:
    """构造人工反馈 Base 记录；所有原话先经过统一外发门禁。"""
    from ..asr.clean import outward_record_text
    from ..highlight.quality import quality_text_hash
    rows: list[tuple[str, dict]] = []
    anchors = " / ".join(summary.get("anchor_names") or [summary.get("anchor_name") or ""])
    for kind, items in (("数据关联", summary.get("data_highlights") or []),
                        ("优质话术", summary.get("quality_highlights") or [])):
        for item in items:
            text_body = outward_record_text(
                cfg, item,
                meta_field=("quality_meta" if kind == "优质话术" else "peak_meta"),
                max_chars=220,
            )
            if not text_body:
                continue
            text_hash = quality_text_hash(text_body)
            key = (f"{summary.get('live_id') or 'local'}:{kind}:"
                   f"{item.get('time') or 'na'}:{text_hash}")
            meta = item.get("quality_meta") if kind == "优质话术" else item.get("peak_meta")
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except ValueError:
                    meta = {}
            meta = meta or {}
            if kind == "优质话术":
                category = str(meta.get("category") or "优质话术")
                score = meta.get("quality_score")
                rationale = "、".join(str(x) for x in meta.get("rationale") or [])
                data_evidence = ""
            else:
                category = str(meta.get("label") or meta.get("type") or "数据峰值")
                score = None
                rationale = "数据变化附近的合格原话；关联不代表因果"
                data_evidence = str(item.get("reasons") or "")
            fields = {
                "唯一键": key,
                "平台场次": str(summary.get("live_id") or ""),
                "本地场次": ",".join(str(x) for x in summary.get("scope_stream_ids") or []),
                "主播": anchors,
                "时间": str(item.get("time") or ""),
                "类型": kind,
                "类别": category,
                "质量分": score,
                "原话": text_body,
                "入选依据": rationale,
                "数据证据": data_evidence,
            }
            rows.append((text_hash, fields))
    return rows


def upsert_highlight_feedback(cfg: dict, summary: dict) -> int:
    """写入双榜候选且保留人工列；按唯一键检索后带 record_id 更新。"""
    if not _feedback_enabled(cfg):
        return 0
    token, table_id = ensure_feedback_table(cfg)
    count = 0
    for _text_hash, fields in build_feedback_records(cfg, summary):
        feedback_key = str(fields["唯一键"])
        record_id = _find_feedback_record(token, table_id, feedback_key)
        payload = dict(fields)
        if not record_id:
            payload["人工评价"] = "待评"
            payload["反馈备注"] = ""
        args = [
            "base", "+record-upsert", "--as", "user", "--base-token", token,
            "--table-id", table_id, "--json",
            json.dumps(payload, ensure_ascii=False), "--format", "json",
        ]
        if record_id:
            args.extend(["--record-id", record_id])
        _run(args)
        count += 1
    return count


def sync_highlight_feedback(cfg: dict, store) -> int:
    """读取人工评价并落 SQLite，下一次精选时对同话术加权或排除。"""
    if not _feedback_enabled(cfg):
        return 0
    token, table_id = ensure_feedback_table(cfg)
    from ..highlight.quality import quality_text_hash
    rows = []
    offset = 0
    while True:
        result = _run([
            "base", "+record-list", "--as", "user", "--base-token", token,
            "--table-id", table_id, "--field-id", "唯一键", "--field-id", "原话",
            "--field-id", "人工评价", "--field-id", "反馈备注",
            "--format", "json", "--limit", "200", "--offset", str(offset),
        ])
        records = _records_from_payload(result)
        for record in records:
            fields = record.get("fields") or {}
            rating = str(fields.get("人工评价") or "")
            if rating not in {"待评", "好", "一般", "误判"}:
                continue
            rows.append({
                "feedback_key": str(fields.get("唯一键") or ""),
                "text_hash": quality_text_hash(str(fields.get("原话") or "")),
                "rating": rating,
                "note": str(fields.get("反馈备注") or ""),
            })
        if len(records) < 200:
            break
        offset += len(records)
    return store.save_highlight_feedback(rows)


# ---------- 群卡片 ----------
def send_card(
        cfg: dict, chat_id: str, card: dict | str, *, idempotency_key: str = "",
        serialized_content: str = "",
) -> dict:
    content = (str(serialized_content) if serialized_content
               else card if isinstance(card, str) else _serialize_card(card))
    args = ["im", "+messages-send", "--as", "bot",
            "--chat-id", chat_id,
            "--msg-type", "interactive",
            "--content", content]
    if idempotency_key:
        if len(idempotency_key) > 50:
            raise ValueError("飞书 idempotency key 不得超过 50 字符")
        args.extend(["--idempotency-key", idempotency_key])
    try:
        result = _run(args)
    except ExplicitRemoteRejection:
        raise
    except DeliveryUnknownError:
        raise
    except subprocess.TimeoutExpired:
        raise DeliveryUnknownError(
            "transport_timeout", error_type="TimeoutExpired") from None
    except subprocess.CalledProcessError:
        raise DeliveryUnknownError(
            "transport_process", error_type="CalledProcessError") from None
    except UnicodeError as exc:
        raise DeliveryUnknownError(
            "transport_decode", error_type=type(exc).__name__) from None
    except Exception as exc:
        raise DeliveryUnknownError(
            "transport_error", error_type=type(exc).__name__) from None
    if not _find_value(result, ("message_id", "messageId")):
        raise DeliveryUnknownError("receipt")
    log.info("飞书卡片已推送到配置群")
    return result


def _ai_points(cfg: dict, store, stream_id: int) -> Optional[dict[str, str]]:
    """从数据库读取完整 AI 结构段落。"""
    from ..review.ai_report import extract_points
    review = store.get_review(stream_id)
    if not review or not review["ai_review"]:
        return None
    pts = extract_points(review["ai_review"], preserve_newlines=True)
    return pts or None


def _scope_ai_points(cfg: dict, store, summary: dict) -> Optional[dict[str, str]]:
    """把同一平台场次各班次的 AI 结论带标签汇总，避免末班报告冒充整场。"""
    scope_ids = [int(item) for item in summary.get("scope_stream_ids") or []]
    if not scope_ids:
        scope_ids = [int(summary["stream_id"])]
    rows: list[tuple[str, dict[str, str]]] = []
    for stream_id in scope_ids:
        points = _ai_points(cfg, store, stream_id)
        if not points:
            continue
        stream = store.get_stream(stream_id)
        anchor = store.get_anchor(stream["anchor_id"]) if stream else None
        label = anchor["name"] if anchor else f"班次 #{stream_id}"
        if stream:
            started = str(stream["started_at"] or "")[11:16]
            ended = str(stream["ended_at"] or "")[11:16]
            if started or ended:
                label += f" · {started or '暂无'}–{ended or '暂无'}"
        rows.append((label, points))
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0][1]

    from ..review.ai_report import REPORT_TAGS
    merged: dict[str, str] = {}
    for tag in REPORT_TAGS:
        parts = [f"**{label}**\n{points[tag]}"
                 for label, points in rows if points.get(tag)]
        if parts:
            merged[tag] = "\n\n".join(parts)
    return merged or None


def _metric(value, unit: str = "", decimals: int = 0) -> str:
    if value is None:
        return "暂无"
    return f"{float(value):,.{decimals}f}{unit}"


def card_v2(title: str, template: str, elements: list[dict], *,
            subtitle: str = "", status: str = "", status_color: str = "blue",
            icon: str = "chart_colorful") -> dict:
    """构造共享 Card 2.0 根结构，所有只读通知卡复用此入口。"""
    header: dict = {
        "title": {"tag": "plain_text", "content": title},
        "template": template,
        "icon": {"tag": "standard_icon", "token": icon},
    }
    if subtitle:
        header["subtitle"] = {"tag": "plain_text", "content": subtitle}
    if status:
        header["text_tag_list"] = [{
            "tag": "text_tag",
            "text": {"tag": "plain_text", "content": status},
            "color": status_color,
        }]
    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "width_mode": "default",
            "summary": {"content": title},
        },
        "header": header,
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 20px 12px",
            "vertical_spacing": "12px",
            "elements": elements,
        },
    }


def metric_column(label: str, value: str, *, focus: bool = False,
                  color: str = "blue") -> dict:
    number = f"## <font color='{color}'>{value}</font>" if focus else f"**{value}**"
    return {
        "tag": "column",
        "width": "weighted",
        "weight": 2 if focus else 1,
        "background_style": f"{color}-50" if focus else "grey-50",
        "padding": "12px",
        "vertical_spacing": "2px",
        "elements": [
            {"tag": "markdown", "content": number, "text_align": "center"},
            {"tag": "markdown", "content": f"<font color='grey'>{label}</font>",
             "text_align": "center", "text_size": "notation"},
        ],
    }


def metric_grid(metrics: list[tuple[str, str]], *, focus_index: int | None = None,
                color: str = "blue") -> dict:
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "horizontal_spacing": "8px",
        "columns": [
            metric_column(label, value, focus=index == focus_index, color=color)
            for index, (label, value) in enumerate(metrics)
        ],
    }


def collapsible_text(title: str, content: str, *, expanded: bool = False) -> dict:
    return {
        "tag": "collapsible_panel",
        "expanded": expanded,
        "header": {"title": {"tag": "plain_text", "content": title}},
        "border": {"color": "grey-200", "corner_radius": "6px"},
        "padding": "8px 12px 12px 12px",
        "elements": [{"tag": "markdown", "content": content or "暂无"}],
    }


def section_panel(title: str, content: str, *, color: str = "grey",
                  kicker: str = "") -> dict:
    """共享内容面板：统一留白、色块和信息层级。"""
    title_text = f"<font color='{color}'>{kicker}</font>  **{title}**" if kicker else f"**{title}**"
    return {
        "tag": "column_set", "flex_mode": "none",
        "columns": [{
            "tag": "column", "width": "weighted", "weight": 1,
            "background_style": f"{color}-50" if color != "grey" else "grey-50",
            "padding": "14px", "vertical_spacing": "6px",
            "elements": [
                {"tag": "markdown", "content": title_text},
                {"tag": "markdown", "content": content or "暂无"},
            ],
        }],
    }


def _data_state(metrics: dict) -> tuple[str, str]:
    state = str(metrics.get("data_state") or "unknown")
    if state == "ok":
        return "数据完整", "blue"
    if state == "partial":
        return "部分数据", "yellow"
    if state == "unavailable":
        return "数据不可用", "red"
    return "数据状态未知", "neutral"


def _peak_blocks(cfg: dict, highlights: list[dict], max_items: int = 3,
                 per_peak: int = 3) -> str:
    """优先展示结构化关联；旧数据或低质量数据只展示峰值窗口。

    max_items=最多展示的峰值个数；per_peak=每个峰值最多展示的话术条数
    （2026-08-04 用户要求：每个时间点多展示几句）。
    """
    from ..asr.clean import (OUTWARD_DISPLAY_REPAIRED,
                             outward_display_text,
                             persisted_display_record_text)
    from ..highlight.peak import parse_peak_meta

    def _relative_hms(ms) -> str:
        if ms in (None, ""):
            return "时间暂无"
        try:
            hours, rem = divmod(max(int(ms), 0), 3_600_000)
            minutes, _sec = divmod(rem, 60_000)
            return f"{hours:02d}:{minutes:02d}"
        except (TypeError, ValueError):
            return "时间暂无"

    lines: list[str] = []
    for highlight in highlights[:max_items]:
        meta = parse_peak_meta(highlight.get("peak_meta"))
        quality = meta.get("quality") or {}
        evidence: list[str] = []
        peak_time = str(meta.get("minute") or "")
        if "T" in peak_time:
            peak_time = peak_time.split("T", 1)[1][:5]
        value = meta.get("value")
        value_text = (_metric(value, str(meta.get("unit") or ""),
                              2 if meta.get("type") == "deal" else 0)
                      if value is not None else "暂无")
        for excerpt in (meta.get("excerpts") or [])[:per_peak]:
            provenance = str(excerpt.get("text_provenance") or
                             meta.get("text_provenance") or "")
            body = (outward_display_text(
                str(excerpt.get("text") or ""), cfg,
                provenance=OUTWARD_DISPLAY_REPAIRED, max_chars=180)
                if provenance == OUTWARD_DISPLAY_REPAIRED else "")
            if not body:
                continue
            relation = excerpt.get("relation")
            lag = int(round(float(excerpt.get("lag_sec") or 0)))
            if relation == "同分钟":
                peak_kind = {"deal": "成交", "itemClick": "点击", "uv": "在线"}.get(
                    meta.get("type"), "数据")
                timing = (f"该话术与 {peak_time or '时间暂无'} 的{peak_kind}峰值"
                          f"（{value_text}）发生在同一分钟内，先后无法确定")
            else:
                timing = f"话术结束后 {lag} 秒观察到 {peak_time or '时间暂无'} 峰值 {value_text}"
            evidence.append(
                f"[{excerpt.get('start') or '时间暂无'}] {body}\n{timing}"
            )
        if quality.get("association_allowed") and evidence:
            lines.extend(evidence)
        else:
            # 旧数据/低质量数据：展示峰值窗口 + 通过展示门禁的摘录；无可用摘录则不占行
            start = highlight.get("time") or _relative_hms(highlight.get("start_ms"))
            end = highlight.get("end_time") or _relative_hms(highlight.get("end_ms"))
            reason = highlight.get("reasons") or "数据峰值"
            excerpts: list[str] = []
            body = persisted_display_record_text(
                cfg, highlight, meta_field="peak_meta", max_chars=360)
            if body:
                excerpts.append(f"- {body}")
            if excerpts:
                lines.append(
                    f"**{reason}**\n峰值窗口 {start} ~ {end}；暂无合格时间关联\n"
                    + "\n".join(excerpts)
                )
        if len(lines) >= max_items * per_peak:
            break  # 软上限：防止异常数据撑爆卡片
    return "\n\n".join(lines[:max_items * per_peak]) if lines else "（本场暂无可引用的峰值话术）"


def _brief_peak_blocks(cfg: dict, highlights: list[dict],
                       max_items: int = 3, *,
                       content_is_repaired: bool = False) -> str:
    """简报详细高亮：原话、内容入选理由、数据观察三层分开呈现。

    峰值列表本身已按成交/点击/在线做多样性排序，因此先从每个峰值取一条，
    不让单个成交峰值的多句摘录挤掉另外两类证据。只有不足 ``max_items``
    时才从同峰值补第二条。内容理由复用质量候选规则，不能用经营结果倒推。
    """
    from ..asr.clean import (DISPLAY_FALLBACK, can_show_raw_excerpt,
                             prepare_display_text, truncate_display_text)
    from ..highlight.peak import parse_peak_meta
    from ..highlight.quality import quality_candidate

    type_meta = {
        "deal": ("成交峰值关联", "red"),
        "itemClick": ("点击峰值关联", "carmine"),
        "uv": ("在线峰值关联", "orange"),
    }

    def display_text(raw: str) -> str:
        if content_is_repaired:
            body = truncate_display_text(str(raw or "").strip(), max_chars=180)
            return body if body != DISPLAY_FALLBACK and can_show_raw_excerpt(body) else ""
        body = prepare_display_text(raw, cfg, max_chars=180)
        if body == DISPLAY_FALLBACK:
            return ""
        return body

    def rationale_for(excerpt: dict, body: str) -> str:
        try:
            candidate = quality_candidate(
                int(excerpt.get("start_ms") or 0),
                int(excerpt.get("end_ms") or excerpt.get("start_ms") or 0),
                body,
            )
        except (TypeError, ValueError):
            candidate = None
        reasons = (candidate or {}).get("rationale") or []
        return " · ".join(str(reason) for reason in reasons[:3]) or "符合内容质量门禁"

    candidates: list[tuple[dict, dict, bool]] = []
    parsed: list[tuple[dict, dict, list[dict], bool]] = []
    for highlight in highlights or []:
        meta = parse_peak_meta(highlight.get("peak_meta"))
        quality = meta.get("quality") or {}
        association_allowed = quality.get("association_allowed") is True
        excerpts = list(
            (meta.get("excerpts") if association_allowed
             else meta.get("nearby_excerpts")) or [])
        nearby_only = not association_allowed
        parsed.append((highlight, meta, excerpts, nearby_only))
        if excerpts:
            candidates.append((meta, excerpts[0], nearby_only))
    if len(candidates) < max_items:
        for _highlight, meta, excerpts, nearby_only in parsed:
            for excerpt in excerpts[1:]:
                candidates.append((meta, excerpt, nearby_only))
                if len(candidates) >= max_items:
                    break
            if len(candidates) >= max_items:
                break

    blocks: list[str] = []
    for meta, excerpt, nearby_only in candidates:
        body = display_text(str(excerpt.get("text") or ""))
        if not body:
            continue
        index = len(blocks) + 1
        typ = str(meta.get("type") or "")
        peak_label, peak_color = type_meta.get(typ, ("数据峰值关联", "blue"))
        event_label, color = (
            ("峰值附近完整原话", "grey") if nearby_only
            else (peak_label, peak_color))
        peak_time = str(meta.get("minute") or "")
        if "T" in peak_time:
            peak_time = peak_time.split("T", 1)[1][:5]
        value = meta.get("value")
        value_text = (_metric(value, str(meta.get("unit") or ""),
                              2 if typ == "deal" else 0)
                      if value is not None else "暂无")
        relation = str(excerpt.get("relation") or "")
        if nearby_only and relation == "同分钟":
            observation = (
                f"该原话与 {peak_time or '时间暂无'} 的{peak_label.removesuffix('关联')}"
                "同一分钟（按时间接近选取）"
            )
        elif nearby_only:
            lag = int(round(float(excerpt.get("lag_sec") or 0)))
            observation = (
                f"该原话结束于 {peak_time or '时间暂无'} 的{peak_label.removesuffix('关联')}"
                f"前 {lag} 秒（按时间接近选取）"
            )
        elif relation == "同分钟":
            observation = (
                f"该话术与 {peak_time or '时间暂无'} 的{peak_label.removesuffix('关联')}"
                f"（{value_text}）发生在同一分钟，先后无法确定"
            )
        else:
            lag = int(round(float(excerpt.get("lag_sec") or 0)))
            observation = (
                f"话术结束后 {lag} 秒观察到 {peak_time or '时间暂无'} "
                f"{peak_label.removesuffix('关联')} {value_text}"
            )
        blocks.append(
            f"**<font color='{color}'>{index:02d} · {event_label} · "
            f"{excerpt.get('start') or '时间暂无'}</font>**\n"
            f"> {body}\n"
            f"**入选理由**  "
            f"{'峰值前后 3 分钟内的完整原话' if nearby_only else rationale_for(excerpt, body)}\n"
            f"**数据观察**  {observation}"
        )
        if len(blocks) >= max_items:
            break

    if not blocks:
        return ""
    return ("\n\n".join(blocks[:max_items])
            + "\n\n<font color='grey'>以上仅为时间先后关系，关联不代表因果。</font>")


def _quality_blocks(cfg: dict, highlights: list[dict], max_items: int = 5, *,
                    content_is_repaired: bool = False,
                    empty_text: str = "（本场暂无达到质量线的可复用话术）") -> str:
    """优质话术榜：只展示原话/补全版、类别、质量分和可审计入选点。"""
    from ..asr.clean import persisted_display_record_text
    from ..highlight.quality import quality_display_text

    blocks: list[str] = []
    for highlight in highlights:
        raw_meta = highlight.get("quality_meta") or {}
        if isinstance(raw_meta, str):
            try:
                raw_meta = json.loads(raw_meta)
            except ValueError:
                raw_meta = {}
        body = (quality_display_text(
            cfg, highlight, max_chars=220, assume_repaired=True)
            if content_is_repaired else persisted_display_record_text(
                cfg, highlight, meta_field="quality_meta", max_chars=220))
        if not body:
            continue
        body = " ".join(str(body).split())
        index = len(blocks) + 1
        category = str(raw_meta.get("category") or "优质话术")
        score = raw_meta.get("quality_score")
        score_text = f"{int(round(float(score)))} 分" if score is not None else "已入选"
        rationale = " · ".join(str(item) for item in (raw_meta.get("rationale") or [])[:3])
        time_text = str(highlight.get("time") or "时间暂无")
        block = (
            f"**{index:02d}  {category}  ·  {score_text}**\n"
            f"<font color='grey'>{time_text}{('  ·  ' + rationale) if rationale else ''}</font>\n"
            f"> {body}"
        )
        blocks.append(block)
        if len(blocks) >= max_items:
            break
    return "\n\n".join(blocks) if blocks else empty_text


_INTELLIGENCE_METRIC_LABELS = {
    "deal": "成交金额",
    "pay_amt": "成交金额",
    "itemClick": "商品点击",
    "ipv_total": "商品点击",
    "uv": "在线人数",
    "online_uv": "在线人数",
    "heatScore": "互动热度",
    "script_completeness": "话术结构完整度",
}


def _intelligence_card_text(value: object, *, max_chars: int) -> str:
    """Return one complete model field or omit it without truncation."""
    body = " ".join(str(value or "").split()).strip()
    if (not body or len(body) > max_chars
            or body.endswith(("，", ",", "、", "：", ":"))
            or "…" in body or "..." in body):
        return ""
    return body


def _brief_intelligence_talktrack_blocks(
        intelligence: object, max_items: int = 3) -> list[str]:
    """Render validated talktracks; model rewrites are explicitly labelled."""
    rows = getattr(intelligence, "reusable_talktracks", None) or []
    blocks: list[str] = []
    for row in rows:
        original = _intelligence_card_text(
            getattr(row, "original_text", ""), max_chars=220)
        reusable = _intelligence_card_text(
            getattr(row, "reusable_script", ""), max_chars=220)
        scene = _intelligence_card_text(getattr(row, "scene", ""), max_chars=60)
        if not original or not reusable or not scene:
            continue
        block = (
            f"**{len(blocks) + 1:02d} · {scene}**\n"
            f"**主播原话**  {original}\n"
            f"**可直接复用**  {reusable}"
        )
        if len(block) > 560:
            continue
        blocks.append(block)
        if len(blocks) >= max_items:
            break
    return blocks


def _brief_intelligence_action_blocks(
        intelligence: object, max_items: int = 3) -> list[str]:
    """Render next-hour suggestions as analyst advice: what + why.

    建议区展示 DeepSeek 基于整轮逐字稿与数据的分析建议：动作（action）
    与依据（issue），不再展示实验式字段（2026-08-08 生产反馈改版）。
    """
    rows = getattr(intelligence, "action_experiments", None) or []
    blocks: list[str] = []
    for row in rows:
        action = _intelligence_card_text(getattr(row, "action", ""), max_chars=220)
        issue = _intelligence_card_text(getattr(row, "issue", ""), max_chars=220)
        if not action:
            continue
        block = (
            f"**{len(blocks) + 1:02d} · 建议**\n"
            f"**做什么**  {action}\n"
            f"**依据**  {issue}"
        )
        if len(block) > 560:
            continue
        blocks.append(block)
        if len(blocks) >= max_items:
            break
    return blocks


def _brief_intelligence_conclusion_blocks(
        intelligence: object, max_items: int = 3) -> list[str]:
    """Render DeepSeek business conclusions separately from Minutes content."""
    analyst_rows = getattr(intelligence, "business_conclusions", None) or []
    rows = analyst_rows or (getattr(intelligence, "observations", None) or [])
    blocks: list[str] = []
    for row in rows:
        statement = _intelligence_card_text(
            row if isinstance(row, str) else getattr(row, "statement", ""),
            max_chars=320,
        )
        if not statement:
            continue
        blocks.append(f"**{len(blocks) + 1:02d} · 经营结论**\n{statement}")
        if len(blocks) >= max_items:
            break
    return blocks


def _ai_full_text(points: dict[str, str], tags: tuple[str, ...]) -> str:
    return "\n\n".join(
        f"**{tag}**\n{points[tag]}" for tag in tags if points.get(tag)
    )


def _card_text_limit(text: str, limit: int = 2200) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n\n<font color='grey'>完整正文见本地整场 Markdown 报告。</font>"


def _whole_platform_card_item(text: object, limit: int) -> str:
    """平台正式卡只保留完整项；超限整项舍弃，绝不切半句。"""
    value = str(text or "").strip()
    return value if value and len(value.encode("utf-8")) <= max(1, int(limit)) else ""


def _platform_intelligence_card_sections(summary: dict) -> tuple[str, str]:
    """从冻结结构中抽取少量辅助分析与最多三个行动。"""
    from ..intelligence.platform import bound_platform_intelligence_from_summary

    try:
        result, _binding = bound_platform_intelligence_from_summary(summary)
    except (TypeError, ValueError):
        return "", ""

    analysis_blocks: list[str] = []

    def add(block: str, *, limit: int = 760) -> None:
        item = _whole_platform_card_item(block, limit)
        if item and len("\n\n".join([*analysis_blocks, item]).encode("utf-8")) <= 3_600:
            analysis_blocks.append(item)

    for index, item in enumerate(result.business_conclusions[:3], 1):
        add(f"**{index:02d} · 经营结论**  {item}")

    action_blocks = [
        f"**{index:02d} · 下一场动作**\n{item}"
        for index, item in enumerate(result.next_actions[:3], 1)
        if _whole_platform_card_item(item, 760)
    ]
    return "\n\n".join(analysis_blocks), "\n\n".join(action_blocks)


def _anchor_person_metric(metrics: dict, key: str, label: str) -> tuple[str, str]:
    """人数优先展示去重值；多片段仅能展示明确标注的分段人次。"""
    value = metrics.get(key)
    if value is not None:
        return label, _metric(value, " 人")
    segment_sum = metrics.get(f"{key}_segment_sum")
    if segment_sum is not None:
        segment_label = label.replace("人数", "人次")
        return f"{segment_label}（分段累计，非去重）", _metric(segment_sum, " 人次")
    return label, "暂无"


def _round_robin_anchor_items(anchors: list[dict], key: str,
                              limit: int = 3) -> list[tuple[dict, dict]]:
    selected: list[tuple[dict, dict]] = []
    offset = 0
    while len(selected) < limit:
        added = False
        for anchor in anchors:
            rows = anchor.get(key) or []
            if offset < len(rows):
                selected.append((anchor, rows[offset]))
                added = True
                if len(selected) >= limit:
                    break
        if not added:
            break
        offset += 1
    return selected


def _split_numbered_actions(text: str) -> list[str]:
    body = str(text or "").strip()
    markers = list(re.finditer(r"(?m)^\s*\d+[.、]\s*", body))
    if not markers:
        return [body] if body else []
    return [body[marker.end():(markers[index + 1].start()
                              if index + 1 < len(markers) else len(body))].strip()
            for index, marker in enumerate(markers)]


def _platform_signal_block(cfg: dict, item: dict) -> str:
    """以“原话”为主键展示该句关联的全部数据观察，避免同句重复。"""
    from ..asr.clean import persisted_display_record_text

    body = persisted_display_record_text(
        cfg, item, meta_field="peak_meta", max_chars=180,
        placeholder="【待回听确认】",
    )
    observations: list[str] = []
    labels = {"deal": "成交", "itemClick": "商品点击", "uv": "在线"}
    for meta in item.get("peak_observations") or [item.get("peak_meta") or {}]:
        if not isinstance(meta, dict) or not meta:
            continue
        minute = str(meta.get("minute") or "")
        if "T" in minute:
            minute = minute.split("T", 1)[1][:5]
        value = meta.get("value")
        value_text = (_metric(value, str(meta.get("unit") or ""),
                              2 if meta.get("type") == "deal" else 0)
                      if value is not None else "暂无")
        observation = f"{labels.get(meta.get('type'), '数据')} {minute or '时间暂无'} {value_text}"
        if observation not in observations:
            observations.append(observation)
    reason = "；".join(str(value) for value in item.get("reasons") or []) or "数据峰值关联"
    return (
        f"**{item.get('clock') or item.get('time') or '时间暂无'}**  {body}\n"
        f"**入选理由**  {reason}\n"
        f"**数据观察**  {'；'.join(observations) if observations else '暂无结构化观察'}"
    )


def build_platform_review_card(cfg: dict, summary: dict) -> dict:
    """一个经营日一张日报卡；完整 DeepSeek 分析留在同源 Markdown。"""
    from ..review.report import _fmt_duration

    platform = summary.get("display_metrics") or {}
    anchor_rows = summary.get("anchors") or []
    states = [str(platform.get("data_state") or "unavailable")]
    states.extend(str((row.get("metrics") or {}).get("data_state") or "unavailable")
                  for row in anchor_rows)
    summary_state = str(summary.get("data_state") or "")
    if summary_state in {"partial", "unavailable"}:
        status, status_color = (("部分数据", "yellow") if summary_state == "partial"
                                else ("数据不可用", "red"))
    elif states and all(state == "ok" for state in states):
        status, status_color = "数据完整", "green"
    elif any(state == "ok" for state in states):
        status, status_color = "部分数据", "yellow"
    else:
        status, status_color = "数据不可用", "red"

    comparison = [
        "| 主播 | 上钟 | 成交金额 | 观看口径 | 成交口径 | 单 / 件 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for anchor in anchor_rows:
        metrics = anchor.get("metrics") or {}
        _look_label, look_value = _anchor_person_metric(metrics, "look_uv", "观看人数")
        _buyer_label, buyer_value = _anchor_person_metric(
            metrics, "pay_byr_cnt", "成交人数")
        duration = float(metrics.get("on_air_duration_sec")
                         or anchor.get("duration_sec") or 0)
        comparison.append(
            f"| {anchor.get('anchor_name') or '未知主播'} | {_fmt_duration(duration)} | "
            f"{_metric(metrics.get('pay_amt'), ' 元', 2)} | {look_value} | {buyer_value} | "
            f"{_metric(metrics.get('pay_ord_cnt'), ' 单')} / "
            f"{_metric(metrics.get('pay_itm_qty'), ' 件')} |"
        )

    intelligence_analysis, intelligence_actions = (
        _platform_intelligence_card_sections(summary))

    hourly_rows = [
        "| 时段 | 主播 | 成交金额 | 新增观看 | 平均在线 |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    # 24 个整点窗口 + 1 个真实下播尾段。
    for row in (summary.get("hourly_trends") or [])[:25]:
        try:
            start = datetime.fromtimestamp(
                int(row.get("window_start_ms") or 0) / 1000, SHANGHAI)
            end = datetime.fromtimestamp(
                int(row.get("window_end_ms") or 0) / 1000, SHANGHAI)
            period = f"{start:%H:%M}–{end:%H:%M}"
        except (TypeError, ValueError, OSError):
            period = "时间暂无"
        hourly_rows.append(
            f"| {period} | {row.get('anchor_name') or '未知主播'} | "
            f"{_metric(row.get('pay_amt'), ' 元', 2)} | "
            f"{_metric(row.get('viewer_uv'), ' 人')} | "
            f"{_metric(row.get('avg_online'), ' 人', 1)} |"
        )

    golden_blocks: list[str] = []
    for anchor in anchor_rows:
        name = anchor.get("anchor_name") or "未知主播"
        for item in anchor.get("smart_minutes") or []:
            url = str(item.get("minute_url") or "")
            for raw_quote in item.get("golden_quotes") or []:
                quote = _whole_platform_card_item(str(raw_quote), 360)
                if not quote:
                    continue
                link = f"  [查看妙记]({url})" if url else ""
                golden_blocks.append(
                    f"**{len(golden_blocks) + 1:02d} · {name}**\n> {quote}{link}")
                if len(golden_blocks) >= 3:
                    break
            if len(golden_blocks) >= 3:
                break
        if len(golden_blocks) >= 3:
            break

    elements: list[dict] = [
        {"tag": "markdown", "content":
         f"<font color='grey'>经营日 {summary.get('business_session_key') or '日期暂无'} "
         f"· {len(anchor_rows)} 位主播</font>\n"
         f"**{_fmt_duration(float(summary.get('duration_sec') or 0))}**  ·  "
         f"{summary.get('started_at') or '开始时间暂无'} ~ "
         f"{summary.get('ended_at') or '结束时间暂无'}"},
        metric_grid([
            ("当日成交金额", _metric(platform.get("pay_amt"), " 元", 2)),
            ("当日观看口径", _metric(
                platform.get("viewer_uv") or platform.get("viewer_uv_segment_sum"), " 人次")),
            ("当日成交订单", _metric(platform.get("order_cnt"), " 单")),
        ], focus_index=0, color="blue"),
        metric_grid([
            ("当日成交人数", _metric(platform.get("buyer_cnt"), " 人")),
            ("当日成交件数", _metric(platform.get("item_qty"), " 件")),
            ("最高在线", _metric(platform.get("max_online_uv"), " 人")),
        ], color="blue"),
    ]

    if intelligence_analysis:
        elements.append(section_panel(
            "DeepSeek 全日经营结论", intelligence_analysis,
            color="purple", kicker="DAILY ANALYSIS",
        ))
    elements.append(section_panel(
        "各主播经营表现对比",
        "\n".join(comparison) if anchor_rows else "暂无主播经营数据",
        color="blue", kicker="ANCHORS",
    ))
    if len(hourly_rows) > 2:
        elements.append(section_panel(
            "小时经营趋势", "\n".join(hourly_rows),
            color="blue", kicker="HOURLY TREND",
        ))
    if golden_blocks:
        elements.append(section_panel(
            "飞书妙记金句（最多 3 条）", "\n\n".join(golden_blocks),
            color="wathet", kicker="SMART MINUTES",
        ))
    if intelligence_actions:
        elements.append(section_panel(
            "下一场行动（最多 3 项）", intelligence_actions,
            color="orange", kicker="NEXT",
        ))

    issues = [str(item) for item in (summary.get("data_issues") or []) if str(item)]
    elements.extend([
        {"tag": "hr"},
        section_panel(
            "数据来源与异常",
            (("\n".join(f"- {item}" for item in issues) + "\n\n") if issues else "")
            + "平台冻结总账、主播上下钟官方指标、小时冻结事实和飞书妙记共同构成日报；"
              "缺失显示“暂无”，分段人次不冒充去重人数，相关关系不冒充因果。",
            color="grey", kicker="SOURCE",
        ),
    ])

    subtitle = (f"经营日口径 · {summary.get('started_at') or '开始暂无'} ~ "
                f"{summary.get('ended_at') or '结束暂无'}")
    return card_v2(
        "直播经营日报", "blue", elements, subtitle=subtitle,
        status=status, status_color=status_color, icon="chart_colorful",
    )


def build_stream_card(cfg: dict, store, summary: dict) -> dict:
    """Card 2.0 整场复盘：单焦点 KPI + 双榜话术 + 行动区。"""
    from ..review.ai_report import REPORT_TAGS

    metrics = summary.get("display_metrics") or {}
    status, status_color = _data_state(metrics)
    evidence_text = _peak_blocks(cfg, summary.get("data_highlights") or [], max_items=3)
    quality_text = _quality_blocks(cfg, summary.get("quality_highlights") or [], max_items=5)

    primary = metric_grid([
        ("成交金额", _metric(metrics.get("pay_amt"), " 元", 2)),
        ("观看人数", _metric(metrics.get("viewer_uv"), " 人")),
        ("最高在线", _metric(metrics.get("max_online_uv"), " 人")),
    ], focus_index=0, color="blue")
    secondary = metric_grid([
        ("成交人数", _metric(metrics.get("buyer_cnt"), " 人")),
        ("成交订单", _metric(metrics.get("order_cnt"), " 单")),
        ("成交件数", _metric(metrics.get("item_qty"), " 件")),
    ], color="blue")
    kpi_block = {
        "tag": "column_set",
        "flex_mode": "none",
        "columns": [{
            "tag": "column", "width": "weighted", "weight": 1,
            "direction": "vertical", "vertical_spacing": "8px",
            "elements": [primary, secondary],
        }],
    }

    ai = _scope_ai_points(cfg, store, summary) or {}
    action = ai.get("下一场行动") or "暂无结构化行动建议"
    # 下一场行动已在首屏单独展示，折叠区不再重复
    folded_tags = tuple(tag for tag in REPORT_TAGS if tag != "下一场行动")
    full_ai = _ai_full_text(ai, folded_tags) if ai else "暂无 AI 复盘正文"
    scope_label = (f"整场 · {summary.get('scope_count', 1)} 个班次 · "
                   + " / ".join(summary.get("anchor_names") or [summary["anchor_name"]]))
    elements = [
        {"tag": "markdown", "content":
         f"<font color='grey'>{scope_label}</font>\n"
         f"**{summary.get('duration_text') or '时长暂无'}**  ·  "
         f"{summary.get('started_at', '')[5:16]} ~ {summary.get('ended_at', '')[5:16]}"},
        kpi_block,
        {"tag": "hr"},
        section_panel("数据关联话术", evidence_text, color="blue", kicker="SIGNALS"),
        section_panel("优质可复用话术", quality_text, color="green", kicker="PLAYBOOK"),
        section_panel("下一场行动", action, color="orange", kicker="NEXT"),
        collapsible_text(
            "查看各班次完整 AI 复盘",
            ("> 以下按班次汇总，不把跨班次相关关系改写为因果。\n\n" + full_ai)
            if summary.get("scope_count", 1) > 1 else full_ai,
        ),
    ]
    subtitle = f"整场口径 · {summary.get('started_at', '')} ~ {summary.get('ended_at', '')}"
    return card_v2(
        "直播经营复盘", "blue", elements, subtitle=subtitle,
        status=status, status_color=status_color, icon="chart_colorful",
    )


def build_weekly_card(cfg: dict, store, days: int = 7) -> dict:
    """周报卡只陈述事实趋势；未配置业务 KPI 时不打好/坏/达标标签。"""
    from datetime import timedelta
    since_dt = now_shanghai() - timedelta(days=days)
    since = since_dt.strftime("%Y-%m-%d %H:%M:%S")
    streams = [dict(row) for row in store.query(
        """SELECT s.*,a.name anchor_name FROM streams s
           LEFT JOIN anchors a ON a.id=s.anchor_id
           WHERE s.status='reported' AND s.started_at>=? ORDER BY s.started_at""",
        (since,),
    )]
    platform_keys = {str(row.get("live_id") or f"local:{row['id']}") for row in streams}
    anchors = {str(row.get("anchor_name") or "未知主播") for row in streams}
    duration_sec = sum(float(row.get("duration_sec") or 0) for row in streams)
    quality_count = 0
    if streams:
        ids = tuple(int(row["id"]) for row in streams)
        ph = ",".join("?" for _ in ids)
        quality_count = int(store.query(
            f"SELECT COUNT(*) c FROM highlights WHERE kind='quality' AND stream_id IN ({ph})",
            ids,
        )[0]["c"])

    anchor_rows: list[str] = []
    for anchor in sorted(anchors):
        owned = [row for row in streams if str(row.get("anchor_name") or "未知主播") == anchor]
        minutes = sum(float(row.get("duration_sec") or 0) for row in owned) / 60
        anchor_rows.append(f"**{anchor}**　{len(owned)} 班次 · {minutes:,.0f} 分钟")
    if not anchor_rows:
        anchor_rows = ["本周期暂无已完成复盘场次"]

    try:
        from ..metrics import daily as daily_metrics
        daily_metrics.ensure_table(store)
        daily = [dict(row) for row in store.query(
            "SELECT date,pay_amt,look_uv,pay_byr_cnt,data_state FROM daily_metrics "
            "WHERE date>=? ORDER BY date", (since_dt.strftime("%Y%m%d"),))]
    except Exception:
        daily = []
    trend_rows = []
    for row in daily[-days:]:
        day = str(row.get("date") or "")
        day = f"{day[4:6]}-{day[6:8]}" if len(day) == 8 else day
        if row.get("data_state") == "pending":
            trend_rows.append(f"{day}　待结算（不计入趋势）")
            continue
        trend_rows.append(
            f"{day}　成交 {_metric(row.get('pay_amt'), ' 元', 2)} · "
            f"观看 {_metric(row.get('look_uv'), ' 人')} · "
            f"成交人数 {_metric(row.get('pay_byr_cnt'), ' 人')}"
        )
    if not trend_rows:
        trend_rows = ["每日经营数据暂无；缺失不按 0 处理"]

    elements = [
        metric_grid([
            ("平台场次", str(len(platform_keys))),
            ("本地班次", str(len(streams))),
            ("复盘主播", str(len(anchors))),
        ], focus_index=0, color="blue"),
        metric_grid([
            ("录制时长", f"{duration_sec / 3600:,.1f} 小时"),
            ("优质话术", f"{quality_count} 条"),
            ("统计周期", f"近 {days} 天"),
        ], color="blue"),
        section_panel("主播覆盖", "\n\n".join(anchor_rows), color="green", kicker="ANCHORS"),
        section_panel("每日经营数据", "\n".join(trend_rows), color="blue", kicker="TREND"),
        section_panel(
            "口径说明",
            "平台场次按 liveId 去重，本地班次按已完成复盘记录统计。当前未配置业务 KPI，"
            "因此本卡只展示事实与趋势，不评价好/坏或达标。",
            color="grey", kicker="SCOPE",
        ),
    ]
    return card_v2(
        "淘宝直播巡检周报", "blue", elements,
        subtitle=f"{since_dt.strftime('%Y-%m-%d')} ~ {now_shanghai().strftime('%Y-%m-%d')}",
        status="事实汇总", status_color="blue", icon="calendar_colorful",
    )


def notify_weekly(cfg: dict, store, days: int = 7) -> bool:
    nf = (cfg.get("notify", {}) or {}).get("feishu", {}) or {}
    if not nf.get("enabled") or not nf.get("chat_id"):
        return False
    period = now_shanghai().strftime("%Y-%m-%d")
    delivery_key = f"weekly:{period}:{days}"
    from ..review.report import generate_weekly_report
    generate_weekly_report(cfg, store, days)
    return send_card_once(
        cfg, nf["chat_id"], build_weekly_card(cfg, store, days), delivery_key)


def _truncate_sentence_end(text: str, max_chars: int = 500) -> str:
    """长详情按完整句省略并显式标注，禁止静默硬切半句。"""
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    pieces = re.findall(r".+?(?:[。！？；…]+|$)", text, re.S)
    kept: list[str] = []
    used = 0
    for piece in pieces:
        if used + len(piece) > max_chars:
            break
        kept.append(piece)
        used += len(piece)
    body = "".join(kept).strip()
    return (body + "…（已省略，完整信息见日志）") if body else text[:max_chars] + "…"


def build_alert_card(room_key: str, detail: str, *, failure_count: int = 3) -> dict:
    """构造只读 Card 2.0 告警；触发和冷却由 watcher 负责。"""
    is_health_check = str(room_key or "").strip() == "健康检查"
    issue_count = len([item for item in str(detail or "").split("；") if item.strip()])
    scope_label = "检查范围" if is_health_check else "直播间"
    count_label = "异常项目" if is_health_check else "连续失败"
    count_value = f"{issue_count} 项" if is_health_check else f"{failure_count} 次"
    investigation = (
        "**处理顺序**\n"
        "1. 先看‘已观察到’中的具体后台任务\n"
        "2. 优先处理当前经营日的未完成任务\n"
        "3. 若录像和大屏取数正常，不按接口故障处置"
        if is_health_check else
        "**排查顺序**\n"
        "1. 查看本机 watcher 日志确认错误类型\n"
        "2. 核验网络、账号授权状态与淘宝接口返回\n"
        "3. 接口不可用时按现有手动录制流程处理"
    )
    elements = [
        {
            "tag": "column_set", "flex_mode": "none",
            "columns": [{
                "tag": "column", "width": "weighted", "weight": 1,
                "background_style": "red-50", "padding": "12px",
                "vertical_spacing": "4px",
                "elements": [
                    {"tag": "markdown", "content": f"**{scope_label}**\n{room_key or '暂无'}"},
                    {"tag": "markdown", "content": f"**{count_label}**\n{count_value}"},
                    {"tag": "markdown",
                     "content": f"**已观察到**\n{_truncate_sentence_end(detail) or '暂无详情'}"},
                ],
            }],
        },
        {
            "tag": "column_set", "flex_mode": "none",
            "columns": [{
                "tag": "column", "width": "weighted", "weight": 1,
                "background_style": "grey-50", "padding": "12px",
                "elements": [{
                    "tag": "markdown",
                    "content": investigation,
                }],
            }],
        },
    ]
    return card_v2(
        "系统健康异常告警" if is_health_check else "巡检接口异常告警",
        "red", elements,
        subtitle=("后台任务与资源状态需要核验" if is_health_check
                  else "探测连续失败，录制状态需人工核验"),
        status="需处理", status_color="red", icon="warning_colorful",
    )


def build_taobao_auth_required_card(payload: dict) -> dict:
    failed_at = float(payload.get("first_failed_at") or 0)
    failed_text = (datetime.fromtimestamp(failed_at, SHANGHAI).strftime("%m-%d %H:%M")
                   if failed_at > 0 else "未知")
    elements = [
        section_panel(
            "当前影响",
            f"**首次失效：** {failed_text}\n"
            "淘宝/千牛经营数据已暂停。录像和飞书妙记继续，系统不推送空数据简报。",
            color="red", kicker="AUTH REQUIRED",
        ),
        section_panel(
            "你只需要做一件事",
            "打开 **Ego Lite**，登录淘宝/千牛。登录后不用复制任何内容，"
            "系统会在 5 分钟内自动验证并恢复。",
            color="orange", kicker="ACTION",
        ),
        section_panel(
            "恢复边界",
            "系统不会填密码、跳过验证码，也不会为缺失小时平均拆分累计数据。",
            color="grey", kicker="SCOPE",
        ),
    ]
    return card_v2(
        "淘宝登录已失效", "red", elements,
        subtitle="经营数据已暂停，内容链路仍在运行",
        status="等待登录", status_color="red", icon="warning_colorful",
    )


def build_taobao_auth_recovered_card(payload: dict) -> dict:
    outage = max(0, int(payload.get("outage_seconds") or 0))
    resumed = max(0, int(payload.get("resumed_briefs") or 0))
    suppressed = max(0, int(payload.get("suppressed_briefs") or 0))
    backfilled = max(0, int(payload.get("backfilled_briefs") or 0))
    failed_at = float(payload.get("first_failed_at") or 0)
    recovered_at = float(payload.get("recovered_at") or 0)
    window_text = ""
    if failed_at > 0 and recovered_at >= failed_at:
        start_text = datetime.fromtimestamp(failed_at, SHANGHAI).strftime("%m-%d %H:%M")
        end_text = datetime.fromtimestamp(recovered_at, SHANGHAI).strftime("%m-%d %H:%M")
        window_text = f"\n**中断窗口：** {start_text} → {end_text}"
    elements = [
        metric_grid([
            ("中断时长", f"{outage // 3600}小时{outage % 3600 // 60}分"),
            ("恢复简报", f"{resumed} 张"),
            ("趋势补抓", f"{backfilled}/{suppressed} 个时段"),
        ], focus_index=0, color="green"),
        section_panel(
            "验证结果",
            "直播详情、实时累计、分钟趋势和场次列表均已通过。"
            "淘宝登录态已恢复，新简报将继续使用真实经营数据。"
            + window_text,
            color="green", kicker="RECOVERED",
        ),
        section_panel(
            "数据口径",
            f"本次已补抓 {backfilled} 个历史分钟趋势时段；"
            "未成功时保留缺口，不用当前累计数倒推，也不平均拆给每小时。",
            color="grey", kicker="DATA SCOPE",
        ),
    ]
    return card_v2(
        "淘宝登录态已恢复", "green", elements,
        subtitle="经营数据链路已重新接通",
        status="已恢复", status_color="green", icon="success_colorful",
    )


def notify_taobao_auth_required(cfg: dict, payload: dict) -> bool:
    nf = (cfg.get("notify", {}) or {}).get("feishu", {}) or {}
    if not nf.get("enabled") or not nf.get("chat_id"):
        return False
    generation = int(payload.get("generation") or 0)
    slot = int(payload.get("reminder_slot") or 0)
    return send_card_once(
        cfg, nf["chat_id"], build_taobao_auth_required_card(payload),
        f"taobao-auth:{generation}:{slot}",
    )


def notify_taobao_auth_recovered(cfg: dict, payload: dict) -> bool:
    nf = (cfg.get("notify", {}) or {}).get("feishu", {}) or {}
    if not nf.get("enabled") or not nf.get("chat_id"):
        return False
    generation = int(payload.get("generation") or 0)
    return send_card_once(
        cfg, nf["chat_id"], build_taobao_auth_recovered_card(payload),
        f"taobao-auth-recovered:{generation}",
    )


# ---------- 入口 ----------
def notify_platform_review(cfg: dict, summary: dict) -> bool:
    """按业务经营日幂等发送唯一日报卡。"""
    nf = (cfg.get("notify", {}) or {}).get("feishu", {}) or {}
    live_id = str(summary.get("live_id") or "").strip()
    if not nf.get("enabled") or not nf.get("chat_id") or not live_id:
        return False
    business_key = str(summary.get("business_session_key") or "").strip()
    # 新链路一个经营日只能有一张日报；无经营日字段的
    # 历史冻结载荷仍保留旧 key，避免升级后重复投递。
    delivery_key = (f"daily:{business_key}" if business_key
                    else f"review:{live_id}")
    if not send_card_once(
            cfg, nf["chat_id"], build_platform_review_card(cfg, summary),
            delivery_key):
        return False
    return True


def notify_stream(cfg: dict, store, stream_id: int,
                  notify_card: bool = True) -> None:
    """本地技术碎片完成后只写多维表格；正式卡必须走 notify_platform_review。"""
    nf = (cfg.get("notify", {}) or {}).get("feishu", {}) or {}
    if not nf.get("enabled"):
        return
    summary = build_summary(cfg, store, stream_id)
    if notify_card:
        log.warning("场次 #%s 是本地技术碎片，已阻止碎片级复盘卡发送", stream_id)
    if not _already_notified(stream_id, "bitable"):
        try:
            append_stream_record(cfg, summary)
            _mark_notified(stream_id, "bitable")
        except Exception as e:
            _mark_pending(stream_id, "bitable", str(e))
            log.warning("飞书多维表格写入失败（可独立重试）: %s", e)
    if _feedback_enabled(cfg):
        try:
            written = upsert_highlight_feedback(cfg, summary)
            if written:
                log.info("场次 #%s 已同步 %d 条高亮到人工反馈表", stream_id, written)
        except Exception as exc:
            log.warning("高亮话术反馈表同步失败（不影响复盘）: %s", exc)


def build_summary(cfg: dict, store, stream_id: int) -> dict:
    """从数据库汇总一场直播的复盘摘要（卡片 + 多维表格共用）"""
    from ..review.report import _hms, _fmt_duration

    stream = store.get_stream(stream_id)
    anchor = store.get_anchor(stream["anchor_id"])
    live_id = str(stream["live_id"] or "")
    scope_streams = ([dict(row) for row in store.query(
        """SELECT s.*,a.name anchor_name FROM streams s
           LEFT JOIN anchors a ON a.id=s.anchor_id
           WHERE s.live_id=? AND s.status='reported' AND s.file_path!=''
           ORDER BY s.started_at""", (live_id,))]
        if live_id else [])
    if not any(int(row["id"]) == int(stream_id) for row in scope_streams):
        scope_streams.append({**dict(stream), "anchor_name": anchor["name"] if anchor else ""})
        scope_streams.sort(key=lambda row: row.get("started_at") or "")
    scope_ids = tuple(int(row["id"]) for row in scope_streams)
    placeholders = ",".join("?" for _ in scope_ids)
    transcripts = store.query(
        f"SELECT * FROM transcripts WHERE stream_id IN ({placeholders}) ORDER BY stream_id,start_ms",
        scope_ids,
    )
    highlights = store.query(
        f"SELECT * FROM highlights WHERE stream_id IN ({placeholders}) ORDER BY score DESC",
        scope_ids,
    )
    occurrences = store.query(
        f"""SELECT o.*,CASE WHEN o.id=(
                SELECT MIN(first.id) FROM talktrack_occurrences first
                WHERE first.anchor_id=o.anchor_id AND first.category=o.category
                  AND first.norm_text=o.norm_text
            ) THEN 1 ELSE 0 END AS is_new
            FROM talktrack_occurrences o WHERE o.stream_id IN ({placeholders})""",
        scope_ids,
    )
    new_tracks = [row for row in occurrences if row["is_new"]]

    categories: dict[str, int] = {}
    for t in occurrences:
        categories[t["category"]] = categories.get(t["category"], 0) + 1

    from ..highlight.peak import has_data_peak_reason
    data_highlights = [h for h in highlights
                       if h["kind"] == "data_association" or has_data_peak_reason(h["reasons"])]
    quality_highlights = [h for h in highlights if h["kind"] == "quality"]
    visible_highlights = data_highlights + quality_highlights

    from ..metrics.qianniu import get_metrics
    platform_rows = (store.query("SELECT * FROM platform_sessions WHERE live_id=?", (live_id,))
                     if live_id else [])
    if platform_rows:
        platform = dict(platform_rows[0])
        display_metrics = {
            **platform,
            "source": "platform_sessions.frozen",
            "source_scope": "平台完整场次（冻结总账）",
            "data_state": "ok",
        }
    elif len(scope_ids) == 1:
        display_metrics = get_metrics(store, stream_id)
    else:
        local_metrics = [get_metrics(store, sid) for sid in scope_ids]

        def _sum_if_complete(key: str):
            values = [row.get(key) for row in local_metrics]
            return sum(float(value) for value in values) if values and all(
                value is not None for value in values) else None

        peaks = [row.get("max_online_uv") for row in local_metrics
                 if row.get("max_online_uv") is not None]
        display_metrics = {
            "source": "aggregate.local-periods",
            "source_scope": "平台整场的本地时段汇总（待冻结）",
            "data_state": "partial",
            "pay_amt": _sum_if_complete("pay_amt"),
            "item_qty": _sum_if_complete("item_qty"),
            "max_online_uv": max(peaks) if peaks else None,
            "order_cnt": None, "buyer_cnt": None, "viewer_uv": None,
        }

    day_seq = store.day_seq(stream_id)
    started = stream["started_at"] or ""
    if day_seq > 0:
        if started[:10] == now_shanghai().strftime("%Y-%m-%d"):
            day_label = f"今日第 {day_seq} 场"
        else:
            m, d = int(started[5:7]), int(started[8:10])
            day_label = f"{m}月{d}日第 {day_seq} 场"
    else:
        day_label = ""

    return {
        "stream_id": stream_id,
        "day_seq": day_seq,
        "day_label": day_label,
        "anchor_name": anchor["name"] if anchor else f"主播#{stream['anchor_id']}",
        "anchor_names": list(dict.fromkeys(
            str(row.get("anchor_name") or "未知主播") for row in scope_streams)),
        "live_id": live_id,
        "scope_stream_ids": list(scope_ids),
        "scope_count": len(scope_ids),
        "date": (stream["started_at"] or "")[:10],
        "started_at": min((row.get("started_at") or "" for row in scope_streams), default=""),
        "ended_at": max((row.get("ended_at") or "" for row in scope_streams), default=""),
        "duration_min": round(sum((row.get("duration_sec") or 0) for row in scope_streams) / 60, 1),
        "duration_text": _fmt_duration(sum((row.get("duration_sec") or 0) for row in scope_streams)),
        "sentence_count": len(transcripts),
        "char_count": sum(len(t["text"]) for t in transcripts),
        "highlight_count": len(visible_highlights),
        "new_talktracks": len(new_tracks),
        "categories": categories,
        "display_metrics": display_metrics,
        "data_highlights": [
            {"time": _hms(h["start_ms"]), "end_time": _hms(h["end_ms"]),
             "score": h["score"],
             "reasons": "、".join(json.loads(h["reasons"]) if isinstance(h["reasons"], str) else h["reasons"]),
             "transcript": h["transcript"], "peak_meta": h["peak_meta"],
             "kind": "data_association"}
            for h in data_highlights[:5]
        ],
        "quality_highlights": [
            {"time": _hms(h["start_ms"]), "end_time": _hms(h["end_ms"]),
             "score": h["score"],
             "reasons": "、".join(json.loads(h["reasons"]) if isinstance(h["reasons"], str) else h["reasons"]),
             "transcript": h["transcript"], "quality_meta": h["quality_meta"],
             "kind": "quality"}
            for h in quality_highlights[:5]
        ],
        # 旧调用方兼容：top_highlights 仍指数据关联榜。
        "top_highlights": [
            {"time": _hms(h["start_ms"]), "end_time": _hms(h["end_ms"]),
             "score": h["score"],
             "reasons": "、".join(json.loads(h["reasons"]) if isinstance(h["reasons"], str) else h["reasons"]),
             "transcript": h["transcript"], "peak_meta": h["peak_meta"]}
            for h in data_highlights[:5]
        ],
        "video_path": stream["file_path"] or "",
        "report_name": f"stream_{stream_id:04d}_{anchor['name']}.md" if anchor else "",
    }
