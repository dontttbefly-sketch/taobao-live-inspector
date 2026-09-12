from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class BrowserSessionResult:
    status: str
    cookie: str | None = field(default=None, repr=False)
    error: str = ""

    def __repr__(self) -> str:
        return f"BrowserSessionResult(status={self.status!r}, has_cookie={bool(self.cookie)!r})"


class BrowserSessionProvider(Protocol):
    def read_session(self) -> BrowserSessionResult: ...


@dataclass(frozen=True)
class StaticBrowserSessionProvider:
    """人工粘贴 Cookie 时也走同一验证与恢复状态机。"""
    cookie: str = field(repr=False)

    def read_session(self) -> BrowserSessionResult:
        return BrowserSessionResult("ready", cookie=self.cookie)
