#!/usr/bin/env python3
"""生成复盘报告

用法：
  python scripts/gen_report.py --stream 3        # 指定场次的复盘报告
  python scripts/gen_report.py --all             # 所有已分析场次的报告
  python scripts/gen_report.py --week [7]        # 周报（默认近 7 天）
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config, ensure_dirs, resolve
from app.db import Store
from app.review.report import (generate_platform_report, generate_stream_report,
                               generate_weekly_report)


def main():
    ap = argparse.ArgumentParser(description="生成复盘报告")
    ap.add_argument("--stream", type=int, help="场次 ID")
    ap.add_argument("--live-id", help="平台场次 liveId（生成整场主播聚合报告）")
    ap.add_argument("--all", action="store_true", help="所有有转写数据的场次")
    ap.add_argument("--week", nargs="?", const=7, type=int, help="周报（天数，默认 7）")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    cfg = load_config()
    ensure_dirs(cfg)
    store = Store(resolve(cfg["paths"]["db"]))

    if args.week:
        out = generate_weekly_report(cfg, store, args.week)
        print(f"周报: {out}")
        # 周报生成后自动清理过期录像（保留 retention_days 天）
        from app.cleanup import cleanup_old_recordings
        n = cleanup_old_recordings(cfg, store)
        print(f"已自动清理 {n} 个过期场次的录像")
        return
    if args.stream:
        out = generate_stream_report(cfg, store, args.stream)
        print(f"报告: {out}")
        return
    if args.live_id:
        from app.review.platform import build_platform_review_summary
        summary = build_platform_review_summary(cfg, store, args.live_id)
        frozen = store.get_platform_review(args.live_id)
        if frozen and frozen.get("payload"):
            summary = frozen["payload"]
        out = generate_platform_report(cfg, store, args.live_id, summary)
        print(f"整场报告: {out}")
        return
    if args.all:
        rows = store.query(
            "SELECT DISTINCT s.id FROM streams s "
            "WHERE EXISTS(SELECT 1 FROM transcripts t WHERE t.stream_id=s.id) ORDER BY s.id"
        )
        if not rows:
            print("没有任何已转写的场次")
        for r in rows:
            print(generate_stream_report(cfg, store, r["id"]))
        return
    ap.error("请指定 --stream / --live-id / --all / --week 之一")


if __name__ == "__main__":
    main()
