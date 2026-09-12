#!/usr/bin/env python3
"""更新淘宝 cookie（风控/过期后重新导出时执行）

用法：
  .venv/bin/python scripts/update_cookie.py < cookie.txt   # 从文件读
  .venv/bin/python scripts/update_cookie.py --no-restart   # 只更新不重启

行为：
1. 从 stdin 读取新 cookie（不经过 shell 参数/历史，避免泄露）
2. 更新 config.yaml 的 taobao.cookie
3. 优雅重启 watcher（当前场次收尾，launchd 自动拉起，新配置生效）
4. 用新 cookie 探测一次直播状态验证
"""
from __future__ import annotations

import logging
import hashlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from collections.abc import Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

log = logging.getLogger("update_cookie")


def recover_session(
        cookie: str, *, cfg: dict, store, config_path: Path,
        validator: Callable | None = None,
        activate_client: Callable | None = None,
        notify_recovered: Callable | None = None) -> bool:
    """手工 Cookie 也经过四层业务验证、原子写入和等待任务恢复。"""
    from app.notify.feishu import (notify_taobao_auth_recovered,
                                   notify_taobao_auth_required)
    from app.recorder.mtop import activate_shared_cookie
    from app.taobao_session.keeper import SessionKeeper, validate_candidate_cookie
    from app.taobao_session.provider import StaticBrowserSessionProvider

    state = store.get_taobao_session_state()
    if str(state["status"]) == "healthy":
        store.note_taobao_auth_failure("manual_cookie_recovery", now=time.time())
    else:
        # 人工已提供新候选会话，不再受自动轮询的 5 分钟截止限制。
        store.set_taobao_session_recovery_state(
            "auto_recovering", next_check_at=time.time())
    keeper = SessionKeeper(
        store, cfg, StaticBrowserSessionProvider(str(cookie)),
        config_path=Path(config_path),
        validator=validator or validate_candidate_cookie,
        activate_client=activate_client or activate_shared_cookie,
        notify_required=lambda payload: notify_taobao_auth_required(cfg, payload),
        notify_recovered=(notify_recovered or
                          (lambda payload: notify_taobao_auth_recovered(cfg, payload))),
    )
    return keeper.tick(now=time.time(), force_recovery=True)


def update_config_cookie(cookie: str, *, path: Path | None = None) -> bool:
    """把 taobao.cookie 写回 config.yaml（只改该字段，保留其余配置）"""
    from app.config import CONFIG_PATH
    from app.taobao_session.config import write_taobao_cookie
    p = Path(path or CONFIG_PATH)
    if not p.exists():
        print("✗ config.yaml 不存在")
        return False
    try:
        write_taobao_cookie(p, cookie)
        fingerprint = hashlib.sha256(cookie.encode("utf-8")).hexdigest()[:10]
        print(f"✓ config.yaml taobao.cookie 已更新（{len(cookie)} 字符，指纹 {fingerprint}）")
        return True
    except Exception as exc:
        print(f"✗ 更新失败：{type(exc).__name__}")
        return False


def _list_watcher_pids() -> list[int]:
    proc = subprocess.run(["pgrep", "-f", "app.recorder.watche[r]"],
                          capture_output=True, text=True)
    return [int(p) for p in proc.stdout.split() if p.strip().isdigit()]


def restart_watcher() -> None:
    for pid in _list_watcher_pids():
        try:
            import os
            os.kill(pid, signal.SIGINT)
            print(f"✓ 已通知旧 watcher({pid}) 收尾")
        except Exception as e:
            print(f"  停止旧进程失败: {e}")
    time.sleep(10)
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True,
                             text=True, timeout=5).stdout
        if "torras.live-inspection" in out:
            print("✓ launchd 托管中，KeepAlive 将自动拉起新 watcher")
            time.sleep(6)
            return
    except Exception:
        pass
    print("⚠ 未检测到系统托管，请手动启动：")
    print("  .venv/bin/python -m app.recorder.watcher > data/logs/watcher.log 2>&1 &")


def quiesce_watcher(lock_path: Path):
    """停稳旧 watcher 并持有单实例锁，防止 launchd 在交接中途抢启旧配置。"""
    handle = open(lock_path, "a+")
    pids = _list_watcher_pids()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGINT)
            print(f"✓ 已通知旧 watcher({pid}) 安全收尾")
        except OSError:
            pass
    try:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_EX)
    except ImportError:
        import msvcrt
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                time.sleep(1)
    print("✓ 旧 watcher 已停稳，开始原子交接新登录态")
    return handle


def wait_watcher_restart(timeout_sec: int = 30) -> bool:
    deadline = time.time() + max(1, int(timeout_sec))
    while time.time() < deadline:
        if _list_watcher_pids():
            print("✓ launchd 已用新登录态拉起 watcher")
            return True
        time.sleep(1)
    print("⚠ 未检测到 watcher 自动拉起，请手工启动")
    return False


def probe() -> None:
    from app.config import load_config
    from app.recorder.mtop import MtopClient
    cfg = load_config()
    t = cfg.get("taobao", {})
    try:
        client = MtopClient(cookie=t.get("cookie", ""), app_key=t.get("app_key", "12574478"),
                            user_agent=t.get("user_agent", ""),
                            referer=t.get("referer", "https://h5.m.taobao.com/"))
        info = client.probe(live_id=t.get("live_id", ""), user_id="")
        ok = "SUCCESS" in str(info.get("ret", ""))
        print("✓ 探测结果:", "直播中" if info.get("is_live") else "非在播",
              "| 接口返回:", "成功" if ok else info.get("ret"))
        return ok
    except Exception as e:
        print("✗ 探测失败（cookie 可能仍有问题）:", e)
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    no_restart = "--no-restart" in sys.argv
    cookie = sys.stdin.read().strip()
    if not cookie:
        print("✗ 未从 stdin 读到 cookie，用法：.venv/bin/python scripts/update_cookie.py < cookie.txt")
        sys.exit(1)
    if no_restart and _list_watcher_pids():
        print("✗ watcher 正在运行时不能使用 --no-restart，否则旧进程会继续持有过期登录态")
        sys.exit(1)
    from app.config import CONFIG_PATH, load_config, resolve
    from app.db import Store
    from app.taobao_session.keeper import CandidateValidation, validate_candidate_cookie
    cfg = load_config()
    validation = validate_candidate_cookie(cfg, cookie)
    if not validation.ok:
        print(f"✗ 新登录态未通过完整业务验证（{validation.failed_layer}），未停止 watcher")
        sys.exit(1)
    handoff_lock = None
    if not no_restart:
        handoff_lock = quiesce_watcher(
            resolve(cfg.get("paths", {}).get("db", "data/inspection.db")).parent
            / "watcher.lock")
    store = Store(resolve(cfg.get("paths", {}).get("db", "data/inspection.db")))
    try:
        recovered = recover_session(
            cookie, cfg=cfg, store=store, config_path=CONFIG_PATH,
            validator=lambda *_args: CandidateValidation(True))
    finally:
        store.conn.close()
        if handoff_lock is not None:
            handoff_lock.close()
    if not recovered:
        print("✗ 新登录态未通过完整业务验证，未写入生产配置")
        sys.exit(1)
    fingerprint = hashlib.sha256(cookie.encode("utf-8")).hexdigest()[:10]
    print(f"✓ 新登录态已通过四层验证并安全写入（{len(cookie)} 字符，指纹 {fingerprint}）")
    if no_restart:
        print("（--no-restart）未检测到运行中 watcher，新登录态将在下次启动时生效")
        sys.exit(0)
    wait_watcher_restart()
    probe()
