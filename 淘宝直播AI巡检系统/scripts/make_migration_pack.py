#!/usr/bin/env python3
"""制作迁移包：把整套系统打包成 zip，发给 Windows 云主机后运行 运行.bat。

用法：
  .venv/bin/python scripts/make_migration_pack.py --cloud

说明：
- --cloud：给公司 Windows 主机用。含代码、config.yaml、数据库快照、通知账本，
  以及解压后即可双击的 运行.bat。不含录像和合规音频。
- 默认不含录像和敏感状态。新主机从 config.example.yaml 自建配置。
- --include-sensitive 仅作兼容；云端请用 --cloud。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACK_PREFIX = "淘宝直播AI巡检系统"
DATA_EXCLUDE_DIRS = ("recordings",)

CODE_FILES = [
    "app", "scripts", "requirements.txt", "README.md", "AGENTS.md",
    "config.example.yaml", "运行.bat",
]

DATA_INCLUDE = [
    "reports",
]

CLOUD_NOTE = """解压后不要改目录结构。在「运行.bat」所在位置双击，或打开命令行执行：

  运行.bat

第一次会自动安装 Python / ffmpeg / 依赖（需联网，大约 10-20 分钟）。
若弹出飞书登录，只在这台电脑登录一次，不要复制旧电脑的授权。
这个压缩包含登录配置和数据库，只能走内网或加密盘，不要用微信或普通网盘。
"""


def _iter_files(base: Path, rel: str, exclude_dirs: tuple[str, ...] = ()) -> list[tuple[Path, str]]:
    out = []
    p = base / rel
    if p.is_file():
        out.append((p, rel))
    elif p.is_dir():
        for f in sorted(p.rglob("*")):
            if f.is_dir():
                continue
            if any(part in exclude_dirs for part in f.relative_to(p).parts):
                continue
            out.append((f, f"{rel}/{f.relative_to(p)}"))
    return out


def _backup_sqlite(source: Path, destination: Path) -> None:
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dest = sqlite3.connect(destination)
        try:
            src.backup(dest)
        finally:
            dest.close()
    finally:
        src.close()


def planned_members(
    *,
    include_sensitive: bool = False,
    include_recordings: bool = False,
    cloud: bool = False,
    root: Path = ROOT,
) -> list[str]:
    names = ["运行.bat", f"{PACK_PREFIX}/发给云端.txt"] if cloud else []
    files = collect_project_files(
        include_sensitive=include_sensitive or cloud,
        include_recordings=include_recordings,
        cloud=cloud,
        root=root,
    )
    names.extend(f"{PACK_PREFIX}/{relative}" for _path, relative in files)
    if cloud:
        names.append(f"{PACK_PREFIX}/data/inspection.db")
    return names


def collect_project_files(
    *,
    include_sensitive: bool,
    include_recordings: bool,
    cloud: bool = False,
    root: Path = ROOT,
) -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    for rel in CODE_FILES:
        files.extend(_iter_files(root, rel))
    for rel in DATA_INCLUDE:
        files.extend(_iter_files(root, f"data/{rel}", DATA_EXCLUDE_DIRS))
    if include_recordings:
        files.extend(_iter_files(root / "data", "data/recordings"))
    if include_sensitive:
        config = root / "config.yaml"
        if config.is_file():
            files.append((config, "config.yaml"))
        notify = root / "data" / "notify_state.json"
        if notify.is_file():
            files.append((notify, "data/notify_state.json"))
        if not cloud:
            files.extend(_iter_files(root, "data/inspection.db"))
            files.extend(_iter_files(root, "data/logs"))
        schedules = root / "schedules"
        if schedules.exists():
            files.extend(_iter_files(root, "schedules"))
    files = [
        (path, relative)
        for path, relative in files
        if "__pycache__" not in relative
        and not relative.endswith(".pyc")
        and not relative.endswith(".lock")
        and "/audio/" not in relative.replace("\\", "/")
    ]
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description="制作迁移包")
    parser.add_argument("--include-recordings", action="store_true", help="包含录像文件（包会很大）")
    parser.add_argument(
        "--include-sensitive",
        action="store_true",
        help="包含敏感数据（config.yaml/数据库/日志）——勿外传",
    )
    parser.add_argument(
        "--cloud",
        action="store_true",
        help="Windows 云主机包：含配置和数据库快照，解压后运行 运行.bat",
    )
    args = parser.parse_args()
    cloud = bool(args.cloud)
    include_sensitive = bool(args.include_sensitive or cloud)

    stamp = datetime.now().strftime("%Y%m%d")
    out_name = (
        f"淘宝直播AI巡检系统_云端包_{stamp}.zip"
        if cloud
        else f"淘宝直播AI巡检系统_迁移包_{stamp}.zip"
    )
    out_path = ROOT / out_name

    files = collect_project_files(
        include_sensitive=include_sensitive,
        include_recordings=args.include_recordings,
        cloud=cloud,
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        backup_path: Path | None = None
        if cloud:
            source_db = ROOT / "data" / "inspection.db"
            if not source_db.is_file():
                print("缺少 data/inspection.db，无法制作云端包。", file=sys.stderr)
                return 1
            if not (ROOT / "config.yaml").is_file():
                print("缺少 config.yaml，无法制作云端包。", file=sys.stderr)
                return 1
            backup_path = Path(temp_dir) / "inspection.db"
            _backup_sqlite(source_db, backup_path)

        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
            if cloud:
                zf.writestr("运行.bat", (ROOT / "运行.bat").read_bytes())
                zf.writestr(f"{PACK_PREFIX}/发给云端.txt", CLOUD_NOTE.encode("utf-8"))
            for path, relative in files:
                zf.write(path, f"{PACK_PREFIX}/{relative}")
            if backup_path is not None:
                zf.write(backup_path, f"{PACK_PREFIX}/data/inspection.db")

    extra = 3 if cloud else 0  # root 运行.bat + 发给云端.txt + db snapshot
    size_mb = out_path.stat().st_size / 1024 / 1024
    print(f"✓ 迁移包已生成: {out_path}")
    print(
        f"  文件数: {len(files) + extra}，大小: {size_mb:.1f} MB"
        + ("（含录像）" if args.include_recordings else "（不含录像）")
    )
    if cloud:
        print("  云端包：解压后运行 运行.bat。含配置和数据库，请加密传输，用后删除中间副本。")
    elif include_sensitive:
        print("  ⚠ 包含 config.yaml/数据库/日志（含 cookie 与 key），请加密传输，用后删除")
    else:
        print("  安全模式：不含 config.yaml（cookie/key）、数据库、日志；"
              "到新机器后需重新填 config.yaml")
        print("  在 Windows 上解压后，按 迁移到Windows.md 执行 setup_windows.bat")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
