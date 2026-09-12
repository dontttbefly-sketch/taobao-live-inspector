#!/usr/bin/env python3
"""批处理：把已录完未分析的场次跑完 转写->高亮->话术->复盘

用法：
  python scripts/transcribe_batch.py            # 处理所有 recorded 场次
  python scripts/transcribe_batch.py --stream 3 # 只处理指定场次
  python scripts/transcribe_batch.py --retry    # 额外重试 failed 场次
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config, ensure_dirs, resolve
from app.db import Store
from app.pipeline import process_stream, retry_failed


def main():
    ap = argparse.ArgumentParser(description="批量转写与分析")
    ap.add_argument("--stream", type=int, help="指定场次 ID")
    ap.add_argument("--retry", action="store_true", help="同时重试 failed 场次")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    cfg = load_config()
    ensure_dirs(cfg)
    store = Store(resolve(cfg["paths"]["db"]))

    if args.stream:
        targets = [store.get_stream(args.stream)]
    else:
        targets = store.find_streams("recorded")
    targets = [s for s in targets if s]
    if not targets:
        print("没有待处理的场次")
    for s in targets:
        print(f">>> 处理场次 #{s['id']} ({s['started_at']})")
        process_stream(cfg, store, s["id"])

    if args.retry:
        print(">>> 重试 failed 场次")
        retry_failed(cfg, store)


if __name__ == "__main__":
    main()
