#!/usr/bin/env python3
"""真实短音频预检：不发群消息，不写生产数据库。"""
from __future__ import annotations

import argparse
import hashlib
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config
from app.db import Store
from app.transcription.feishu import FeishuMinutesProvider
from app.transcription.funasr import FunASRProvider
from app.transcription.service import TranscriptionService


def main() -> None:
    parser = argparse.ArgumentParser(description="飞书妙记真实短音频预检")
    parser.add_argument("--file", required=True, help="项目目录内的短音频相对路径")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    media = (root / args.file).resolve()
    media.relative_to(root)
    if not media.is_file():
        parser.error("音频不存在")

    cfg = load_config()
    provider = FeishuMinutesProvider(cfg, project_root=root)
    with tempfile.TemporaryDirectory(prefix="minutes-preflight-") as temp:
        store = Store(Path(temp) / "inspection.db")
        anchor_id = store.upsert_anchor("妙记预检")
        stream_id = store.start_stream(anchor_id, live_id="preflight")
        digest = hashlib.sha256(media.read_bytes()).hexdigest()
        key = f"preflight:{digest[:20]}"
        store.queue_transcription_job(
            job_key=key, stream_id=stream_id, live_id="preflight", purpose="review",
            window_start_ms=0, window_end_ms=60_000, media_manifest=[str(media)],
            media_hash=digest, deadline_at=None, media_path=str(media),
        )
        store.update_transcription_job(
            key, status="media_ready", remote_status="media_ready", next_poll_at=0)
        service = TranscriptionService(store, cfg, provider, FunASRProvider(cfg))
        deadline = time.time() + max(60, args.timeout)
        while time.time() < deadline:
            service.tick(now=time.time(), limit=1)
            row = store.get_transcription_job(key)
            if row["status"] == "blocked":
                raise RuntimeError(f"预检 blocked: {row['error_class']}")
            if row["status"] == "ready" and row["cleanup_status"] == "deleted":
                result = service.result_for(key)
                if not result or not result.segments:
                    raise RuntimeError("妙记 ready 但逐字稿为空")
                # 云盘源文件删除后再查询一次；能继续读到逐字稿才算通过。
                check = provider.advance({**dict(row), "remote_status": "processing"}, time.time())
                reread = check.get("result")
                if not reread or not reread.segments:
                    raise RuntimeError("删除云盘源文件后逐字稿不可读")
                print(
                    f"PREFLIGHT_OK provider={result.provider} segments={len(result.segments)} "
                    f"summary={'yes' if result.smart and result.smart.summary else 'no'} "
                    "source_cleanup=deleted reread=ok"
                )
                return
            time.sleep(5)
        raise TimeoutError("妙记预检超时")


if __name__ == "__main__":
    main()
