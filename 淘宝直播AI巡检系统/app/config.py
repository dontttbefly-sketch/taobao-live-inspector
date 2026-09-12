"""配置加载：读取 config.yaml（不存在则回退到 config.example.yaml）"""
from __future__ import annotations

import copy
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
EXAMPLE_PATH = PROJECT_ROOT / "config.example.yaml"
SHANGHAI = ZoneInfo("Asia/Shanghai")


def now_shanghai() -> datetime:
    """返回项目统一使用的上海时区当前时间。"""
    return datetime.now(SHANGHAI)


def parse_local_datetime(value: str | None) -> datetime | None:
    """把数据库中的本地时间字符串按上海时区解析为 aware datetime。"""
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI)


def local_epoch_ms(value: str | None) -> int | None:
    parsed = parse_local_datetime(value)
    return int(parsed.timestamp() * 1000) if parsed else None


def load_config() -> dict:
    # 录像、转写、cookie 和经营数据都只供本机当前用户使用；所有后续新文件默认私有。
    os.umask(0o077)
    path = CONFIG_PATH if CONFIG_PATH.exists() else EXAMPLE_PATH
    if path == CONFIG_PATH:
        try:
            path.chmod(0o600)
        except OSError:
            pass
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    # 确保关键节存在
    defaults = {
        "anchors": [],
        "taobao": {},
        "recorder": {},
        "asr": {},
        "highlight": {"merge_window": 30, "categories": {}},
        "talktrack": {"opening_window_sec": 180, "dedupe_threshold": 0.85, "llm": {}},
        "report": {"out_dir": "data/reports", "week_days": 7},
        "paths": {"db": "data/inspection.db", "logs": "data/logs"},
    }
    merged = copy.deepcopy(defaults)
    for k, v in cfg.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k].update(v)
        else:
            merged[k] = v
    return merged


def resolve(path_str: str) -> Path:
    """把配置里的相对路径解析到项目根目录"""
    p = Path(path_str)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p


def ensure_dirs(cfg: dict) -> None:
    os.umask(0o077)
    for key in ("out_dir", "db", "logs"):
        for section in ("recorder", "report", "paths"):
            val = cfg.get(section, {}).get(key)
            if val:
                resolve(val).parent.mkdir(parents=True, exist_ok=True)
    resolve(cfg["recorder"]["out_dir"]).mkdir(parents=True, exist_ok=True)
    resolve(cfg["report"]["out_dir"]).mkdir(parents=True, exist_ok=True)
    resolve(cfg["paths"]["logs"]).mkdir(parents=True, exist_ok=True)
    resolve(cfg["paths"]["db"]).parent.mkdir(parents=True, exist_ok=True)
    harden_runtime_permissions(cfg)


def harden_runtime_permissions(cfg: dict) -> None:
    """把既有运行时目录/文件收紧为当前用户私有；不跟随符号链接。"""
    roots = {
        resolve("data"),
        resolve(cfg.get("recorder", {}).get("out_dir", "data/recordings")),
        resolve(cfg.get("report", {}).get("out_dir", "data/reports")),
        resolve(cfg.get("paths", {}).get("logs", "data/logs")),
        resolve(cfg.get("paths", {}).get("db", "data/inspection.db")).parent,
    }
    for root in roots:
        if not root.exists() or root.is_symlink():
            continue
        try:
            root.chmod(0o700)
        except OSError:
            pass
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_symlink():
                continue
            try:
                path.chmod(0o700 if path.is_dir() else 0o600)
            except OSError:
                pass
