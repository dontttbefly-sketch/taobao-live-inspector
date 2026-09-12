#!/usr/bin/env python3
"""备份并清空旧版产生的脏巡检数据，建立干净生产基线。

只在 watcher 已停止后运行。旧数据库、报告和录像整体移入 data/backups，
因此清理可恢复；anchors 配置保留，通知幂等 ID 清零但飞书多维表 token 保留。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def watcher_pids() -> list[int]:
    proc = subprocess.run(["pgrep", "-f", "app.recorder.watche[r]"],
                          capture_output=True, text=True)
    return [int(x) for x in proc.stdout.split() if x.isdigit()]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="确认执行清理")
    args = parser.parse_args()
    if not args.execute:
        raise SystemExit("未执行：必须显式传 --execute")
    pids = watcher_pids()
    if pids:
        raise SystemExit(f"拒绝清理：watcher 仍在运行 PID={pids}")

    from app.config import load_config, resolve
    cfg = load_config()
    db_path = resolve(cfg["paths"]["db"])
    recordings = resolve(cfg["recorder"]["out_dir"])
    reports = resolve(cfg["report"]["out_dir"])
    logs = resolve(cfg["paths"]["logs"])
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = ROOT / "data" / "backups" / f"legacy_dirty_{stamp}"
    backup.mkdir(parents=True, exist_ok=False)

    conn = sqlite3.connect(str(db_path))
    counts = {}
    tables = (
        "streams", "transcripts", "highlights", "talktracks", "talktrack_occurrences",
        "reviews", "brief_snapshots", "brief_transcripts", "stream_metrics",
        "daily_metrics", "daibo_daily",
    )
    for table in tables:
        try:
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            counts[table] = 0

    backup_conn = sqlite3.connect(str(backup / "inspection.db"))
    conn.backup(backup_conn)
    backup_conn.close()

    # 先完成数据库备份，再将旧产物目录从生产路径移出。
    for source, name in ((recordings, "recordings"), (reports, "reports"), (logs, "logs")):
        if source.exists():
            shutil.move(str(source), str(backup / name))
        source.mkdir(parents=True, exist_ok=True)

    conn.execute("PRAGMA foreign_keys=OFF")
    for table in tables:
        try:
            conn.execute(f"DELETE FROM {table}")
        except sqlite3.OperationalError:
            pass
    placeholders = ",".join("?" for _ in tables)
    conn.execute(f"DELETE FROM sqlite_sequence WHERE name IN ({placeholders})", tables)
    conn.commit()
    conn.execute("VACUUM")
    conn.close()

    state_path = ROOT / "data" / "notify_state.json"
    if state_path.exists():
        shutil.copy2(state_path, backup / "notify_state.json")
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            state = {}
        for key in ("notified_streams", "notified_card_streams", "notified_bitable_streams"):
            state[key] = []
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "reason": "旧版时间戳、缺失值=0、语义截断、高亮重复评分和不合规报告，退出生产数据集",
        "counts": counts,
        "active_database": str(db_path),
        "recoverable_backup": str(backup),
    }
    (backup / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
