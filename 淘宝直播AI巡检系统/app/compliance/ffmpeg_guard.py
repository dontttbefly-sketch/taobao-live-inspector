from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time


def _stop_child(process, *, platform_name: str) -> bool:
    del platform_name
    try:
        if process.poll() is not None:
            return True
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        return process.poll() is not None
    except Exception:
        return process.poll() is not None


def supervise(
    command: tuple[str, ...],
    *,
    control,
    child_factory=subprocess.Popen,
    ready=lambda: None,
    stop_child=None,
    stop_event=None,
    platform_name=sys.platform,
) -> int:
    if not command or any(not isinstance(token, str) or not token for token in command):
        return 2
    kwargs: dict[str, object] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    os_name = "nt" if platform_name in {"nt", "win32"} else "posix"
    try:
        child = child_factory(list(command), **kwargs)
    except Exception:
        return 3
    stopper = stop_child or (
        lambda process: _stop_child(process, platform_name=os_name)
    )
    eof = threading.Event()

    def observe_control() -> None:
        try:
            while control.read(4096):
                pass
        finally:
            eof.set()

    watcher = threading.Thread(target=observe_control, daemon=True)
    watcher.start()
    try:
        ready()
        while child.poll() is None:
            if eof.wait(0.1) or (
                stop_event is not None and stop_event.is_set()
            ):
                break
        while child.poll() is None and stopper(child) is not True:
            time.sleep(0.1)
        return int(child.poll() or 0)
    finally:
        if child.poll() is None:
            stopper(child)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--marker-hash", required=True)
    parser.add_argument("--ack-file", required=True)
    parser.add_argument("--owner-file", required=True)
    args = parser.parse_args(argv)
    if (
        len(args.marker_hash) != 64
        or any(character not in "0123456789abcdef" for character in args.marker_hash)
    ):
        return 2
    try:
        line = sys.stdin.readline()
        payload = json.loads(line)
        if (
            not isinstance(payload, list)
            or not payload
            or any(not isinstance(token, str) or not token for token in payload)
        ):
            return 2
        ack_path = Path(args.ack_file)
        owner_path = Path(args.owner_file)
        if (
            not ack_path.is_absolute()
            or ack_path.name != ".compliance-guard-ready"
            or ack_path.parent.is_symlink()
            or not ack_path.parent.is_dir()
            or hashlib.sha256(
                str(ack_path.parent).encode("utf-8")
            ).hexdigest() != args.marker_hash
            or ack_path.is_symlink()
            or ack_path.exists()
            or not owner_path.is_absolute()
            or owner_path.name != ".compliance-guard-owner.json"
            or owner_path.parent != ack_path.parent
            or owner_path.is_symlink()
            or not owner_path.is_file()
        ):
            return 2
        encoded_owner = owner_path.read_text(encoding="utf-8")
        owner = json.loads(encoded_owner)
        if (
            not isinstance(owner, dict)
            or set(owner) != {"marker_hash", "pid", "start_token", "version"}
            or owner["version"] != 1
            or type(owner["pid"]) is not int
            or owner["pid"] != os.getpid()
            or not isinstance(owner["start_token"], str)
            or not owner["start_token"]
            or owner["marker_hash"] != args.marker_hash
            or json.dumps(
                owner, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            ) != encoded_owner
            or (os.name != "nt" and owner_path.stat().st_mode & 0o077)
        ):
            return 2

        def ready() -> None:
            temporary = ack_path.with_name(f".{ack_path.name}.{time.time_ns()}")
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                os.write(descriptor, b"ready\n")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, ack_path)
            if os.name != "nt":
                directory = os.open(str(ack_path.parent), os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)

        termination = threading.Event()
        prior_handlers: dict[int, object] = {}
        try:
            for signal_value in (signal.SIGINT, signal.SIGTERM):
                prior_handlers[signal_value] = signal.signal(
                    signal_value, lambda _signum, _frame: termination.set()
                )
            return supervise(
                tuple(payload), control=sys.stdin, ready=ready,
                stop_event=termination,
            )
        finally:
            for signal_value, handler in prior_handlers.items():
                signal.signal(signal_value, handler)
    except Exception:
        return 2


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess
    raise SystemExit(_main())
