from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class AuthFailureSignal:
    kind: str
    api: str
    occurred_at: float
    epoch: int = 0


_LOCK = threading.Lock()
_LATEST: AuthFailureSignal | None = None


def publish_auth_failure(kind: str, api: str, *, epoch: int = 0) -> None:
    global _LATEST
    with _LOCK:
        if _LATEST is not None and int(_LATEST.epoch) > int(epoch):
            return
        _LATEST = AuthFailureSignal(
            kind=str(kind), api=str(api), occurred_at=time.time(), epoch=int(epoch))


def consume_auth_failure() -> AuthFailureSignal | None:
    global _LATEST
    with _LOCK:
        current = _LATEST
        _LATEST = None
        return current


def clear_auth_failure() -> None:
    global _LATEST
    with _LOCK:
        _LATEST = None
