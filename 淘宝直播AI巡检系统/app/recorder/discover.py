"""直播场次发现：用千牛直播列表接口自动获取当天场次（含 liveId）

接口：mtop.taobao.dreamweb.live.list.query（从千牛直播管理列表页逆向确认）
参数：roomNum（直播间编号，配置 taobao.room_num）+ 分页
响应：$.data.data[]，id=liveId，startTime=毫秒时间戳（上海时区），roomStatus 1=直播中

用途：liveId 每场变化，用本模块自动发现当天场次，watcher 探测失败时自动切换。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone, timedelta

log = logging.getLogger("discover")

API = "mtop.taobao.dreamweb.live.list.query"
API_VERSION = "1.0"

SHANGHAI = timezone(timedelta(hours=8))


def query_live_list(cfg: dict, room_num: str, page_size: int = 20) -> list[dict]:
    """查询场次列表，返回原始场次 dict 列表（按 startTime 倒序）"""
    from .mtop import shared_client
    client = shared_client(cfg)
    r = client.call(API, API_VERSION, {
        "roomNum": room_num, "pageNum": 1, "searchValue": "", "pageSize": page_size,
    })
    d = r.get("data") or {}
    items = d.get("data") or []
    if not isinstance(items, list):
        return []
    items.sort(key=lambda x: int(x.get("startTime") or 0), reverse=True)
    return items


def _parse_start(items: list[dict], now: datetime | None = None) -> list[dict]:
    """把毫秒时间戳转成本地时间，标记是否今天"""
    now = now or datetime.now(SHANGHAI)
    out = []
    for it in items:
        ts = int(it.get("startTime") or 0)
        dt = datetime.fromtimestamp(ts / 1000, SHANGHAI)
        it["_start_dt"] = dt
        it["_is_today"] = dt.strftime("%Y-%m-%d") == now.strftime("%Y-%m-%d")
        out.append(it)
    return out


def find_today_live(cfg: dict, room_num: str) -> dict | None:
    """发现今天的场次，优先返回直播中（roomStatus=1），其次最新一场。
    返回场次 dict（含 id=liveId），没有则 None"""
    try:
        items = _parse_start(query_live_list(cfg, room_num))
    except Exception as e:
        log.warning("场次列表查询失败: %s", e)
        return None
    today = [it for it in items if it.get("_is_today")]
    if not today:
        log.info("今天没有场次（最近一场 %s）",
                 items[0]["_start_dt"].strftime("%m-%d %H:%M") if items else "无")
        return None
    # 优先直播中
    for it in today:
        if str(it.get("roomStatus")) == "1":
            log.info("发现直播中场次 #%s [%s] %s", it.get("id"),
                     it["_start_dt"].strftime("%H:%M"), it.get("title", "")[:30])
            return it
    # 其次最新
    it = today[0]
    log.info("今天暂无直播中场次，最新场次 #%s [%s]", it.get("id"),
             it["_start_dt"].strftime("%H:%M"))
    return it


def find_current_live(cfg: dict, room_num: str) -> dict | None:
    """只返回今天明确处于直播中的场次；没有时返回 None。

    与 find_today_live 不同，本函数绝不回退到“今天最新但已下播”的场次，适合 watcher
    自动切换 liveId，避免重新切回刚结束的旧场次。
    """
    try:
        items = _parse_start(query_live_list(cfg, room_num))
    except Exception as exc:
        log.warning("场次列表查询失败: %s", exc)
        return None
    for item in items:
        if item.get("_is_today") and str(item.get("roomStatus")) == "1":
            log.info("发现直播中场次 #%s [%s] %s", item.get("id"),
                     item["_start_dt"].strftime("%H:%M"), item.get("title", "")[:30])
            return item
    log.info("今天暂无直播中场次")
    return None


def update_config_live_id(live_id: str) -> None:
    """把发现的 liveId 写回 config.yaml（与 update_liveid.py 保持一致）"""
    from ..config import CONFIG_PATH
    from ..taobao_session.config import atomic_update_yaml_scalar

    p = CONFIG_PATH
    if not p.exists():
        return
    atomic_update_yaml_scalar(
        p, section="taobao", key="live_id", value=str(live_id), mode=0o600)
    log.info("config.yaml taobao.live_id 已原子更新为 %s", live_id)
