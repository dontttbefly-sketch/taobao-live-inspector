"""代播主播单日经营指标（2026-08-02 逆向确认，数据与中控台「主播数据」页逐项一致）

接口：mtop.dreamweb.query.general.generalQuery（与每日店铺数据同一网关）
dataApi：live_daibo_analysis_ind_list（主播数据列表；上下钟明细为 content_ind_list）
param（JSON 字符串）：
  queryCycleStartDate/queryCycleEndDate  本期日期范围（YYYYMMDD，单日用同一天）
  queryCpCycleStartDate/queryCpCycleEndDate  环比上期（单日用前一天）
  start/hit  分页（字符串）
  orderColumn/orderType  排序（可选）
响应：data.result[] = {daibo_id, daibo_name, index,
  <指标>_uv/_pv/_amt/_cnt 等数值（字符串格式如 "3,817"），
  每个主指标带 <指标>_cp_rate 环比（"--" 表示无对比）}
look_time 为 "37小时52分钟" 格式，解析为秒存 look_time_sec。

注意：count 参数不能传（传了只返回总数）；单日查询返回当天有开播的主播。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta

log = logging.getLogger("metrics.daibo")

API = "mtop.dreamweb.query.general.generalQuery"
API_VERSION = "1.0"
DATA_API = "live_daibo_analysis_ind_list"
CONTENT_TYPE = "live_daibo_analysis_content_ind_list"


class DaiboDataPending(RuntimeError):
    """存在直播活动，但平台尚未返回主播单日结算数据。"""


CONTENT_SOURCE = f"generalQuery.{CONTENT_TYPE}"

# 数据库数值列（响应字段名 → 列名）；look_time 特殊处理
NUMERIC_COLS = {
    "look_uv": "look_uv", "look_pv": "look_pv",
    "pay_amt": "pay_amt", "pay_byr_cnt": "pay_byr_cnt",
    "pay_ord_cnt": "pay_ord_cnt", "pay_itm_qty": "pay_itm_qty",
    "cvr_pay": "cvr_pay", "ipv_uv": "ipv_uv", "ipv": "ipv",
    "ctr_itm": "ctr_itm", "cart_uv": "cart_uv", "cart_pv": "cart_pv",
    "atn_uv": "atn_uv", "atn_uv_rate": "atn_uv_rate",
    "cmt_uv": "cmt_uv", "cmt_pv": "cmt_pv",
    "shr_uv": "shr_uv", "shr_pv": "shr_pv",
    "fvr_uv": "fvr_uv", "fvr_pv": "fvr_pv",
    "look_time_pu": "look_time_pu", "look_time_pt": "look_time_pt",
    "atv": "atv", "rfd_amt": "rfd_amt", "confirm_ord_cnt": "confirm_ord_cnt",
    "sns_uv": "sns_uv", "sns_pv": "sns_pv", "cart_itm_qty": "cart_itm_qty",
}

FIELD_LABELS = {
    "look_uv": "观看人数", "look_pv": "观看次数", "pay_amt": "成交金额(元)",
    "pay_byr_cnt": "成交人数", "pay_ord_cnt": "成交订单数", "pay_itm_qty": "成交件数",
    "cvr_pay": "成交转化率", "ipv_uv": "商品点击人数", "ipv": "商品点击次数",
    "ctr_itm": "商品点击率", "cart_uv": "加购人数", "cart_pv": "加购次数",
    "atn_uv": "新增粉丝", "atn_uv_rate": "转粉率", "cmt_uv": "评论人数",
    "cmt_pv": "评论次数", "shr_uv": "分享人数", "shr_pv": "分享次数",
    "fvr_uv": "点赞人数", "fvr_pv": "点赞次数", "look_time": "观看时长",
    "look_time_pu": "人均观看(秒)", "look_time_pt": "人均观看场次",
}

_RE_DURATION = re.compile(r"(?:(\d+)小时)?(?:(\d+)分钟)?")


def _parse_duration(s: str) -> int | None:
    """'37小时52分钟' → 秒；'--'/空 → None"""
    if not s or s == "--":
        return None
    m = _RE_DURATION.match(str(s))
    if not m:
        return None
    h, mi = m.group(1), m.group(2)
    return (int(h or 0) * 3600 + int(mi or 0) * 60) if (h or mi) else None


def _num(s) -> float | None:
    """'3,817' → 3817.0；'7.23%' → 7.23；'--' → None"""
    if s in (None, "", "--"):
        return None
    try:
        return float(str(s).replace(",", "").replace("%", ""))
    except ValueError:
        return None


def _client(cfg: dict):
    from ..recorder.mtop import shared_client
    return shared_client(cfg)


def fetch_daibo_daily(cfg: dict, date_str: str, hit: int = 30) -> list[dict]:
    """抓取某天每位主播的单日指标（date_str YYYYMMDD，环比自动取前一天）"""
    import datetime
    d = datetime.datetime.strptime(date_str, "%Y%m%d")
    cp = (d - datetime.timedelta(days=1)).strftime("%Y%m%d")
    param = {
        "queryCycleStartDate": date_str, "queryCycleEndDate": date_str,
        "queryCpCycleStartDate": cp, "queryCpCycleEndDate": cp,
        "start": "0", "hit": str(hit),
        "orderColumn": "look_uv", "orderType": "1",
    }
    r = _client(cfg).call(API, API_VERSION, {
        "dataApi": DATA_API,
        "param": json.dumps(param, ensure_ascii=False, separators=(",", ":")),
    })
    rows = (r.get("data") or {}).get("result") or []
    out = []
    for x in rows:
        if not x.get("daibo_name"):
            continue
        rec = {"date": date_str, "daibo_id": x.get("daibo_id"),
               "daibo_name": x.get("daibo_name")}
        for f in x:
            if f in ("daibo_id", "daibo_name", "index"):
                continue
            rec[f] = x[f]  # 原始字符串（含 _cp_rate）
        look_time_sec = _parse_duration(x.get("look_time"))
        if look_time_sec is not None:
            rec["look_time_sec"] = look_time_sec
        out.append(rec)
    log.info("主播单日指标抓取完成 %s：%d 位主播", date_str, len(out))
    return out


def fetch_daibo_content(cfg: dict, live_id: str, started_at: str,
                        hit: int = 100) -> list[dict]:
    """抓取一个平台场次的主播上下钟明细，只返回 content_id 精确匹配行。"""
    started = datetime.fromisoformat(str(started_at).strip())
    date_str = started.strftime("%Y%m%d")
    previous = (started - timedelta(days=1)).strftime("%Y%m%d")
    param = {
        "queryCycleStartDate": date_str, "queryCycleEndDate": date_str,
        "queryCpCycleStartDate": previous, "queryCpCycleEndDate": previous,
        "start": "0", "hit": str(hit),
        "type": CONTENT_TYPE, "contentId": str(live_id),
    }
    response = _client(cfg).call(API, API_VERSION, {
        "dataApi": DATA_API,
        "param": json.dumps(param, ensure_ascii=False, separators=(",", ":")),
    })
    rows = (response.get("data") or {}).get("result") or []
    matched = [dict(row) for row in rows
               if str(row.get("content_id") or "") == str(live_id)]
    log.info("主播上下钟明细抓取完成 liveId=%s：%d 条", live_id, len(matched))
    return matched


# 金额、订单/件数和次数可以跨上下钟片段相加；UV/人数只能在单片段时原样展示。
# 多片段的人数相加是“分段人次”，不能冒充平台整场去重人数。
CONTENT_ADDITIVE_FIELDS = (
    "look_pv", "pay_amt", "pay_ord_cnt", "pay_itm_qty", "ipv", "cart_pv",
    "cart_itm_qty", "cmt_pv", "shr_pv", "fvr_pv", "sns_pv", "rfd_amt",
)
CONTENT_UNIQUE_FIELDS = (
    "look_uv", "pay_byr_cnt", "ipv_uv", "cart_uv", "atn_uv", "cmt_uv",
    "shr_uv", "fvr_uv", "sns_uv",
)
CONTENT_SEGMENT_SUM_FIELDS = tuple(f"{field}_segment_sum"
                                   for field in CONTENT_UNIQUE_FIELDS)


def platform_anchor_metric_issues(row: dict) -> list[str]:
    """判断主播场次数据是否足够用于“完整”复盘，缺失不能伪装成 0。"""
    labels = {
        "pay_amt": "成交金额", "pay_ord_cnt": "成交订单数",
        "pay_itm_qty": "成交件数",
    }
    issues = [f"缺少{label}" for key, label in labels.items()
              if row.get(key) is None]
    if row.get("look_uv") is None and row.get("look_uv_segment_sum") is None:
        issues.append("缺少观看人数/分段观看人次")
    if (row.get("pay_byr_cnt") is None
            and row.get("pay_byr_cnt_segment_sum") is None):
        issues.append("缺少成交人数/分段成交人次")
    return issues


def _parse_clock(value) -> datetime | None:
    if value in (None, "", "--"):
        return None
    text = str(value).strip()
    if text.isdigit():
        stamp = int(text)
        if stamp > 100_000_000_000:
            stamp /= 1000
        return datetime.fromtimestamp(stamp)
    try:
        return datetime.fromisoformat(text.replace("/", "-"))
    except ValueError:
        return None


def aggregate_daibo_content(rows: list[dict], live_id: str,
                            anchor_name_to_id: dict[str, int],
                            daibo_id_to_anchor_id: dict[str, int] | None = None) -> list[dict]:
    """把同一主播的官方上下钟行归并为平台场次指标，不分摊整场总账。"""
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows or []:
        if str(row.get("content_id") or "") != str(live_id):
            continue
        name = str(row.get("daibo_name") or "").strip()
        daibo_id = str(row.get("daibo_id") or "").strip()
        anchor_id = (daibo_id_to_anchor_id or {}).get(daibo_id)
        if anchor_id is None:
            anchor_id = anchor_name_to_id.get(name)
        if not name or anchor_id is None:
            continue
        key = (daibo_id or name, name)
        groups.setdefault(key, []).append(dict(row))

    result: list[dict] = []
    for (daibo_id, name), segments in groups.items():
        values: dict[str, float | None] = {}
        for field in CONTENT_ADDITIVE_FIELDS:
            parsed = [_num(segment.get(field)) for segment in segments]
            present = [value for value in parsed if value is not None]
            values[field] = round(sum(present), 4) if present else None
        for field in CONTENT_UNIQUE_FIELDS:
            parsed = [_num(segment.get(field)) for segment in segments]
            present = [value for value in parsed if value is not None]
            values[f"{field}_segment_sum"] = (
                round(sum(present), 4) if present else None
            )
            values[field] = present[0] if len(segments) == 1 and present else None

        spans: list[tuple[datetime, datetime]] = []
        for segment in segments:
            up = _parse_clock(segment.get("work_up_time"))
            down = _parse_clock(segment.get("work_down_time"))
            if up is not None and down is not None and down >= up:
                spans.append((up, down))
        duration = sum((down - up).total_seconds() for up, down in spans)

        # 比率和客单价都依赖去重人数；多个片段不能用人次重新计算。
        for field in ("cvr_pay", "atv", "ctr_itm"):
            values[field] = (
                _num(segments[0].get(field)) if len(segments) == 1 else None
            )

        aggregated = {
            "live_id": str(live_id),
            "anchor_id": int((daibo_id_to_anchor_id or {}).get(daibo_id)
                             or anchor_name_to_id[name]),
            "daibo_id": daibo_id,
            "daibo_name": name,
            "segment_count": len(segments),
            "on_air_duration_sec": round(duration, 3) if spans else None,
            "work_up_at": min((up for up, _down in spans), default=None).isoformat(
                sep=" ", timespec="seconds") if spans else "",
            "work_down_at": max((down for _up, down in spans), default=None).isoformat(
                sep=" ", timespec="seconds") if spans else "",
            **values,
            "aggregation_note": (
                "官方上下钟分段归并：金额/订单/件数/次数为可加总指标；"
                "多片段人数仅保留分段人次，不冒充整场去重人数"
            ),
            "source": CONTENT_SOURCE,
            "raw": {"segments": segments},
        }
        data_issues = platform_anchor_metric_issues(aggregated)
        aggregated["data_state"] = "partial" if data_issues else "ok"
        aggregated["data_issues"] = data_issues
        result.append(aggregated)
    return sorted(result, key=lambda item: (item["work_up_at"], item["anchor_id"]))


# ---------- 存储 ----------
def ensure_table(store) -> None:
    store.conn.execute("""
        CREATE TABLE IF NOT EXISTS daibo_daily (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            daibo_id TEXT NOT NULL,
            daibo_name TEXT NOT NULL,
            look_uv REAL DEFAULT 0, look_pv REAL DEFAULT 0,
            pay_amt REAL DEFAULT 0, pay_byr_cnt REAL DEFAULT 0,
            pay_ord_cnt REAL DEFAULT 0, pay_itm_qty REAL DEFAULT 0,
            cvr_pay REAL DEFAULT 0, ipv_uv REAL DEFAULT 0, ipv REAL DEFAULT 0,
            ctr_itm REAL DEFAULT 0, cart_uv REAL DEFAULT 0, cart_pv REAL DEFAULT 0,
            atn_uv REAL DEFAULT 0, atn_uv_rate REAL DEFAULT 0,
            cmt_uv REAL DEFAULT 0, cmt_pv REAL DEFAULT 0,
            shr_uv REAL DEFAULT 0, shr_pv REAL DEFAULT 0,
            fvr_uv REAL DEFAULT 0, fvr_pv REAL DEFAULT 0,
            look_time_sec REAL DEFAULT 0, look_time_pu REAL DEFAULT 0,
            look_time_pt REAL DEFAULT 0,
            atv REAL DEFAULT 0, rfd_amt REAL DEFAULT 0,
            confirm_ord_cnt REAL DEFAULT 0, sns_uv REAL DEFAULT 0,
            sns_pv REAL DEFAULT 0, cart_itm_qty REAL DEFAULT 0,
            raw TEXT DEFAULT '{}',
            fetched_at TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(date, daibo_id)
        )""")
    store.conn.commit()


def ensure_platform_anchor_table(store) -> None:
    """兼容旧数据库：Store 初始化迁移之外也可独立确保主播场次指标表存在。"""
    store.conn.execute("""
        CREATE TABLE IF NOT EXISTS platform_anchor_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            live_id TEXT NOT NULL,
            anchor_id INTEGER NOT NULL REFERENCES anchors(id),
            daibo_id TEXT DEFAULT '', daibo_name TEXT DEFAULT '',
            segment_count INTEGER DEFAULT 0, on_air_duration_sec REAL,
            work_up_at TEXT DEFAULT '', work_down_at TEXT DEFAULT '',
            look_uv REAL, look_pv REAL, pay_amt REAL, pay_byr_cnt REAL,
            pay_ord_cnt REAL, pay_itm_qty REAL, cvr_pay REAL, atv REAL,
            ipv_uv REAL, ipv REAL, ctr_itm REAL, cart_uv REAL, cart_pv REAL,
            cart_itm_qty REAL, atn_uv REAL, cmt_uv REAL, cmt_pv REAL,
            shr_uv REAL, shr_pv REAL, fvr_uv REAL, fvr_pv REAL,
            sns_uv REAL, sns_pv REAL, rfd_amt REAL,
            look_uv_segment_sum REAL, pay_byr_cnt_segment_sum REAL,
            ipv_uv_segment_sum REAL, cart_uv_segment_sum REAL,
            atn_uv_segment_sum REAL, cmt_uv_segment_sum REAL,
            shr_uv_segment_sum REAL, fvr_uv_segment_sum REAL,
            sns_uv_segment_sum REAL,
            aggregation_note TEXT DEFAULT '', source TEXT DEFAULT '',
            data_state TEXT DEFAULT '', data_issues TEXT DEFAULT '[]',
            raw TEXT DEFAULT '{}',
            fetched_at TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(live_id, anchor_id)
        )""")
    existing = {row["name"] for row in store.conn.execute(
        "PRAGMA table_info(platform_anchor_metrics)")}
    for column in CONTENT_SEGMENT_SUM_FIELDS:
        if column not in existing:
            store.conn.execute(
                f"ALTER TABLE platform_anchor_metrics ADD COLUMN {column} REAL")
    store.conn.execute("""CREATE INDEX IF NOT EXISTS idx_platform_anchor_live
                          ON platform_anchor_metrics(live_id, anchor_id)""")
    store.conn.commit()


PLATFORM_ANCHOR_NUMERIC_FIELDS = (
    "segment_count", "on_air_duration_sec", *CONTENT_ADDITIVE_FIELDS,
    *CONTENT_UNIQUE_FIELDS, *CONTENT_SEGMENT_SUM_FIELDS,
    "cvr_pay", "atv", "ctr_itm",
)


def save_platform_anchor_metrics(store, rows: list[dict]) -> int:
    ensure_platform_anchor_table(store)
    if not rows:
        return 0
    columns = (
        "live_id", "anchor_id", "daibo_id", "daibo_name",
        "segment_count", "on_air_duration_sec", "work_up_at", "work_down_at",
        *CONTENT_ADDITIVE_FIELDS, *CONTENT_UNIQUE_FIELDS,
        *CONTENT_SEGMENT_SUM_FIELDS, "cvr_pay", "atv", "ctr_itm",
        "aggregation_note", "source", "data_state", "data_issues", "raw",
    )
    values = []
    for row in rows:
        encoded = {
            **row,
            "data_issues": json.dumps(row.get("data_issues") or [], ensure_ascii=False),
            "raw": json.dumps(row.get("raw") or {}, ensure_ascii=False),
        }
        values.append(tuple(encoded.get(column) for column in columns))
    updates = ",".join(
        f"{column}=excluded.{column}" for column in columns
        if column not in ("live_id", "anchor_id")
    )
    placeholders = ",".join("?" for _ in columns)
    with store._write_lock:
        cursor = store.conn.executemany(
            f"""INSERT INTO platform_anchor_metrics({','.join(columns)})
                VALUES({placeholders})
                ON CONFLICT(live_id,anchor_id) DO UPDATE SET {updates},
                    fetched_at=datetime('now','localtime')""",
            values,
        )
        store.conn.commit()
    return cursor.rowcount


def get_platform_anchor_metrics(store, live_id: str) -> list[dict]:
    ensure_platform_anchor_table(store)
    return [dict(row) for row in store.query(
        """SELECT * FROM platform_anchor_metrics WHERE live_id=?
           ORDER BY work_up_at,anchor_id""", (str(live_id),))]


def save_daibo_daily(store, rows: list[dict]) -> int:
    ensure_table(store)
    n = 0
    for rec in rows:
        date, did = rec.get("date"), rec.get("daibo_id")
        if not date or not did:
            continue
        # 所有已知字段都显式写入；接口缺失写 NULL，不能继承表默认值 0。
        cols = {col: None for col in NUMERIC_COLS.values()}
        for f, col in NUMERIC_COLS.items():
            v = rec.get(f)
            num = _num(v) if v is not None else None
            if num is not None:
                cols[col] = num
        if rec.get("look_time_sec") is not None:
            cols["look_time_sec"] = rec["look_time_sec"]
        raw = json.dumps({k: v for k, v in rec.items()
                          if k not in ("date", "daibo_id", "daibo_name")},
                         ensure_ascii=False)
        names = list(cols)
        store.conn.execute(
            f"INSERT INTO daibo_daily (date, daibo_id, daibo_name, raw,{','.join(names)}) "
            f"VALUES (?,?,?,?,{','.join('?' * len(names))}) "
            f"ON CONFLICT(date,daibo_id) DO UPDATE SET daibo_name=excluded.daibo_name,"
            f"raw=excluded.raw,{','.join(f'{c}=excluded.{c}' for c in names)},"
            "fetched_at=datetime('now','localtime')",
            (date, did, rec.get("daibo_name"), raw, *cols.values()),
        )
        n += 1
    store.conn.commit()
    return n


def fetch_and_save(cfg: dict, store, date_str: str, *, strict: bool = False,
                   require_settled: bool = False) -> int:
    try:
        rows = fetch_daibo_daily(cfg, date_str)
        if not rows and require_settled:
            raise DaiboDataPending(f"{date_str} 主播单日数据尚未结算")
        return save_daibo_daily(store, rows)
    except Exception as e:
        log.warning("主播单日指标抓取失败 %s: %s", date_str, e)
        if strict:
            raise
        return 0


def get_daibo_daily(store, date_str: str) -> list[dict]:
    rows = store.query(
        "SELECT * FROM daibo_daily WHERE date=? ORDER BY look_uv DESC", (date_str,))
    return [dict(r) for r in rows]


def format_daibo_text(rows: list[dict], date_str: str = "") -> str:
    """主播单日数据 → 文本（用于报告/提示词）"""
    if not rows:
        return "（无主播数据）"
    date = date_str or rows[0]["date"]
    lines = [f"主播单日经营数据（{date[:4]}-{date[4:6]}-{date[6:]}）："]
    for r in rows:
        parts = [r["daibo_name"]]
        for f, label in (("look_uv", "观看"), ("pay_amt", "成交"),
                         ("cvr_pay", "转化率"), ("atv", "客单价")):
            v = r.get(f)
            if v not in (None, 0, "0", "--"):
                parts.append(f"{label} {v}")
        lt = r.get("look_time_sec")
        if lt:
            h, m = divmod(int(lt) // 60, 60)
            parts.append(f"时长 {h}h{m}m")
        lines.append(" | ".join(parts))
    return "\n".join(lines)
