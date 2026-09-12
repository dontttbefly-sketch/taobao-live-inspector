"""共享 lark-cli JSON 执行器；所有调用使用 argv，禁止拼 shell 字符串。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


class LarkCliError(RuntimeError):
    def __init__(self, message: str, *, error_class: str = "remote", retryable: bool = True):
        super().__init__(message)
        self.error_class = error_class
        self.retryable = retryable


def find_value(obj, keys: tuple[str, ...]):
    if isinstance(obj, dict):
        for key in keys:
            if obj.get(key) not in (None, ""):
                return obj[key]
        for value in obj.values():
            found = find_value(value, keys)
            if found not in (None, ""):
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find_value(value, keys)
            if found not in (None, ""):
                return found
    return None


def _json_object(raw: str) -> dict | None:
    """从 CLI 的纯 JSON 或“进度行 + JSON”输出中取首个对象。"""
    text = str(raw or "").strip()
    start = text.find("{")
    if start < 0:
        return None
    try:
        value, _end = json.JSONDecoder().raw_decode(text[start:])
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


_PATH_ERROR_MARKS = (
    "unsafe file path", "cannot read file", "createfile",
    "must be relative", "must be a relative path",
)
_AUTH_ERROR_MARKS = (
    "unauthorized", "permission_denied", "invalid_token", "unauthenticated",
    "auth_required", "login_required", "authorization", "missing_scope",
    "permission_deny", "permission deny", "missing permission",
)
_UNSTRUCTURED_AUTH_MARKS = (
    "unauthorized", "invalid token", "auth_required", "login_required",
    "missing_scope", "permission_deny", "permission deny",
)


def _classify_error_payload(payload: dict) -> tuple[str, str, bool]:
    message = str(find_value(payload, ("message", "msg", "error")) or "飞书接口拒绝")
    flattened = json.dumps(payload, ensure_ascii=False).lower()
    # Path failures must win over auth: lark-cli stderr often mentions
    # `lark-cli auth login`, and bare "auth"/"login" used to block uploads.
    if any(mark in flattened for mark in _PATH_ERROR_MARKS):
        return message[:200], "invalid_path", True
    if any(mark in flattened for mark in _AUTH_ERROR_MARKS):
        return "飞书用户授权失效", "auth", False
    if any(mark in flattened for mark in ("rate_limit", "too_many_requests", '"429"')):
        return "飞书接口限流", "rate_limit", True
    if any(mark in flattened for mark in (
            "invalid_argument", "validation", "bad_request", "unsupported_media",
            "unsupported_file", "duration_limit", "file_too_large")):
        return message[:200], "invalid_request", False
    return message[:200], "remote", True


def _error_from_output(stdout: str, stderr: str) -> tuple[str, str, bool] | None:
    payload = _json_object(stdout) or _json_object(stderr)
    return _classify_error_payload(payload) if payload else None


def run_lark_cli(args: list[str], *, cwd: Path, timeout: int = 120) -> dict:
    if os.name == "nt":
        command = shutil.which("lark-cli.cmd") or shutil.which("lark-cli") or "lark-cli"
    else:
        command = "lark-cli"
    try:
        proc = subprocess.run(
            [command, *args], cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        # TimeoutExpired.__str__ embeds the complete argv. Some lark commands
        # carry Base tokens/table IDs, so the transport boundary must replace
        # the exception before any upper-level log.exception call sees it.
        raise LarkCliError(
            "lark-cli 调用超时", error_class="transport_timeout", retryable=True,
        ) from None
    if proc.returncode != 0:
        structured = _error_from_output(proc.stdout, proc.stderr)
        if structured:
            message, error_class, retryable = structured
            raise LarkCliError(
                message, error_class=error_class, retryable=retryable)
        diagnostic = f"{proc.stderr}\n{proc.stdout}".lower()
        if args[:2] == ["drive", "+delete"] and any(
                mark in diagnostic for mark in ("not found", "already deleted", "does not exist")):
            return {"ok": True, "data": {"deleted": True, "already_absent": True}}
        if any(mark in diagnostic for mark in _PATH_ERROR_MARKS):
            raise LarkCliError(
                (proc.stderr or proc.stdout or "lark-cli 无法读取本地文件")[:200],
                error_class="invalid_path", retryable=True)
        if any(mark in diagnostic for mark in _UNSTRUCTURED_AUTH_MARKS):
            raise LarkCliError("飞书用户授权失效", error_class="auth", retryable=False)
        if any(mark in diagnostic for mark in ("429", "rate limit", "too many")):
            raise LarkCliError("飞书接口限流", error_class="rate_limit", retryable=True)
        if any(mark in diagnostic for mark in (
                "unsupported file", "unsupported media", "invalid file", "duration limit",
                "file too large", "bad request")):
            raise LarkCliError("飞书明确拒绝该媒体任务", error_class="invalid_media",
                               retryable=False)
        raise LarkCliError("lark-cli 调用失败", error_class="transport", retryable=True)
    output = proc.stdout.strip()
    if not output:
        return {}
    try:
        payload = json.loads(output)
    except (TypeError, ValueError):
        # upload 等快捷命令会先打印一行进度，再输出 JSON；从首个对象起解析，
        # 仍不接受没有完整 JSON 对象的回执。
        payload = _json_object(output)
        if payload is None:
            raise LarkCliError(
                "lark-cli 返回非 JSON", error_class="protocol", retryable=True) from None
    if isinstance(payload, dict) and payload.get("ok") is False:
        message, error_class, retryable = _classify_error_payload(payload)
        raise LarkCliError(
            message, error_class=error_class, retryable=retryable,
        )
    return payload if isinstance(payload, dict) else {"data": payload}
