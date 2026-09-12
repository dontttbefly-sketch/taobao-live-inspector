from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
from typing import Callable


PRODUCTION_MODULES = ("app.recorder.watcher", "app.compliance.listener")
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class StartResult:
    watcher_started: bool
    compliance_started: bool


def venv_python(root: Path, *, platform_name: str = os.name) -> Path:
    if platform_name == "nt":
        return Path(root) / ".venv" / "Scripts" / "python.exe"
    return Path(root) / ".venv" / "bin" / "python"


def missing_config(root: Path) -> bool:
    return not (Path(root) / "config.yaml").is_file()


def _spawn_flags(platform_name: str) -> dict:
    if platform_name != "nt":
        return {"start_new_session": True}
    flags = (
        int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        | int(getattr(subprocess, "DETACHED_PROCESS", 0))
        | int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    )
    return {"creationflags": flags} if flags else {}


def start_production(
    root: Path,
    *,
    python: Path,
    prepare_logs: Callable[[Path], Path],
    popen=subprocess.Popen,
    platform_name: str = os.name,
) -> StartResult:
    project = Path(root)
    log_dir = prepare_logs(project)
    started = []
    for module, log_name in (
        (PRODUCTION_MODULES[0], "watcher.log"),
        (PRODUCTION_MODULES[1], "compliance.log"),
    ):
        log_path = Path(log_dir) / log_name
        handle = open(log_path, "a", encoding="utf-8")
        popen(
            [str(python), "-m", module],
            cwd=str(project),
            stdout=handle,
            stderr=handle,
            **_spawn_flags(platform_name),
        )
        started.append(module)
    return StartResult(
        watcher_started=PRODUCTION_MODULES[0] in started,
        compliance_started=PRODUCTION_MODULES[1] in started,
    )


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=False, capture_output=True, text=True)


def _ensure_venv(root: Path, python: Path) -> None:
    if python.is_file():
        return
    created = _run([sys.executable, "-m", "venv", str(root / ".venv")])
    if created.returncode != 0 or not python.is_file():
        raise RuntimeError("无法创建虚拟环境，请先安装 Python 3.12")


def _ensure_dependencies(root: Path, python: Path) -> None:
    probe = _run([str(python), "-c", "import yaml, requests"])
    if probe.returncode == 0:
        return
    installed = subprocess.run(
        [str(python), "-m", "pip", "install", "-r", str(root / "requirements.txt")],
        check=False,
    )
    if installed.returncode != 0:
        raise RuntimeError("依赖安装失败，请检查网络后重试")


def _ensure_lark_cli() -> None:
    status = _run(["lark-cli", "auth", "status"])
    if status.returncode == 0:
        return
    print("本机尚未完成飞书授权，即将打开登录。只在这台电脑登录，不要复制旧授权。")
    login = subprocess.run(["lark-cli", "auth", "login"], check=False)
    if login.returncode != 0:
        raise RuntimeError("飞书授权未完成，请稍后重新运行 运行.bat")


def main() -> int:
    root = PROJECT_ROOT
    os.chdir(root)
    if missing_config(root):
        print("缺少 config.yaml，无法启动。", file=sys.stderr)
        return 1
    try:
        from scripts.windows.prepare_runtime import prepare_private_log_directory

        python = venv_python(root)
        _ensure_venv(root, python)
        _ensure_dependencies(root, python)
        _ensure_lark_cli()
        result = start_production(
            root,
            python=python,
            prepare_logs=prepare_private_log_directory,
        )
    except Exception as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1
    if not (result.watcher_started and result.compliance_started):
        print("启动失败：巡检或极限词监听未拉起。", file=sys.stderr)
        return 1
    print("已启动。巡检日志：data\\logs\\watcher.log")
    print("极限词日志：data\\logs\\compliance.log")
    print("不要按映像名结束 python.exe；停止时请结束对应窗口或服务。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
