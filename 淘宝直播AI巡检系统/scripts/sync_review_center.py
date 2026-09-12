#!/usr/bin/env python3
"""手动回填飞书「淘宝直播经营复盘中心」。

用法：
  .venv/bin/python scripts/sync_review_center.py --scope all --strict
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import ensure_dirs, load_config, resolve
from app.db import Store
from app.notify.review_center import review_center_enabled, sync_review_center


def main() -> int:
    parser = argparse.ArgumentParser(description="同步淘宝直播经营复盘中心")
    parser.add_argument("--scope", choices=("all", "daily", "platform"), default="all")
    parser.add_argument("--strict", action="store_true", help="任一同步失败即返回非零")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    cfg = load_config()
    if not review_center_enabled(cfg):
        print("经营复盘中心未启用：请在 config.yaml 设置 notify.feishu.review_center.enabled: true")
        return 2
    ensure_dirs(cfg)
    store = Store(resolve(cfg["paths"]["db"]))
    try:
        counts = sync_review_center(cfg, store, args.scope, strict=args.strict)
    except Exception as exc:
        print(f"同步失败：{str(exc)[:300]}")
        return 1
    for table_name, count in counts.items():
        print(f"{table_name}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
