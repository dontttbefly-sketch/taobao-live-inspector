from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sqlite3
import tempfile
import time
import hmac
import sys
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import quote


_SHA256_LENGTH = 64
_TIMELINE_ROOT_KEYS = {
    "schema_version", "created_at_ms", "updated_at_ms", "parts",
}
_TIMELINE_PART_KEYS = {
    "relative_path", "started_at_ms", "first_media_at_ms",
    "last_growth_at_ms", "duration_ms", "size_bytes", "state",
}


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_private_json(path: Path, payload: dict[str, object]) -> None:
    path = Path(path)
    parent = path.parent
    if parent.exists() and (parent.is_symlink() or not parent.is_dir()):
        raise ValueError("compliance watchdog directory is invalid")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError("compliance watchdog directory is invalid")
    parent.chmod(0o700)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("compliance watchdog file is invalid")
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=parent,
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
        _fsync_directory(parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _read_private_json(path: Path) -> dict[str, object] | None:
    path = Path(path)
    try:
        if (
            path.parent.is_symlink()
            or path.is_symlink()
            or not path.is_file()
            or path.stat().st_mode & 0o077
        ):
            return None
        encoded = path.read_text(encoding="utf-8")
        document = json.loads(
            encoded,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
        if not isinstance(document, dict):
            return None
        return document
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def process_identity(pid: int) -> str:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError("invalid compliance process identity")
    if os.name == "nt":
        command = (
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            "(Get-Process -Id $args[0]).StartTime.ToFileTimeUtc()", str(pid),
        )
    else:
        command = ("ps", "-o", "lstart=", "-o", "command=", "-p", str(pid))
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    material = str(completed.stdout).strip()
    if completed.returncode != 0 or not material:
        raise RuntimeError("compliance process identity unavailable")
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProgressEvidence:
    upstream_active: bool
    compliance_stalled: bool
    reason: str


@dataclass(frozen=True)
class WatchdogSettings:
    heartbeat_path: Path
    budget_path: Path
    db_path: Path
    parent_pid: int
    token: str
    process_identity: str
    heartbeat_stale_seconds: float = 180.0
    audio_stale_seconds: float = 300.0
    upstream_fresh_seconds: float = 120.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.parent_pid, bool)
            or not isinstance(self.parent_pid, int)
            or self.parent_pid <= 0
            or not isinstance(self.token, str)
            or not self.token
            or len(self.token) > 128
            or not isinstance(self.process_identity, str)
            or len(self.process_identity) != _SHA256_LENGTH
            or any(
                character not in "0123456789abcdef"
                for character in self.process_identity
            )
            or float(self.heartbeat_stale_seconds) != 180.0
            or float(self.audio_stale_seconds) != 300.0
            or float(self.upstream_fresh_seconds) != 120.0
        ):
            raise ValueError("invalid compliance watchdog settings")
        object.__setattr__(self, "heartbeat_path", Path(self.heartbeat_path))
        object.__setattr__(self, "budget_path", Path(self.budget_path))
        object.__setattr__(self, "db_path", Path(self.db_path))


class RecoveryBudget:
    _WINDOW_SECONDS = 1_800.0
    _COOLDOWN_SECONDS = 1_800.0
    _REPLACEMENT_GRACE_SECONDS = 300.0
    _MAX_RECOVERIES = 3
    _REASONS = {"heartbeat_stale", "compliance_audio_stale"}

    def __init__(self, path: Path, *, clock=time.time) -> None:
        self.path = Path(path)
        self.clock = clock

    def _load(self) -> dict[str, object] | None:
        if not self.path.exists() and not self.path.is_symlink():
            return {"version": 1, "events": [], "cooldown_until": 0.0}
        document = _read_private_json(self.path)
        if (
            document is None
            or set(document) != {"version", "events", "cooldown_until"}
            or document.get("version") != 1
            or not isinstance(document.get("events"), list)
            or isinstance(document.get("cooldown_until"), bool)
            or not isinstance(document.get("cooldown_until"), (int, float))
            or not math.isfinite(float(document["cooldown_until"]))
            or float(document["cooldown_until"]) < 0
        ):
            return None
        for event in document["events"]:
            if (
                not isinstance(event, dict)
                or set(event) != {"at", "reason"}
                or isinstance(event["at"], bool)
                or not isinstance(event["at"], (int, float))
                or not math.isfinite(float(event["at"]))
                or float(event["at"]) < 0
                or event["reason"] not in self._REASONS
            ):
                return None
        return document

    @staticmethod
    def _now(value: float) -> float:
        now = float(value)
        if not math.isfinite(now) or now < 0:
            raise ValueError("invalid compliance recovery time")
        return now

    def _recent(
        self, document: dict[str, object], now: float,
    ) -> list[dict[str, object]]:
        return [
            dict(event)
            for event in document["events"]  # type: ignore[union-attr]
            if now - self._WINDOW_SECONDS < float(event["at"]) <= now
        ]

    @staticmethod
    def _has_future_event(document: dict[str, object], now: float) -> bool:
        return any(
            float(event["at"]) > now
            for event in document["events"]  # type: ignore[union-attr]
        )

    def allow(self, now: float) -> bool:
        current = self._now(now)
        document = self._load()
        if document is None:
            return False
        if self._has_future_event(document, current):
            return False
        if current < float(document["cooldown_until"]):
            return False
        return len(self._recent(document, current)) < self._MAX_RECOVERIES

    def record(self, now: float, reason: str) -> None:
        current = self._now(now)
        if reason not in self._REASONS:
            raise ValueError("invalid compliance recovery reason")
        document = self._load()
        if document is None or self._has_future_event(document, current):
            raise RuntimeError("compliance recovery budget is invalid")
        events = self._recent(document, current)
        events.append({"at": current, "reason": reason})
        # A replacement listener needs one segment window to acquire audio
        # and publish fresh progress.  Persist this grace before signalling
        # so a newly spawned watchdog cannot immediately kill its parent for
        # the predecessor's stale timestamp.
        cooldown_until = max(
            float(document["cooldown_until"]),
            current + self._REPLACEMENT_GRACE_SECONDS,
        )
        if len(events) >= self._MAX_RECOVERIES:
            cooldown_until = max(
                cooldown_until, current + self._COOLDOWN_SECONDS
            )
        _write_private_json(self.path, {
            "version": 1,
            "events": events,
            "cooldown_until": cooldown_until,
        })

    def record_healthy(self, now: float) -> None:
        current = self._now(now)
        document = self._load()
        if document is None or self._has_future_event(document, current) or (
            not self.path.exists() and not self.path.is_symlink()
        ):
            return
        recent = self._recent(document, current)
        cooldown_until = (
            0.0
            if current >= float(document["cooldown_until"])
            else float(document["cooldown_until"])
        )
        if (
            recent == document["events"]
            and cooldown_until == float(document["cooldown_until"])
        ):
            return
        _write_private_json(self.path, {
            "version": 1,
            "events": recent,
            "cooldown_until": cooldown_until,
        })


def _wait_for_exit(pid: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while True:
        try:
            process_identity(pid)
        except Exception:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _validated_heartbeat(
    settings: WatchdogSettings, reader,
) -> dict[str, object] | None:
    document = reader(settings.heartbeat_path)
    if (
        not isinstance(document, dict)
        or set(document) != {
            "version", "pid", "token", "process_identity", "updated_at", "state",
        }
        or document.get("version") != 1
        or document.get("pid") != settings.parent_pid
        or not isinstance(document.get("token"), str)
        or not hmac.compare_digest(document["token"], settings.token)
        or not isinstance(document.get("process_identity"), str)
        or not hmac.compare_digest(
            document["process_identity"], settings.process_identity
        )
        or isinstance(document.get("updated_at"), bool)
        or not isinstance(document.get("updated_at"), (int, float))
        or not math.isfinite(float(document["updated_at"]))
        or float(document["updated_at"]) < 0
        or document.get("state") not in {"running", "stopping"}
    ):
        return None
    return document


def _same_listener_process(settings: WatchdogSettings, dependencies) -> bool:
    heartbeat = _validated_heartbeat(settings, dependencies.read_heartbeat)
    if heartbeat is None:
        return False
    try:
        current = dependencies.identity(settings.parent_pid)
    except Exception:
        return False
    return isinstance(current, str) and hmac.compare_digest(
        current, settings.process_identity
    )


def supervise_once(settings: WatchdogSettings, *, dependencies=None) -> str:
    if dependencies is None:
        dependencies = type("WatchdogRuntime", (), {
            "clock": staticmethod(time.time),
            "identity": staticmethod(process_identity),
            "probe": staticmethod(probe_progress),
            "read_heartbeat": staticmethod(_read_private_json),
            "send_signal": staticmethod(os.kill),
            "wait_for_exit": staticmethod(_wait_for_exit),
        })()
    try:
        now = float(dependencies.clock())
        if not math.isfinite(now) or now < 0:
            return "refused"
        heartbeat = _validated_heartbeat(
            settings, dependencies.read_heartbeat
        )
        if heartbeat is None:
            return "refused"
        if heartbeat["state"] == "stopping":
            return "healthy"
        if not _same_listener_process(settings, dependencies):
            return "refused"
        updated_at = float(heartbeat["updated_at"])
        if updated_at > now:
            return "refused"
        reason = ""
        if now - updated_at > settings.heartbeat_stale_seconds:
            reason = "heartbeat_stale"
        else:
            evidence = dependencies.probe(
                settings.db_path, now_ms=int(now * 1000)
            )
            if (
                isinstance(evidence, ProgressEvidence)
                and evidence.compliance_stalled
                and evidence.reason == "compliance_audio_stale"
            ):
                reason = "compliance_audio_stale"
        budget = RecoveryBudget(settings.budget_path)
        if not reason:
            budget.record_healthy(now)
            return "healthy"
        if not budget.allow(now):
            return "cooldown"
        if not _same_listener_process(settings, dependencies):
            return "refused"
        # Persist the recovery budget before the destructive signal.  The
        # listener deliberately terminates this child during shutdown, so an
        # attempted recovery must already be rate-limited if that handoff
        # interrupts the child before the parent fully exits.
        budget.record(now, reason)
        dependencies.send_signal(settings.parent_pid, signal.SIGTERM)
        if dependencies.wait_for_exit(settings.parent_pid, 30.0):
            return "recovered"
        if not _same_listener_process(settings, dependencies):
            return "refused"
        dependencies.send_signal(settings.parent_pid, signal.SIGKILL)
        if not dependencies.wait_for_exit(settings.parent_pid, 5.0):
            return "refused"
        return "recovered"
    except Exception:
        return "refused"


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("invalid progress timestamp")
    return value


def _latest_timeline_growth(session_value: object) -> int:
    if not isinstance(session_value, str) or not session_value:
        raise ValueError("invalid recording session")
    session = Path(session_value)
    if not session.is_absolute() or session.is_symlink() or not session.is_dir():
        raise ValueError("invalid recording session")
    timeline = session / "timeline.json"
    if (
        timeline.is_symlink()
        or not timeline.is_file()
        or timeline.resolve(strict=True).parent != session.resolve(strict=True)
    ):
        raise ValueError("invalid recording timeline")
    document = json.loads(
        timeline.read_text(encoding="utf-8"),
        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
    )
    if (
        not isinstance(document, dict)
        or set(document) != _TIMELINE_ROOT_KEYS
        or document.get("schema_version") != 1
        or not isinstance(document.get("parts"), list)
    ):
        raise ValueError("invalid recording timeline")
    growth: list[int] = []
    for part in document["parts"]:
        if not isinstance(part, dict) or set(part) != _TIMELINE_PART_KEYS:
            raise ValueError("invalid recording timeline")
        relative = part["relative_path"]
        pure = PurePosixPath(relative) if isinstance(relative, str) else None
        if (
            pure is None
            or pure.is_absolute()
            or not pure.parts
            or any(item in {"", ".", ".."} for item in pure.parts)
            or part["state"] not in {"started", "recording", "complete", "failed"}
        ):
            raise ValueError("invalid recording timeline")
        _nonnegative_int(part["started_at_ms"])
        first = part["first_media_at_ms"]
        last = part["last_growth_at_ms"]
        if first is not None:
            _nonnegative_int(first)
        if last is not None:
            growth.append(_nonnegative_int(last))
        _nonnegative_int(part["duration_ms"])
        _nonnegative_int(part["size_bytes"])
        if first is not None and last is not None and last < first:
            raise ValueError("invalid recording timeline")
    if not growth:
        raise ValueError("recording timeline has no growth")
    return max(growth)


def probe_progress(db_path: Path, *, now_ms: int) -> ProgressEvidence:
    try:
        current_ms = _nonnegative_int(now_ms)
        database = Path(db_path)
        if database.is_symlink() or not database.is_file():
            raise ValueError("invalid compliance database")
        resolved = database.resolve(strict=True)
        uri = f"file:{quote(str(resolved), safe='/')}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=1.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            rows = connection.execute(
                "SELECT live_id,session_dir FROM streams "
                "WHERE status='recording' AND live_id<>'' AND session_dir<>''"
            ).fetchall()
            if len(rows) != 1:
                return ProgressEvidence(
                    False,
                    False,
                    "upstream_inactive" if not rows else "upstream_ambiguous",
                )
            live_id = rows[0]["live_id"]
            if not isinstance(live_id, str) or not live_id:
                raise ValueError("invalid recording identity")
            latest_growth = _latest_timeline_growth(rows[0]["session_dir"])
            if latest_growth > current_ms or current_ms - latest_growth > 120_000:
                return ProgressEvidence(False, False, "upstream_stale")
            runtime = connection.execute(
                "SELECT last_valid_audio_ms,last_valid_audio_live_id "
                "FROM compliance_runtime_state WHERE singleton_id=1"
            ).fetchone()
        finally:
            connection.close()
        if runtime is None:
            raise ValueError("missing compliance runtime")
        audio_ms = _nonnegative_int(runtime["last_valid_audio_ms"])
        audio_live_id = runtime["last_valid_audio_live_id"]
        if not isinstance(audio_live_id, str) or audio_live_id != live_id:
            return ProgressEvidence(True, False, "audio_identity_unproven")
        if audio_ms > current_ms:
            return ProgressEvidence(True, False, "audio_time_unproven")
        stalled = current_ms - audio_ms > 300_000
        return ProgressEvidence(
            True,
            stalled,
            "compliance_audio_stale" if stalled else "progress_healthy",
        )
    except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
        return ProgressEvidence(False, False, "probe_unavailable")


class HeartbeatPublisher:
    def __init__(
        self,
        path: Path,
        *,
        pid: int,
        token: str,
        process_identity: str,
        interval_seconds: float = 5.0,
        clock=time.time,
    ) -> None:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ValueError("invalid compliance heartbeat pid")
        if not isinstance(token, str) or not token or len(token) > 128:
            raise ValueError("invalid compliance heartbeat token")
        if (
            not isinstance(process_identity, str)
            or len(process_identity) != _SHA256_LENGTH
            or any(character not in "0123456789abcdef" for character in process_identity)
        ):
            raise ValueError("invalid compliance heartbeat identity")
        interval = float(interval_seconds)
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("invalid compliance heartbeat interval")
        self.path = Path(path)
        self.pid = pid
        self.token = token
        self.process_identity = process_identity
        self.interval_seconds = interval
        self.clock = clock
        self._last_beat = float("-inf")

    def _publish(self, state: str, now: float) -> None:
        _write_private_json(self.path, {
            "version": 1,
            "pid": self.pid,
            "token": self.token,
            "process_identity": self.process_identity,
            "updated_at": now,
            "state": state,
        })

    def beat(self, *, force: bool = False) -> bool:
        now = float(self.clock())
        if not math.isfinite(now) or now < 0:
            raise ValueError("invalid compliance heartbeat time")
        if not force and now - self._last_beat < self.interval_seconds:
            return False
        self._publish("running", now)
        self._last_beat = now
        return True

    def mark_stopping(self) -> None:
        now = float(self.clock())
        if not math.isfinite(now) or now < 0:
            raise ValueError("invalid compliance heartbeat time")
        self._publish("stopping", now)
        self._last_beat = now


def build_watchdog_command(
    settings: WatchdogSettings,
    *,
    python_executable: str = sys.executable,
) -> tuple[str, ...]:
    paths = (
        settings.heartbeat_path, settings.budget_path, settings.db_path,
    )
    if (
        not isinstance(python_executable, str)
        or not python_executable
        or any(not path.is_absolute() for path in paths)
    ):
        raise ValueError("invalid compliance watchdog command")
    return (
        python_executable,
        "-m",
        "app.compliance.watchdog",
        "--heartbeat", str(settings.heartbeat_path),
        "--budget", str(settings.budget_path),
        "--db", str(settings.db_path),
        "--parent-pid", str(settings.parent_pid),
        "--token", settings.token,
        "--process-identity", settings.process_identity,
    )


def run_watchdog(
    settings: WatchdogSettings,
    *,
    poll_seconds: float = 5.0,
    sleep=time.sleep,
) -> int:
    if float(poll_seconds) != 5.0:
        raise ValueError("invalid compliance watchdog poll interval")
    while True:
        outcome = supervise_once(settings)
        heartbeat = _validated_heartbeat(settings, _read_private_json)
        if outcome == "recovered":
            return 0
        if outcome == "refused" or heartbeat is None:
            return 2
        if heartbeat["state"] == "stopping":
            return 0
        sleep(5.0)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--heartbeat", type=Path, required=True)
    parser.add_argument("--budget", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--process-identity", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = WatchdogSettings(
        heartbeat_path=args.heartbeat,
        budget_path=args.budget,
        db_path=args.db,
        parent_pid=args.parent_pid,
        token=args.token,
        process_identity=args.process_identity,
    )
    if any(
        not path.is_absolute()
        for path in (
            settings.heartbeat_path, settings.budget_path, settings.db_path,
        )
    ):
        return 2
    return run_watchdog(settings)


__all__ = [
    "HeartbeatPublisher",
    "ProgressEvidence",
    "RecoveryBudget",
    "WatchdogSettings",
    "build_watchdog_command",
    "probe_progress",
    "process_identity",
    "run_watchdog",
    "supervise_once",
]


if __name__ == "__main__":
    raise SystemExit(main())
