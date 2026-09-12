#!/usr/bin/env python3
"""Preflight, snapshot, restart, verify, and record a local production release."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, cast

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config  # noqa: E402
from app.db import Store  # noqa: E402
from app.runtime.invariants import inspect_business_sessions  # noqa: E402


Run = Callable[[list[str]], subprocess.CompletedProcess[str]]
LAUNCHD_LABEL = "com.torras.live-inspection"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
COMPLIANCE_LABEL = "com.torras.live-compliance"
COMPLIANCE_PLIST_PATH = (
    Path.home() / "Library" / "LaunchAgents" / f"{COMPLIANCE_LABEL}.plist"
)
STATE_SCHEMA_VERSION = 1
PASSED_RE = re.compile(r"(?m)\b(\d+) passed\b")
RELEASE_SNAPSHOT_DIRECTORY_RE = re.compile(
    r"\d{8}T\d{6}\.\d{6}_[0-9a-f]{12}\Z")
STOP_CONFIRMATION_PROBES = 30
STOP_CONFIRMATION_INTERVAL_SECONDS = 0.5


class ReleaseRefused(RuntimeError):
    """A precondition failed before production was changed."""


class ReleaseFailed(RuntimeError):
    """The candidate could not pass post-start verification."""


class _RecordingGenerationChanged(ReleaseFailed):
    """A legitimate stream boundary requires restarting the final gate."""


_TRANSIENT_MEDIA_OWNER_ERRORS = frozenset({
    "recording owner is not a live watcher child",
    "compliance audio did not start",
    "compliance audio owner is not live",
    "compliance audio live identity did not converge",
})


def _is_transient_media_owner_error(exc: BaseException) -> bool:
    """Recognize only self-healing owner handoff windows, not bad state."""
    return (
        type(exc) is ReleaseFailed
        and str(exc) in _TRANSIENT_MEDIA_OWNER_ERRORS
    )


@dataclass(frozen=True)
class ReleasePreflightResult:
    commit: str
    test_count: int
    db_snapshot: Path
    recording_was_active: bool = False
    main_config_hash: str = ""
    compliance_config_hash: str = ""


def _config_fingerprints(cfg: dict) -> tuple[str, str]:
    """Return irreversible hashes for restart-relevant and sidecar config.

    The watcher hot-swaps ``taobao.cookie`` and ``taobao.live_id`` without a
    process restart.  Excluding only those two runtime identities prevents a
    later compliance-mode switch from turning their expected drift into an
    unrelated watcher restart.
    """
    if not isinstance(cfg, dict):
        raise ValueError("production config is not a mapping")

    def digest(value: object) -> str:
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False,
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    main = {key: value for key, value in cfg.items() if key != "compliance"}
    taobao = main.get("taobao")
    if isinstance(taobao, dict):
        stable_taobao = dict(taobao)
        stable_taobao.pop("cookie", None)
        stable_taobao.pop("live_id", None)
        main["taobao"] = stable_taobao
    compliance = cfg.get("compliance") or {}
    return digest(main), digest(compliance)


def _default_runner(root: Path) -> Run:
    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        timeout = 900 if "pytest" in args or "-c" in args else 120
        return subprocess.run(
            args,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    return run


def _command(
    run: Run,
    args: list[str],
    *,
    error_type: type[RuntimeError] = ReleaseRefused,
    description: str,
) -> subprocess.CompletedProcess[str]:
    try:
        result = run(args)
    except Exception as exc:
        raise error_type(f"{description} could not run: {type(exc).__name__}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        suffix = f": {detail[-1][:200]}" if detail else ""
        raise error_type(f"{description} failed{suffix}")
    return result


def _tracked_status(root: Path, run: Run) -> None:
    del root
    result = _command(
        run,
        ["git", "status", "--porcelain", "--untracked-files=no"],
        description="tracked worktree check",
    )
    if result.stdout.strip():
        raise ReleaseRefused("tracked worktree is not clean")


def _head_commit(run: Run) -> str:
    result = _command(
        run, ["git", "rev-parse", "HEAD"], description="HEAD resolution")
    commit = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        raise ReleaseRefused("HEAD did not resolve to one immutable commit")
    return commit.lower()


def _parse_test_count(result: subprocess.CompletedProcess[str]) -> int:
    match = PASSED_RE.search(f"{result.stdout}\n{result.stderr}")
    if result.returncode != 0 or match is None:
        raise ReleaseRefused("full test suite did not pass")
    count = int(match.group(1))
    if count <= 0:
        raise ReleaseRefused("full test suite reported no passing tests")
    return count


def _database_path(root: Path, cfg: dict) -> Path:
    raw = str(cfg.get("paths", {}).get("db", "data/inspection.db") or "")
    if not raw:
        raise ReleaseRefused("production config has no database path")
    path = Path(raw)
    return path if path.is_absolute() else root / path


def _sqlite_integrity(path: Path, *, error_type: type[RuntimeError]) -> None:
    if not path.is_file():
        raise error_type("SQLite database is missing")
    try:
        connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        try:
            quick = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
            foreign = list(connection.execute("PRAGMA foreign_key_check"))
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise error_type(f"SQLite integrity check failed: {type(exc).__name__}") from exc
    if quick != ["ok"]:
        raise error_type("SQLite quick_check did not return ok")
    if foreign:
        raise error_type(f"SQLite foreign_key_check found {len(foreign)} row(s)")


def _has_active_recording(path: Path) -> bool:
    with sqlite3.connect(str(path)) as connection:
        return bool(connection.execute(
            "SELECT 1 FROM streams "
            "WHERE status IN ('recording','recovering') LIMIT 1"
        ).fetchone())


def backup_sqlite(source: Path, destination: Path) -> None:
    """Create one transactionally consistent snapshot, including live WAL data."""
    source = Path(source)
    destination = Path(destination)
    if not source.is_file():
        raise ReleaseRefused("SQLite database is missing")
    if destination.exists():
        raise ReleaseRefused("release snapshot destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.parent.chmod(0o700)
    try:
        source_connection = sqlite3.connect(
            f"file:{source.resolve()}?mode=ro", uri=True, timeout=30)
        destination_connection = sqlite3.connect(str(destination), timeout=30)
        try:
            source_connection.backup(destination_connection)
            destination_connection.commit()
        finally:
            destination_connection.close()
            source_connection.close()
        destination.chmod(0o600)
        _sqlite_integrity(destination, error_type=ReleaseRefused)
    except Exception:
        if destination.exists():
            try:
                destination.unlink()
            except OSError:
                pass
        raise


def _snapshot_path(root: Path, commit: str, *, now: float | None = None) -> Path:
    moment = datetime.fromtimestamp(time.time() if now is None else float(now))
    stamp = moment.strftime("%Y%m%dT%H%M%S.%f")
    return (
        root / "data" / "backups" / "releases"
        / f"{stamp}_{commit[:12]}" / "inspection.db"
    )


def preflight_release(root: Path, *, run: Run) -> ReleasePreflightResult:
    """Prove source, tests, config, and database before touching launchd."""
    root = Path(root).resolve()
    _tracked_status(root, run)
    commit = _head_commit(run)
    test_result = run([
        str(root / ".venv" / "bin" / "python"),
        "-m", "pytest", "tests/", "-q", "-W", "error",
    ])
    test_count = _parse_test_count(test_result)
    try:
        cfg = load_config()
    except Exception as exc:
        raise ReleaseRefused(
            f"production config could not load: {type(exc).__name__}") from exc
    if not isinstance(cfg, dict):
        raise ReleaseRefused("production config is not a mapping")
    try:
        main_config_hash, compliance_config_hash = _config_fingerprints(cfg)
    except (TypeError, ValueError) as exc:
        raise ReleaseRefused(
            "production config could not be fingerprinted") from exc
    database = _database_path(root, cfg)
    _sqlite_integrity(database, error_type=ReleaseRefused)
    snapshot = _snapshot_path(root, commit)
    backup_sqlite(database, snapshot)
    return ReleasePreflightResult(
        commit, test_count, snapshot,
        recording_was_active=_has_active_recording(database),
        main_config_hash=main_config_hash,
        compliance_config_hash=compliance_config_hash,
    )


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(str(path), flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _release_snapshot_directory(
    project_root: Path,
    value: object,
) -> Path | None:
    """Resolve one state-referenced snapshot only when it is a safe release dir."""
    if not isinstance(value, str) or not value:
        return None
    release_root = (
        Path(project_root).resolve() / "data" / "backups" / "releases"
    )
    path = Path(value)
    candidate = path if path.is_absolute() else Path(project_root) / path
    snapshot = candidate.resolve(strict=False)
    try:
        directory = snapshot.parent
        if directory.resolve(strict=False).parent != release_root:
            return None
    except OSError:
        return None
    if (
        not RELEASE_SNAPSHOT_DIRECTORY_RE.fullmatch(directory.name)
        or directory.is_symlink()
        or not directory.is_dir()
        or snapshot.name != "inspection.db"
    ):
        return None
    try:
        children = tuple(directory.iterdir())
    except OSError:
        return None
    allowed = {"inspection.db", "inspection.db-wal", "inspection.db-shm"}
    if (
        not children
        or any(child.name not in allowed or child.is_symlink() or not child.is_file()
               for child in children)
        or not snapshot.is_file()
        or snapshot.is_symlink()
    ):
        return None
    return directory


def prune_release_snapshots(
    project_root: Path,
    state: dict[str, object],
    *,
    keep: int = 7,
) -> int:
    """Prune only recognized release snapshots after a verified release.

    The current and previous verified recovery points are retained even if they
    fall outside the newest ``keep`` snapshots. Unknown directories, symlinks,
    and unexpected directory layouts are intentionally left untouched.
    """
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
        raise ValueError("release snapshot keep count must be a positive integer")
    root = Path(project_root).resolve()
    release_root = root / "data" / "backups" / "releases"
    if not release_root.is_dir() or release_root.is_symlink():
        return 0
    protected: set[Path] = set()
    current = _release_snapshot_directory(root, state.get("db_snapshot"))
    if current is not None:
        protected.add(current)
    protected_commits = {
        str(value) for value in (
            state.get("current_verified_commit"),
            state.get("previous_verified_commit"),
        ) if isinstance(value, str) and value
    }
    history = state.get("history")
    if isinstance(history, list):
        for item in history:
            if (
                not isinstance(item, dict)
                or item.get("status") != "verified"
                or str(item.get("candidate_commit") or "")
                not in protected_commits
            ):
                continue
            directory = _release_snapshot_directory(
                root, item.get("db_snapshot"))
            if directory is not None:
                protected.add(directory)
    try:
        directories = tuple(
            directory for directory in release_root.iterdir()
            if _release_snapshot_directory(
                root, str(directory / "inspection.db")) == directory
        )
    except OSError:
        return 0
    retained = set(sorted(
        directories, key=lambda directory: directory.name, reverse=True)[:keep]
    ) | protected
    removed = 0
    for directory in directories:
        if directory in retained:
            continue
        try:
            children = tuple(directory.iterdir())
            if any(child.is_symlink() or not child.is_file() for child in children):
                continue
            for child in children:
                child.unlink()
            directory.rmdir()
        except OSError:
            continue
        removed += 1
    if removed:
        try:
            _fsync_directory(release_root)
        except OSError:
            pass
    return removed


def _write_state(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{encoded}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _read_state(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReleaseRefused(
            f"release state is unreadable: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise ReleaseRefused("release state is not an object")
    return value


def _relative_snapshot(root: Path, snapshot: Path) -> str:
    try:
        return str(snapshot.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(snapshot.resolve())


def _launchd_domain() -> str:
    return f"gui/{os.getuid()}"


def _wait_for_managed_process_exit(
    run: Run,
    process_pids: Callable[[Run], list[int]],
) -> bool:
    """Bounded confirmation after launchd accepts a managed-service stop."""
    for probe in range(STOP_CONFIRMATION_PROBES):
        if not process_pids(run):
            return True
        if probe + 1 < STOP_CONFIRMATION_PROBES:
            time.sleep(STOP_CONFIRMATION_INTERVAL_SECONDS)
    return False


def _stop_agent(
    run: Run,
    *,
    label: str,
    plist_path: Path | None,
    process_pids: Callable[[Run], list[int]],
    required: bool,
    confirm_pid_exit: bool = False,
) -> None:
    target = (
        [_launchd_domain(), str(plist_path)]
        if plist_path is not None
        else [f"{_launchd_domain()}/{label}"]
    )
    for attempt in range(3):
        result = run(["launchctl", "bootout", *target])
        if result.returncode == 0:
            if (
                not confirm_pid_exit
                or _wait_for_managed_process_exit(run, process_pids)
            ):
                return
            if not required:
                return
        else:
            if not required:
                return
            # A failed candidate recovery intentionally leaves production unloaded.
            # That is a safe cold-start state, not a reason to make the next verified
            # release impossible.  Ignore only the provable combination of no launchd
            # service and no standalone watcher; every other bootout error stays fatal.
            service = run([
                "launchctl", "print", f"{_launchd_domain()}/{label}",
            ])
            if service.returncode != 0:
                if _wait_for_managed_process_exit(run, process_pids):
                    return
        if attempt < 2:
            time.sleep(1)
            continue
        if result.returncode == 0:
            raise ReleaseFailed(
                f"launchd bootout left a process running for {label}"
            )
        raise ReleaseFailed(f"launchd bootout failed for {label}")


def _stop_launch_agent(run: Run, *, required: bool) -> None:
    _stop_agent(
        run, label=LAUNCHD_LABEL, plist_path=PLIST_PATH,
        process_pids=_watcher_pids, required=required,
        confirm_pid_exit=True,
    )


def _start_launch_agent(run: Run) -> None:
    _command(
        run,
        ["launchctl", "bootstrap", _launchd_domain(), str(PLIST_PATH)],
        error_type=ReleaseFailed,
        description="launchd bootstrap",
    )


def _compliance_pids(run: Run) -> list[int]:
    result = run(["pgrep", "-f", "app.compliance.listene[r]"])
    if result.returncode not in (0, 1):
        raise ReleaseFailed("compliance process lookup failed")
    values: list[int] = []
    for raw in result.stdout.split():
        if raw.isdigit():
            values.append(int(raw))
    return sorted(set(values))


def _stop_compliance_launch_agent(run: Run, *, required: bool) -> None:
    _stop_agent(
        run, label=COMPLIANCE_LABEL, plist_path=None,
        process_pids=_compliance_pids, required=required,
        confirm_pid_exit=True,
    )


def _start_compliance_launch_agent(run: Run) -> None:
    _command(
        run,
        ["launchctl", "bootstrap", _launchd_domain(), str(COMPLIANCE_PLIST_PATH)],
        error_type=ReleaseFailed,
        description="compliance launchd bootstrap",
    )


def _compliance_plist_is_installed() -> bool:
    return COMPLIANCE_PLIST_PATH.is_file()


def _compliance_service_is_startable(root: Path) -> bool:
    return _compliance_plist_is_installed() and (
        Path(root) / "app" / "compliance" / "listener.py"
    ).is_file()


def _watchdog_service_is_startable(root: Path) -> bool:
    """Only require the child watchdog from code that actually supplies it.

    This keeps an automatic rollback to a previously verified revision safe:
    that revision has no watchdog module and its watcher must not be judged by
    a contract it cannot satisfy.
    """
    return (Path(root) / "app" / "recorder" / "watchdog.py").is_file()


def _stop_launch_agents(root: Path, run: Run, *, required: bool) -> None:
    """Stop the sidecar before watcher; required callers need both confirmed."""
    del root
    errors: list[ReleaseFailed] = []
    try:
        _stop_compliance_launch_agent(run, required=required)
    except ReleaseFailed as exc:
        errors.append(exc)
    try:
        _stop_launch_agent(run, required=required)
    except ReleaseFailed as exc:
        errors.append(exc)
    if errors and required:
        raise errors[0]


def _start_launch_agents(root: Path, run: Run) -> bool:
    """Start watcher first; sidecar starts only from an installed compatible tree."""
    _start_launch_agent(run)
    compliance_started = _compliance_service_is_startable(root)
    if compliance_started:
        _start_compliance_launch_agent(run)
    return compliance_started


def _watcher_pids(run: Run) -> list[int]:
    result = run(["pgrep", "-f", "app.recorder.watche[r]"])
    if result.returncode not in (0, 1):
        raise ReleaseFailed("watcher process lookup failed")
    values: list[int] = []
    for raw in result.stdout.split():
        if raw.isdigit():
            values.append(int(raw))
    return sorted(set(values))


def _watchdog_pids(run: Run) -> list[int]:
    result = run(["pgrep", "-f", "app.recorder.watchdo[g]"])
    if result.returncode not in (0, 1):
        raise ReleaseFailed("watchdog process lookup failed")
    values: list[int] = []
    for raw in result.stdout.split():
        if raw.isdigit():
            values.append(int(raw))
    return sorted(set(values))


def _lock_is_held(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        import fcntl
    except ImportError:
        return True
    try:
        with path.open("r+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return False
    except OSError:
        return False


def _wait_for_single_watcher(
    root: Path,
    run: Run,
    *,
    clock: Callable[[], float],
    timeout_seconds: float = 45.0,
) -> int:
    deadline = float(clock()) + float(timeout_seconds)
    lock_path = root / "data" / "watcher.lock"
    last: list[int] = []
    while float(clock()) <= deadline:
        last = _watcher_pids(run)
        if len(last) > 1:
            raise ReleaseFailed(f"expected one watcher, found {len(last)}")
        if len(last) == 1 and lock_path.is_file() and _lock_is_held(lock_path):
            try:
                owner = int(lock_path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                owner = 0
            if owner == last[0]:
                return owner
        time.sleep(1)
    raise ReleaseFailed(f"watcher singleton was not ready (found {len(last)})")


def _wait_for_single_compliance(
    root: Path,
    run: Run,
    *,
    clock: Callable[[], float],
    timeout_seconds: float = 45.0,
) -> int:
    deadline = float(clock()) + float(timeout_seconds)
    lock_path = Path(root) / "data" / "compliance-listener.lock"
    last: list[int] = []
    while float(clock()) <= deadline:
        last = _compliance_pids(run)
        if len(last) > 1:
            raise ReleaseFailed(f"expected one compliance listener, found {len(last)}")
        if len(last) == 1 and lock_path.is_file() and _lock_is_held(lock_path):
            try:
                owner = int(lock_path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                owner = 0
            if owner == last[0]:
                return owner
        time.sleep(1)
    raise ReleaseFailed(
        f"compliance singleton was not ready (found {len(last)})")


def _active_recording_parts(database: Path) -> list[Path]:
    with sqlite3.connect(str(database)) as connection:
        rows = connection.execute(
            "SELECT session_dir FROM streams WHERE status='recording' ORDER BY id"
        ).fetchall()
    result: list[Path] = []
    for row in rows:
        session_dir = Path(str(row[0] or ""))
        if not session_dir.is_dir():
            continue
        result.extend(sorted(session_dir.glob("part_*.ts")))
    return result


def _process_parent_pid(run: Run, process_pid: int) -> int:
    result = run(["ps", "-o", "ppid=", "-p", str(int(process_pid))])
    if result.returncode != 0:
        return 0
    value = result.stdout.strip()
    return int(value) if value.isdigit() else 0


def _process_identity_hash(run: Run, process_pid: int) -> str:
    """Recompute the watchdog's non-reversible process identity."""
    result = run([
        "ps", "-p", str(int(process_pid)), "-o", "lstart=", "-o", "command=",
    ])
    raw = result.stdout.strip()
    if result.returncode != 0 or not raw:
        return ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _watchdog_heartbeat_is_fresh(
    path: Path,
    *,
    watcher_pid: int,
    watcher_identity: str,
    now: float,
    freshness_seconds: float = 30.0,
) -> bool:
    try:
        if path.is_symlink() or not path.is_file():
            return False
        if path.stat().st_mode & 0o077:
            return False
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(value, dict):
        return False
    updated_at = value.get("updated_at")
    if (
        value.get("version") != 1
        or value.get("pid") != int(watcher_pid)
        or value.get("state") != "running"
        or not isinstance(value.get("token"), str)
        or not value.get("token")
        or not isinstance(value.get("process_identity"), str)
        or not value.get("process_identity")
        or value.get("process_identity") != str(watcher_identity)
        or not isinstance(updated_at, (int, float))
        or not math.isfinite(float(updated_at))
    ):
        return False
    age = float(now) - float(updated_at)
    return -float(freshness_seconds) <= age <= float(freshness_seconds)


def _wait_for_watcher_watchdog(
    root: Path,
    run: Run,
    *,
    watcher_pid: int,
    clock: Callable[[], float],
    heartbeat_path: Path | None = None,
    timeout_seconds: float = 45.0,
) -> int:
    heartbeat_path = Path(
        heartbeat_path or (Path(root) / "data" / "watcher-heartbeat.json")
    )
    deadline = float(clock()) + float(timeout_seconds)
    last: list[int] = []
    while float(clock()) <= deadline:
        watchdog_pids = _watchdog_pids(run)
        last = watchdog_pids
        watcher_identity = _process_identity_hash(run, watcher_pid)
        if (
            len(watchdog_pids) == 1
            and _process_parent_pid(run, watchdog_pids[0]) == int(watcher_pid)
            and _watchdog_heartbeat_is_fresh(
                heartbeat_path,
                watcher_pid=watcher_pid,
                watcher_identity=watcher_identity,
                now=float(clock()),
            )
        ):
            return watchdog_pids[0]
        time.sleep(1)
    if len(last) > 1:
        raise ReleaseFailed(f"expected one watcher watchdog, found {len(last)}")
    raise ReleaseFailed("watchdog was not ready for current watcher")


def _wait_for_recording_owner(
    database: Path,
    *,
    watcher_pid: int,
    run: Run,
    clock: Callable[[], float],
    expect_recording: bool,
    timeout_seconds: float = 60.0,
) -> str:
    """Wait until no stale pre-release recording row owns production media."""
    deadline = float(clock()) + float(timeout_seconds)
    last_count = 0
    while float(clock()) <= deadline:
        with sqlite3.connect(str(database)) as connection:
            rows = connection.execute(
                "SELECT id,recorder_pid FROM streams "
                "WHERE status='recording' ORDER BY id"
            ).fetchall()
        last_count = len(rows)
        if not rows and not expect_recording:
            return "not_recording"
        if len(rows) == 1:
            recorder_pid = int(rows[0][1] or 0)
            if (recorder_pid > 0
                    and _process_parent_pid(run, recorder_pid) == int(watcher_pid)):
                return f"watcher:{recorder_pid}"
        time.sleep(1)
    raise ReleaseFailed(
        "recording ownership did not converge to current watcher "
        f"(active_rows={last_count}, expected_recording={expect_recording})")


def _verify_recording_owner_now(
    database: Path,
    *,
    watcher_pid: int,
    run: Run,
    expected_generation: tuple[int, str] | None,
) -> str:
    """Instantly bind the final stream row to a live watcher child process."""
    signature = _recording_owner_signature_now(
        database,
        watcher_pid=watcher_pid,
        run=run,
        expected_generation=expected_generation,
    )
    return (
        "not_recording" if signature is None else f"watcher:{signature[0]}"
    )


def _recording_owner_signature_now(
    database: Path,
    *,
    watcher_pid: int,
    run: Run,
    expected_generation: tuple[int, str] | None,
) -> tuple[int, str] | None:
    """Return the exact live recorder generation after proving its parent."""
    if _active_recording_generation(database) != expected_generation:
        raise _RecordingGenerationChanged("active recording generation changed")
    try:
        with sqlite3.connect(
            f"file:{Path(database).resolve()}?mode=ro", uri=True,
        ) as connection:
            rows = connection.execute(
                "SELECT id,recorder_pid,session_dir FROM streams "
                "WHERE status='recording' ORDER BY id"
            ).fetchall()
    except sqlite3.Error as exc:
        raise ReleaseFailed("recording ownership final check failed") from exc
    if expected_generation is None:
        if rows:
            raise _RecordingGenerationChanged(
                "active recording generation changed"
            )
        return None
    if len(rows) != 1 or int(rows[0][0]) != int(expected_generation[0]):
        raise _RecordingGenerationChanged("active recording generation changed")
    recorder_pid = int(rows[0][1] or 0)
    if (
        recorder_pid <= 0
        or _process_parent_pid(run, recorder_pid) != int(watcher_pid)
    ):
        raise ReleaseFailed("recording owner is not a live watcher child")
    session_dir = str(rows[0][2] or "")
    if not session_dir:
        raise ReleaseFailed("recording owner session is missing")
    if _active_recording_generation(database) != expected_generation:
        raise _RecordingGenerationChanged("active recording generation changed")
    return recorder_pid, session_dir


def _verify_recording_growth(
    database: Path,
    *,
    clock: Callable[[], float],
    timeout_seconds: float = 120.0,
) -> str:
    with sqlite3.connect(str(database)) as connection:
        active_count = int(connection.execute(
            "SELECT COUNT(*) FROM streams WHERE status='recording'"
        ).fetchone()[0])
    if active_count == 0:
        return "not_recording"
    deadline = float(clock()) + float(timeout_seconds)
    observed: dict[Path, int] = {}
    while float(clock()) <= deadline:
        for path in _active_recording_parts(database):
            try:
                size = path.stat().st_size
            except OSError:
                continue
            previous = observed.setdefault(path, size)
            if size > previous:
                return f"grew:{path.name}"
        time.sleep(2)
    raise ReleaseFailed("active recording bytes did not grow within 120 seconds")


_COMPLIANCE_HEALTH_CODES = frozenset({
    "COMPLIANCE_AUDIO_BLIND",
    "COMPLIANCE_BACKLOG",
    "COMPLIANCE_WORDLIST",
    "COMPLIANCE_MODEL",
    "COMPLIANCE_DELIVERY_UNKNOWN",
})


def _decode_compliance_health(encoded: object) -> dict[str, dict[str, object]]:
    if not isinstance(encoded, str):
        raise ReleaseFailed("compliance health state is malformed")
    try:
        value = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ReleaseFailed("compliance health state is malformed") from exc
    if not isinstance(value, dict) or any(
        code not in _COMPLIANCE_HEALTH_CODES for code in value
    ):
        raise ReleaseFailed("compliance health state is malformed")
    result: dict[str, dict[str, object]] = {}
    for code, raw in value.items():
        if (
            not isinstance(raw, dict)
            or set(raw) != {"active", "last_alert_at", "observed_since"}
            or type(raw["active"]) is not bool
            or isinstance(raw["last_alert_at"], bool)
            or not isinstance(raw["last_alert_at"], (int, float))
            or isinstance(raw["observed_since"], bool)
            or not isinstance(raw["observed_since"], (int, float))
            or not math.isfinite(float(raw["last_alert_at"]))
            or not math.isfinite(float(raw["observed_since"]))
            or float(raw["last_alert_at"]) < 0
            or float(raw["observed_since"]) < 0
        ):
            raise ReleaseFailed("compliance health state is malformed")
        result[code] = {
            "active": raw["active"],
            "last_alert_at": float(raw["last_alert_at"]),
            "observed_since": float(raw["observed_since"]),
        }
    canonical = json.dumps(
        result, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )
    if canonical != encoded:
        raise ReleaseFailed("compliance health state is malformed")
    return result


def _normalize_compliance_term(raw: str) -> str:
    value = unicodedata.normalize("NFKC", raw).casefold()
    return "".join(
        char for char in value
        if char == "%" or (
            not char.isspace()
            and not unicodedata.category(char).startswith(("P", "Z"))
        )
    )


def _validate_compliance_wordlist(
    source_hash: object, entries_json: object, entry_count: object,
) -> None:
    if (
        not isinstance(source_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", source_hash) is None
        or type(entry_count) is not int
        or int(entry_count) <= 0
        or not isinstance(entries_json, str)
    ):
        raise ReleaseFailed("compliance active wordlist is malformed")
    try:
        entries = json.loads(entries_json)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ReleaseFailed("compliance active wordlist is malformed") from exc
    if not isinstance(entries, list) or len(entries) != entry_count:
        raise ReleaseFailed("compliance active wordlist is malformed")
    normalized_seen: set[str] = set()
    canonical_entries: list[dict[str, str]] = []
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"raw", "normalized", "replacement", "note"}
            or any(not isinstance(value, str) for value in entry.values())
            or not 1 <= len(entry["raw"].strip()) <= 50
            or entry["raw"] != entry["raw"].strip()
            or entry["normalized"] != _normalize_compliance_term(entry["raw"])
            or not entry["normalized"]
            or entry["normalized"] in normalized_seen
        ):
            raise ReleaseFailed("compliance active wordlist is malformed")
        normalized_seen.add(entry["normalized"])
        canonical_entries.append({
            "note": entry["note"],
            "normalized": entry["normalized"],
            "raw": entry["raw"],
            "replacement": entry["replacement"],
        })
    if canonical_entries != sorted(
        canonical_entries,
        key=lambda item: (
            item["normalized"], item["raw"], item["replacement"], item["note"],
        ),
    ):
        raise ReleaseFailed("compliance active wordlist is malformed")
    canonical = json.dumps(
        canonical_entries, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )
    import hashlib

    if (
        entries_json != canonical
        or hashlib.sha256(canonical.encode("utf-8")).hexdigest() != source_hash
    ):
        raise ReleaseFailed("compliance active wordlist is malformed")


def _verify_compliance_release_state(
    database: Path,
    *,
    mode: str,
    require_audio: bool = False,
    forbid_audio: bool = False,
    audio_owner_probe: Callable[[int, Path, str], bool] | None = None,
) -> dict[str, object]:
    """Read-only release gate for the separate listener's durable state."""
    if mode not in {"disabled", "shadow", "live"}:
        raise ReleaseFailed("compliance mode is invalid")
    if require_audio and forbid_audio:
        raise ReleaseFailed("compliance audio expectation is contradictory")
    try:
        connection = sqlite3.connect(
            f"file:{Path(database).resolve()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM compliance_runtime_state"
            ).fetchone()
            if count is None or int(count[0]) != 1:
                raise ReleaseFailed("compliance runtime state is ambiguous")
            state = connection.execute(
                "SELECT active_wordlist_version,audio_ffmpeg_pid,audio_marker,"
                "audio_process_token,model_may_be_loaded,health_json,listener_mode "
                "FROM compliance_runtime_state WHERE singleton_id=1"
            ).fetchone()
            if state is None:
                raise ReleaseFailed("compliance runtime state is missing")
            if state["listener_mode"] != mode:
                raise ReleaseFailed("compliance listener mode did not converge")
            health = _decode_compliance_health(state["health_json"])
            pid = state["audio_ffmpeg_pid"]
            marker = state["audio_marker"]
            token = state["audio_process_token"]
            if pid is not None and (type(pid) is not int or int(pid) <= 0):
                raise ReleaseFailed("compliance audio ownership is malformed")
            if not isinstance(marker, str) or not isinstance(token, str):
                raise ReleaseFailed("compliance audio ownership is malformed")
            if pid is None and (marker or token):
                raise ReleaseFailed("compliance audio ownership is malformed")
            if pid is not None and (not marker or not token):
                raise ReleaseFailed("compliance audio ownership is malformed")
            model_may_be_loaded = state["model_may_be_loaded"]
            if (
                type(model_may_be_loaded) is not int
                or int(model_may_be_loaded) not in {0, 1}
            ):
                raise ReleaseFailed("compliance model residency is malformed")
            active_version = state["active_wordlist_version"]
            if mode == "disabled":
                if pid is not None:
                    raise ReleaseFailed("disabled compliance retains audio ownership")
                if int(model_may_be_loaded) != 0:
                    raise ReleaseFailed("disabled compliance model may be loaded")
                return {
                    "mode": mode,
                    "audio_ownership": "none",
                    "model_may_be_loaded": False,
                }
            if type(active_version) is not int or int(active_version) <= 0:
                raise ReleaseFailed("compliance active wordlist is missing")
            if require_audio and pid is None:
                raise ReleaseFailed("compliance audio did not start")
            if forbid_audio and pid is not None:
                raise ReleaseFailed("compliance audio did not stop")
            if pid is not None:
                if audio_owner_probe is None:
                    from app.compliance.audio import LiveAudioSource

                    audio_owner_probe = (
                        lambda process_id, capture, process_token:
                        LiveAudioSource.guard_owner_is_live(
                            process_id, capture, process_token,
                        )
                    )
                if not audio_owner_probe(int(pid), Path(marker), str(token)):
                    raise ReleaseFailed("compliance audio owner is not live")
            if require_audio:
                active_rows = connection.execute(
                    "SELECT live_id FROM streams WHERE status='recording' "
                    "ORDER BY id"
                ).fetchall()
                active_live_ids = {
                    str(row[0]) for row in active_rows if str(row[0] or "")
                }
                if len(active_rows) != 1 or len(active_live_ids) != 1:
                    raise ReleaseFailed(
                        "compliance active recording identity is ambiguous"
                    )
                current = connection.execute(
                    "SELECT current_live_id FROM compliance_runtime_state "
                    "WHERE singleton_id=1"
                ).fetchone()
                active_live_id = next(iter(active_live_ids))
                if current is None or str(current[0] or "") != active_live_id:
                    raise ReleaseFailed(
                        "compliance audio live identity did not converge"
                    )
            wordlist = connection.execute(
                "SELECT source_hash,entries_json,entry_count "
                "FROM compliance_wordlist_versions WHERE id=?",
                (int(active_version),),
            ).fetchone()
            if wordlist is None:
                raise ReleaseFailed("compliance active wordlist is missing")
            _validate_compliance_wordlist(
                wordlist["source_hash"], wordlist["entries_json"],
                wordlist["entry_count"],
            )
            return {
                "mode": mode,
                "active_wordlist_version": int(active_version),
                "audio_ownership": "active" if pid is not None else "none",
            }
        finally:
            connection.close()
    except ReleaseFailed:
        raise
    except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ReleaseFailed(
            f"compliance release state check failed: {type(exc).__name__}") from exc


def _compliance_audio_owner_signature(
    database: Path,
) -> tuple[int, str, str, str]:
    """Return the exact durable guard generation used by release evidence."""
    try:
        with sqlite3.connect(
            f"file:{Path(database).resolve()}?mode=ro", uri=True,
        ) as connection:
            row = connection.execute(
                "SELECT audio_ffmpeg_pid,audio_marker,audio_process_token,"
                "current_live_id FROM compliance_runtime_state "
                "WHERE singleton_id=1"
            ).fetchone()
    except sqlite3.Error as exc:
        raise ReleaseFailed("compliance audio ownership state failed") from exc
    if (
        row is None
        or type(row[0]) is not int
        or int(row[0]) <= 0
        or not str(row[1] or "")
        or not str(row[2] or "")
        or not str(row[3] or "")
    ):
        raise ReleaseFailed("compliance audio ownership is malformed")
    return int(row[0]), str(row[1]), str(row[2]), str(row[3])


def _wait_for_compliance_release_state(
    database: Path,
    *,
    mode: str,
    clock: Callable[[], float],
    timeout_seconds: float = 60.0,
    require_audio: bool = False,
    forbid_audio: bool = False,
    audio_owner_probe: Callable[[int, Path, str], bool] | None = None,
) -> dict[str, object]:
    """Keep the strict shadow/live gate, allowing its first durable sync to land."""
    if mode == "disabled":
        return _verify_compliance_release_state(
            database, mode=mode, require_audio=False,
        )
    deadline = float(clock()) + float(timeout_seconds)
    last_error: ReleaseFailed | None = None
    while float(clock()) <= deadline:
        try:
            kwargs: dict[str, object] = {
                "mode": mode,
                "require_audio": require_audio,
            }
            if forbid_audio:
                kwargs["forbid_audio"] = True
            if audio_owner_probe is not None:
                kwargs["audio_owner_probe"] = audio_owner_probe
            return _verify_compliance_release_state(database, **kwargs)
        except ReleaseFailed as exc:
            last_error = exc
        time.sleep(1)
    if last_error is not None:
        raise last_error
    raise ReleaseFailed("compliance release state did not converge")


def _active_recording_generation(
    database: Path,
) -> tuple[int, str] | None:
    """Return ``(stream_id, live_id)`` so same-live rotations remain distinct."""
    try:
        with sqlite3.connect(
            f"file:{Path(database).resolve()}?mode=ro", uri=True,
        ) as connection:
            rows = connection.execute(
                "SELECT id,live_id FROM streams WHERE status='recording' "
                "ORDER BY id"
            ).fetchall()
    except sqlite3.Error as exc:
        raise ReleaseFailed("active recording identity check failed") from exc
    if not rows:
        return None
    if len(rows) != 1:
        raise ReleaseFailed("active recording identity is ambiguous")
    stream_id = rows[0][0]
    live_id = rows[0][1]
    if type(stream_id) is not int or int(stream_id) <= 0 or not str(live_id or ""):
        raise ReleaseFailed("active recording identity is malformed")
    return int(stream_id), str(live_id)


def _wait_for_compliance_audio_growth(
    database: Path,
    *,
    mode: str,
    clock: Callable[[], float],
    timeout_seconds: float = 60.0,
    expected_generation: tuple[int, str],
    audio_owner_probe: Callable[[int, Path, str], bool] | None = None,
) -> str:
    """Require bytes from the exact owned capture, not only a persisted PID."""
    deadline = float(clock()) + float(timeout_seconds)
    marker = Path()
    observed: dict[str, int] = {}
    last_error: ReleaseFailed | None = None
    while float(clock()) <= deadline:
        generation = _active_recording_generation(database)
        if generation != expected_generation:
            raise _RecordingGenerationChanged(
                "active recording generation changed"
            )
        if generation is None:
            raise _RecordingGenerationChanged(
                "active recording generation changed"
            )
        try:
            _verify_compliance_release_state(
                database,
                mode=mode,
                require_audio=True,
                audio_owner_probe=audio_owner_probe,
            )
            with sqlite3.connect(
                f"file:{Path(database).resolve()}?mode=ro", uri=True,
            ) as connection:
                row = connection.execute(
                    "SELECT audio_marker FROM compliance_runtime_state "
                    "WHERE singleton_id=1"
                ).fetchone()
            current_marker = Path(str(row[0] if row is not None else ""))
            if current_marker != marker:
                marker = current_marker
                observed = {}
            for path in sorted(marker.glob("segment_*.wav")):
                if (
                    path.parent != marker
                    or path.is_symlink()
                    or not path.is_file()
                    or re.fullmatch(r"segment_\d{6,}\.wav", path.name) is None
                ):
                    continue
                size = int(path.stat().st_size)
                previous = observed.get(path.name)
                if previous is not None and size > previous:
                    if _active_recording_generation(database) != generation:
                        raise _RecordingGenerationChanged(
                            "active recording generation changed"
                        )
                    return f"grew:{path.name}"
                observed[path.name] = size
        except (OSError, ReleaseFailed) as exc:
            last_error = (
                exc if isinstance(exc, ReleaseFailed)
                else ReleaseFailed("compliance audio growth check failed")
            )
        time.sleep(1)
    if last_error is not None:
        raise last_error
    raise ReleaseFailed("compliance audio bytes did not grow")


def _wait_for_release_media_growth(
    database: Path,
    *,
    watcher_pid: int,
    run: Run,
    mode: str,
    expected_generation: tuple[int, str],
    clock: Callable[[], float],
    timeout_seconds: float = 60.0,
    audio_owner_probe: Callable[[int, Path, str], bool] | None = None,
) -> dict[str, object]:
    """Observe main and sidecar bytes in one final, identity-bound window."""
    deadline = float(clock()) + float(timeout_seconds)
    main_observed: dict[Path, int] = {}
    audio_observed: dict[Path, int] = {}
    main_growth = ""
    audio_growth = "not_required" if mode == "disabled" else ""
    observed_owner: tuple[
        tuple[int, str], tuple[int, str, str, str] | None,
    ] | None = None
    while float(clock()) <= deadline:
        if _active_recording_generation(database) != expected_generation:
            raise _RecordingGenerationChanged(
                "active recording generation changed"
            )
        try:
            main_owner = _recording_owner_signature_now(
                database,
                watcher_pid=watcher_pid,
                run=run,
                expected_generation=expected_generation,
            )
        except ReleaseFailed as exc:
            if not _is_transient_media_owner_error(exc):
                raise
            observed_owner = None
            time.sleep(1)
            continue
        if main_owner is None:
            raise _RecordingGenerationChanged(
                "active recording generation changed"
            )
        marker: Path | None = None
        compliance_owner: tuple[int, str, str, str] | None = None
        if mode != "disabled":
            try:
                _verify_compliance_release_state(
                    database,
                    mode=mode,
                    require_audio=True,
                    audio_owner_probe=audio_owner_probe,
                )
            except ReleaseFailed as exc:
                if not _is_transient_media_owner_error(exc):
                    raise
                observed_owner = None
                time.sleep(1)
                continue
            try:
                compliance_owner = _compliance_audio_owner_signature(database)
            except ReleaseFailed as exc:
                if str(exc) != "compliance audio ownership is malformed":
                    raise
                observed_owner = None
                time.sleep(1)
                continue
            marker = Path(compliance_owner[1])
        current_owner = (main_owner, compliance_owner)
        if current_owner != observed_owner:
            observed_owner = current_owner
            main_observed = {}
            audio_observed = {}
            main_growth = ""
            audio_growth = "not_required" if mode == "disabled" else ""
        for path in _active_recording_parts(database):
            try:
                size = int(path.stat().st_size)
            except OSError:
                continue
            previous = main_observed.get(path)
            if previous is not None and size > previous:
                main_growth = f"grew:{path.name}"
            main_observed[path] = size
        if marker is not None:
            for path in sorted(marker.glob("segment_*.wav")):
                if (
                    path.parent != marker
                    or path.is_symlink()
                    or not path.is_file()
                    or re.fullmatch(r"segment_\d{6,}\.wav", path.name) is None
                ):
                    continue
                try:
                    size = int(path.stat().st_size)
                except OSError:
                    continue
                previous = audio_observed.get(path)
                if previous is not None and size > previous:
                    audio_growth = f"grew:{path.name}"
                audio_observed[path] = size
        if main_growth and audio_growth:
            if _active_recording_generation(database) != expected_generation:
                raise _RecordingGenerationChanged(
                    "active recording generation changed"
                )
            try:
                final_main_owner = _recording_owner_signature_now(
                    database,
                    watcher_pid=watcher_pid,
                    run=run,
                    expected_generation=expected_generation,
                )
            except ReleaseFailed as exc:
                if not _is_transient_media_owner_error(exc):
                    raise
                observed_owner = None
                continue
            final_compliance_owner = None
            if mode != "disabled":
                try:
                    _verify_compliance_release_state(
                        database,
                        mode=mode,
                        require_audio=True,
                        audio_owner_probe=audio_owner_probe,
                    )
                except ReleaseFailed as exc:
                    if not _is_transient_media_owner_error(exc):
                        raise
                    observed_owner = None
                    continue
                try:
                    final_compliance_owner = (
                        _compliance_audio_owner_signature(database)
                    )
                except ReleaseFailed as exc:
                    if str(exc) != "compliance audio ownership is malformed":
                        raise
                    observed_owner = None
                    continue
            if (
                final_main_owner,
                final_compliance_owner,
            ) != observed_owner:
                observed_owner = None
                continue
            return {
                "recording": main_growth,
                "compliance": audio_growth,
                "recording_owner": final_main_owner,
                "compliance_owner": final_compliance_owner,
            }
        time.sleep(1)
    raise ReleaseFailed("release media bytes did not grow together")


def _wait_for_business_invariants(
    database: Path,
    *,
    clock: Callable[[], float],
    timeout_seconds: float = 60.0,
) -> None:
    """Require invariant convergence after the newly started watcher recovers."""
    deadline = float(clock()) + float(timeout_seconds)
    last_codes = ""
    while float(clock()) <= deadline:
        store = Store(database)
        try:
            issues = inspect_business_sessions(store, now=float(clock()))
        finally:
            store.conn.close()
        if not issues:
            return
        last_codes = ",".join(sorted({issue.code for issue in issues}))
        time.sleep(1)
    raise ReleaseFailed(f"runtime business invariants failed: {last_codes}")


def verify_started_release(
    root: Path,
    *,
    run: Run,
    clock: Callable[[], float],
    expect_recording: bool = False,
    expect_compliance: bool | None = None,
    expected_main_config_hash: str | None = None,
    expected_compliance_config_hash: str | None = None,
) -> dict[str, object]:
    def verify_config_fingerprints() -> None:
        if (
            expected_main_config_hash is None
            and expected_compliance_config_hash is None
        ):
            return
        try:
            candidate_cfg = load_config()
            if not isinstance(candidate_cfg, dict):
                raise TypeError("config is not a mapping")
            candidate_main_hash, candidate_compliance_hash = (
                _config_fingerprints(candidate_cfg)
            )
        except Exception as exc:
            raise ReleaseFailed(
                "production config failed final release check"
            ) from exc
        if (
            expected_main_config_hash is not None
            and candidate_main_hash != expected_main_config_hash
        ):
            raise ReleaseFailed("main config changed during release")
        if (
            expected_compliance_config_hash is not None
            and candidate_compliance_hash != expected_compliance_config_hash
        ):
            raise ReleaseFailed("compliance config changed during release")

    root = Path(root).resolve()
    watcher_pid = _wait_for_single_watcher(root, run, clock=clock)
    try:
        cfg = load_config()
    except Exception as exc:
        raise ReleaseFailed(
            f"production config failed after start: {type(exc).__name__}") from exc
    if not isinstance(cfg, dict):
        raise ReleaseFailed("production config after start is not a mapping")
    try:
        actual_main_hash, actual_compliance_hash = _config_fingerprints(cfg)
    except (TypeError, ValueError) as exc:
        raise ReleaseFailed(
            "production config could not be fingerprinted after start"
        ) from exc
    if (
        expected_main_config_hash is not None
        and actual_main_hash != expected_main_config_hash
    ):
        raise ReleaseFailed("main config changed during release")
    if (
        expected_compliance_config_hash is not None
        and actual_compliance_hash != expected_compliance_config_hash
    ):
        raise ReleaseFailed("compliance config changed during release")
    watchdog_started = False
    if _watchdog_service_is_startable(root):
        try:
            from app.recorder.watchdog import WatchdogSettings

            watchdog_started = WatchdogSettings.from_config(cfg).enabled
        except Exception as exc:
            raise ReleaseFailed(
                f"watchdog config failed after start: {type(exc).__name__}"
            ) from exc
    compliance_started = (
        _compliance_service_is_startable(root)
        if expect_compliance is None else bool(expect_compliance)
    )
    if compliance_started:
        try:
            from app.compliance.config import ComplianceSettings

            compliance_mode = ComplianceSettings.from_config(cfg).mode
        except Exception as exc:
            raise ReleaseFailed(
                f"compliance config failed after start: {type(exc).__name__}") from exc
    database = _database_path(root, cfg)
    _sqlite_integrity(database, error_type=ReleaseFailed)
    watchdog_pid: int | None = None
    heartbeat_path = database.parent / "watcher-heartbeat.json"
    if watchdog_started:
        watchdog_pid = _wait_for_watcher_watchdog(
            root,
            run,
            watcher_pid=watcher_pid,
            clock=clock,
            heartbeat_path=heartbeat_path,
        )
    compliance_evidence: dict[str, object] | None = None
    if compliance_started:
        listener_pid = _wait_for_single_compliance(root, run, clock=clock)
        compliance_evidence = _wait_for_compliance_release_state(
            database, mode=compliance_mode, clock=clock)
        compliance_evidence["listener_pid"] = listener_pid
    recording_owner = _wait_for_recording_owner(
        database,
        watcher_pid=watcher_pid,
        run=run,
        clock=clock,
        expect_recording=bool(expect_recording),
    )
    _wait_for_business_invariants(database, clock=clock)
    growth = _verify_recording_growth(database, clock=clock)
    verify_config_fingerprints()
    # Re-sample recording state at the end instead of reusing the preflight
    # snapshot.  A stream can begin while the slow release gates run; in that
    # case both watcher ownership and sidecar audio must converge before the
    # release is accepted.  Repeat on a boundary transition so one mixed
    # before/after snapshot cannot pass.
    final_listener_pid: int | None = None
    for _attempt in range(3):
        final_watcher_pid = _wait_for_single_watcher(root, run, clock=clock)
        if final_watcher_pid != watcher_pid:
            raise ReleaseFailed("watcher changed during release verification")
        if watchdog_started:
            current_watchdog_pid = _wait_for_watcher_watchdog(
                root,
                run,
                watcher_pid=watcher_pid,
                clock=clock,
                heartbeat_path=heartbeat_path,
            )
            if current_watchdog_pid != watchdog_pid:
                raise ReleaseFailed("watchdog changed during release verification")
        attempt_listener_pid: int | None = None
        before_generation = _active_recording_generation(database)
        final_recording_owner = _wait_for_recording_owner(
            database,
            watcher_pid=watcher_pid,
            run=run,
            clock=clock,
            expect_recording=before_generation is not None,
        )
        if _active_recording_generation(database) != before_generation:
            continue
        if compliance_started:
            # The main recording/business gates can take minutes.  Reacquire
            # the singleton lock, prove the exact guard PID/token/live ID, and
            # then require bytes from that owned capture.
            attempt_listener_pid = _wait_for_single_compliance(
                root, run, clock=clock
            )
            final_listener_pid = attempt_listener_pid
            audio_required = bool(
                before_generation is not None and compliance_mode != "disabled"
            )
            audio_forbidden = bool(
                before_generation is None and compliance_mode != "disabled"
            )
            compliance_evidence = _wait_for_compliance_release_state(
                database,
                mode=compliance_mode,
                clock=clock,
                timeout_seconds=180.0,
                require_audio=audio_required,
                forbid_audio=audio_forbidden,
            )
            compliance_evidence["listener_pid"] = final_listener_pid
        growth_main_owner: tuple[int, str] | None = None
        growth_compliance_owner: tuple[int, str, str, str] | None = None
        compliance_growth = "not_required"
        if before_generation is not None:
            try:
                media_growth = _wait_for_release_media_growth(
                    database,
                    watcher_pid=watcher_pid,
                    run=run,
                    mode=(compliance_mode if compliance_started else "disabled"),
                    expected_generation=before_generation,
                    clock=clock,
                    timeout_seconds=60.0,
                )
            except _RecordingGenerationChanged:
                continue
            final_recording_growth = str(media_growth["recording"])
            compliance_growth = str(media_growth["compliance"])
            growth_main_owner = cast(
                tuple[int, str], media_growth["recording_owner"]
            )
            growth_compliance_owner = cast(
                tuple[int, str, str, str] | None,
                media_growth["compliance_owner"],
            )
            if compliance_evidence is not None:
                compliance_evidence["audio_growth"] = compliance_growth
        else:
            final_recording_growth = "not_recording"
        # Seal the joint byte-growth window with instantaneous ownership,
        # singleton, generation, and configuration proofs.
        if _active_recording_generation(database) != before_generation:
            continue
        try:
            current_main_owner = _recording_owner_signature_now(
                database,
                watcher_pid=watcher_pid,
                run=run,
                expected_generation=before_generation,
            )
        except _RecordingGenerationChanged:
            continue
        except ReleaseFailed as exc:
            if not _is_transient_media_owner_error(exc):
                raise
            continue
        if current_main_owner != growth_main_owner:
            continue
        if compliance_started:
            current_listener_pid = _wait_for_single_compliance(
                root, run, clock=clock
            )
            if current_listener_pid != attempt_listener_pid:
                continue
            final_listener_pid = current_listener_pid
            try:
                compliance_evidence = _verify_compliance_release_state(
                    database,
                    mode=compliance_mode,
                    require_audio=audio_required,
                    forbid_audio=audio_forbidden,
                )
            except ReleaseFailed as exc:
                if not _is_transient_media_owner_error(exc):
                    raise
                continue
            compliance_evidence["listener_pid"] = final_listener_pid
            compliance_evidence["audio_growth"] = compliance_growth
            try:
                current_compliance_owner = (
                    _compliance_audio_owner_signature(database)
                    if audio_required else None
                )
            except ReleaseFailed as exc:
                if str(exc) != "compliance audio ownership is malformed":
                    raise
                continue
            if current_compliance_owner != growth_compliance_owner:
                continue
        verify_config_fingerprints()
        if _active_recording_generation(database) != before_generation:
            continue
        # Nothing after this seal may substitute a newly alive owner for the
        # owner that produced the byte-growth evidence.  If either process
        # rotates, discard both growth observations and retry the joint gate.
        if _wait_for_single_watcher(root, run, clock=clock) != watcher_pid:
            raise ReleaseFailed("watcher changed during release verification")
        if watchdog_started:
            current_watchdog_pid = _wait_for_watcher_watchdog(
                root,
                run,
                watcher_pid=watcher_pid,
                clock=clock,
                heartbeat_path=heartbeat_path,
            )
            if current_watchdog_pid != watchdog_pid:
                raise ReleaseFailed("watchdog changed during release verification")
        if compliance_started:
            final_listener_pid = _wait_for_single_compliance(
                root, run, clock=clock
            )
            if final_listener_pid != attempt_listener_pid:
                continue
        if compliance_started:
            try:
                compliance_evidence = _verify_compliance_release_state(
                    database,
                    mode=compliance_mode,
                    require_audio=audio_required,
                    forbid_audio=audio_forbidden,
                )
            except ReleaseFailed as exc:
                if not _is_transient_media_owner_error(exc):
                    raise
                continue
            compliance_evidence["listener_pid"] = final_listener_pid
            compliance_evidence["audio_growth"] = compliance_growth
        verify_config_fingerprints()
        if _active_recording_generation(database) != before_generation:
            continue
        try:
            final_main_owner = _recording_owner_signature_now(
                database,
                watcher_pid=watcher_pid,
                run=run,
                expected_generation=before_generation,
            )
        except _RecordingGenerationChanged:
            continue
        except ReleaseFailed as exc:
            if not _is_transient_media_owner_error(exc):
                raise
            continue
        try:
            final_compliance_owner = (
                _compliance_audio_owner_signature(database)
                if compliance_started and audio_required else None
            )
        except ReleaseFailed as exc:
            if str(exc) != "compliance audio ownership is malformed":
                raise
            continue
        if (
            final_main_owner != growth_main_owner
            or final_compliance_owner != growth_compliance_owner
        ):
            continue
        recording_owner = (
            "not_recording" if final_main_owner is None
            else f"watcher:{final_main_owner[0]}"
        )
        evidence: dict[str, object] = {
            "watcher_pid": watcher_pid,
            "recording_owner": recording_owner,
            "recording_growth": final_recording_growth,
            "sqlite": "ok",
            "business_invariants": "ok",
        }
        if watchdog_started:
            evidence["watchdog"] = "ok"
        if compliance_evidence is not None:
            evidence["compliance"] = compliance_evidence
        return evidence
    raise ReleaseFailed("recording state changed during release verification")


def _prepared_state(
    old: dict[str, object],
    *,
    candidate: str,
    test_count: int,
    snapshot: Path,
    root: Path,
    now: float,
    operation: str,
) -> dict[str, object]:
    state = dict(old)
    history = list(state.get("history") or [])
    history.append({
        "operation": operation,
        "candidate_commit": candidate,
        "status": "prepared",
        "timestamp": float(now),
        "test_count": int(test_count),
        "db_snapshot": _relative_snapshot(root, snapshot),
    })
    state.update({
        "schema_version": STATE_SCHEMA_VERSION,
        "status": "prepared",
        "operation": operation,
        "candidate_commit": candidate,
        "prepared_at": float(now),
        "test_count": int(test_count),
        "db_snapshot": _relative_snapshot(root, snapshot),
        "history": history[-50:],
    })
    return state


def _safe_failure_stage(exc: BaseException) -> str:
    """Classify post-start failures without recording PIDs, paths, or messages."""
    if not isinstance(exc, ReleaseFailed):
        return "unexpected"
    message = str(exc)
    if "watchdog" in message:
        return "watchdog"
    if "watcher singleton" in message or "expected one watcher" in message:
        return "watcher_singleton"
    if "compliance singleton" in message or "expected one compliance" in message:
        return "compliance_singleton"
    if "launchd bootout" in message:
        return "launchd_stop"
    if "launchd bootstrap" in message:
        return "launchd_start"
    if "recording ownership" in message:
        return "recording_ownership"
    if "active recording bytes" in message:
        return "recording_growth"
    if "runtime business invariants" in message:
        return "business_invariants"
    if "compliance active wordlist" in message:
        return "compliance_wordlist"
    if "compliance listener mode" in message:
        return "compliance_mode"
    if "compliance" in message:
        return "compliance_state"
    return "post_start_other"


def _mark_failed(
    state_path: Path,
    state: dict[str, object],
    *,
    now: float,
    exc: BaseException,
    recovery: dict[str, object] | None = None,
) -> None:
    failed = dict(state)
    failed.update({
        "status": "failed",
        "failed_at": float(now),
        "failure": type(exc).__name__,
        "failure_stage": _safe_failure_stage(exc),
    })
    if recovery is not None:
        failed["recovery"] = recovery
    history = list(failed.get("history") or [])
    if history:
        history[-1] = {**dict(history[-1]), "status": "failed", "failed_at": float(now)}
    failed["history"] = history[-50:]
    _write_state(state_path, failed)


def _restore_verified_commit(
    root: Path,
    commit: str,
    *,
    run: Run,
    clock: Callable[[], float],
    expect_recording: bool,
    expected_main_config_hash: str | None = None,
    expected_compliance_config_hash: str | None = None,
) -> dict[str, object]:
    """Best-effort recovery after a candidate fails post-start gates."""
    if not commit:
        return {"status": "unavailable", "reason": "no_previous_verified_commit"}
    exists = run(["git", "cat-file", "-e", f"{commit}^{{commit}}"])
    if exists.returncode != 0:
        return {"status": "unavailable", "reason": "previous_commit_missing"}
    switched = run(["git", "switch", "--detach", commit])
    if switched.returncode != 0:
        return {"status": "failed", "reason": "code_switch_failed"}
    try:
        compliance_started = _start_launch_agents(root, run)
        evidence = verify_started_release(
            root, run=run, clock=clock,
            expect_recording=expect_recording,
            expect_compliance=compliance_started,
            expected_main_config_hash=expected_main_config_hash,
            expected_compliance_config_hash=expected_compliance_config_hash,
        )
    except Exception as exc:
        try:
            _stop_launch_agents(root, run, required=True)
        except Exception as stop_exc:
            return {
                "status": "failed",
                "reason": "restored_commit_stop_unconfirmed",
                "verification_failure": type(exc).__name__,
                "stop_failure": type(stop_exc).__name__,
            }
        return {"status": "failed", "reason": type(exc).__name__}
    return {"status": "restored", "commit": commit, "verification": evidence}


_COMPLIANCE_ONLY_PREFIXES = ("app/compliance/", "tests/test_compliance")


def _compliance_only_changes(
        root: Path, run: Run, previous_commit: str, commit: str) -> bool:
    """Return whether HEAD only touches the compliance sidecar since last release."""
    del root
    previous_commit = str(previous_commit or "").strip()
    commit = str(commit or "").strip()
    if not previous_commit or previous_commit == commit:
        return False
    exists = run(["git", "cat-file", "-e", f"{previous_commit}^{{commit}}"])
    if exists.returncode != 0:
        return False
    diff = run(["git", "diff", "--name-only", previous_commit, commit])
    if diff.returncode != 0:
        return False
    changed = [line.strip() for line in diff.stdout.splitlines() if line.strip()]
    return bool(changed) and all(
        path.startswith(_COMPLIANCE_ONLY_PREFIXES) for path in changed)


def _release_scope(
    root: Path,
    run: Run,
    previous_state: dict[str, object],
    candidate: ReleasePreflightResult,
) -> str:
    """Choose the smallest safe process scope for a verified release.

    Config fingerprints are deliberately required before a sidecar-only release:
    a legacy state cannot prove that main-system configuration stayed unchanged.
    """
    if not _compliance_service_is_startable(root):
        return "full"
    previous_commit = str(
        previous_state.get("current_verified_commit") or ""
    ).strip()
    previous_main = str(previous_state.get("main_config_hash") or "").strip()
    previous_compliance = str(
        previous_state.get("compliance_config_hash") or ""
    ).strip()
    candidate_main = str(candidate.main_config_hash or "").strip()
    candidate_compliance = str(candidate.compliance_config_hash or "").strip()
    hashes = (
        previous_main,
        previous_compliance,
        candidate_main,
        candidate_compliance,
    )
    if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in hashes):
        return "full"
    if previous_main != candidate_main:
        return "full"
    if previous_commit == candidate.commit:
        return (
            "compliance_only"
            if previous_compliance != candidate_compliance
            else "full"
        )
    if _compliance_only_changes(
        root, run, previous_commit, candidate.commit
    ):
        return "compliance_only"
    return "full"


def _verify_compliance_only_release(
    root: Path,
    *,
    run: Run,
    clock: Callable[[], float],
    expected_watcher_pid: int,
    expect_recording: bool,
    expected_main_config_hash: str,
    expected_compliance_config_hash: str,
) -> dict[str, object]:
    """Run every production gate while proving watcher continuity."""
    evidence = verify_started_release(
        root,
        run=run,
        clock=clock,
        expect_recording=expect_recording,
        expect_compliance=True,
        expected_main_config_hash=expected_main_config_hash,
        expected_compliance_config_hash=expected_compliance_config_hash,
    )
    watcher_pid = evidence.get("watcher_pid")
    if type(watcher_pid) is not int or watcher_pid != int(expected_watcher_pid):
        raise ReleaseFailed("watcher changed during compliance-only release")
    if _watcher_pids(run) != [int(expected_watcher_pid)]:
        raise ReleaseFailed("watcher changed during compliance-only release")
    return evidence


def _recover_compliance_only_release(
    root: Path,
    *,
    run: Run,
    clock: Callable[[], float],
    previous_commit: str,
    candidate: ReleasePreflightResult,
    previous_compliance_hash: str,
    expected_watcher_pid: int,
    expect_recording: bool,
) -> dict[str, object]:
    """Recover sidecar code only; a changed private config is not reversible."""
    try:
        _stop_compliance_launch_agent(run, required=True)
    except Exception as exc:
        return {
            "status": "failed",
            "reason": "candidate_sidecar_stop_unconfirmed",
            "failure": type(exc).__name__,
            "scope": "compliance_only",
        }

    if previous_compliance_hash != candidate.compliance_config_hash:
        return {
            "status": "stopped",
            "reason": "config_not_auto_restored",
            "scope": "compliance_only",
        }
    if not previous_commit:
        return {
            "status": "failed",
            "reason": "no_previous_verified_commit",
            "scope": "compliance_only",
        }
    exists = run(["git", "cat-file", "-e", f"{previous_commit}^{{commit}}"])
    if exists.returncode != 0:
        return {
            "status": "failed",
            "reason": "previous_commit_missing",
            "scope": "compliance_only",
        }
    switched = run(["git", "switch", "--detach", previous_commit])
    if switched.returncode != 0:
        return {
            "status": "failed",
            "reason": "code_switch_failed",
            "scope": "compliance_only",
        }
    try:
        _start_compliance_launch_agent(run)
        evidence = _verify_compliance_only_release(
            root,
            run=run,
            clock=clock,
            expected_watcher_pid=expected_watcher_pid,
            expect_recording=expect_recording,
            expected_main_config_hash=candidate.main_config_hash,
            expected_compliance_config_hash=candidate.compliance_config_hash,
        )
    except Exception as exc:
        try:
            _stop_compliance_launch_agent(run, required=True)
        except Exception as stop_exc:
            return {
                "status": "failed",
                "reason": "restored_sidecar_stop_unconfirmed",
                "failure": type(exc).__name__,
                "stop_failure": type(stop_exc).__name__,
                "scope": "compliance_only",
            }
        return {
            "status": "failed",
            "reason": type(exc).__name__,
            "scope": "compliance_only",
        }
    return {
        "status": "restored",
        "commit": previous_commit,
        "scope": "compliance_only",
        "verification": evidence,
    }


def _apply_release(
    root: Path,
    *,
    run: Run,
    clock: Callable[[], float],
) -> dict[str, object]:
    preflight = preflight_release(root, run=run)
    state_path = root / "data" / "release_state.json"
    old = _read_state(state_path)
    old_current = str(old.get("current_verified_commit") or "")
    release_scope = _release_scope(root, run, old, preflight)
    compliance_only = release_scope == "compliance_only"
    prepared = _prepared_state(
        old,
        candidate=preflight.commit,
        test_count=preflight.test_count,
        snapshot=preflight.db_snapshot,
        root=root,
        now=float(clock()),
        operation="apply",
    )
    prepared["release_scope"] = release_scope
    _write_state(state_path, prepared)
    watcher_pid = 0
    sidecar_transition_started = False
    try:
        if compliance_only:
            # 只热更新极限词 sidecar：主 watcher 继续录制，
            # 不产生重启切片（2026-08-13 的 13/14 点漏发即因反复重启）。
            watcher_pid = _wait_for_single_watcher(root, run, clock=clock)
            sidecar_transition_started = True
            _stop_compliance_launch_agent(run, required=True)
            _start_compliance_launch_agent(run)
            evidence = _verify_compliance_only_release(
                root,
                run=run,
                clock=clock,
                expected_watcher_pid=watcher_pid,
                expect_recording=preflight.recording_was_active,
                expected_main_config_hash=preflight.main_config_hash,
                expected_compliance_config_hash=preflight.compliance_config_hash,
            )
        else:
            _stop_launch_agents(root, run, required=True)
            compliance_started = _start_launch_agents(root, run)
            evidence = verify_started_release(
                root, run=run, clock=clock,
                expect_recording=preflight.recording_was_active,
                expect_compliance=compliance_started,
                expected_main_config_hash=preflight.main_config_hash,
                expected_compliance_config_hash=preflight.compliance_config_hash,
            )
    except Exception as exc:
        recovery: dict[str, object]
        try:
            if compliance_only:
                if sidecar_transition_started and watcher_pid > 0:
                    recovery = _recover_compliance_only_release(
                        root,
                        run=run,
                        clock=clock,
                        previous_commit=old_current,
                        candidate=preflight,
                        previous_compliance_hash=str(
                            old.get("compliance_config_hash") or ""
                        ),
                        expected_watcher_pid=watcher_pid,
                        expect_recording=preflight.recording_was_active,
                    )
                else:
                    recovery = {
                        "status": "unnecessary",
                        "reason": "production_unchanged",
                        "scope": "compliance_only",
                    }
            else:
                _stop_launch_agents(root, run, required=True)
                recovery = _restore_verified_commit(
                    root, old_current, run=run, clock=clock,
                    expect_recording=preflight.recording_was_active,
                    expected_main_config_hash=preflight.main_config_hash,
                    expected_compliance_config_hash=(
                        preflight.compliance_config_hash
                    ),
                )
        finally:
            if "recovery" not in locals():
                recovery = {"status": "failed", "reason": "recovery_exception"}
            _mark_failed(
                state_path,
                prepared,
                now=float(clock()),
                exc=exc,
                recovery=recovery,
            )
        if isinstance(exc, ReleaseFailed):
            raise
        raise ReleaseFailed(f"post-start verification failed: {type(exc).__name__}") from exc

    previous = old_current if old_current and old_current != preflight.commit else str(
        old.get("previous_verified_commit") or "")
    verified = dict(prepared)
    verified.update({
        "status": "verified",
        "current_verified_commit": preflight.commit,
        "previous_verified_commit": previous,
        "main_config_hash": preflight.main_config_hash,
        "compliance_config_hash": preflight.compliance_config_hash,
        "verified_at": float(clock()),
        "verification": evidence,
    })
    history = list(verified.get("history") or [])
    history[-1] = {
        **dict(history[-1]),
        "status": "verified",
        "verified_at": float(clock()),
        "verification": evidence,
    }
    verified["history"] = history[-50:]
    _write_state(state_path, verified)
    try:
        prune_release_snapshots(root, verified)
    except OSError:
        # A completed release must remain verified even if optional retention
        # maintenance cannot touch the local backup directory.
        pass
    return verified


def _preflight_rollback_target(root: Path, target: str, run: Run) -> int:
    parent = Path(tempfile.mkdtemp(prefix="torras-release-rollback-"))
    worktree = parent / "candidate"
    try:
        _command(
            run,
            ["git", "worktree", "add", "--detach", str(worktree), target],
            description="rollback worktree creation",
        )
        code = (
            "import os,pytest,sys; os.chdir(sys.argv[1]); "
            "raise SystemExit(pytest.main(['tests/','-q','-W','error']))"
        )
        result = run([
            str(root / ".venv" / "bin" / "python"), "-c", code, str(worktree),
        ])
        return _parse_test_count(result)
    finally:
        run(["git", "worktree", "remove", "--force", str(worktree)])
        shutil.rmtree(parent, ignore_errors=True)


def _rollback_release(
    root: Path,
    *,
    run: Run,
    clock: Callable[[], float],
) -> dict[str, object]:
    state_path = root / "data" / "release_state.json"
    old = _read_state(state_path)
    target = str(old.get("previous_verified_commit") or "")
    if not target:
        raise ReleaseRefused("no recorded rollback commit")
    _tracked_status(root, run)
    exists = run(["git", "cat-file", "-e", f"{target}^{{commit}}"])
    if exists.returncode != 0:
        raise ReleaseRefused("recorded rollback commit is not present locally")
    current = _head_commit(run)
    test_count = _preflight_rollback_target(root, target, run)
    try:
        cfg = load_config()
    except Exception as exc:
        raise ReleaseRefused(
            f"production config could not load: {type(exc).__name__}") from exc
    if not isinstance(cfg, dict):
        raise ReleaseRefused("production config is not a mapping")
    try:
        main_config_hash, compliance_config_hash = _config_fingerprints(cfg)
    except (TypeError, ValueError) as exc:
        raise ReleaseRefused(
            "production config could not be fingerprinted") from exc
    database = _database_path(root, cfg)
    _sqlite_integrity(database, error_type=ReleaseRefused)
    recording_was_active = _has_active_recording(database)
    snapshot = _snapshot_path(root, current, now=float(clock()))
    backup_sqlite(database, snapshot)
    prepared = _prepared_state(
        old,
        candidate=target,
        test_count=test_count,
        snapshot=snapshot,
        root=root,
        now=float(clock()),
        operation="rollback",
    )
    _write_state(state_path, prepared)
    try:
        _stop_launch_agents(root, run, required=True)
        _command(
            run,
            ["git", "switch", "--detach", target],
            error_type=ReleaseFailed,
            description="rollback code switch",
        )
        compliance_started = _start_launch_agents(root, run)
        evidence = verify_started_release(
            root, run=run, clock=clock,
            expect_recording=recording_was_active,
            expect_compliance=compliance_started,
            expected_main_config_hash=main_config_hash,
            expected_compliance_config_hash=compliance_config_hash,
        )
    except Exception as exc:
        _stop_launch_agents(root, run, required=True)
        restore = run(["git", "switch", "--detach", current])
        if restore.returncode == 0:
            try:
                _start_launch_agents(root, run)
            except ReleaseFailed:
                pass
        _mark_failed(state_path, prepared, now=float(clock()), exc=exc)
        if isinstance(exc, ReleaseFailed):
            raise
        raise ReleaseFailed(f"rollback verification failed: {type(exc).__name__}") from exc
    verified = dict(prepared)
    verified.update({
        "status": "verified",
        "current_verified_commit": target,
        "previous_verified_commit": current,
        "main_config_hash": main_config_hash,
        "compliance_config_hash": compliance_config_hash,
        "verified_at": float(clock()),
        "verification": evidence,
    })
    history = list(verified.get("history") or [])
    history[-1] = {
        **dict(history[-1]),
        "status": "verified",
        "verified_at": float(clock()),
        "verification": evidence,
    }
    verified["history"] = history[-50:]
    _write_state(state_path, verified)
    return verified


def release(
    root: Path,
    *,
    rollback: bool = False,
    run: Run,
    clock: Callable[[], float],
) -> dict[str, object]:
    root = Path(root).resolve()
    if rollback:
        return _rollback_release(root, run=run, clock=clock)
    return _apply_release(root, run=run, clock=clock)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Safely release or roll back the local watcher")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--apply", action="store_true", help="release current clean HEAD")
    mode.add_argument("--rollback", action="store_true", help="use recorded previous commit")
    args = parser.parse_args()
    runner = _default_runner(ROOT)
    try:
        result = release(
            ROOT,
            rollback=bool(args.rollback),
            run=runner,
            clock=time.time,
        )
    except (ReleaseRefused, ReleaseFailed) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    public = {
        "status": result.get("status"),
        "commit": result.get("current_verified_commit"),
        "previous_commit": result.get("previous_verified_commit"),
        "test_count": result.get("test_count"),
        "db_snapshot": result.get("db_snapshot"),
        "verification": result.get("verification"),
    }
    print(json.dumps(public, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
