from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    directory_fd = os.open(str(path), flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def atomic_update_yaml_scalar(
    path: Path,
    *,
    section: str,
    key: str,
    value: str,
    mode: int = 0o600,
) -> None:
    """Replace one scalar without re-serializing or partially writing YAML."""
    path = Path(path)
    if not re.fullmatch(r"[A-Za-z_][\w-]*", str(section)):
        raise ValueError("invalid YAML section name")
    if not re.fullmatch(r"[A-Za-z_][\w-]*", str(key)):
        raise ValueError("invalid YAML key name")
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    in_section = False
    section_line: int | None = None
    matches: list[int] = []
    encoded = json.dumps(str(value), ensure_ascii=False)
    for index, line in enumerate(lines):
        top = re.match(r"^([A-Za-z_][\w-]*):\s*(?:#.*)?$", line.rstrip("\r\n"))
        if top:
            in_section = top.group(1) == section
            if in_section:
                if section_line is not None:
                    raise ValueError(f"config.yaml 包含重复的 {section} 配置段")
                section_line = index
            continue
        if in_section and re.match(rf"^\s{{2}}{re.escape(key)}:\s*", line):
            matches.append(index)
    if section_line is None:
        raise ValueError(f"config.yaml 缺少 {section} 配置段")
    if len(matches) > 1:
        raise ValueError(f"config.yaml 包含重复的 {section}.{key}")
    if matches:
        index = matches[0]
        newline = "\r\n" if lines[index].endswith("\r\n") else (
            "\n" if lines[index].endswith("\n") else "")
        lines[index] = f"  {key}: {encoded}{newline}"
    else:
        default_newline = "\r\n" if any(
            line.endswith("\r\n") for line in lines) else "\n"
        lines.insert(section_line + 1, f"  {key}: {encoded}{default_newline}")

    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, int(mode))
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("".join(lines))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        os.chmod(path, int(mode))
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


def write_taobao_cookie(path: Path, cookie: str) -> None:
    atomic_update_yaml_scalar(
        path,
        section="taobao",
        key="cookie",
        value=str(cookie),
        mode=0o600,
    )
