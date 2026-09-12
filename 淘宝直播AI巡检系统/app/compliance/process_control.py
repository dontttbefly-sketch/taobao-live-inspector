from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import ntpath
import os
from pathlib import Path
import shlex
import signal
import subprocess
import time


WINDOWS_NEW_PROCESS_GROUP = 0x00000200
GUARD_OWNER_NAME = ".compliance-guard-owner.json"


@dataclass(frozen=True)
class ProcessIdentity:
    start_token: str
    argv: tuple[str, ...]


def coerce_process_identity(value) -> ProcessIdentity | None:
    if value is None:
        return None
    if isinstance(value, ProcessIdentity):
        identity = value
    elif (
        isinstance(value, tuple)
        and len(value) == 2
        and isinstance(value[0], str)
        and isinstance(value[1], (tuple, list))
    ):
        identity = ProcessIdentity(value[0], tuple(value[1]))
    else:
        raise ValueError("audio_process_identity_invalid")
    if (
        not identity.start_token
        or not identity.argv
        or any(not isinstance(token, str) or not token for token in identity.argv)
    ):
        raise ValueError("audio_process_identity_invalid")
    return identity


def read_process_identity(
    pid: int,
    *,
    platform_name: str = os.name,
    run=subprocess.run,
) -> ProcessIdentity | None:
    process_id = int(pid)
    if process_id <= 0:
        raise ValueError("audio_process_identity_invalid")
    if platform_name == "nt":
        command = (
            "$p=Get-CimInstance Win32_Process -Filter \"ProcessId="
            f"{process_id}\"; if($null -eq $p){{exit 3}}; "
            "$p | Select-Object CreationDate,ExecutablePath,CommandLine | "
            "ConvertTo-Json -Compress"
        )
        completed = run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode == 3:
            return None
        if completed.returncode != 0:
            raise RuntimeError("audio_process_probe_failed")
        payload = json.loads(completed.stdout)
        creation = payload.get("CreationDate")
        executable = payload.get("ExecutablePath")
        command_line = payload.get("CommandLine")
        if not all(
            isinstance(value, str) and value
            for value in (creation, executable, command_line)
        ):
            raise RuntimeError("audio_process_probe_failed")
        argv = tuple(shlex.split(command_line, posix=False))
        if not argv:
            argv = (executable,)
        return ProcessIdentity(creation, argv)
    completed = run(
        ["ps", "-o", "lstart=", "-o", "command=", "-p", str(process_id)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 1:
        return None
    if completed.returncode != 0:
        raise RuntimeError("audio_process_probe_failed")
    raw = str(completed.stdout).strip()
    fields = raw.split(None, 5)
    if len(fields) != 6:
        raise RuntimeError("audio_process_probe_failed")
    argv = tuple(shlex.split(fields[5], posix=True))
    return coerce_process_identity((" ".join(fields[:5]), argv))


def stop_owned_process(
    process,
    *,
    platform_name: str = os.name,
    getpgid=None,
    killpg=None,
    run=subprocess.run,
    terminate_timeout: float = 15.0,
    kill_timeout: float = 5.0,
) -> bool:
    if process.poll() is not None:
        return True
    if platform_name == "nt":
        try:
            total_timeout = max(0.0, float(terminate_timeout))
            command_timeout = total_timeout / 2
            wait_timeout = total_timeout - command_timeout
            completed = run(
                ["taskkill", "/PID", str(int(process.pid)), "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
                timeout=command_timeout,
            )
            if completed.returncode != 0 and process.poll() is None:
                return False
            process.wait(timeout=wait_timeout)
            return process.poll() is not None
        except Exception:
            return process.poll() is not None

    getpgid = getpgid or os.getpgid
    killpg = killpg or os.killpg
    try:
        process_group = getpgid(int(process.pid))
        if int(process_group) != int(process.pid):
            return False
        killpg(process_group, signal.SIGTERM)
        try:
            process.wait(timeout=max(0.0, float(terminate_timeout)))
        except subprocess.TimeoutExpired:
            killpg(process_group, signal.SIGKILL)
            process.wait(timeout=max(0.0, float(kill_timeout)))
        return process.poll() is not None
    except Exception:
        return process.poll() is not None


def terminate_owned_ffmpeg(
    pid: int,
    marker: Path,
    *,
    process_token: str = "",
    identity_reader=None,
    identity_bound_terminator=None,
    command_reader=None,
    terminator=None,
    platform_name: str = os.name,
    run=subprocess.run,
    monotonic=time.monotonic,
    sleep=time.sleep,
    confirmation_timeout: float = 5.0,
    poll_interval: float = 0.05,
) -> bool:
    del command_reader, terminator
    if not isinstance(process_token, str) or not process_token:
        return False
    reader = identity_reader or (
        lambda process_id: read_process_identity(
            process_id, platform_name=platform_name, run=run
        )
    )

    def normalized(value: str) -> str:
        raw = value.strip('"')
        if platform_name == "nt":
            return ntpath.normcase(ntpath.normpath(raw))
        return os.path.normpath(raw)

    marker_value = normalized(str(marker))
    marker_hash = hashlib.sha256(str(marker).encode("utf-8")).hexdigest()

    def token_is_beneath(value: str) -> bool:
        candidate = normalized(value)
        if platform_name == "nt":
            try:
                return ntpath.commonpath((marker_value, candidate)) == marker_value
            except ValueError:
                return False
        try:
            return os.path.commonpath((marker_value, candidate)) == marker_value
        except ValueError:
            return False

    def is_guard(identity: ProcessIdentity) -> bool:
        tokens = identity.argv
        try:
            module_index = tokens.index("-m")
        except ValueError:
            return False
        expected_ack = normalized(str(marker / ".compliance-guard-ready"))
        expected_owner = normalized(str(marker / GUARD_OWNER_NAME))
        expected_prefix = (
            "-m",
            "app.compliance.ffmpeg_guard",
            "--marker-hash",
            marker_hash,
            "--ack-file",
        )
        legacy = (
            identity.start_token == process_token
            and tuple(tokens[module_index:module_index + 5]) == expected_prefix
            and len(tokens) == module_index + 6
            and normalized(tokens[-1]) == expected_ack
        )
        current = (
            identity.start_token == process_token
            and tuple(tokens[module_index:module_index + 5]) == expected_prefix
            and len(tokens) == module_index + 8
            and normalized(tokens[module_index + 5]) == expected_ack
            and tokens[module_index + 6] == "--owner-file"
            and normalized(tokens[module_index + 7]) == expected_owner
        )
        return legacy or current

    def is_owned(identity: ProcessIdentity) -> bool:
        if is_guard(identity):
            return True
        executable = normalized(identity.argv[0])
        expected_name = "ffmpeg.exe" if platform_name == "nt" else "ffmpeg"
        executable_name = (
            ntpath.basename(executable)
            if platform_name == "nt"
            else os.path.basename(executable)
        )
        if (
            identity.start_token != process_token
            or executable_name.casefold() != expected_name.casefold()
        ):
            return False
        return any(token_is_beneath(token) for token in identity.argv[1:])

    try:
        process_id = int(pid)
        if process_id <= 0:
            return False
        first = coerce_process_identity(reader(process_id))
    except Exception:
        return False
    if first is None or first.start_token != process_token:
        return True
    if not is_owned(first):
        return False
    try:
        second = coerce_process_identity(reader(process_id))
    except Exception:
        return False
    if second is None or second.start_token != process_token:
        return True
    if not is_owned(second):
        return False
    try:
        if not is_guard(second):
            if not callable(identity_bound_terminator):
                return False
            if identity_bound_terminator(process_id, second) is not True:
                return False
        timeout = float(confirmation_timeout)
        interval = float(poll_interval)
        if timeout < 0 or interval <= 0:
            return False
        deadline = float(monotonic()) + timeout
        max_probes = max(1, int(timeout / interval) + 2)
    except Exception:
        return False
    for probe_index in range(max_probes):
        try:
            current = coerce_process_identity(reader(process_id))
        except Exception:
            return False
        if current is None or current.start_token != process_token:
            return True
        if not is_owned(current):
            return True
        remaining = deadline - float(monotonic())
        if remaining <= 0 or probe_index + 1 >= max_probes:
            return False
        try:
            sleep(min(interval, remaining))
        except Exception:
            return False
    return False


__all__ = [
    "GUARD_OWNER_NAME",
    "ProcessIdentity",
    "WINDOWS_NEW_PROCESS_GROUP",
    "coerce_process_identity",
    "read_process_identity",
    "stop_owned_process",
    "terminate_owned_ffmpeg",
]
