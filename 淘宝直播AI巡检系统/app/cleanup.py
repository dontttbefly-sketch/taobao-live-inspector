"""录像保留策略：按天滚动清理过期录像

- 录像只保留 retention_days 天（config.yaml → recorder.retention_days，默认 1，0=不清理）
- 清理的是原始文件（mp4 + 同名 wav/srt/transcript.json + 分片目录），
  数据库里的转写/高亮/话术/指标/报告全部保留，分析成果不丢
- 触发：1) watcher 每日自动检查一次；2) gen_report.py --week 生成周报后自动清理
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from .config import now_shanghai, resolve

log = logging.getLogger("cleanup")

_ACTIVE_SESSION_STATUSES = {
    "recording", "recovering", "transcribing", "analyzing", "reporting",
}
_ORPHAN_TRANSCRIPTION_MEDIA_AGE_SECONDS = 24 * 60 * 60
_FUNASR_TEMP_AUDIO_RE = re.compile(
    r"^(?P<media_stem>tx_[A-Za-z0-9_-]+)\.funasr\.[A-Za-z0-9_-]+\.wav\Z"
)
_HOUR_MEDIA_WORKDIR_RE = re.compile(r"^\.hour-media-[A-Za-z0-9_-]{8}\Z")
_HOUR_MEDIA_WORKFILE_RE = re.compile(
    r"^(?:clip_\d{3}\.m4a|hour\.m4a|layout\.ffconcat)\Z"
)


def cleanup_old_recordings(cfg: dict, store, days: int | None = None) -> int:
    """删除超过保留期的录像，返回删除的场次数"""
    recorder_cfg = cfg.get("recorder", {}) or {}
    days = days if days is not None else int(recorder_cfg.get("retention_days", 1))
    if days <= 0:
        return 0
    cutoff = (now_shanghai() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    recording_root = resolve(
        recorder_cfg.get("out_dir", "data/recordings")
    ).resolve(strict=False)
    streams = tuple(store.find_streams())
    active_session_dirs: set[Path] = set()
    for stream in streams:
        if (
            stream["ended_at"]
            and str(stream["status"] or "") not in _ACTIVE_SESSION_STATUSES
        ):
            continue
        active_dir = _owned_failed_session_dir(
            stream["session_dir"] or "", recording_root
        )
        if active_dir is not None:
            active_session_dirs.add(active_dir)

    removed = 0
    for s in streams:
        # 只清理已结束且开始时间早于保留期的场次。历史数据库可能已有
        # 超龄但仍在录制/处理中记录，不能仅凭 started_at 删除其活动文件。
        if not s["ended_at"] or s["status"] in _ACTIVE_SESSION_STATUSES:
            continue
        started = s["started_at"] or ""
        if not started or started >= cutoff:
            continue
        fp = s["file_path"] or ""
        if not fp:
            if str(s["status"] or "") not in {"failed", "interrupted"}:
                continue
            session_dir = _owned_failed_session_dir(
                s["session_dir"] or "", recording_root
            )
            if (
                session_dir is None
                or session_dir in active_session_dirs
                or not _delete_failed_session_dir(session_dir)
            ):
                continue
            store.set_stream_status(
                s["id"], s["status"], error=f"录像已清理（保留{days}天）"
            )
            store.execute(
                "UPDATE streams SET session_dir='' WHERE id=?", (s["id"],)
            )
            removed += 1
            continue
        video = Path(fp)
        if _delete_video_artifacts(video):
            # 标记已清理（保留场次记录与分析数据）
            store.set_stream_status(s["id"], s["status"],
                                    error=f"录像已清理（保留{days}天）")
            store.execute("UPDATE streams SET file_path='' WHERE id=?", (s["id"],))
            removed += 1
    if removed:
        log.info("已清理 %d 个过期场次的录像（保留 %d 天）", removed, days)
    return removed


def cleanup_orphan_transcription_media(
        cfg: dict, store, *, now: float | None = None,
        min_age_seconds: int = _ORPHAN_TRANSCRIPTION_MEDIA_AGE_SECONDS,
) -> int:
    """Remove only stale, known temporary transcription files.

    Finished Feishu minutes keep their durable result in SQLite.  This sweep
    deliberately excludes the normal ``.m4a`` job media and touches only two
    crash residues: temporary FunASR WAVs and the flat temporary directories
    created while composing strict hourly audio.  Active jobs and unfamiliar
    files are always retained.
    """
    age = int(min_age_seconds)
    if age <= 0:
        raise ValueError("transcription media retention age must be positive")
    now = float(time.time() if now is None else now)
    paths_cfg = cfg.get("paths", {}) if isinstance(cfg, dict) else {}
    data_path = paths_cfg.get("data", "data") if isinstance(paths_cfg, dict) else "data"
    root = resolve(str(data_path)).resolve(strict=False) / "transcription_media"
    if root.is_symlink() or not root.is_dir():
        return 0
    cutoff = now - age
    protected_media = _active_transcription_media_paths(store, root)
    removed = 0
    try:
        entries = tuple(root.iterdir())
    except OSError:
        return 0
    for entry in entries:
        match = _FUNASR_TEMP_AUDIO_RE.fullmatch(entry.name)
        if match:
            related_media = (root / f"{match.group('media_stem')}.m4a").resolve(
                strict=False)
            if related_media in protected_media or not _expired_regular_file(
                    entry, cutoff):
                continue
            try:
                entry.unlink()
            except OSError:
                continue
            removed += 1
            continue
        if not _HOUR_MEDIA_WORKDIR_RE.fullmatch(entry.name):
            continue
        if _delete_expired_hour_media_workdir(entry, cutoff):
            removed += 1
    if removed:
        log.info("已清理 %d 项过期转写临时文件", removed)
    return removed


def _active_transcription_media_paths(store, root: Path) -> set[Path]:
    """Return local media still owned by a non-finalized transcription job."""
    protected: set[Path] = set()
    rows = store.query(
        "SELECT status,cleanup_status,media_path FROM transcription_jobs "
        "WHERE media_path!=''"
    )
    for row in rows:
        if (
            str(row["status"] or "") == "ready"
            and str(row["cleanup_status"] or "") == "deleted"
        ):
            continue
        candidate = Path(str(row["media_path"] or ""))
        if candidate.is_symlink() or candidate.suffix != ".m4a":
            continue
        try:
            resolved = candidate.resolve(strict=False)
        except OSError:
            continue
        if resolved.parent == root:
            protected.add(resolved)
    return protected


def _expired_regular_file(path: Path, cutoff: float) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    return _expired_path(path, cutoff)


def _expired_path(path: Path, cutoff: float) -> bool:
    """Return whether a non-symlink filesystem object is older than cutoff."""
    if path.is_symlink():
        return False
    try:
        return path.stat().st_mtime <= cutoff
    except OSError:
        return False


def _delete_expired_hour_media_workdir(directory: Path, cutoff: float) -> bool:
    """Delete one old, flat, recognized media-builder workdir without recursion."""
    if directory.is_symlink() or not directory.is_dir():
        return False
    try:
        children = tuple(directory.iterdir())
    except OSError:
        return False
    if (
        not _expired_path(directory, cutoff)
        or any(
            not _HOUR_MEDIA_WORKFILE_RE.fullmatch(child.name)
            or not _expired_regular_file(child, cutoff)
            for child in children
        )
    ):
        return False
    try:
        for child in children:
            child.unlink()
        directory.rmdir()
    except OSError:
        return False
    return True


def _owned_failed_session_dir(raw: str, recording_root: Path) -> Path | None:
    """Return one non-symlink session directory strictly beneath the root."""
    if not str(raw or "").strip():
        return None
    candidate = Path(str(raw))
    if candidate.is_symlink():
        return None
    resolved = candidate.resolve(strict=False)
    try:
        relative = resolved.relative_to(recording_root)
    except ValueError:
        return None
    # Recorder creates exactly ``out_dir / base_name``.  Refuse broader or
    # nested paths even when a stale/corrupt DB row still points below the
    # recording root: automatic cleanup must never become recursive in scope.
    if len(relative.parts) != 1:
        return None
    return resolved


def _delete_failed_session_dir(session_dir: Path) -> bool:
    """Delete a flat, inactive failed-session directory without recursion."""
    if session_dir.is_symlink() or not session_dir.is_dir():
        return False
    try:
        children = tuple(session_dir.iterdir())
        if any(child.is_dir() and not child.is_symlink() for child in children):
            return False
        for child in children:
            child.unlink(missing_ok=True)
        session_dir.rmdir()
        return True
    except OSError as exc:
        log.warning("清理失败场次分片目录失败 %s: %s", session_dir, exc)
        return False


def _delete_video_artifacts(video: Path) -> bool:
    """删除录像及其附属文件（wav/srt/json/分片目录）。文件不存在也算成功"""
    deleted_any = False
    # 主视频
    if video.exists():
        try:
            video.unlink()
            deleted_any = True
        except OSError as e:
            log.warning("删除录像失败 %s: %s", video, e)
            return False
    # 附属文件：同名的 wav/srt/transcript.json
    for suffix in (".wav", ".srt", ".transcript.json"):
        p = video.with_suffix(suffix) if suffix != ".transcript.json" else \
            Path(str(video) + ".transcript.json")
        try:
            if p.exists():
                p.unlink()
                deleted_any = True
        except OSError:
            pass
    # 分片目录（同名目录，如 主播A_20260801_120000/）
    session_dir = video.parent / video.stem
    if session_dir.is_dir():
        try:
            for f in session_dir.iterdir():
                f.unlink(missing_ok=True)
            session_dir.rmdir()
            deleted_any = True
        except OSError as e:
            log.warning("清理分片目录失败 %s: %s", session_dir, e)
    return deleted_any or not video.exists()
