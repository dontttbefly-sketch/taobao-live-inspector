from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
project_root_text = str(PROJECT_ROOT)
if not sys.path or sys.path[0] != project_root_text:
    sys.path.insert(0, project_root_text)


class RuntimePreparationError(RuntimeError):
    """The Windows launcher cannot safely write its private logs."""


WindowsAclSetter = Callable[[Path], None]
WindowsAclVerifier = Callable[[Path], bool]


def _set_windows_private_acl(path: Path) -> None:
    from app.windows_acl import establish_private_acl

    establish_private_acl(path)


def _verify_windows_private_acl(path: Path) -> bool:
    from app.windows_acl import acl_is_private

    return acl_is_private(path)


def prepare_private_log_directory(
    root: Path = PROJECT_ROOT,
    *,
    platform_name: str | None = None,
    windows_acl_setter: WindowsAclSetter = _set_windows_private_acl,
    windows_acl_verifier: WindowsAclVerifier = _verify_windows_private_acl,
) -> Path:
    """Create and verify the private log directory before cmd redirects to it."""
    project_root = Path(root).resolve()
    log_dir = project_root / "data" / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        resolved = log_dir.resolve(strict=True)
        if (
            log_dir.is_symlink()
            or not resolved.is_dir()
            or os.path.commonpath((str(project_root), str(resolved)))
            != str(project_root)
        ):
            raise RuntimePreparationError("private log directory is unsafe")
        log_dir.chmod(0o700)
    except RuntimePreparationError:
        raise
    except (OSError, ValueError) as exc:
        raise RuntimePreparationError(
            "private log directory could not be created"
        ) from exc

    effective_platform = os.name if platform_name is None else platform_name
    if effective_platform == "nt":
        try:
            windows_acl_setter(log_dir)
            private_acl = windows_acl_verifier(log_dir) is True
        except (AttributeError, OSError, TypeError, ValueError):
            private_acl = False
        if not private_acl:
            raise RuntimePreparationError(
                "private log directory ACL could not be verified"
            )

    probe: Path | None = None
    try:
        handle = tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".runtime-log-probe-",
            dir=log_dir,
            delete=False,
        )
        probe = Path(handle.name)
        with handle:
            handle.write(b"ok")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise RuntimePreparationError(
            "private log directory is not writable"
        ) from exc
    finally:
        if probe is not None:
            try:
                probe.unlink(missing_ok=True)
            except OSError as exc:
                raise RuntimePreparationError(
                    "private log directory probe could not be removed"
                ) from exc
    return log_dir


def main() -> int:
    try:
        prepare_private_log_directory(PROJECT_ROOT)
    except Exception:
        print("无法安全准备私有日志目录。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
