#!/usr/bin/env python3
"""切换直播间 liveId（淘宝每场直播的 liveId 会变，换场次时执行）

用法：
  .venv/bin/python scripts/update_liveid.py 4223058047060632
  .venv/bin/python scripts/update_liveid.py --probe          # 只验证当前配置的 liveId

行为：
1. 原子更新 config.yaml 的 taobao.live_id（不会留下半份 YAML）
2. 优雅重启 watcher（当前场次收尾+完整复盘，新场次用新 liveId 探测）
3. 验证新 liveId 探测结果
"""
from __future__ import annotations

import argparse
import logging
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def update_config(live_id: str) -> None:
    from app.recorder.discover import update_config_live_id
    update_config_live_id(live_id)
    print(f"✓ config.yaml live_id 已原子更新为 {live_id}")


def _list_watcher_pids() -> list[int]:
    """跨平台查找 watcher 进程 PID（Windows 用 wmic，其它用 pgrep）"""
    import subprocess
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["wmic", "process", "where", "name like '%python%'", "get", "processid,commandline"],
                capture_output=True, text=True, timeout=30)
            pids = []
            for line in out.stdout.splitlines():
                if "app.recorder.watcher" in line:
                    parts = line.split()
                    for p in parts:
                        if p.isdigit():
                            pids.append(int(p))
            return pids
        except Exception:
            return []
    proc = subprocess.run(["pgrep", "-f", "app.recorder.watche[r]"],
                          capture_output=True, text=True)
    return [int(p) for p in proc.stdout.split() if p.strip().isdigit()]


def os_kill(pid: int, sig: int) -> None:
    import os
    if sys.platform == "win32":
        # Windows 无 SIGINT 概念，用 terminate（优雅收尾依赖 Ctrl+C 场景，Windows 下直接终止进程，
        # 场次会由下次启动的 _recover_interrupted 收尾）
        os.kill(pid, signal.SIGTERM)
    else:
        os.kill(pid, sig)


def restart_watcher() -> None:
    """优雅停止旧 watcher（收尾当前场次）。
    不再自行 Popen 新进程：mac 上由 launchd KeepAlive 自动拉起（单实例锁保证不双开）；
    非托管环境请手动启动：.venv/bin/python -m app.recorder.watcher"""
    for pid in _list_watcher_pids():
        try:
            os_kill(pid, signal.SIGINT)
            print(f"✓ 已通知旧 watcher({pid}) 收尾")
        except Exception as e:
            print(f"  停止旧进程失败: {e}")
    time.sleep(8)  # 等收尾完成（合并分片+进入复盘流水线）
    if sys.platform != "win32":
        # mac/Linux：若 launchd/launchctl 托管则等待自动拉起
        try:
            out = subprocess.run(["launchctl", "list"], capture_output=True,
                                 text=True, timeout=5).stdout
            if "torras.live-inspection" in out:
                print("✓ launchd 托管中，KeepAlive 将自动拉起新 watcher")
                time.sleep(5)
                return
        except Exception:
            pass
    print("⚠ 未检测到系统托管，请手动启动：")
    print("  .venv/bin/python -m app.recorder.watcher > data/logs/watcher.log 2>&1 &")


def probe(live_id: str) -> None:
    from app.config import load_config
    from app.recorder.mtop import MtopClient
    cfg = load_config()
    t = cfg["taobao"]
    c = MtopClient(cookie=t["cookie"], app_key=t["app_key"],
                   user_agent=t["user_agent"], referer=t["referer"])
    info = c.probe(live_id=live_id)
    print(f"直播间 {live_id}: 在播={info['is_live']}，流地址 {len(info['stream_urls'])} 个")
    if not info["is_live"]:
        print("  ⚠️ 当前未在播或流未就绪（可能是预告状态），等正式开播后系统自动开始录制")
    else:
        print("  ✅ 流可用，将开始录制")


def main():
    ap = argparse.ArgumentParser(description="切换直播间 liveId")
    ap.add_argument("live_id", nargs="?", help="新直播间 liveId（tbzb 链接 ?liveId= 后的数字）")
    ap.add_argument("--probe", action="store_true", help="只探测当前配置的 liveId")
    ap.add_argument("--no-restart", action="store_true", help="只更新配置不重启 watcher")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING)

    if args.probe or not args.live_id:
        from app.config import load_config
        cur = load_config()["taobao"].get("live_id", "")
        print(f"当前配置 live_id: {cur}")
        if cur:
            probe(cur)
        return

    update_config(args.live_id)
    probe(args.live_id)
    if not args.no_restart:
        restart_watcher()
    print("完成。")


if __name__ == "__main__":
    main()
