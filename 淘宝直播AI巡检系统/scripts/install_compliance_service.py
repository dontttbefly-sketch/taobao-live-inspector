#!/usr/bin/env python3
"""Render or explicitly install the independent compliance launchd agent."""

from __future__ import annotations

import argparse
import os
import plistlib
import tempfile
from pathlib import Path


LABEL = "com.torras.live-compliance"
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DESTINATION = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(str(path), flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def render_plist(root: Path) -> bytes:
    project = Path(root).resolve()
    log_path = project / "data" / "logs" / "compliance.log"
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [
            str(project / ".venv" / "bin" / "python"),
            "-m", "app.compliance.listener",
        ],
        "WorkingDirectory": str(project),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
    }, fmt=plistlib.FMT_XML, sort_keys=True)


def _atomic_install(destination: Path, content: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        destination.parent.chmod(0o700)
    except OSError:
        pass
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=str(destination.parent))
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        destination.chmod(0o600)
        _fsync_directory(destination.parent)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def render_or_install(*, root: Path, destination: Path, apply: bool) -> bytes:
    """Return the canonical plist; install only when explicitly requested."""
    rendered = render_plist(Path(root))
    if apply:
        _atomic_install(Path(destination), rendered)
    return rendered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render or explicitly install the compliance launchd plist")
    parser.add_argument("--apply", action="store_true", help="atomically install plist")
    args = parser.parse_args(argv)
    rendered = render_or_install(
        root=ROOT, destination=DEFAULT_DESTINATION, apply=bool(args.apply))
    print(rendered.decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
