#!/usr/bin/env python3
"""人工处理妙记 blocked 任务；不会自动登录、扩权或降级复盘。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config, resolve
from app.db import Store
from app.transcription.feishu import FeishuMinutesProvider
from app.transcription.funasr import FunASRProvider
from app.transcription.service import TranscriptionService


def main() -> None:
    parser = argparse.ArgumentParser(description="管理飞书妙记转写任务")
    parser.add_argument("--list-blocked", action="store_true", help="列出阻塞任务")
    parser.add_argument("--job", help="transcription_jobs.job_key")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--retry-feishu", action="store_true", help="重新进入飞书队列")
    action.add_argument("--use-funasr", action="store_true", help="明确确认该任务使用 FunASR")
    args = parser.parse_args()

    cfg = load_config()
    store = Store(resolve(cfg["paths"]["db"]))
    if args.list_blocked:
        rows = store.query(
            "SELECT job_key,stream_id,purpose,error_class,error FROM transcription_jobs "
            "WHERE status='blocked' ORDER BY created_at")
        for row in rows:
            print(f"{row['job_key']}  stream={row['stream_id']}  purpose={row['purpose']}  "
                  f"type={row['error_class']}  error={row['error']}")
        return
    if not args.job or not (args.retry_feishu or args.use_funasr):
        parser.error("请使用 --list-blocked，或同时指定 --job 与一个处理动作")
    service = TranscriptionService(
        store, cfg, FeishuMinutesProvider(cfg), FunASRProvider(cfg))
    service.resolve_blocked(
        args.job, "retry_feishu" if args.retry_feishu else "use_funasr")
    print("已重新排队" if args.retry_feishu else "已确认使用 FunASR 备胎")


if __name__ == "__main__":
    main()
