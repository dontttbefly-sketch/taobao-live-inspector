"""千牛直播经营数据：实时累计、分钟趋势、下播冻结场次三层数据源。

只读数据源（2026-08-02 在千牛页面逐项核验）：

1. ``iliad ... data.get / totalStats``：当前平台直播的累计总量与当前值。
   适合在播快照，包含在线、最高在线、观看、成交金额、成交人数/件数；不含订单数。
2. ``tblive.portal ... data.get``：分钟趋势，适合计算某一小时的在线走势、点击和成交增量。
3. ``generalQuery / live_overview_rt_content_v3``：已结束平台场次的冻结行，包含真实订单数。

核心约束：接口没返回就是 ``None``，绝不把缺失字段保存或展示成 0。
"""
from __future__ import annotations

import datetime as dt
import copy
import json
import logging
import time
import urllib.parse

from ..config import SHANGHAI, local_epoch_ms, now_shanghai

log = logging.getLogger("metrics.qianniu")

TOTAL_API = "mtop.taobao.iliad.live.user.assistant.data.get"
SERIES_API = "mtop.taobao.tblive.portal.live.user.assistant.data.get"
GENERAL_API = "mtop.dreamweb.query.general.generalQuery"
API_VERSION = "1.0"
SESSION_DATA_API = "live_overview_rt_content_v3"
FROZEN_SESSION_SOURCE = f"generalQuery.{SESSION_DATA_API}"

CORE_TYPES = ["uv", "deal", "itemClick", "popularityScore", "trdScore", "heatScore"]
MAIN_FIELD = {
    "uv": "online",
    "itemClick": "value",
    "deal": "amount",
    "heatScore": "value",
}
MAX_WINDOW_MS = 2 * 3600 * 1000

TOTAL_FIELD_MAP = {
    "online_uv": "online_uv",
    "max_online_uv": "max_online_uv",
    "uv": "viewer_uv",
    "pv": "viewer_pv",
    "pay_amt": "pay_amt",
    "pay_buyer_cnt": "buyer_cnt",
    "pay_item_qty": "item_qty",
    "pay_byr_rate": "pay_byr_rate",
    "ipv_uv_rate": "ipv_uv_rate",
    "atn_uv": "atn_uv",
    "comment_uv": "comment_uv",
    "favor_uv": "favor_uv",
    "share_uv": "share_uv",
    "heat_score": "heat_score",
    "refund_amt": "refund_amt",
    "refund_uv": "refund_uv",
    "refund_item_qty": "refund_item_qty",
    "stay_time_pu": "stay_time_pu",
    "pctr": "pctr",
    "popularity_score": "popularity_score",
    "trd_score": "trd_score",
}

SESSION_FIELD_MAP = {
    "max_online_uv": "max_online_uv",
    "look_uv": "viewer_uv",
    "look_pv": "viewer_pv",
    "ipv": "ipv_total",
    "ipv_uv": "ipv_uv",
    "pay_amt": "pay_amt",
    "pay_buyer_cnt": "buyer_cnt",
    "pay_order_cnt": "order_cnt",
    "pay_item_qty": "item_qty",
    "pay_byr_rate": "pay_byr_rate",
    "ipv_uv_rate": "ipv_uv_rate",
    "atn_uv": "atn_uv",
    "cmt_uv": "comment_uv",
    "fvr_uv": "favor_uv",
    "shr_uv": "share_uv",
    "refund_amt": "refund_amt",
    "refund_uv": "refund_uv",
    "refund_item_qty": "refund_item_qty",
    "look_time_pu": "stay_time_pu",
    "pctr": "pctr",
}

SNAPSHOT_FIELDS = (
    "online_uv", "max_online_uv", "viewer_uv", "viewer_pv", "visitor_total",
    "ipv_total", "pay_amt", "buyer_cnt", "order_cnt", "item_qty",
    "pay_byr_rate", "ipv_uv_rate", "atn_uv", "comment_uv", "favor_uv",
    "share_uv", "heat_score", "refund_amt", "stay_time_pu",
)


def _client(cfg: dict):
    from ..recorder.mtop import shared_client
    return shared_client(cfg, default_referer="https://market.m.taobao.com/")


def _number(value, *, percent: bool = False) -> float | None:
    if value in (None, "", "--", "null"):
        return None
    raw = str(value).strip().replace(",", "")
    is_pct = raw.endswith("%")
    if is_pct:
        raw = raw[:-1]
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return None
    if percent and is_pct:
        n /= 100.0
    return n


def _int(value) -> int | None:
    n = _number(value)
    return int(round(n)) if n is not None else None


def _now() -> str:
    return now_shanghai().strftime("%Y-%m-%d %H:%M:%S")


def parse_buckets(item: dict) -> list[dict]:
    """解析分钟桶；先按原始分隔符拆分，再逐字段 URL 解码。"""
    rows: list[dict] = []
    for rec in item.get("data") or []:
        fields = (rec.get("format") or "").split(",")
        values = str(rec.get("value") or "").split(rec.get("split") or ",")
        rows.append({k: urllib.parse.unquote(v) for k, v in zip(fields, values)})
    return rows


def parse_total_stats(item: dict) -> dict[str, dict]:
    """解析 totalStats 为 ``valueType -> 原始记录``。"""
    out: dict[str, dict] = {}
    for row in parse_buckets(item):
        key = row.get("valueType")
        if key:
            out[key] = row
    return out


def fetch_live_totals(cfg: dict, live_id: str) -> dict:
    """抓当前平台直播累计快照（订单字段不存在，因此结果不会包含 order_cnt）。

    2026-08-08 起统一使用直播专业大屏口径（tblive.portal + timeType=5），
    与数据大屏/复盘/周报完全一致；接口失败时回退 iliad 原口径。
    """
    try:
        screen = fetch_screen_totals(cfg, live_id)
        if screen:
            return screen
        log.warning("大屏累计快照为空，回退 iliad 口径")
    except Exception as exc:
        log.warning("大屏累计快照失败，回退 iliad: %s", str(exc)[:120])
    now_ms = int(time.time() * 1000)
    payload = _client(cfg).call(TOTAL_API, API_VERSION, {
        "liveId": str(live_id), "types": "totalStats", "timeType": 1,
        "searchType": "1", "startTime": now_ms - 10 * 60 * 1000,
        "endTime": now_ms, "extParams": "{}",
    })
    data = payload.get("data") or {}
    rows: dict[str, dict] = {}
    for item in data.get("dataList") or []:
        if item.get("type") == "totalStats":
            rows.update(parse_total_stats(item))
    if not rows:
        return {}

    out: dict = {
        "live_id": str(live_id), "fetched_at": _now(),
        "source": "iliad.totalStats", "source_scope": "平台当前直播累计",
        "data_state": "ok", "room_status": str(data.get("roomStatus") or ""), "raw": rows,
    }
    for source_key, target_key in TOTAL_FIELD_MAP.items():
        row = rows.get(source_key)
        if not row:
            continue
        n = _number(row.get("valueNum"))
        if n is None:
            n = _number(row.get("value"), percent=True)
        if n is None:
            continue
        if target_key.endswith("_rate") or target_key in ("pctr",):
            out[target_key] = float(n)
        elif target_key in ("pay_amt", "refund_amt", "stay_time_pu"):
            out[target_key] = round(float(n), 2)
        else:
            out[target_key] = int(round(n))
    # 兼容已有调用，但不再混淆口径。
    out["deal_amt"] = out.get("pay_amt")
    out["uv"] = out.get("online_uv")
    out["uv_peak"] = out.get("max_online_uv")
    log.info("实时累计快照：在线=%s 最高=%s 观看=%s 成交人数=%s 成交=%.2f",
             out.get("online_uv"), out.get("max_online_uv"), out.get("viewer_uv"),
             out.get("buyer_cnt"), out.get("pay_amt") or 0.0)
    return out


SCREEN_API = "mtop.taobao.tblive.portal.live.user.assistant.data.get"


def _parse_screen_csv(value: str) -> dict[str, str]:
    """解析大屏 totalStats 的 CSV 指标行。

    实测列序（值含千分位）：title,valueType,value千位前段,value千位后段,
    valueNum完整数值,cmpValue,cmpValueFormat,index,time,sendTime,features。
    例如：直播成交金额,pay_amt,108,265,108265.17000000006,null,... →
    value="108,265"、valueNum="108265.17"。短行（如费率字段）回退 row[3]。
    """
    text = urllib.parse.unquote(str(value or ""))
    row = text.split(",")
    if len(row) < 3:
        return {}
    # valueNum 是 value 之后的第一个数值字段：千分位拆两段时在 row[4]
    # （108,265,108265.17），小数值紧跟在 row[3]（403,403,null）。
    value_num = row[4] if (len(row) >= 5 and _number(row[4]) is not None) else row[3]
    value = f"{row[2]},{row[3]}" if len(row) >= 4 else row[2]
    return {"title": row[0], "valueType": row[1],
            "value": value, "valueNum": value_num}


def fetch_screen_totals(
        cfg: dict, live_id: str, *, request_timeout: float = 20.0,
        network_retries: int | None = None,
        lock_timeout: float | None = None) -> dict:
    """直播专业大屏口径的实时累计快照（2026-08-08 逆向确认）。

    大屏页面（live-professional-screen）用 tblive.portal 接口 + timeType=5 +
    searchType=1 + plantflow=web，totalStats 以 CSV 指标行返回；与 iliad 接口
    数值口径不同（成交金额/观看人数等）。简报、复盘、周报统一改用本函数后
    与数据大屏完全一致。
    """
    now_ms = int(time.time() * 1000)
    call_options = {}
    if request_timeout != 20.0:
        call_options["request_timeout"] = float(request_timeout)
    if network_retries is not None:
        call_options["network_retries"] = int(network_retries)
    if lock_timeout is not None:
        call_options["lock_timeout"] = float(lock_timeout)
    payload = _client(cfg).call(SCREEN_API, API_VERSION, {
        "liveId": str(live_id), "types": "totalStats", "timeType": 5,
        "searchType": "1", "startTime": now_ms - 10 * 60 * 1000,
        "endTime": now_ms, "extParams": json.dumps({"plantflow": "web"}),
    }, **call_options)
    rows: dict[str, dict] = {}
    data = payload.get("data") or {}
    for item in data.get("dataList") or []:
        for chunk in item.get("data") or []:
            parsed = _parse_screen_csv(chunk.get("value", ""))
            key = parsed.get("valueType")
            if key:
                rows[key] = parsed
    if not rows:
        return {}
    out: dict = {
        "live_id": str(live_id), "fetched_at": _now(),
        "source": "screen.totalStats", "source_scope": "直播专业大屏口径累计",
        "data_state": "ok", "room_status": str(data.get("roomStatus") or ""), "raw": rows,
    }
    for source_key, target_key in TOTAL_FIELD_MAP.items():
        row = rows.get(source_key)
        if not row:
            continue
        n = _number(row.get("valueNum"))
        if n is None:
            n = _number(row.get("value"), percent=True)
        if n is None:
            continue
        if target_key.endswith("_rate") or target_key in ("pctr",):
            out[target_key] = float(n)
        elif target_key in ("pay_amt", "refund_amt", "stay_time_pu"):
            out[target_key] = round(float(n), 2)
        else:
            out[target_key] = int(round(n))
    out["deal_amt"] = out.get("pay_amt")
    out["uv"] = out.get("online_uv")
    out["uv_peak"] = out.get("max_online_uv")
    log.info("大屏累计快照：在线=%s 最高=%s 观看=%s 成交人数=%s 成交=%.2f",
             out.get("online_uv"), out.get("max_online_uv"), out.get("viewer_uv"),
             out.get("buyer_cnt"), out.get("pay_amt") or 0.0)
    return out


def fetch_metric_series(cfg: dict, live_id: str, search_type: str = "1",
                        time_type: int = 1, types: list[str] | None = None,
                        start_ms: int | None = None, end_ms: int | None = None) -> dict:
    """抓分钟趋势并聚合指定时段；该结果是时段量，不是平台累计总量。"""
    types = types or CORE_TYPES
    windows: list[tuple[int | None, int | None]] = [(None, None)]
    if start_ms is not None and end_ms is not None and end_ms > start_ms:
        windows = []
        cursor = start_ms
        while cursor < end_ms:
            nxt = min(cursor + MAX_WINDOW_MS, end_ms)
            windows.append((cursor, nxt))
            cursor = nxt

    client = _client(cfg)
    all_raw: dict[str, list[dict]] = {}
    for window_start, window_end in windows:
        params: dict = {
            "liveId": str(live_id), "types": ",".join(types), "timeType": time_type,
            "searchType": search_type, "extParams": "{}",
        }
        if window_start is not None:
            params.update(startTime=window_start, endTime=window_end)
        payload = client.call(SERIES_API, API_VERSION, params)
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            continue
        for item in data.get("dataList") or []:
            typ = item.get("type")
            if not typ:
                continue
            seen = {x.get("time") for x in all_raw.get(typ, [])}
            for row in parse_buckets(item):
                marker = row.get("time")
                if start_ms is not None and end_ms is not None:
                    try:
                        marker_ms = int(marker)
                    except (TypeError, ValueError):
                        continue
                    # 正式小时使用半开区间 [start, end)：整点 end 的
                    # 分钟桶属于下一小时，不能被当前小时重复计算。
                    if marker_ms < int(start_ms) or marker_ms >= int(end_ms):
                        continue
                if marker not in seen:
                    all_raw.setdefault(typ, []).append(row)
                    seen.add(marker)
    if not all_raw:
        return {}

    out: dict = {
        "live_id": str(live_id), "fetched_at": _now(), "raw": all_raw,
        "source": "tblive.portal.minuteSeries", "source_scope": "指定时段分钟趋势",
        "data_state": "ok", "search_type": search_type,
    }
    for typ, rows in all_raw.items():
        key = MAIN_FIELD.get(typ, "value")
        values = [_number(row.get(key)) for row in rows]
        values = [v for v in values if v is not None]
        if not values:
            continue
        if typ == "uv":
            out["online_uv"] = int(round(values[-1]))
            out["max_online_uv"] = int(round(max(values)))
            out["uv_avg"] = round(sum(values) / len(values), 1)
            enters = [_number(row.get("visitorEnter")) for row in rows]
            enters = [v for v in enters if v is not None]
            if enters:
                out["visitor_total"] = int(round(sum(enters)))
        elif typ == "itemClick":
            out["ipv_total"] = int(round(sum(values)))
        elif typ == "deal":
            out["pay_amt"] = round(sum(values), 2)
            out["deal_amt"] = out["pay_amt"]
        elif typ == "heatScore":
            out["heat_score"] = int(round(max(values)))
    out["uv"] = out.get("online_uv")
    out["uv_peak"] = out.get("max_online_uv")
    return out


# 保留旧函数名，外部脚本无需同时迁移。
fetch_live_metrics = fetch_metric_series


def _date_range(start_ms: int | None, end_ms: int | None) -> tuple[str, str]:
    end = dt.datetime.fromtimestamp(
        (end_ms or int(now_shanghai().timestamp() * 1000)) / 1000, SHANGHAI)
    start = dt.datetime.fromtimestamp(
        (start_ms or int((end - dt.timedelta(days=1)).timestamp() * 1000)) / 1000,
        SHANGHAI)
    return (start - dt.timedelta(days=1)).strftime("%Y%m%d"), (end + dt.timedelta(days=1)).strftime("%Y%m%d")


def fetch_session_metrics(cfg: dict, live_id: str, start_ms: int | None = None,
                          end_ms: int | None = None) -> dict:
    """读取「场次分析」冻结行；仅接受 content_id 与 live_id 精确匹配的记录。"""
    begin, end = _date_range(start_ms, end_ms)
    param = {
        "queryCycleStartDate": begin, "queryCycleEndDate": end,
        "beginDate": begin, "endDate": end, "queryUserRole": "ALL",
        "start": "0", "hit": "100", "orderColumn": "live_start_time", "orderType": "1",
    }
    payload = _client(cfg).call(GENERAL_API, API_VERSION, {
        "dataApi": SESSION_DATA_API,
        "param": json.dumps(param, ensure_ascii=False, separators=(",", ":")),
    })
    rows = (payload.get("data") or {}).get("result") or []
    row = next((x for x in rows if str(x.get("content_id") or "") == str(live_id)), None)
    if not row:
        return {}

    out: dict = {
        "live_id": str(live_id), "fetched_at": _now(),
        "source": FROZEN_SESSION_SOURCE,
        "source_scope": "平台完整场次（冻结数据）", "data_state": "ok",
        "source_started_at": row.get("live_start_time") or "",
        "source_ended_at": row.get("live_end_time") or "",
        "raw": {"session": row},
    }
    for source_key, target_key in SESSION_FIELD_MAP.items():
        value = row.get(source_key)
        n = _number(value, percent=target_key.endswith("_rate") or target_key == "pctr")
        if n is None:
            continue
        if target_key.endswith("_rate") or target_key in ("pctr", "stay_time_pu"):
            out[target_key] = float(n)
        elif target_key in ("pay_amt", "refund_amt"):
            out[target_key] = round(float(n), 2)
        else:
            out[target_key] = int(round(n))
    out["deal_amt"] = out.get("pay_amt")
    out["uv_peak"] = out.get("max_online_uv")
    return out


def ensure_table(store) -> None:
    store.conn.execute("""
        CREATE TABLE IF NOT EXISTS stream_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stream_id INTEGER NOT NULL UNIQUE REFERENCES streams(id),
            live_id TEXT DEFAULT '', fetched_at TEXT,
            online_uv INTEGER, max_online_uv INTEGER, uv_avg REAL,
            viewer_uv INTEGER, viewer_pv INTEGER, visitor_total INTEGER,
            ipv_total INTEGER, ipv_uv INTEGER,
            pay_amt REAL, deal_amt REAL, buyer_cnt INTEGER, order_cnt INTEGER, item_qty INTEGER,
            pay_byr_rate REAL, ipv_uv_rate REAL,
            atn_uv INTEGER, comment_uv INTEGER, favor_uv INTEGER, share_uv INTEGER,
            heat_score INTEGER, refund_amt REAL, refund_uv INTEGER, refund_item_qty INTEGER,
            stay_time_pu REAL, pctr REAL, popularity_score INTEGER, trd_score INTEGER,
            source TEXT DEFAULT '', source_scope TEXT DEFAULT '',
            source_started_at TEXT DEFAULT '', source_ended_at TEXT DEFAULT '',
            data_state TEXT DEFAULT '', data_issues TEXT DEFAULT '[]', coverage_ratio REAL,
            raw TEXT DEFAULT '{}', created_at TEXT DEFAULT (datetime('now','localtime'))
        )""")
    existing = {r["name"] for r in store.conn.execute("PRAGMA table_info(stream_metrics)")}
    columns = {
        "online_uv": "INTEGER", "max_online_uv": "INTEGER", "viewer_uv": "INTEGER",
        "viewer_pv": "INTEGER", "visitor_total": "INTEGER", "ipv_uv": "INTEGER",
        "pay_amt": "REAL", "buyer_cnt": "INTEGER", "item_qty": "INTEGER",
        "pay_byr_rate": "REAL", "ipv_uv_rate": "REAL", "atn_uv": "INTEGER",
        "comment_uv": "INTEGER", "favor_uv": "INTEGER", "share_uv": "INTEGER",
        "refund_amt": "REAL", "refund_uv": "INTEGER", "refund_item_qty": "INTEGER",
        "stay_time_pu": "REAL", "pctr": "REAL", "popularity_score": "INTEGER",
        "trd_score": "INTEGER", "source": "TEXT DEFAULT ''",
        "source_scope": "TEXT DEFAULT ''", "source_started_at": "TEXT DEFAULT ''",
        "source_ended_at": "TEXT DEFAULT ''", "data_state": "TEXT DEFAULT ''",
        "data_issues": "TEXT DEFAULT '[]'", "coverage_ratio": "REAL",
    }
    for name, decl in columns.items():
        if name not in existing:
            store.conn.execute(f"ALTER TABLE stream_metrics ADD COLUMN {name} {decl}")
    store.conn.commit()


METRIC_COLUMNS = (
    "live_id", "fetched_at", "online_uv", "max_online_uv", "uv_avg", "viewer_uv",
    "viewer_pv", "visitor_total", "ipv_total", "ipv_uv", "pay_amt", "deal_amt",
    "buyer_cnt", "order_cnt", "item_qty", "pay_byr_rate", "ipv_uv_rate", "atn_uv",
    "comment_uv", "favor_uv", "share_uv", "heat_score", "refund_amt", "refund_uv",
    "refund_item_qty", "stay_time_pu", "pctr", "popularity_score", "trd_score",
    "source", "source_scope", "source_started_at", "source_ended_at", "data_state",
    "coverage_ratio",
)


def save_metrics(store, stream_id: int, metrics: dict) -> bool:
    """保存场次指标，返回是否写入。

    平台冻结场次数据是最终口径，不能被后续接口失败时的本地时段回退数据降级覆盖。
    该判断放在 UPSERT 的原子 WHERE 条件中，避免并发读取后判断的竞争窗口。
    """
    ensure_table(store)
    cols = ",".join(METRIC_COLUMNS)
    placeholders = ",".join("?" for _ in METRIC_COLUMNS)
    updates = ",".join(f"{c}=excluded.{c}" for c in METRIC_COLUMNS)
    issues = json.dumps(metrics.get("data_issues") or [], ensure_ascii=False)
    raw = json.dumps(metrics.get("raw") or {}, ensure_ascii=False)
    with store._write_lock:
        cur = store.conn.execute(
            f"""INSERT INTO stream_metrics(stream_id,{cols},data_issues,raw)
                VALUES(?,{placeholders},?,?)
                ON CONFLICT(stream_id) DO UPDATE SET {updates},
                    data_issues=excluded.data_issues, raw=excluded.raw
                WHERE NOT (
                    stream_metrics.source = '{FROZEN_SESSION_SOURCE}'
                    AND stream_metrics.data_state = 'ok'
                    AND NOT (
                        excluded.source = '{FROZEN_SESSION_SOURCE}'
                        AND excluded.data_state = 'ok'
                    )
                )""",
            (stream_id, *(metrics.get(c) for c in METRIC_COLUMNS), issues, raw),
        )
        store.conn.commit()
        return cur.rowcount > 0


def get_metrics(store, stream_id: int) -> dict:
    ensure_table(store)
    row = store.conn.execute("SELECT * FROM stream_metrics WHERE stream_id=?", (stream_id,)).fetchone()
    return dict(row) if row else {}


def save_brief_snapshot(store, stream_id: int, metrics: dict,
                        snapshot_kind: str = "brief") -> int:
    """保存累计快照；未返回字段直接写 NULL。"""
    values = [metrics.get(k) for k in SNAPSHOT_FIELDS]
    fields = ",".join(SNAPSHOT_FIELDS)
    placeholders = ",".join("?" for _ in SNAPSHOT_FIELDS)
    return store.execute(
        f"""INSERT INTO brief_snapshots(
                stream_id,ts,snapshot_kind,source,data_state,{fields},raw)
            VALUES(?,?,?,?,?,{placeholders},?)""",
        (stream_id, metrics.get("fetched_at") or _now(), snapshot_kind,
         metrics.get("source") or "", metrics.get("data_state") or "",
         *values, json.dumps(metrics.get("raw") or {}, ensure_ascii=False)),
    )


def latest_brief_snapshot(store, stream_id: int, *, before_save: bool = True) -> dict:
    rows = store.query(
        """SELECT * FROM brief_snapshots
           WHERE stream_id=? AND source!='' ORDER BY id DESC LIMIT 1""",
        (stream_id,),
    )
    return dict(rows[0]) if rows else {}


def baseline_snapshot(store, stream_id: int) -> dict:
    rows = store.query(
        """SELECT * FROM brief_snapshots
           WHERE stream_id=? AND snapshot_kind='baseline' AND source!=''
           ORDER BY id LIMIT 1""",
        (stream_id,),
    )
    return dict(rows[0]) if rows else {}


def safe_delta(current, previous):
    """累计指标差值；任一缺失或累计值重置时返回 None。"""
    a, b = _number(current), _number(previous)
    if a is None or b is None or a < b:
        return None
    return a - b


def weighted_interval_average(current_average, current_count,
                              previous_average, previous_count):
    """从两个累计平均值反算区间平均值；不可核验时返回 ``None``。"""
    current_avg = _number(current_average)
    current_total = _number(current_count)
    previous_avg = _number(previous_average)
    previous_total = _number(previous_count)
    if None in (current_avg, current_total, previous_avg, previous_total):
        return None
    interval_count = current_total - previous_total
    if interval_count <= 0 or current_total < previous_total:
        return None
    interval_total = current_avg * current_total - previous_avg * previous_total
    if interval_total < 0:
        return None
    return interval_total / interval_count


def fetch_and_save(cfg: dict, store, stream_id: int, live_id: str,
                   search_type: str = "2", start_ms: int | None = None,
                   end_ms: int | None = None) -> dict:
    """保存下播指标：冻结场次优先，未结算时保存带明确限制的本地时段数据。"""
    del search_type  # 兼容旧调用；最终数据源由可信度策略决定。
    frozen_seen = False
    try:
        frozen = fetch_session_metrics(cfg, live_id, start_ms=start_ms, end_ms=end_ms)
        if frozen:
            frozen_seen = True
            # 平台整场总账无论本地是否分段都只保存一份。
            upsert_platform_session(store, live_id, frozen)
            # 冻结场次提供最终指标；另补抓分钟趋势，只用于把峰值与本地转写对齐，
            # 不会替代冻结行的订单数等权威字段。
            local_frozen = copy.deepcopy(frozen)
            try:
                series = fetch_metric_series(
                    cfg, live_id, search_type="2", start_ms=start_ms, end_ms=end_ms)
                if series.get("raw"):
                    local_frozen.setdefault("raw", {})["series"] = series["raw"]
            except Exception as exc:
                log.warning("冻结场次分钟趋势暂不可用，跳过数据峰值话术: %s", exc)
            if _can_assign_frozen_to_stream(store, stream_id, live_id, frozen):
                if save_metrics(store, stream_id, local_frozen):
                    return local_frozen
                return get_metrics(store, stream_id)
            log.info("liveId=%s 是本地分段场次，冻结总账不写入场次 #%d",
                     live_id, stream_id)
    except Exception as exc:
        log.warning("冻结场次数据暂不可用: %s", exc)

    issues = (["平台整场冻结总账已入库；本场为本地拆分时段，不重复写入整场指标"]
              if frozen_seen else
              ["平台场次冻结数据尚未返回；当前仅能核验本地录制时段"])
    totals: dict = {}
    try:
        totals = fetch_live_totals(cfg, live_id)
    except Exception as exc:
        issues.append(f"累计快照抓取失败：{exc}")
    # 平台已确认非在播时，冻结行可能有数秒结算延迟；短暂重试，优先交付完整最终报告。
    if totals and totals.get("room_status") not in ("1", "2", "LIVE", "live"):
        for delay in (5, 10):
            time.sleep(delay)
            try:
                frozen = fetch_session_metrics(cfg, live_id, start_ms=start_ms, end_ms=end_ms)
                if frozen:
                    upsert_platform_session(store, live_id, frozen)
                    local_frozen = copy.deepcopy(frozen)
                    try:
                        series = fetch_metric_series(
                            cfg, live_id, search_type="2", start_ms=start_ms, end_ms=end_ms)
                        if series.get("raw"):
                            local_frozen.setdefault("raw", {})["series"] = series["raw"]
                    except Exception as series_exc:
                        log.warning("冻结场次分钟趋势暂不可用，跳过数据峰值话术: %s", series_exc)
                    if _can_assign_frozen_to_stream(store, stream_id, live_id, frozen):
                        if save_metrics(store, stream_id, local_frozen):
                            return local_frozen
                        return get_metrics(store, stream_id)
            except Exception as exc:
                log.warning("等待冻结场次数据重试失败: %s", exc)

    series: dict = {}
    try:
        series = fetch_metric_series(cfg, live_id, search_type="2", start_ms=start_ms, end_ms=end_ms)
    except Exception as exc:
        issues.append(f"分钟趋势抓取失败：{exc}")

    base = baseline_snapshot(store, stream_id)
    out: dict = {
        "live_id": str(live_id), "fetched_at": _now(),
        "source": "hybrid.local-period", "source_scope": "本地录制时段（部分数据）",
        "data_state": "partial", "data_issues": issues,
        "raw": {"series": series.get("raw") or {}, "totals": totals.get("raw") or {},
                "baseline": {k: base.get(k) for k in SNAPSHOT_FIELDS} if base else {}},
    }
    for key in ("online_uv", "max_online_uv", "uv_avg", "visitor_total", "ipv_total",
                "heat_score"):
        if series.get(key) is not None:
            out[key] = series[key]
    if start_ms and end_ms and end_ms > start_ms:
        minutes = (end_ms - start_ms) / 60000
        buckets = len((series.get("raw") or {}).get("uv") or [])
        out["coverage_ratio"] = min(1.0, buckets / minutes) if minutes else None

    if base and totals:
        for key in ("viewer_uv", "viewer_pv", "pay_amt", "buyer_cnt", "item_qty", "atn_uv",
                    "comment_uv", "favor_uv", "share_uv"):
            delta = safe_delta(totals.get(key), base.get(key))
            if delta is not None:
                out[key] = round(delta, 2) if key == "pay_amt" else int(round(delta))
        if out.get("pay_amt") is not None:
            out["deal_amt"] = out["pay_amt"]
    else:
        issues.append("缺少开录基线，累计观看/成交人数/成交件数不能归属于本地时段")
        if series.get("pay_amt") is not None:
            out["pay_amt"] = series["pay_amt"]
            out["deal_amt"] = series["pay_amt"]
    # 分钟接口没有订单数；保持 None 并明确说明。
    issues.append("当前实时接口不提供订单数，等待平台场次冻结后才能确认")
    if save_metrics(store, stream_id, out):
        return out
    return get_metrics(store, stream_id)


def ensure_platform_sessions(store) -> None:
    store.conn.execute("""
        CREATE TABLE IF NOT EXISTS platform_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            live_id TEXT NOT NULL UNIQUE,
            content_id TEXT DEFAULT '',
            started_at TEXT DEFAULT '',
            ended_at TEXT DEFAULT '',
            pay_amt REAL, order_cnt INTEGER, buyer_cnt INTEGER,
            viewer_uv INTEGER, viewer_pv INTEGER, max_online_uv INTEGER, item_qty INTEGER,
            raw TEXT DEFAULT '{}',
            fetched_at TEXT DEFAULT (datetime('now','localtime')),
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )""")


def upsert_platform_session(store, live_id: str, frozen: dict) -> None:
    """把平台场次整场总账写入 platform_sessions（liveId 唯一，幂等覆盖）。"""
    ensure_platform_sessions(store)
    raw = json.dumps(frozen.get("raw") or {}, ensure_ascii=False)
    with store._write_lock:
        store.conn.execute(
            """INSERT INTO platform_sessions(
                   live_id, content_id, started_at, ended_at, pay_amt, order_cnt,
                   buyer_cnt, viewer_uv, viewer_pv, max_online_uv, item_qty, raw)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(live_id) DO UPDATE SET
                   content_id=excluded.content_id, started_at=excluded.started_at,
                   ended_at=excluded.ended_at, pay_amt=excluded.pay_amt,
                   order_cnt=excluded.order_cnt, buyer_cnt=excluded.buyer_cnt,
                   viewer_uv=excluded.viewer_uv, viewer_pv=excluded.viewer_pv,
                   max_online_uv=excluded.max_online_uv, item_qty=excluded.item_qty,
                   raw=excluded.raw, fetched_at=excluded.fetched_at""",
            (str(live_id), str(frozen.get("content_id") or ""),
             str(frozen.get("source_started_at") or ""), str(frozen.get("source_ended_at") or ""),
             frozen.get("pay_amt"), frozen.get("order_cnt"), frozen.get("buyer_cnt"),
             frozen.get("viewer_uv"), frozen.get("viewer_pv"), frozen.get("max_online_uv"),
             frozen.get("item_qty"), raw))
        store.conn.commit()


def _local_streams_for_live(store, day_str: str) -> dict[str, list]:
    """按 liveId 分组某日的本地场次；同一 liveId 多场 = 平台一场被本地拆分。"""
    rows = store.query(
        "SELECT id, live_id, started_at, ended_at FROM streams "
        "WHERE status NOT IN ('recording','recovering','interrupted') "
        "AND started_at LIKE ?",
        (day_str + '%',))
    grouped: dict[str, list] = {}
    for s in rows:
        live_id = s["live_id"] or ""
        if not live_id:
            continue
        grouped.setdefault(live_id, []).append(s)
    return grouped


def _can_assign_frozen_to_stream(store, stream_id: int, live_id: str,
                                 frozen: dict, tolerance: float = 0.15) -> bool:
    """只有 liveId 与本地完整场次一对一且时长接近时才可下发总账。"""
    rows = store.query(
        """SELECT id,started_at,ended_at,duration_sec FROM streams
           WHERE live_id=? AND status!='interrupted' AND (
               file_path!='' OR id=?
           ) ORDER BY id""",
        (str(live_id), stream_id),
    )
    if len(rows) != 1 or int(rows[0]["id"]) != int(stream_id):
        return False
    local_sec = float(rows[0]["duration_sec"] or 0)
    if local_sec <= 0:
        local_sec = _parse_dt_sec(rows[0]["started_at"], rows[0]["ended_at"]) or 0
    platform_sec = _parse_dt_sec(
        frozen.get("source_started_at"), frozen.get("source_ended_at")) or 0
    if local_sec <= 0 or platform_sec <= 0:
        return False
    return abs(local_sec - platform_sec) / max(platform_sec, 1) <= tolerance


def backfill_frozen_metrics(cfg: dict, store, day_str: str, *,
                            strict: bool = False) -> list[int]:
    """次日补抓：为指定日期（YYYY-MM-DD）已结束但仍是估算口径的场次抓平台冻结数据。

    平台把一天直播算作一个平台场次（liveId 当天不变）；本地可能因轮换/重启拆成多场。
    规则：
    - 平台场次整场总账写入 platform_sessions（liveId 唯一）。
    - 该 liveId 本地只有一场且时长与平台场次接近（偏差 <15%）时，才把冻结数据写进本地场次。
    - 本地拆分成多场时，不把整场总账重复写入每一场（避免求和翻倍），各场保持估算。
    幂等：已冻结的本地场次跳过；尚未结算的静默跳过，后续日期再补。
    """
    updated: list[int] = []
    errors: list[Exception] = []
    for live_id, local_streams in _local_streams_for_live(store, day_str).items():
        if not local_streams:
            continue
        probe = local_streams[0]
        start_ms, end_ms = None, None
        try:
            if probe["started_at"]:
                start_ms = local_epoch_ms(probe["started_at"])
            if probe["ended_at"]:
                end_ms = local_epoch_ms(probe["ended_at"])
            frozen = fetch_session_metrics(cfg, live_id, start_ms=start_ms, end_ms=end_ms)
        except Exception as exc:
            log.warning("冻结数据补抓失败 liveId=%s: %s", live_id, str(exc)[:160])
            errors.append(exc)
            continue
        if not frozen:
            continue
        # 平台场次整场总账：只存一份，供拆分场次关联展示。
        upsert_platform_session(store, live_id, frozen)
        if len(local_streams) > 1:
            log.info("liveId=%s 本地 %d 场（平台 1 场），总账已入库，各场保持估算",
                     live_id, len(local_streams))
            continue
        # 一对一：仅当本地时长与平台场次接近时才写冻结数据，防止小分段误配整场值。
        s = local_streams[0]
        existing = get_metrics(store, s["id"])
        if existing and str(existing.get("source") or "").startswith("generalQuery"):
            continue
        try:
            if not _can_assign_frozen_to_stream(store, s["id"], live_id, frozen):
                local_sec = _parse_dt_sec(s["started_at"], s["ended_at"])
                plat_sec = _parse_dt_sec(
                    frozen.get("source_started_at"), frozen.get("source_ended_at"))
                log.info("liveId=%s 本地 %.0fs 与平台 %.0fs 偏差过大，跳过本地写入",
                         live_id, local_sec or 0, plat_sec or 0)
                continue
            # 保留旧分钟序列，冻结 raw 只补充 session 总账，避免 series 丢失。
            old_raw = json.loads(existing.get("raw") or "{}") if existing else {}
            if old_raw.get("series"):
                frozen.setdefault("raw", {})["series"] = old_raw["series"]
            if save_metrics(store, s["id"], frozen):
                updated.append(s["id"])
                log.info("冻结数据补抓成功：场次 #%d 订单数=%s 成交=%.2f",
                         s["id"], frozen.get("order_cnt"), frozen.get("pay_amt") or 0)
        except Exception as exc:
            log.warning("冻结数据补抓失败 场次 #%d: %s", s["id"], str(exc)[:160])
            errors.append(exc)
    if updated:
        log.info("冻结数据补抓完成 %s：%d 场已升级为平台最终值", day_str, len(updated))
    if strict and errors:
        raise RuntimeError(
            f"冻结数据补抓 {len(errors)} 个请求失败；首个错误：{str(errors[0])[:180]}")
    return updated


def _parse_dt_sec(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        return (dt.datetime.strptime(end, "%Y-%m-%d %H:%M:%S")
                - dt.datetime.strptime(start, "%Y-%m-%d %H:%M:%S")).total_seconds()
    except ValueError:
        return None


def check_metrics_health(metrics: dict, duration_min: float) -> list[str]:
    """数据质量门禁。只报告可证实的缺失/冲突，不把真实的 0 当异常。"""
    if not metrics:
        return ["未抓到任何经营数据"]
    problems = list(metrics.get("data_issues") or [])
    state = metrics.get("data_state")
    if state and state != "ok" and not problems:
        problems.append(f"数据状态为 {state}")
    if metrics.get("source", "").startswith("generalQuery"):
        for key, label in (("viewer_uv", "观看人数"), ("pay_amt", "成交金额"),
                           ("buyer_cnt", "成交人数"), ("order_cnt", "成交订单数")):
            if metrics.get(key) is None:
                problems.append(f"冻结场次缺少{label}")
    coverage = metrics.get("coverage_ratio")
    if coverage is not None and duration_min > 30 and coverage < 0.8:
        problems.append(f"分钟趋势覆盖率仅 {coverage:.0%}")
    if (metrics.get("pay_amt") or 0) > 0 and metrics.get("buyer_cnt") == 0:
        problems.append("成交金额大于 0，但成交人数为 0（字段互相矛盾）")
    return list(dict.fromkeys(problems))


def _fmt_number(value, digits: int = 0) -> str:
    if value is None:
        return "暂无"
    if digits:
        return f"{float(value):,.{digits}f}"
    return f"{float(value):,.0f}"


def _fmt_rate(value) -> str:
    if value is None:
        return "暂无"
    n = float(value)
    return f"{n * 100:.2f}%" if abs(n) <= 1 else f"{n:.2f}%"


def format_metrics_text(metrics: dict) -> str:
    """报告/提示词文本：显示口径、状态和缺失值，不隐藏真实 0。"""
    if not metrics:
        return "（未抓取到经营数据）"
    lines = [
        f"数据口径：{metrics.get('source_scope') or '未说明'}；来源：{metrics.get('source') or '未知'}；状态：{metrics.get('data_state') or '未知'}",
    ]
    if metrics.get("source_started_at") or metrics.get("source_ended_at"):
        lines.append(f"平台场次时间：{metrics.get('source_started_at') or '暂无'} ~ {metrics.get('source_ended_at') or '暂无'}")
    fields = (
        ("online_uv", "当前在线", "人", 0), ("max_online_uv", "最高在线", "人", 0),
        ("viewer_uv", "观看人数", "人", 0), ("viewer_pv", "观看次数", "次", 0),
        ("visitor_total", "时段进入人次", "人次", 0), ("ipv_total", "商品点击次数", "次", 0),
        ("ipv_uv", "商品点击人数", "人", 0), ("pay_amt", "成交金额", "元", 2),
        ("buyer_cnt", "成交人数", "人", 0), ("order_cnt", "成交订单数", "单", 0),
        ("item_qty", "成交件数", "件", 0), ("atn_uv", "新增粉丝", "人", 0),
        ("comment_uv", "评论人数", "人", 0), ("favor_uv", "点赞人数", "人", 0),
        ("share_uv", "分享人数", "人", 0), ("heat_score", "热度分", "", 0),
    )
    shown = []
    for key, label, unit, digits in fields:
        if key in metrics:
            value = metrics.get(key)
            shown.append(f"{label}：{_fmt_number(value, digits)}{unit if value is not None else ''}")
    if metrics.get("pay_byr_rate") is not None:
        shown.append(
            f"成交转化率（成交人数/商品点击人数）：{_fmt_rate(metrics['pay_byr_rate'])}"
        )
    if metrics.get("ipv_uv_rate") is not None:
        shown.append(
            f"商品点击率（平台字段，分母口径以千牛为准）：{_fmt_rate(metrics['ipv_uv_rate'])}"
        )
    lines.append("；".join(shown) if shown else "无可用数值字段")
    issues = metrics.get("data_issues") or []
    if isinstance(issues, str):
        try:
            issues = json.loads(issues)
        except ValueError:
            issues = [issues]
    if issues:
        lines.append("数据限制：" + "；".join(str(x) for x in issues))
    return "\n".join(lines)
