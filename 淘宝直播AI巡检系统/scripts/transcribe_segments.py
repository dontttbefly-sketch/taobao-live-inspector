"""场次 mp4 分段转写：把大视频按固定时长切片逐段转写（MPS 上每段约 40 秒），
写入 brief_transcripts（带全场偏移），供复盘复用——解决整段大文件转写极慢的问题。

用法：
  .venv/bin/python scripts/transcribe_segments.py --stream 11 [--segment 600] [--dry]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def segment_count(duration_sec: float, seg: int) -> int:
    return max(1, int(-(-duration_sec // seg)))  # 向上取整


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stream", type=int, required=True, help="场次 id")
    ap.add_argument("--segment", type=int, default=600, help="每段秒数（默认 600=10 分钟）")
    ap.add_argument("--dry", action="store_true", help="只打印分段计划不转写")
    args = ap.parse_args()

    from app.config import load_config, resolve
    from app.db import Store
    from app.asr.transcribe import transcribe_wav

    cfg = load_config()
    store = Store(resolve(cfg["paths"]["db"]))
    stream = store.get_stream(args.stream)
    if not stream:
        print(f"场次 #{args.stream} 不存在"); sys.exit(1)
    video = Path(stream["file_path"])
    if not video.exists():
        print(f"录像不存在: {video}"); sys.exit(1)

    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
        capture_output=True, text=True, timeout=60)
    dur = float(out.stdout.strip())
    n = segment_count(dur, args.segment)
    print(f"场次 #{args.stream} 时长 {dur/60:.1f} 分钟 → 切 {n} 段（每段 {args.segment}s）")
    if args.dry:
        return

    # 先清掉旧的整段转写状态残留（如有）
    store.conn.execute("DELETE FROM brief_transcripts WHERE stream_id=?", (args.stream,))
    store.conn.commit()

    t0 = time.time()
    done = 0
    for i in range(n):
        start = i * args.segment
        seg_path = Path(f"/tmp/seg_{args.stream}_{i:03d}.wav")
        # 快速定位（-ss 在 -i 前）：音频按 start 起切，误差 <1s
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-ss", str(start), "-t", str(args.segment), "-i", str(video),
               "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(seg_path)]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode != 0:
            print(f"  seg {i} 提取失败: {r.stderr.decode(errors='ignore')[:120]}")
            continue
        sentences = transcribe_wav(seg_path, cfg)
        offset_ms = start * 1000
        shifted = [(s + offset_ms, e + offset_ms, t) for s, e, t in sentences]
        store.save_brief_transcripts(args.stream, f"seg_{i:03d}", shifted)
        seg_path.unlink(missing_ok=True)
        done += 1
        el = time.time() - t0
        print(f"  [{i+1}/{n}] seg_{i:03d} 完成: {len(sentences)} 句 "
              f"(累计 {el/60:.1f} 分钟, 预计剩余 {(el/done)*(n-done)/60:.1f} 分钟)")
    print(f"✅ 分段转写完成：{done}/{n} 段，共 {store.conn.execute('SELECT COUNT(*) FROM brief_transcripts WHERE stream_id=?', (args.stream,)).fetchone()[0]} 句")
    print("下一步：把场次状态改回 recorded 并重启 watcher（或直接跑 process_stream）触发复用复盘")


if __name__ == "__main__":
    main()
