"""直播中控台每日经营数据（新版 generalQuery 接口，2026-08-02 逆向确认）

接口：mtop.dreamweb.query.general.generalQuery（从直播中控台页面抓真实请求逆向）
dataApi：zkt_zbgl_core_card_overview（中控台核心卡片概览）
param（JSON 字符串）：
  startDate/endDate  查询日（YYYYMMDD）
  cpStartDate/cpEndDate  对比日（环比）
  cycleCode  "1d" 日 / "7d" 近7日
  fieldColumns  页面实际请求的核心指标编码
返回：data.result[] = {type: 指标编码, value: [{f: 格式化值, avg, cp_rate: 环比, ...}]}

注意：旧接口 currentDimension=atn_uv 已废弃（旧 atn_uv 口径不可用），
新版观看人数字段为 look_uv_nd。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta

log = logging.getLogger("metrics.daily")

API = "mtop.dreamweb.query.general.generalQuery"
API_VERSION = "1.0"
DATA_API = "zkt_zbgl_core_card_overview"

# 页面 2026-08-02 实际请求字段。旧代码臆测的 order_cnt_nd/ipv_nd 等字段返回 null，
# 已全部移除；每日概览显示的是「成交人数」，不是「下单数」。
DAILY_FIELDS = (
    "pay_amt_nd,pay_amt_nd_fans_boron_and_above,pay_amt_nd_fans_boron_and_above_rate,"
    "pay_amt_nd_fans_new_and_below,pay_amt_nd_fans_new_and_below_rate,look_uv_nd,"
    "pay_byr_cnt_nd_distinct,pay_byr_cnt_nd,mbr_cnt_incr_nd,pay_byr_cnt_nd_mbr,"
    "pay_amt_nd_mbr,pay_byr_cnt_nd_gouwujin"
)

FIELD_LABELS = {
    "pay_amt_nd": "成交金额(元)", "look_uv_nd": "观看人数",
    "pay_byr_cnt_nd": "成交人数", "pay_byr_cnt_nd_distinct": "去重成交人数",
    "mbr_cnt_incr_nd": "新增会员数", "pay_byr_cnt_nd_mbr": "会员成交人数",
    "pay_amt_nd_mbr": "会员成交金额", "pay_byr_cnt_nd_gouwujin": "购物金充值人数",
    "pay_amt_nd_fans_boron_and_above": "铁粉及以上成交金额",
    "pay_amt_nd_fans_boron_and_above_rate": "铁粉及以上成交占比",
    "pay_amt_nd_fans_new_and_below": "新粉及以下成交金额",
    "pay_amt_nd_fans_new_and_below_rate": "新粉及以下成交占比",
}

# 数据库列映射（数值字段）
DB_COLS = {
    "pay_amt_nd": "pay_amt", "look_uv_nd": "look_uv",
    "pay_byr_cnt_nd": "pay_byr_cnt", "pay_byr_cnt_nd_distinct": "pay_byr_cnt_distinct",
    "mbr_cnt_incr_nd": "mbr_cnt_incr", "pay_byr_cnt_nd_mbr": "pay_byr_cnt_mbr",
    "pay_amt_nd_mbr": "pay_amt_mbr", "pay_byr_cnt_nd_gouwujin": "pay_byr_cnt_gouwujin",
    "pay_amt_nd_fans_boron_and_above": "pay_amt_loyal_fans",
    "pay_amt_nd_fans_boron_and_above_rate": "pay_amt_loyal_fans_rate",
    "pay_amt_nd_fans_new_and_below": "pay_amt_new_fans",
    "pay_amt_nd_fans_new_and_below_rate": "pay_amt_new_fans_rate",
}


class DailyDataPending(RuntimeError):
    """平台返回了尚未结算的每日数据，调用方应稍后重试。"""


def has_local_activity(store, date_str: str) -> bool:
    """本地是否有覆盖该自然日的直播记录。"""
    try:
        day = datetime.strptime(date_str, "%Y%m%d")
    except (TypeError, ValueError):
        return False
    start = day.strftime("%Y-%m-%d 00:00:00")
    end = (day + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
    row = store.conn.execute(
        """SELECT 1 FROM streams
           WHERE started_at < ?
             AND COALESCE(ended_at, datetime('now','localtime')) >= ?
           LIMIT 1""",
        (end, start),
    ).fetchone()
    return row is not None


def assess_daily_quality(store, data: dict) -> tuple[str, list[str]]:
    """区分真实零值与尚未生成的日结响应。"""
    raw = data.get("raw") or {}
    if not raw:
        return "pending", ["平台未返回每日经营指标"]
    core = [data.get("pay_amt_nd"), data.get("look_uv_nd"), data.get("pay_byr_cnt_nd")]
    numeric = [float(value) for value in core if isinstance(value, (int, float))]
    if any(value > 0 for value in numeric):
        return "complete", []
    if len(numeric) < len(core):
        return "pending", ["核心日结指标缺失"]
    if has_local_activity(store, str(data.get("date") or "")):
        return "pending", ["当日存在直播记录，但平台日结核心指标全为 0"]
    return "pending", ["平台日结核心指标全为 0，无法确认已完成结算"]


def _client(cfg: dict):
    from ..recorder.mtop import shared_client
    return shared_client(cfg)


def fetch_daily(cfg: dict, date_str: str, cp_date: str = "", fields: str = DAILY_FIELDS) -> dict:
    """抓取某天的每日数据。date_str/cp_date 格式 YYYYMMDD；cp_date 留空自动取前一天"""
    if not cp_date:
        import datetime
        cp_date = (datetime.datetime.strptime(date_str, "%Y%m%d")
                   - datetime.timedelta(days=1)).strftime("%Y%m%d")
    param = {
        "startDate": date_str, "endDate": date_str,
        "cpStartDate": cp_date, "cpEndDate": cp_date,
        "cycleCode": "1d",
        "fieldColumns": fields,
    }
    client = _client(cfg)
    r = client.call(API, API_VERSION, {
        "dataApi": DATA_API,
        "param": json.dumps(param, ensure_ascii=False, separators=(",", ":")),
    })
    result = (r.get("data") or {}).get("result") or []
    out: dict = {"date": date_str, "raw": {}}
    for item in result:
        typ = item.get("type")
        vals = item.get("value") or [{}]
        v = vals[0] if vals else {}
        out["raw"][typ] = {
            "f": v.get("f"), "avg": v.get("avg"), "cp_rate": v.get("cp_rate"),
        }
        # 数值化（f 优先，其次 avg）
        num = None
        for src in ("f", "avg"):
            s = v.get(src)
            if s not in (None, "", "--"):
                try:
                    num = float(str(s).replace(",", "").replace("%", ""))
                    break
                except ValueError:
                    pass
        if num is not None:
            out[typ] = num
    log.info("每日数据抓取完成 %s：%d 个指标", date_str, len(out.get("raw", {})))
    return out


# ---------- 存储 ----------
def ensure_table(store) -> None:
    store.conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL UNIQUE,
            pay_amt REAL DEFAULT 0, look_uv INTEGER DEFAULT 0, order_cnt INTEGER DEFAULT 0,
            ipv INTEGER DEFAULT 0, cvr REAL DEFAULT 0, new_fans INTEGER DEFAULT 0,
            deal_cnt INTEGER DEFAULT 0, avg_online INTEGER DEFAULT 0, praise INTEGER DEFAULT 0,
            comment INTEGER DEFAULT 0, share INTEGER DEFAULT 0, follow INTEGER DEFAULT 0,
            watch_uv INTEGER DEFAULT 0, gmv REAL DEFAULT 0, refund_amt REAL DEFAULT 0,
            item_cnt INTEGER DEFAULT 0, order_uv INTEGER DEFAULT 0, pay_uv INTEGER DEFAULT 0,
            raw TEXT DEFAULT '{}',
            fetched_at TEXT DEFAULT (datetime('now','localtime'))
        )""")
    existing = {r["name"] for r in store.conn.execute("PRAGMA table_info(daily_metrics)")}
    for name, decl in {
        "pay_byr_cnt": "REAL", "pay_byr_cnt_distinct": "REAL", "mbr_cnt_incr": "REAL",
        "pay_byr_cnt_mbr": "REAL", "pay_amt_mbr": "REAL", "pay_byr_cnt_gouwujin": "REAL",
        "pay_amt_loyal_fans": "REAL", "pay_amt_loyal_fans_rate": "REAL",
        "pay_amt_new_fans": "REAL", "pay_amt_new_fans_rate": "REAL",
        "data_state": "TEXT DEFAULT ''", "data_issues": "TEXT DEFAULT '[]'",
        "settled_at": "TEXT",
    }.items():
        if name not in existing:
            store.conn.execute(f"ALTER TABLE daily_metrics ADD COLUMN {name} {decl}")
    # 旧版把午夜未结算响应的 0 当成正式数据。一次性将这类历史行改为待结算，
    # 并把经营值置为 NULL，避免任何下游继续将“缺失”解释为真实 0。
    metric_columns = list(dict.fromkeys(DB_COLS.values()))
    blank_rows = store.conn.execute(
        "SELECT * FROM daily_metrics WHERE COALESCE(data_state,'')=''"
    ).fetchall()
    for row in blank_rows:
        core = (row["pay_amt"], row["look_uv"], row["pay_byr_cnt"])
        if any(value is not None and float(value) > 0 for value in core):
            store.conn.execute(
                """UPDATE daily_metrics
                   SET data_state='complete', data_issues='[]',
                       settled_at=COALESCE(settled_at,fetched_at)
                   WHERE id=?""",
                (row["id"],),
            )
        else:
            assignments = ",".join(f"{column}=NULL" for column in metric_columns)
            store.conn.execute(
                f"""UPDATE daily_metrics
                    SET data_state='pending',
                        data_issues=?,{assignments}
                    WHERE id=?""",
                (json.dumps(["历史零值未通过结算校验"], ensure_ascii=False), row["id"]),
            )
    store.conn.commit()


def save_daily(store, data: dict) -> str:
    ensure_table(store)
    state = str(data.get("data_state") or "")
    issues = data.get("data_issues")
    if state not in ("complete", "pending"):
        state, issues = assess_daily_quality(store, data)
    issues = list(issues or [])
    # data 的 key 是指标代码（pay_amt_nd），映射到数据库列名（pay_amt）
    cols = {col: data.get(src) for src, col in DB_COLS.items()}
    if state == "pending":
        cols = {column: None for column in cols}
    raw = json.dumps(data.get("raw", {}), ensure_ascii=False)
    names = list(cols)
    with store._write_lock:
        store.conn.execute(
            "INSERT INTO daily_metrics (date, raw, data_state, data_issues, " +
            ",".join(names) + ") VALUES (?,?,?,? ," +
            ",".join("?" * len(names)) + ") ON CONFLICT(date) DO UPDATE SET " +
            "raw=excluded.raw,data_state=excluded.data_state,data_issues=excluded.data_issues," +
            ",".join(f"{c}=excluded.{c}" for c in names) +
            ",fetched_at=datetime('now','localtime')," +
            "settled_at=CASE WHEN excluded.data_state='complete' " +
            "THEN datetime('now','localtime') ELSE daily_metrics.settled_at END " +
            "WHERE daily_metrics.data_state!='complete' OR excluded.data_state='complete'",
            (data.get("date", ""), raw, state,
             json.dumps(issues, ensure_ascii=False), *cols.values()),
        )
        if state == "complete":
            store.conn.execute(
                """UPDATE daily_metrics SET settled_at=COALESCE(settled_at,fetched_at)
                   WHERE date=?""",
                (data.get("date", ""),),
            )
        store.conn.commit()
    return state


def get_daily(store, date_str: str) -> dict:
    row = store.conn.execute(
        "SELECT * FROM daily_metrics WHERE date=?", (date_str,)
    ).fetchone()
    return dict(row) if row else {}


def fetch_and_save(cfg: dict, store, date_str: str, *, strict: bool = False,
                   require_settled: bool = False) -> dict:
    try:
        data = fetch_daily(cfg, date_str)
        state, issues = assess_daily_quality(store, data)
        data["data_state"] = state
        data["data_issues"] = issues
        save_daily(store, data)
        if require_settled and state != "complete":
            raise DailyDataPending(f"{date_str} 每日经营数据尚未结算")
        return data
    except Exception as e:
        log.warning("每日数据抓取失败 %s: %s", date_str, e)
        if strict:
            raise
        return {}


def format_daily_text(daily: dict) -> str:
    """把每日数据转成报告/提示词文本"""
    if not daily:
        return "（无数据）"
    if daily.get("data_state") == "pending":
        return "（数据待结算）"
    parts = []
    for col, label in (("look_uv", "观看人数"), ("pay_amt", "成交金额"),
                       ("pay_byr_cnt", "成交人数"),
                       ("mbr_cnt_incr", "新增会员"),
                       ("pay_byr_cnt_mbr", "会员成交人数")):
        v = daily.get(col)
        if v is not None:
            parts.append(f"{label} {v}")
    return "；".join(parts) if parts else "（该日无有效数据）"
