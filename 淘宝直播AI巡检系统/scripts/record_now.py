#!/usr/bin/env python3
"""手动录制：立即录制指定主播的直播（或直接给流地址）

用法：
  python scripts/record_now.py --anchor 主播A [--seconds 3600] [--analyze]
  python scripts/record_now.py --url "http://..." --name "主播A" [--seconds 3600]
  python scripts/record_now.py --probe 主播A        # 只探测直播状态和流地址，不录制

--analyze 结束后自动跑 转写->高亮->话术->复盘 全流程（需 ASR 模型）
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config, ensure_dirs, resolve
from app.db import Store
from app.recorder.mtop import MtopClient, MtopError
from app.recorder.recorder import Recorder
from app.pipeline import process_stream


def find_anchor(cfg, name: str):
    for a in cfg["anchors"]:
        if a.get("name") == name:
            return a
    raise SystemExit(f"config.yaml 中找不到主播: {name}")


def main():
    ap = argparse.ArgumentParser(description="手动录制淘宝直播")
    ap.add_argument("--anchor", help="主播名（config.yaml 中配置）")
    ap.add_argument("--url", help="直播流地址（绕过接口探测）")
    ap.add_argument("--name", help="--url 模式下指定主播名")
    ap.add_argument("--seconds", type=int, default=0, help="录 N 秒后自动停止（0=直到手动 Ctrl+C）")
    ap.add_argument("--probe", action="store_true", help="只探测流地址不录制")
    ap.add_argument("--analyze", action="store_true", help="录制结束后自动跑分析流水线")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    cfg = load_config()
    ensure_dirs(cfg)
    store = Store(resolve(cfg["paths"]["db"]))
    t = cfg["taobao"]

    # ---------- 探测模式 ----------
    if args.probe:
        if not args.anchor:
            ap.error("--probe 需要 --anchor")
        anchor = find_anchor(cfg, args.anchor)
        client = MtopClient(t.get("cookie", ""), app_key=t.get("app_key", "12574478"),
                            user_agent=t.get("user_agent"), referer=t.get("referer"))
        live_id = anchor.get("live_id", "") or t.get("live_id", "")
        info = client.probe(live_id=live_id, user_id=anchor.get("taobao_user_id", ""))
        print(f"主播: {anchor['name']}")
        print(f"在播: {info['is_live']}")
        print(f"接口: {info['tried']}  返回: {info['ret']}")
        for i, u in enumerate(info["stream_urls"], 1):
            print(f"流地址 {i}: {u}")
        return

    # ---------- 取流地址 ----------
    url, name = args.url, args.name
    anchor = None
    if not url:
        if not args.anchor:
            ap.error("需要 --anchor 或 --url")
        anchor = find_anchor(cfg, args.anchor)
        name = anchor["name"]
        client = MtopClient(t.get("cookie", ""), app_key=t.get("app_key", "12574478"),
                            user_agent=t.get("user_agent"), referer=t.get("referer"))
        # liveId 兼容两种配置：主播自己的 / taobao.live_id 共享直播间
        live_id = anchor.get("live_id", "") or t.get("live_id", "")
        try:
            info = client.probe(live_id=live_id, user_id=anchor.get("taobao_user_id", ""))
        except MtopError as e:
            raise SystemExit(f"探测直播状态失败: {e}")
        if not info["is_live"] or not info["stream_urls"]:
            raise SystemExit(f"[{name}] 当前未在直播，或未解析到流地址（接口返回: {info['ret']}）。"
                             f"可先用 --probe 查看，或用 --url 手动指定流地址")
        url = info["stream_urls"][0]
    if not name:
        name = "manual"

    # ---------- 录制 ----------
    store.upsert_anchor(name)
    anchor_row = store.query("SELECT id FROM anchors WHERE name=?", (name,))[0]
    stream_id = store.start_stream(anchor_row["id"])
    base = f"{name}_{time.strftime('%Y%m%d_%H%M%S')}"
    recorder = Recorder(cfg["recorder"].get("ffmpeg", "ffmpeg"),
                        resolve(cfg["recorder"].get("out_dir", "data/recordings")),
                        base, segment_seconds=cfg["recorder"].get("segment_seconds", 0),
                        referer=t.get("referer", "https://h5.m.taobao.com/"))
    print(f"[{name}] 开始录制 -> {base}  (Ctrl+C 停止)")
    recorder.start(url)

    refresh_interval = int(t.get("stream_refresh_interval", 900))
    last_refresh = time.time()
    t0 = time.time()
    try:
        while True:
            recorder.tick()
            if args.seconds and time.time() - t0 >= args.seconds:
                print(f"已达 {args.seconds}s，自动停止")
                break
            # 定期刷新流地址（切流续录）
            if anchor and time.time() - last_refresh >= refresh_interval:
                last_refresh = time.time()
                try:
                    info = client.probe(live_id=anchor.get("live_id", "") or t.get("live_id", ""),
                                        user_id=anchor.get("taobao_user_id", ""))
                    if info["stream_urls"]:
                        recorder.tick(info["stream_urls"][0])
                except MtopError as e:
                    logging.warning("刷新流地址失败: %s", e)
            time.sleep(10)
    except KeyboardInterrupt:
        print("\n停止录制...")

    final = recorder.stop()
    duration = _probe_duration(final)
    store.finish_stream(stream_id, file_path=str(final), duration_sec=duration)
    print(f"录制完成: {final}（{duration:.0f} 秒）")

    if args.analyze:
        process_stream(cfg, store, stream_id)


def _probe_duration(video: Path) -> float:
    import subprocess
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, timeout=30,
        )
        return float(out.stdout.strip())
    except Exception:
        return 0.0


if __name__ == "__main__":
    main()
