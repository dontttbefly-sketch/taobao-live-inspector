from __future__ import annotations

import json
import subprocess
from collections.abc import Callable

from .provider import BrowserSessionResult


_RESULT_PREFIX = "__TORRAS_SESSION__"
_ALLOWED_DOMAINS = ("taobao.com", "tmall.com", "alibaba.com", "qianniu.com")
_IDENTITY_COOKIES = {"cookie2", "unb", "sgcookie", "tracknick"}


class EgoLiteSessionProvider:
    def __init__(self, cfg: dict, *, runner: Callable | None = None):
        self.cfg = cfg
        self.settings = cfg.get("taobao_session", {}) or {}
        self.runner = runner or subprocess.run
        self.cli_path = str(self.settings.get("cli_path") or "ego-browser")

    def read_session(self) -> BrowserSessionResult:
        script = self._script()
        try:
            completed = self.runner(
                [self.cli_path, "nodejs"], input=script, capture_output=True,
                text=True, timeout=max(15, int(self.settings.get("cli_timeout_sec", 45))),
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return BrowserSessionResult("unavailable", error="ego_lite_unavailable")
        payload = self._result_payload("\n".join((
            str(completed.stdout or ""), str(completed.stderr or ""))))
        if completed.returncode != 0 or payload is None:
            return BrowserSessionResult("unavailable", error="ego_lite_failed")
        status = str(payload.get("status") or "unavailable")
        if status == "user_control":
            return BrowserSessionResult("login_required", error="user_control")
        self._cleanup_task_space()
        if status != "ok":
            return BrowserSessionResult(
                "login_required" if status == "login_required" else "unavailable",
                error=status,
            )
        cookie = self._assemble_cookie(payload.get("cookies") or [])
        if not cookie:
            return BrowserSessionResult("login_required", error="merchant_cookie_missing")
        return BrowserSessionResult("ready", cookie=cookie)

    @staticmethod
    def _result_payload(stdout: str) -> dict | None:
        frames = [line for line in stdout.splitlines()
                  if line.startswith(_RESULT_PREFIX)]
        if len(frames) != 1:
            return None
        for line in frames:
            try:
                value = json.loads(line[len(_RESULT_PREFIX):])
            except (TypeError, ValueError):
                return None
            return value if isinstance(value, dict) else None
        return None

    @staticmethod
    def _assemble_cookie(cookies: list) -> str | None:
        selected: dict[str, str] = {}
        for item in cookies:
            if not isinstance(item, dict):
                continue
            domain = str(item.get("domain") or "").lower().lstrip(".")
            if not any(domain == suffix or domain.endswith("." + suffix)
                       for suffix in _ALLOWED_DOMAINS):
                continue
            name = str(item.get("name") or "").strip()
            value = str(item.get("value") or "")
            if name and value:
                selected[name] = value
        if not {"_m_h5_tk", "_m_h5_tk_enc"}.issubset(selected):
            return None
        if not (_IDENTITY_COOKIES & selected.keys()):
            return None
        return "; ".join(f"{name}={selected[name]}" for name in sorted(selected))

    def _script(self) -> str:
        task_name = json.dumps(str(self.settings.get(
            "ego_task_name", "torras taobao session recovery")))
        landing_url = json.dumps(str(self.settings.get(
            "landing_url", "https://market.m.taobao.com/")))
        return f"""
const task = await useOrCreateTaskSpace({task_name})
try {{
  await openOrReuseTab({landing_url}, {{wait: true, timeout: 20}})
  const info = await pageInfo()
  const url = String((info && info.url) || '')
  if (/login|passport|captcha|verify/i.test(url)) {{
    cliLog('{_RESULT_PREFIX}' + JSON.stringify({{status: 'login_required'}}))
  }} else {{
    const result = await cdp('Network.getAllCookies')
    cliLog('{_RESULT_PREFIX}' + JSON.stringify({{status: 'ok', cookies: result.cookies || []}}))
  }}
}} catch (error) {{
  const kind = /user is controlling|inactive|not assigned/i.test(String(error))
    ? 'user_control' : 'unavailable'
  cliLog('{_RESULT_PREFIX}' + JSON.stringify({{status: kind}}))
}}
""".strip() + "\n"

    def _cleanup_task_space(self) -> None:
        task_name = json.dumps(str(self.settings.get(
            "ego_task_name", "torras taobao session recovery")))
        script = f"await completeTaskSpace({task_name}, {{keep: false}})\n"
        try:
            self.runner(
                [self.cli_path, "nodejs"], input=script, capture_output=True,
                text=True, timeout=15, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return
