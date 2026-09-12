"""External liveness watchdog for the recorder watcher.

The watchdog deliberately supervises only a process heartbeat.  It never
imports ``Watcher`` or ``Recorder`` and therefore cannot become a second
recording controller.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _read_heartbeat(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    return value


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_heartbeat(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink():
        raise ValueError("watchdog heartbeat directory is invalid")
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


class HeartbeatPublisher:
    """Atomically publish main-thread watcher liveness to a private file."""

    def __init__(
        self,
        path: Path,
        *,
        pid: int,
        token: str,
        process_identity: str,
        interval_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if int(pid) <= 1:
            raise ValueError("watchdog heartbeat pid is invalid")
        if not token or not process_identity:
            raise ValueError("watchdog heartbeat identity is invalid")
        self.path = Path(path)
        self.pid = int(pid)
        self.token = str(token)
        self.process_identity = str(process_identity)
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.clock = clock
        self._last_written_at: float | None = None
        self._state = "running"

    def _payload(self, now: float) -> dict[str, Any]:
        return {
            "version": 1,
            "pid": self.pid,
            "token": self.token,
            "process_identity": self.process_identity,
            "updated_at": float(now),
            "state": self._state,
        }

    def beat(self, *, force: bool = False) -> bool:
        now = float(self.clock())
        if (
            not force
            and self._last_written_at is not None
            and now - self._last_written_at < self.interval_seconds
        ):
            return False
        self._state = "running"
        _write_heartbeat(self.path, self._payload(now))
        self._last_written_at = now
        return True

    def mark_stopping(self) -> None:
        now = float(self.clock())
        self._state = "stopping"
        _write_heartbeat(self.path, self._payload(now))
        self._last_written_at = now


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return int(default)
    if parsed < minimum or parsed > maximum:
        return int(default)
    return parsed


@dataclass(frozen=True)
class WatchdogSettings:
    enabled: bool = True
    heartbeat_interval_seconds: int = 5
    timeout_seconds: int = 180
    check_interval_seconds: int = 5
    terminate_grace_seconds: int = 30

    @classmethod
    def from_config(cls, cfg: dict) -> "WatchdogSettings":
        raw = cfg.get("watchdog", {}) if isinstance(cfg, dict) else {}
        if not isinstance(raw, dict):
            raw = {}
        enabled_value = raw.get("enabled", True)
        enabled = enabled_value if isinstance(enabled_value, bool) else True
        timeout_seconds = _bounded_int(
            raw.get("timeout_seconds"), default=180, minimum=60, maximum=900,
        )
        check_interval_seconds = _bounded_int(
            raw.get("check_interval_seconds"), default=5, minimum=1, maximum=60,
        )
        return cls(
            enabled=enabled,
            heartbeat_interval_seconds=_bounded_int(
                raw.get("heartbeat_interval_seconds"), default=5,
                minimum=1, maximum=60,
            ),
            timeout_seconds=timeout_seconds,
            check_interval_seconds=min(check_interval_seconds, timeout_seconds),
            terminate_grace_seconds=_bounded_int(
                raw.get("terminate_grace_seconds"), default=30,
                minimum=5, maximum=120,
            ),
        )


def build_watchdog_command(
    settings: WatchdogSettings,
    heartbeat: HeartbeatPublisher,
    *,
    python_executable: str,
) -> tuple[str, ...]:
    if not python_executable:
        raise ValueError("watchdog python executable is invalid")
    return (
        str(python_executable), "-m", "app.recorder.watchdog",
        "--heartbeat-path", str(heartbeat.path.resolve()),
        "--parent-pid", str(heartbeat.pid),
        "--timeout-seconds", str(settings.timeout_seconds),
        "--check-interval-seconds", str(settings.check_interval_seconds),
        "--terminate-grace-seconds", str(settings.terminate_grace_seconds),
    )


def process_identity(pid: int) -> str:
    """Return a non-reversible identity for one live watcher process."""
    if int(pid) <= 1:
        return ""
    try:
        result = subprocess.run(
            ["ps", "-p", str(int(pid)), "-o", "lstart=", "-o", "command="],
            capture_output=True, text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    raw = result.stdout.strip()
    if result.returncode != 0 or not raw:
        return ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def process_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def create_watcher_heartbeat(
    settings: WatchdogSettings,
    runtime_dir: Path,
    *,
    pid: int | None = None,
    identity_reader: Callable[[int], str] = process_identity,
    token_factory: Callable[[int], str] = secrets.token_hex,
    clock: Callable[[], float] = time.time,
) -> HeartbeatPublisher:
    if not settings.enabled:
        raise ValueError("watchdog is disabled")
    target_pid = int(os.getpid() if pid is None else pid)
    identity = identity_reader(target_pid)
    if not identity:
        raise RuntimeError("watchdog process identity is unavailable")
    token = token_factory(24)
    if not token:
        raise RuntimeError("watchdog instance token is unavailable")
    publisher = HeartbeatPublisher(
        Path(runtime_dir) / "watcher-heartbeat.json",
        pid=target_pid,
        token=token,
        process_identity=identity,
        interval_seconds=settings.heartbeat_interval_seconds,
        clock=clock,
    )
    publisher.beat(force=True)
    return publisher


def wait_for_process_exit(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + max(0.0, float(seconds))
    while time.monotonic() < deadline:
        if not process_alive(pid):
            return True
        time.sleep(0.1)
    return not process_alive(pid)


def start_watchdog_process(
    settings: WatchdogSettings,
    heartbeat: HeartbeatPublisher,
    *,
    python_executable: str = sys.executable,
) -> subprocess.Popen:
    if not settings.enabled:
        raise ValueError("watchdog is disabled")
    command = build_watchdog_command(
        settings, heartbeat, python_executable=python_executable,
    )
    root = Path(__file__).resolve().parents[2]
    return subprocess.Popen(
        list(command), cwd=root, start_new_session=True, close_fds=True,
    )


def run_watchdog_loop(
    supervisor: WatchdogSupervisor,
    *,
    check_interval_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    delay = max(0.1, float(check_interval_seconds))
    while True:
        outcome = supervisor.check_once()
        if outcome in {"recovered", "target_gone"}:
            return outcome
        sleep(delay)


def _initial_target(heartbeat_path: Path, parent_pid: int) -> tuple[str, str] | None:
    heartbeat = _read_heartbeat(heartbeat_path)
    if heartbeat is None or heartbeat.get("version") != 1:
        return None
    if heartbeat.get("pid") != int(parent_pid):
        return None
    token = heartbeat.get("token")
    identity = heartbeat.get("process_identity")
    if not isinstance(token, str) or not token:
        return None
    if not isinstance(identity, str) or not identity:
        return None
    return token, identity


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--heartbeat-path", required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=int, required=True)
    parser.add_argument("--check-interval-seconds", type=int, required=True)
    parser.add_argument("--terminate-grace-seconds", type=int, required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2
    heartbeat_path = Path(args.heartbeat_path)
    if (
        not heartbeat_path.is_absolute()
        or heartbeat_path.is_symlink()
        or heartbeat_path.name != "watcher-heartbeat.json"
        or int(args.parent_pid) <= 1
        or int(args.timeout_seconds) < 60
        or int(args.check_interval_seconds) <= 0
        or int(args.terminate_grace_seconds) < 5
    ):
        return 2
    target = _initial_target(heartbeat_path, int(args.parent_pid))
    if target is None:
        return 2
    token, identity = target
    supervisor = WatchdogSupervisor(
        heartbeat_path=heartbeat_path,
        expected_pid=int(args.parent_pid),
        expected_token=token,
        expected_identity=identity,
        timeout_seconds=int(args.timeout_seconds),
        terminate_grace_seconds=int(args.terminate_grace_seconds),
        process_identity=process_identity,
        process_alive=process_alive,
        signal_process=os.kill,
        wait_for_exit=wait_for_process_exit,
    )
    run_watchdog_loop(
        supervisor, check_interval_seconds=int(args.check_interval_seconds),
    )
    return 0


class WatchdogSupervisor:
    """Verify one watcher identity and recover it after a stale heartbeat."""

    def __init__(
        self,
        *,
        heartbeat_path: Path,
        expected_pid: int,
        expected_token: str,
        expected_identity: str,
        timeout_seconds: float,
        terminate_grace_seconds: float,
        startup_grace_seconds: float | None = None,
        started_at: float | None = None,
        process_identity: Callable[[int], str],
        process_alive: Callable[[int], bool],
        signal_process: Callable[[int, int], None],
        wait_for_exit: Callable[[int, float], bool],
        clock: Callable[[], float] = time.time,
    ) -> None:
        if int(expected_pid) <= 1:
            raise ValueError("watchdog expected pid is invalid")
        if not expected_token or not expected_identity:
            raise ValueError("watchdog target identity is invalid")
        self.heartbeat_path = Path(heartbeat_path)
        self.expected_pid = int(expected_pid)
        self.expected_token = str(expected_token)
        self.expected_identity = str(expected_identity)
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.terminate_grace_seconds = max(0.0, float(terminate_grace_seconds))
        self.startup_grace_seconds = max(
            1.0,
            float(timeout_seconds if startup_grace_seconds is None else startup_grace_seconds),
        )
        self.process_identity = process_identity
        self.process_alive = process_alive
        self.signal_process = signal_process
        self.wait_for_exit = wait_for_exit
        self.clock = clock
        self.started_at = float(self.clock() if started_at is None else started_at)

    def _target_matches(self) -> bool:
        try:
            return bool(self.process_alive(self.expected_pid)) and (
                self.process_identity(self.expected_pid) == self.expected_identity
            )
        except Exception:
            return False

    def _matching_heartbeat(self) -> dict[str, Any] | None:
        heartbeat = _read_heartbeat(self.heartbeat_path)
        if heartbeat is None:
            return None
        if (
            heartbeat.get("version") != 1
            or heartbeat.get("pid") != self.expected_pid
            or heartbeat.get("token") != self.expected_token
            or heartbeat.get("process_identity") != self.expected_identity
            or heartbeat.get("state") not in {"running", "stopping"}
        ):
            return None
        updated_at = heartbeat.get("updated_at")
        if not isinstance(updated_at, (int, float)) or not math.isfinite(updated_at):
            return None
        return heartbeat

    def _is_stale(self, heartbeat: dict[str, Any]) -> bool:
        updated_at = float(heartbeat["updated_at"])
        return float(self.clock()) - updated_at >= self.timeout_seconds

    def _recover_verified_target(self) -> None:
        try:
            self.signal_process(self.expected_pid, signal.SIGTERM)
        except OSError:
            return
        if self.wait_for_exit(self.expected_pid, self.terminate_grace_seconds):
            return
        if not self._target_matches():
            return
        try:
            self.signal_process(self.expected_pid, signal.SIGKILL)
        except OSError:
            return

    def check_once(self) -> str:
        if not self._target_matches():
            return "target_gone"
        heartbeat = self._matching_heartbeat()
        if heartbeat is None:
            if float(self.clock()) - self.started_at >= self.startup_grace_seconds:
                self._recover_verified_target()
                return "recovered"
            return "healthy"
        if heartbeat["state"] == "stopping":
            return "stopping"
        if not self._is_stale(heartbeat):
            return "healthy"
        self._recover_verified_target()
        return "recovered"


if __name__ == "__main__":  # pragma: no cover - covered through subprocess
    raise SystemExit(_main())
