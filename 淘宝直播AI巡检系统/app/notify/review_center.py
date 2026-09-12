"""飞书「淘宝直播经营复盘中心」的建库与安全同步。

这个模块独立于旧的技术碎片审计表。所有远端记录按业务键查找再更新，
并且把人工填写的行动、评价与异常处理字段排除在程序覆盖范围之外。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from ..config import now_shanghai, resolve
from ..review_center.data import (
    build_action_rows,
    build_anchor_day_rows,
    build_anchor_week_rows,
    build_platform_rows,
    build_talktrack_rows,
)
from .feishu import (
    _field_names,
    _find_value,
    _load_state,
    _notify_lock,
    _records_from_payload,
    _run,
    _save_state,
)

log = logging.getLogger("notify.review_center")

BASE_NAME = "淘宝直播经营复盘中心"
_review_center_lock = threading.RLock()
_review_center_lock_depth = threading.local()


@contextmanager
def _review_center_process_lock():
    """跨进程串行建库和写入，避免手动回填与 watcher 竞争状态/业务键。"""
    with _review_center_lock:
        depth = int(getattr(_review_center_lock_depth, "value", 0))
        if depth:
            _review_center_lock_depth.value = depth + 1
            try:
                yield
            finally:
                _review_center_lock_depth.value = depth
            return
        lock_path = Path(resolve("data/review_center.lock"))
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as handle:
            try:
                lock_path.chmod(0o600)
            except OSError:
                pass
            try:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except (ImportError, OSError):
                fcntl = None
            _review_center_lock_depth.value = 1
            try:
                yield
            finally:
                _review_center_lock_depth.value = 0
                if fcntl is not None:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass


def _serialized_base_creation(fn):
    @wraps(fn)
    def guarded(*args, **kwargs):
        with _review_center_process_lock():
            return fn(*args, **kwargs)
    return guarded


def _text(name: str, **extra: Any) -> dict[str, Any]:
    return {"type": "text", "name": name, **extra}


def _number(name: str, *, currency: bool = False, percentage: bool = False,
            precision: int = 0) -> dict[str, Any]:
    if currency:
        style = {"type": "currency", "precision": 2, "currency_code": "CNY"}
    else:
        style = {"type": "plain", "precision": precision,
                 "percentage": percentage, "thousands_separator": True}
    return {"type": "number", "name": name, "style": style}


def _select(name: str, values: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "type": "select", "name": name, "multiple": False,
        "options": [{"name": value, "hue": hue, "lightness": "Light"}
                    for value, hue in values],
    }


def _datetime(name: str, *, with_time: bool = False) -> dict[str, Any]:
    return {"type": "datetime", "name": name,
            "style": {"format": "yyyy-MM-dd HH:mm" if with_time else "yyyy-MM-dd"}}


REVIEW_CENTER_TABLES: dict[str, list[dict[str, Any]]] = {
    "整场直播复盘": [
        _text("直播名称"), _datetime("直播日期"), _datetime("开始时间", with_time=True),
        _datetime("结束时间", with_time=True), _number("整场成交金额", currency=True),
        _number("观看人数"), _number("观看次数"), _number("成交人数"),
        _number("成交订单"), _number("成交件数"), _number("最高在线"), _number("主播数"),
        _select("数据状态", [("完整", "Green"), ("部分", "Orange"), ("已冻结", "Green"),
                            ("待冻结", "Yellow"), ("不可用", "Red")]),
        _select("复盘状态", [("已复盘", "Green"), ("部分复盘", "Orange"),
                            ("待复盘", "Yellow"), ("投递待核验", "Red")]),
        _text("报告链接"), _text("数据限制"), _text("业务键"),
    ],
    "主播日表现": [
        _text("主播"), _datetime("日期"), _number("成交金额", currency=True),
        _number("昨日成交金额", currency=True), _number("成交金额增减", currency=True),
        _text("成交金额日环比"), _number("观看人数"), _number("观看次数"),
        _number("成交人数"), _number("成交订单"), _number("成交件数"),
        _number("成交转化率", percentage=True, precision=1), _number("客单价", currency=True),
        _number("商品点击人数"), _number("商品点击次数"),
        _number("商品点击率", percentage=True, precision=1), _number("加购人数"),
        _number("加购次数"), _number("加购件数"), _number("新增粉丝"),
        _number("转粉率", percentage=True, precision=1), _number("评论人数"),
        _number("分享人数"), _number("点赞人数"), _number("上播时长秒"),
        _select("数据状态", [("进行中", "Orange"), ("已冻结", "Green"),
                            ("部分数据", "Yellow")]), _text("业务键"),
    ],
    "主播周表现": [
        _text("主播"), _text("自然周"), _datetime("周开始"), _datetime("周截止"),
        _number("本周成交金额", currency=True), _number("上周同期成交金额", currency=True),
        _number("成交金额增减", currency=True), _text("成交金额周环比"),
        _number("本周有效天数"), _number("上周同期有效天数"),
        _number("本周观看人数"), _number("本周成交人数"), _number("本周成交订单"),
        _number("本周成交件数"),
        _select("数据状态", [("进行中", "Orange"), ("已冻结", "Green"),
                            ("部分数据", "Yellow")]), _text("业务键"),
    ],
    "高亮话术库": [
        _text("原话"), _text("主播"), _text("直播"), _datetime("发生时间", with_time=True),
        _select("类型", [("高质复用", "Orange"), ("数据关联", "Purple")]), _text("类别"),
        _number("质量分", precision=1), _text("入选理由"), _text("数据观察"),
        _select("人工评价", [("待评", "Yellow"), ("好", "Green"), ("一般", "Orange"),
                            ("误判", "Red")]), _text("反馈备注"), _text("业务键"),
    ],
    "复盘行动跟进": [
        _text("行动"), _text("直播"), _text("动作"), _text("证据"),
        _text("负责人"), _datetime("截止日期", with_time=True),
        _select("状态", [("待确认", "Yellow"), ("待执行", "Orange"), ("执行中", "Blue"),
                          ("已完成", "Green"), ("已取消", "Gray")]),
        _datetime("完成时间", with_time=True), _text("验证结果"),
        _text("人工评价"), _text("人工备注"),
        _text("业务键"),
    ],
    "数据同步与异常": [
        _text("事件"), _datetime("发生时间", with_time=True), _text("对象类型"),
        _text("对象名称"),
        _select("同步状态", [("成功", "Green"), ("失败", "Red"), ("部分成功", "Orange")]),
        _select("数据状态", [("完整", "Green"), ("进行中", "Orange"),
                            ("部分数据", "Yellow"), ("异常", "Red")]),
        _text("问题摘要"), _number("重试次数"),
        _select("解决状态", [("待处理", "Red"), ("处理中", "Orange"), ("已解决", "Green")]),
        _text("解决备注"), _text("业务键"),
    ],
}


PROTECTED_FIELDS = {
    "高亮话术库": ("人工评价", "反馈备注"),
    "复盘行动跟进": (
        "负责人", "截止日期", "状态", "完成时间", "验证结果", "人工评价", "人工备注"),
    "数据同步与异常": ("解决状态", "解决备注"),
}


def review_center_enabled(cfg: dict) -> bool:
    feishu = (cfg.get("notify", {}) or {}).get("feishu", {}) or {}
    setting = feishu.get("review_center") or {}
    return bool(setting.get("enabled")) if isinstance(setting, dict) else bool(setting)


def sync_review_center_safely(cfg: dict, store, scope: str) -> dict[str, int]:
    """自动路径的防火墙：Base 异常不能中断抓数、录制或整场复盘。"""
    feishu = (cfg.get("notify", {}) or {}).get("feishu", {}) or {}
    setting = feishu.get("review_center") or {}
    auto_sync = bool(setting.get("auto_sync", True)) if isinstance(setting, dict) else True
    if not review_center_enabled(cfg) or not auto_sync:
        return {}
    try:
        return sync_review_center(cfg, store, scope)
    except Exception:
        log.exception("经营复盘中心自动同步失败 scope=%s（不影响直播主流程）", scope)
        return {}


def merge_program_fields(existing: dict, program_fields: dict,
                         protected_fields: tuple[str, ...] = ()) -> dict:
    """程序更新可控字段，远端人工列（哪怕人为清空）原样保留。"""
    merged = dict(program_fields)
    for name in protected_fields:
        if name in existing:
            merged[name] = existing[name]
    return merged


def _table_id(result: dict, name: str) -> str:
    def walk(value: Any) -> str:
        if isinstance(value, dict):
            if str(value.get("name") or "") == name:
                candidate = value.get("table_id") or value.get("id")
                if candidate:
                    return str(candidate)
            for item in value.values():
                found = walk(item)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = walk(item)
                if found:
                    return found
        return ""
    return walk(result)


def _table_ids_from_remote(token: str) -> dict[str, str]:
    result = _run(["base", "+table-list", "--as", "user", "--base-token", token,
                   "--format", "json"])
    return {name: found for name in REVIEW_CENTER_TABLES
            if (found := _table_id(result, name))}


def _ensure_writable_fields(token: str, table_id: str,
                            fields: list[dict[str, Any]]) -> None:
    """为既有复盘库补充新的程序字段，不修改任何已有字段。"""
    payload = _run([
        "base", "+field-list", "--as", "user", "--base-token", token,
        "--table-id", table_id, "--format", "json", "--limit", "200",
    ])
    existing = _field_names(payload)
    for field in fields:
        name = str(field.get("name") or "")
        if not name or name in existing:
            continue
        _run([
            "base", "+field-create", "--as", "user", "--base-token", token,
            "--table-id", table_id,
            "--json", json.dumps(field, ensure_ascii=False),
            "--format", "json",
        ])
        existing.add(name)


def _search_keyword(key: str) -> str:
    """飞书搜索关键词上限 50 字；后续仍逐行精确比对完整业务键。"""
    return str(key)[:50]


@_serialized_base_creation
def ensure_review_center(cfg: dict) -> tuple[str, dict[str, str]]:
    """创建或复用新 Base；不会触碰旧的技术碎片审计 Base。"""
    if not review_center_enabled(cfg):
        raise RuntimeError("经营复盘中心未启用")
    state = _load_state()
    token = str(state.get("review_center_base_token") or "")
    table_ids = {str(key): str(value) for key, value in
                 (state.get("review_center_table_ids") or {}).items() if value}
    first_name = next(iter(REVIEW_CENTER_TABLES))
    if not token:
        created = _run([
            "base", "+base-create", "--as", "user", "--name", BASE_NAME,
            "--table-name", first_name,
            "--fields", json.dumps(REVIEW_CENTER_TABLES[first_name], ensure_ascii=False),
            "--time-zone", "Asia/Shanghai", "--format", "json",
        ])
        data = created.get("data") or created
        base = data.get("base") or {}
        token = str(base.get("base_token") or data.get("app_token") or data.get("token") or "")
        first_id = _table_id(created, first_name) or str(
            _find_value(created, ("table_id",)) or "")
        if not token or not first_id:
            raise RuntimeError("创建经营复盘中心失败：未获取到 Base 或首表标识")
        table_ids[first_name] = first_id
        with _notify_lock:
            state = _load_state()
            state["review_center_base_token"] = token
            state["review_center_table_ids"] = table_ids
            _save_state(state)
    elif not table_ids:
        table_ids = _table_ids_from_remote(token)

    for name, fields in REVIEW_CENTER_TABLES.items():
        table_id = table_ids.get(name, "")
        if not table_id:
            created = _run([
                "base", "+table-create", "--as", "user", "--base-token", token,
                "--name", name, "--fields", json.dumps(fields, ensure_ascii=False),
                "--format", "json",
            ])
            table_id = _table_id(created, name) or str(_find_value(created, ("table_id",)) or "")
            if not table_id:
                raise RuntimeError(f"创建经营复盘中心表失败：{name}")
            table_ids[name] = table_id
            with _notify_lock:
                state = _load_state()
                state["review_center_base_token"] = token
                state["review_center_table_ids"] = table_ids
                _save_state(state)
        _ensure_writable_fields(token, table_id, fields)
    return token, table_ids


def _find_record(token: str, table_id: str, key_field: str, key: str,
                 protected_fields: tuple[str, ...]) -> tuple[str, dict]:
    args = [
        "base", "+record-search", "--as", "user", "--base-token", token,
        "--table-id", table_id, "--keyword", _search_keyword(key), "--search-field", key_field,
    ]
    for name in (key_field, *protected_fields):
        args.extend(["--field-id", name])
    args.extend(["--format", "json", "--limit", "20"])
    response = _run(args)
    for row in _records_from_payload(response):
        fields = row.get("fields") or {}
        if str(fields.get(key_field) or "") == key:
            return str(row.get("record_id") or row.get("id") or ""), dict(fields)
    return "", {}


def upsert_review_center_row(token: str, table_id: str, key_field: str, fields: dict,
                             protected_fields: tuple[str, ...] = ()) -> None:
    """按可审计业务键写一条记录，远端人工字段始终优先。"""
    key = str(fields.get(key_field) or "")
    if not key:
        raise ValueError(f"经营复盘中心记录缺少业务键字段：{key_field}")
    record_id, existing = _find_record(token, table_id, key_field, key, protected_fields)
    payload = merge_program_fields(existing, fields, protected_fields)
    args = [
        "base", "+record-upsert", "--as", "user", "--base-token", token,
        "--table-id", table_id, "--json", json.dumps(payload, ensure_ascii=False),
        "--format", "json",
    ]
    if record_id:
        args.extend(["--record-id", record_id])
    _run(args)


def _sync_event(scope: str, *, success: bool, detail: str = "", retry_count: int = 0) -> dict:
    timestamp = now_shanghai().strftime("%Y-%m-%d %H:%M:%S")
    return {
        "事件": f"{scope} 数据同步", "发生时间": timestamp,
        "对象类型": "经营复盘中心", "对象名称": scope,
        "同步状态": "成功" if success else "失败",
        "数据状态": "完整" if success else "异常",
        "问题摘要": detail or "同步完成", "重试次数": retry_count,
        "解决状态": "已解决" if success else "待处理", "解决备注": "",
        "业务键": f"sync:{scope}:{timestamp[:10]}",
    }


@_serialized_base_creation
def sync_review_center(cfg: dict, store, scope: str = "all", *, strict: bool = False) -> dict[str, int]:
    """同步指定业务范围；异常留痕且默认不阻断直播主流程。"""
    if not review_center_enabled(cfg):
        return {"skipped": 1}
    if scope not in {"all", "daily", "platform"}:
        raise ValueError("scope 仅支持 all、daily、platform")
    token, tables = ensure_review_center(cfg)
    builders: list[tuple[str, Callable[[], list[dict]]]] = []
    if scope in {"all", "daily"}:
        builders += [("主播日表现", lambda: build_anchor_day_rows(store)),
                     ("主播周表现", lambda: build_anchor_week_rows(store))]
    if scope in {"all", "platform"}:
        builders += [("整场直播复盘", lambda: build_platform_rows(store)),
                     ("高亮话术库", lambda: build_talktrack_rows(cfg, store)),
                     ("复盘行动跟进", lambda: build_action_rows(store))]

    counts: dict[str, int] = {}
    errors: list[str] = []
    for table_name, build_rows in builders:
        try:
            rows = build_rows()
            for fields in rows:
                upsert_review_center_row(
                    token, tables[table_name], "业务键", fields,
                    PROTECTED_FIELDS.get(table_name, ()),
                )
            counts[table_name] = len(rows)
        except Exception as exc:
            counts[table_name] = 0
            errors.append(f"{table_name}：{str(exc)[:240]}")
            log.exception("经营复盘中心同步失败 table=%s", table_name)

    event = _sync_event(scope, success=not errors, detail="；".join(errors))
    try:
        upsert_review_center_row(
            token, tables["数据同步与异常"], "业务键", event,
            PROTECTED_FIELDS["数据同步与异常"],
        )
        counts["数据同步与异常"] = 1
    except Exception as exc:
        errors.append(f"数据同步与异常：{str(exc)[:240]}")
        counts["数据同步与异常"] = 0
        log.exception("经营复盘中心异常留痕失败")
    if errors and strict:
        raise RuntimeError("经营复盘中心同步失败：" + "；".join(errors))
    return counts
