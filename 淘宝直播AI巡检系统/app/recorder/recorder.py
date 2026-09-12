"""ffmpeg 录制：分片录制 + 断流重连 + 流地址轮换切流 + 合并封装

策略：
- 每次 ffmpeg 调用写一个 part_XXX.ts（最多 segment_seconds 秒，0 为不限）
- 流地址轮换/进程异常退出 → 自动开下一个 part，衔接录制
- 结束/下播后把所有 part 用 concat demuxer 合并成单一 mp4
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

from .timeline import TimelineManifest

log = logging.getLogger(__name__)

MIN_VALID_PART_BYTES = 16 * 1024
MIN_MEDIA_PROGRESS_BYTES = 256 * 1024


def _ffmpeg_common(ffmpeg: str, url: str) -> list[str]:
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "warning"]
    # reconnect 参数只对裸流（FLV/RTMP/TCP）有效；HLS 输入加了反而会陷入
    # 无限重拉播放列表的死循环（实测），HLS 直播源自带断点续拉能力
    if ".m3u8" not in url:
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5", "-reconnect_at_eof", "1"]
    return cmd


def url_stem(url: str) -> str:
    """URL 主干（去掉 query 参数）。auth_key 每次探测都会变，比较主干判断是否真的换了流"""
    return url.split("?")[0]


class Recorder:
    def __init__(self, ffmpeg: str, out_dir: Path, base_name: str,
                 segment_seconds: int = 0, referer: str = "https://h5.m.taobao.com/",
                 user_agent: str = "", candidates: list[str] | None = None,
                 stall_timeout_sec: int = 90, retry_base_sec: int = 30,
                 retry_max_sec: int = 300,
                 process_started: Callable[[int], None] | None = None,
                 media_started: Callable[[str], None] | None = None):
        self.ffmpeg = ffmpeg
        self.out_dir = Path(out_dir)
        self.base_name = base_name
        self.segment_seconds = segment_seconds
        self.referer = referer
        self.user_agent = user_agent
        # 候选流地址（不同主干：HLS/FLV 等），当前 URL 反复失败时自动轮换
        self.candidates: list[str] = []
        self.proc: subprocess.Popen | None = None
        self.current_url: str = ""
        self.current_part: Path | None = None
        self.part_index = 0
        self._timeline_finalized_part = ""
        self.stopping = False
        # 连续失败跨候选流累计；只有当前分片真实写入足量媒体字节后才清零。
        self.fail_count = 0
        self.stall_timeout_sec = max(30, int(stall_timeout_sec))
        self.retry_base_sec = max(5, int(retry_base_sec))
        self.retry_max_sec = max(self.retry_base_sec, int(retry_max_sec))
        self._proc_started_at = 0.0
        self._last_observed_size = 0
        self._part_had_media = False
        self._failed_stems: set[str] = set()
        self._retry_round = 0
        self._next_retry_at = 0.0
        # 当前 ffmpeg 的卡死时钟与全局媒体时钟分开。切流只能
        # 重置前者；后者只能由真实的媒体字节增长更新。
        self._attempt_progress_at = time.time()
        self.last_media_at = time.time()
        self._process_started = process_started
        self._media_started = media_started
        self.session_dir = self.out_dir / base_name
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.timeline = TimelineManifest(self.session_dir)
        existing_indexes = [
            int(path.stem[5:])
            for path in self.session_dir.glob("part_*.ts")
            if path.stem.startswith("part_") and path.stem[5:].isdigit()
        ]
        # A restart must append.  Legacy parts deliberately remain absent from
        # the new manifest, but they must never be overwritten.
        self.part_index = max(
            int(self.timeline.next_part_index) - 1,
            max(existing_indexes, default=0),
        )
        self.update_candidates(candidates or [])

    # ---------- 生命周期 ----------
    def start(self, url: str) -> None:
        """开始（或切换）录制一个 part"""
        self.stop_proc()
        self.current_url = url
        # 新 part 只重置当次拉流的卡死时钟。
        self._attempt_progress_at = time.time()
        self.part_index += 1
        self._proc_started_at = time.time()
        part_file = self.session_dir / f"part_{self.part_index:03d}.ts"
        self.current_part = part_file
        self._timeline_finalized_part = ""
        self._last_observed_size = part_file.stat().st_size if part_file.exists() else 0
        self._part_had_media = False
        log.info("[%s] 开始录制 part_%03d: %s", self.base_name, self.part_index, url[:120])
        # 视频流 copy；音频重编码为 AAC（mp4->ts 直接 copy AAC 会损坏 ADTS 封装，
        # 重编码保证每个分片自包含、可独立解码，后续拼接与 ASR 才稳定）
        cmd = _ffmpeg_common(self.ffmpeg, url) + [
            "-headers", f"User-Agent: {self.user_agent or 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}\r\n"
                        f"Referer: {self.referer}\r\n",
            "-i", url,
            "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-f", "mpegts",
        ]
        if self.segment_seconds > 0:
            # 本 part 最多录 segment_seconds 秒，到点由外层重新拉起（自动续录）
            cmd += ["-t", str(self.segment_seconds)]
        cmd += [str(part_file)]
        logfile = self.session_dir / f"ffmpeg_{self.part_index:03d}.log"
        with open(logfile, "wb") as log_handle:
            self.proc = subprocess.Popen(
                cmd, stdout=log_handle, stderr=subprocess.STDOUT, start_new_session=True,
            )
        try:
            self.timeline.mark_started(part_file)
        except Exception as exc:
            # Do not leave an untracked ffmpeg process behind when the durable
            # time ledger cannot be initialized.
            try:
                self.proc.terminate()
                self.proc.wait(timeout=15)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None
            raise RuntimeError(
                f"recording timeline initialization failed: {type(exc).__name__}") from exc
        if self._media_started is not None:
            try:
                self._media_started(url)
            except Exception as exc:
                # Recovery metadata is best-effort; never stop live media and
                # never expose the signed URL in logs.
                log.error("[%s] 续录线索持久化失败: %s",
                          self.base_name, type(exc).__name__)
        if self._process_started is not None:
            self._process_started(self.proc.pid)

    def stop_proc(self) -> None:
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=15)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None
        self._finalize_current_part()

    def _observe_timeline_growth(self, part: Path, size: int) -> None:
        try:
            self.timeline.observe_growth(part, size_bytes=size)
        except Exception as exc:
            # A missing/incomplete ledger fails formal brief coverage closed;
            # it must not stop the recorder from retaining recoverable media.
            log.error("[%s] 录像时间线增长记录失败: %s",
                      self.base_name, type(exc).__name__)

    def _finalize_current_part(self) -> None:
        part = self.current_part
        if part is None or self._timeline_finalized_part == part.name:
            return
        try:
            self.timeline.finalize_part(part)
        except Exception as exc:
            log.error("[%s] 录像时间线分片收尾失败: %s",
                      self.base_name, type(exc).__name__)
        finally:
            self._timeline_finalized_part = part.name

    def is_running(self) -> bool:
        if not self.proc:
            return False
        if self.proc.poll() is None:
            size = 0
            if self.current_part and self.current_part.exists():
                try:
                    size = self.current_part.stat().st_size
                except OSError:
                    size = 0
            if size > self._last_observed_size:
                self._last_observed_size = size
                if self.current_part is not None:
                    self._observe_timeline_growth(self.current_part, size)
                now = time.time()
                self._attempt_progress_at = now
                self.last_media_at = now
                if size >= MIN_MEDIA_PROGRESS_BYTES:
                    self._part_had_media = True
                    self.fail_count = 0
                    self._failed_stems.clear()
                    self._retry_round = 0
                    self._next_retry_at = 0.0
            if time.time() - self._attempt_progress_at >= self.stall_timeout_sec:
                ran = time.time() - self._proc_started_at
                log.warning("[%s] ffmpeg 存活但媒体 %ds 未增长，判定拉流卡死",
                            self.base_name, self.stall_timeout_sec)
                self.stop_proc()
                self._mark_failure(ran)
                return False
            return True
        # 进程已退出：真实写入媒体的正常分片不计失败；空转/小文件才计失败。
        ran = time.time() - self._proc_started_at
        self.proc = None
        self._finalize_current_part()
        if not self._part_had_media:
            self._mark_failure(ran)
        return False

    def _mark_failure(self, ran: float) -> None:
        self.fail_count += 1
        if self.current_url:
            self._failed_stems.add(url_stem(self.current_url))
        log.warning("[%s] 拉流失败（运行 %.0fs，无有效媒体），连续失败 %d",
                    self.base_name, ran, self.fail_count)

    def update_candidates(self, urls: list[str]) -> None:
        """刷新候选地址并去掉同主干重复项；同主干的新 auth URL 留给下一分片使用。"""
        unique: list[str] = []
        seen: set[str] = set()
        for url in urls:
            stem = url_stem(url)
            if url and stem not in seen:
                seen.add(stem)
                unique.append(url)
        if unique:
            self.candidates = unique
            current_stem = url_stem(self.current_url) if self.current_url else ""
            for candidate in unique:
                if url_stem(candidate) == current_stem:
                    # 不打断正在录制的进程，只更新下一次拉起时使用的签名 URL。
                    self.current_url = candidate
                    break

    def rearm_after_live_signal(self) -> None:
        """明确在播信号出现后立即解除地址轮换退避，但保留失败证据。"""
        self._failed_stems.clear()
        self._retry_round = 0
        self._next_retry_at = 0.0

    def _next_candidate(self) -> str | None:
        """从候选里找与当前 URL 主干不同的下一个；没有则 None（全部试过）"""
        for u in self.candidates:
            if u and url_stem(u) not in self._failed_stems:
                return u
        return None

    def tick(self, url: str | None = None, candidates: list[str] | None = None) -> None:
        """周期调用：进程死了就续录/轮换候选；全部失败后定期重试"""
        if self.stopping:
            return
        if candidates:
            self.update_candidates(candidates)
        if url:
            if not self.current_url:
                self.current_url = url
            elif url_stem(url) == url_stem(self.current_url):
                self.current_url = url  # 刷新 auth，当前 ffmpeg 不重启
            elif url_stem(self.current_url) not in {url_stem(x) for x in self.candidates}:
                self.switch_url(url)
                return
        if self.is_running():
            return
        now = time.time()
        if now < self._next_retry_at:
            return
        nxt = self._next_candidate()
        if nxt is not None:
            if self.current_url and url_stem(nxt) != url_stem(self.current_url):
                log.info("[%s] 当前流不可用，轮换候选地址", self.base_name)
            self.start(nxt)
            return
        delay = min(self.retry_max_sec, self.retry_base_sec * (2 ** self._retry_round))
        self._retry_round = min(self._retry_round + 1, 10)
        self._next_retry_at = now + delay
        self._failed_stems.clear()
        log.warning("[%s] 所有候选流均失败，%ds 后重试", self.base_name, delay)

    def switch_url(self, url: str) -> bool:
        """流地址主干变化时切流。返回是否发生了切换"""
        if url_stem(url) == url_stem(self.current_url):
            self.current_url = url
            return False
        log.info("[%s] 流地址变化，切换录制", self.base_name)
        self._next_retry_at = 0.0
        self.start(url)
        return True

    def seconds_since_media(self, now: float | None = None) -> float:
        return max(0.0, (now if now is not None else time.time()) - self.last_media_at)

    # ---------- 结束 ----------
    def stop(self, cleanup_parts: bool = True) -> Path:
        """停止录制并合并 part -> mp4，返回最终文件路径"""
        self.stopping = True
        self.stop_proc()
        self.timeline.finalize_unfinished_parts()
        parts = [p for p in sorted(self.session_dir.glob("part_*.ts"))
                 if p.stat().st_size >= MIN_VALID_PART_BYTES]
        if not parts:
            raise FileNotFoundError(f"{self.base_name} 没有产生任何录制分片")

        final_mp4 = self.out_dir / f"{self.base_name}.mp4"
        temp_mp4 = self.out_dir / f".{self.base_name}.recovering.mp4"
        if len(parts) == 1:
            self._remux(parts[0], temp_mp4)
        else:
            self._concat(parts, temp_mp4)
        if not temp_mp4.exists() or temp_mp4.stat().st_size <= 0:
            raise RuntimeError(f"{self.base_name} 合并输出无效")
        os.replace(temp_mp4, final_mp4)
        log.info("[%s] 录制完成: %s (%d 个分片)", self.base_name, final_mp4.name, len(parts))
        if cleanup_parts:
            self.cleanup_parts()
        return final_mp4

    def abort(self) -> None:
        """异常中断：保留分片但不合并"""
        self.stopping = True
        self.stop_proc()
        self.timeline.finalize_unfinished_parts()

    def _remux(self, src: Path, dst: Path) -> None:
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "warning",
               "-y", "-fflags", "+genpts", "-i", str(src), "-c", "copy",
               "-movflags", "+faststart", str(dst)]
        subprocess.run(cmd, check=True, capture_output=True)

    def _concat(self, parts: list[Path], dst: Path) -> None:
        list_file = self.session_dir / "concat.txt"
        list_file.write_text("\n".join(f"file '{p.name}'" for p in parts) + "\n", encoding="utf-8")
        # 视频流 copy（快），音频重编码（解决 AAC priming 采样导致的时间错位/丢时长，
        # 音频量极小，重编码成本可忽略，且保证后续 ASR 转写的时间轴准确）
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "warning",
               "-y", "-fflags", "+genpts", "-f", "concat", "-safe", "0",
               "-i", str(list_file), "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
               "-movflags", "+faststart", str(dst)]
        subprocess.run(cmd, check=True, capture_output=True)

    def cleanup_parts(self) -> None:
        for f in self.session_dir.iterdir():
            if f.suffix in (".ts", ".log", ".wav"):
                f.unlink(missing_ok=True)


def probe_ffmpeg(ffmpeg: str) -> bool:
    return shutil.which(ffmpeg) is not None


def extract_audio(ffmpeg: str, video_path: Path, wav_path: Path) -> None:
    """提取 16k 单声道 wav，供 ASR 使用"""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000",
           "-c:a", "pcm_s16le", str(wav_path)]
    subprocess.run(cmd, check=True, capture_output=True)
