#!/usr/bin/env python3
"""一次性恢复脚本：把被“平台整场冻结值”污染的拆分场次恢复为本地估算。

背景：2026-08-04 补抓时，平台把一天直播算作一个平台场次（liveId 当天不变），
本地因轮换/重启拆成多场；旧补抓逻辑把整场总账写进了每一场，导致单场数据失真。

本脚本：
1. 对同一 liveId 的拆分场次，恢复 stream_metrics 为本地估算（分钟序列聚合）。
2. 确保 platform_sessions 存有该 liveId 的平台整场总账（幂等）。
用法：.venv/bin/python scripts/restore_split_streams.py --day 2026-08-03
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config, local_epoch_ms, now_shanghai, resolve  # noqa: E402
from app.db import Store  # noqa: E402
from app.metrics.qianniu import (  # noqa: E402
    METRIC_COLUMNS, _local_streams_for_live, ensure_platform_sessions,
    fetch_metric_series, fetch_session_metrics, upsert_platform_session,
)

log = logging.getLogger("restore_split")


def _ms(value: str | None):
    if not value:
        return None
    return local_epoch_ms(value)


def restore(cfg: dict, store, day: str) -> None:
    ensure_platform_sessions(store)
    restored = 0
    for live_id, local_streams in _local_streams_for_live(store, day).items():
        if not local_streams:
            continue
        probe = local_streams[0]
        frozen = None
        try:
            frozen = fetch_session_metrics(cfg, live_id,
                                           start_ms=_ms(probe["started_at"]),
                                           end_ms=_ms(probe["ended_at"]))
        except Exception as exc:
            log.warning("冻结数据查询失败 liveId=%s: %s", live_id, str(exc)[:150])
        if frozen:
            upsert_platform_session(store, live_id, frozen)
        if len(local_streams) <= 1:
            continue  # 一对一场次不在此脚本处理范围
        for s in local_streams:
            sid = s["id"]
            try:
                series = fetch_metric_series(cfg, live_id, search_type="2",
                                             start_ms=_ms(s["started_at"]),
                                             end_ms=_ms(s["ended_at"]))
            except Exception as exc:
                log.warning("分钟趋势恢复失败 场次 #%d: %s", sid, str(exc)[:150])
                continue
            if not series:
                continue
            raw_series = series.get("raw") or {}
            minutes = max(1, ((_ms(s["ended_at"]) or 0) - (_ms(s["started_at"]) or 0)) / 60000)
            coverage = min(1.0, len(raw_series.get("uv") or []) / minutes)
            values = {
                "source": "hybrid.local-period",
                "source_scope": "本地录制时段（部分数据）",
                "data_state": "partial",
                "data_issues": json.dumps([
                    "本场为平台场次拆分时段；平台整场总账见 platform_sessions（liveId=%s）" % live_id
                ], ensure_ascii=False),
                "pay_amt": series.get("pay_amt"),
                "deal_amt": series.get("pay_amt"),
                "max_online_uv": series.get("max_online_uv"),
                "uv_avg": series.get("uv_avg"),
                "ipv_total": series.get("ipv_total"),
                "heat_score": series.get("heat_score"),
                "coverage_ratio": coverage,
                "live_id": live_id,
                "fetched_at": now_shanghai().strftime("%Y-%m-%d %H:%M:%S"),
            }
            # 整场冻结值不可信字段置空，避免拆分场次显示平台总账。
            for key in ("buyer_cnt", "order_cnt", "item_qty", "viewer_uv", "viewer_pv",
                        "visitor_total", "pay_byr_rate", "ipv_uv_rate"):
                values[key] = None
            cols = ",".join(METRIC_COLUMNS)
            updates = ",".join(f"{c}=excluded.{c}" for c in METRIC_COLUMNS)
            store.conn.execute(
                f"""INSERT INTO stream_metrics(stream_id,{cols},data_issues,raw)
                    VALUES(?,{','.join('?' * len(METRIC_COLUMNS))},?,?)
                    ON CONFLICT(stream_id) DO UPDATE SET {updates},
                        data_issues=excluded.data_issues, raw=excluded.raw""",
                (sid, *(values.get(c) for c in METRIC_COLUMNS),
                 values["data_issues"], json.dumps({"series": raw_series}, ensure_ascii=False)))
            store.conn.commit()
            restored += 1
            log.info("场次 #%d 已恢复为本地估算（成交=%.2f，覆盖 %d 分钟）",
                     sid, series.get("pay_amt") or 0, len(raw_series.get("uv") or []))
    log.info("恢复完成：%d 个拆分场次已恢复为本地估算；平台总账已写入 platform_sessions", restored)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--day", default=(now_shanghai() - timedelta(days=1)).strftime("%Y-%m-%d"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    cfg = load_config()
    store = Store(resolve(cfg["paths"]["db"]))
    restore(cfg, store, args.day)


if __name__ == "__main__":
    main()
