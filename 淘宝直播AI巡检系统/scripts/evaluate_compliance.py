#!/usr/bin/env python3
"""Run an offline compliance replay and write a private aggregate report."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import tempfile
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.compliance.evaluation import (
    ProcessResourceSampler,
    ProductionEvidenceProvider,
    _path_has_symlink_component,
    _replace_private,
    _sync_directory,
    _windows_private_path,
    run_replay_evaluation,
    write_private_report,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate compliance recognition with a private shadow replay",
    )
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--labels", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--full-day", action="store_true")
    return parser


def _shadow_config() -> dict[str, object]:
    return {
        "compliance": {
            "mode": "shadow",
            "recognizer": {},
            "delivery": {"recipient_chat_id": ""},
        },
    }


def _private_workspace_parent(
    path: Path,
    *,
    platform_name: str = os.name,
    windows_acl_verifier=None,
) -> Path:
    if (
        not path.is_absolute()
        or path.is_symlink()
        or _path_has_symlink_component(path.parent)
    ):
        raise ValueError("evaluation output is invalid")
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (
        _path_has_symlink_component(parent)
        or parent.is_symlink()
        or not parent.is_dir()
    ):
        raise ValueError("evaluation output is invalid")
    if platform_name != "nt" and parent.stat().st_mode & 0o077:
        raise ValueError("evaluation output directory is not private")
    if platform_name == "nt" and not _windows_private_path(
        parent, windows_acl_verifier, establish=True
    ):
        raise ValueError("evaluation output directory is not private")
    return parent


def _quarantine_existing_output(
    path: Path,
    *,
    platform_name: str = os.name,
    windows_acl_verifier=None,
) -> None:
    if not path.exists() and not path.is_symlink():
        return
    for index in range(1_000):
        stale = path.with_name(
            f".{path.name}.stale-{os.getpid()}-{index:03d}"
        )
        if stale.exists() or stale.is_symlink():
            continue
        _replace_private(path, stale, platform_name=platform_name)
        if platform_name != "nt":
            stale.chmod(0o600)
        elif not _windows_private_path(
            stale, windows_acl_verifier, establish=True
        ):
            raise ValueError("evaluation stale output is not private")
        _sync_directory(path.parent, platform_name=platform_name)
        return
    raise ValueError("evaluation stale output cannot be quarantined")


def main(
    argv: list[str] | None = None,
    *,
    runner: Callable[..., object] = run_replay_evaluation,
    sampler_factory: Callable[[], object] = ProcessResourceSampler.current,
    evidence_provider_factory: Callable[[], object] = lambda: (
        ProductionEvidenceProvider(PROJECT_ROOT / "data" / "inspection.db")
    ),
) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = Path(args.manifest)
        labels = Path(args.labels)
        output = Path(args.output)
        parent = _private_workspace_parent(output)
        _quarantine_existing_output(output)
        evidence_provider = (
            evidence_provider_factory() if args.full_day else None
        )
        sampler = sampler_factory()
        with tempfile.TemporaryDirectory(
            prefix=".compliance-evaluation-", dir=parent,
        ) as temporary:
            workspace = Path(temporary)
            if os.name != "nt":
                workspace.chmod(0o700)
            elif not _windows_private_path(workspace, None, establish=True):
                raise ValueError("evaluation workspace is not private")
            report = runner(
                manifest,
                labels,
                config=_shadow_config(),
                workspace=workspace,
                full_day_requested=bool(args.full_day),
                evidence_provider=evidence_provider,
                resource_sampler=sampler,
            )
        write_private_report(output, report)
        return 0 if report.passes_shadow_gate() else 2
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
