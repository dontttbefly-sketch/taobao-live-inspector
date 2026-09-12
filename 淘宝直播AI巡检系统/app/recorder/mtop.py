"""淘宝 H5 mtop 接口：签名 + 直播流地址获取

签名算法（已从官方 mtop.js 2.7.1 源码验证）：
    sign = md5(token + "&" + t + "&" + appKey + "&" + data)
    token 取 cookie `_m_h5_tk` 中第一个下划线前的部分
首次请求无 token 时服务端会返回 FAIL_SYS_TOKEN_EMPTY 并在 Set-Cookie 下发 token，重试一次即可。

风险提示：淘宝接口为逆向接口，可能被风控（滑块验证 FAIL_SYS_USER_VALIDATE）。
调用需保持低频，本模块对响应做防御性解析（递归找 .flv/.m3u8 URL），
字段名变化也能尽量兼容。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import urllib.parse
from typing import Optional

import requests

log = logging.getLogger(__name__)

MTOP_HOST = "https://h5api.m.taobao.com/h5"

# 同一进程里的探测/简报/每日/代播请求统一串行限速。淘宝会对短时间连续调用触发
# FAIL_SYS_USER_VALIDATE；1.5 秒间隔对分钟级巡检没有时效损失。
_CALL_LOCK = threading.Lock()
_LAST_CALL_AT = 0.0
MIN_CALL_INTERVAL_SEC = 1.5
NETWORK_RETRIES = 2
_CLIENT_CACHE_LOCK = threading.Lock()
_CLIENT_CACHE: dict[tuple[str, str, str, str], "MtopClient"] = {}
_AUTH_EPOCH = 0

URL_RE = re.compile(r"https?://[^\s\"']+\.(flv|m3u8)(\?[^\s\"']*)?")


class MtopError(Exception):
    """接口层错误（含风控）"""


class MtopAuthError(MtopError):
    """淘宝浏览器登录态不可用。"""

    def __init__(self, message: str, *, auth_epoch: int | None = None):
        super().__init__(message)
        self.auth_epoch = auth_epoch


class MtopSessionExpired(MtopAuthError):
    """浏览器 Session 已过期。"""


class MtopUserValidationRequired(MtopAuthError):
    """淘宝要求用户完成验证。"""


class MtopClient:
    def __init__(self, cookie: str, app_key: str = "12574478",
                 user_agent: Optional[str] = None, referer: str = "https://h5.m.taobao.com/",
                 auth_epoch: int | None = None):
        self.app_key = app_key
        self.referer = referer
        self.cookie = cookie
        self.auth_epoch = current_auth_epoch() if auth_epoch is None else int(auth_epoch)
        # 同一个共享 client 的 token/cookie 刷新必须与下一次签名原子衔接；
        # RLock 允许 token 失效时在 call 内递归重试一次。
        self._request_lock = threading.RLock()
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent or (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
            ),
            "Referer": referer,
        })
        if cookie:
            self.session.headers["Cookie"] = cookie

    # ---------- 签名 ----------
    @staticmethod
    def _sign(token: str, t: str, app_key: str, data: str) -> str:
        raw = f"{token}&{t}&{app_key}&{data}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _extract_token(cookie_str: str) -> str:
        m = re.search(r"(?:^|;)\s*_m_h5_tk=([^;]+)", cookie_str)
        if not m:
            return ""
        return m.group(1).split("_")[0]

    def _new_token(self, resp_cookies) -> str:
        """从响应 Set-Cookie 中提取 _m_h5_tk"""
        for c in resp_cookies:
            if c.name == "_m_h5_tk":
                return c.value.split("_")[0]
        return ""

    def _merge_new_cookies(self, resp) -> str:
        """把响应 Set-Cookie 里的 cookie 完整替换进会话 cookie。

        实测（iliad 数据接口）：token 过期后必须同时替换 _m_h5_tk / _m_h5_tk_enc /
        sgcookie 等全套新 cookie，只换 token 会报 FAIL_SYS_TOKEN_ILLEGAL。
        """
        merged = self.cookie
        for name, value in resp.cookies.items():
            if name in ("_m_h5_tk", "_m_h5_tk_enc", "sgcookie", "cna", "isg"):
                if name in merged:
                    merged = re.sub(rf"(^|; ){re.escape(name)}=[^;]*",
                                    rf"\g<1>{name}={value}", merged)
                else:
                    merged = merged.rstrip(";") + f"; {name}={value}"
        return merged

    # ---------- 请求 ----------
    def call(
            self, api: str, version: str, data: dict, retry: bool = True, *,
            request_timeout: float = 20.0,
            network_retries: int | None = None,
            lock_timeout: float | None = None) -> dict:
        """Issue one serialized request, optionally with a strict time budget.

        Normal callers retain the historical retry and blocking behaviour.
        Boundary snapshots use the bounded options so a maintenance stop cannot
        wait behind an unrelated long request or an internal HTTP retry loop.
        """
        timeout = float(request_timeout)
        if timeout <= 0:
            raise ValueError("request_timeout must be positive")
        if lock_timeout is None:
            with self._request_lock:
                return self._call_locked(
                    api, version, data, retry=retry,
                    request_timeout=timeout,
                    network_retries=network_retries,
                    lock_timeout=None,
                )
        lock_budget = max(0.0, float(lock_timeout))
        if not self._request_lock.acquire(timeout=lock_budget):
            raise MtopError("请求队列繁忙，未在边界读取时限内取得客户端锁")
        try:
            return self._call_locked(
                api, version, data, retry=retry,
                request_timeout=timeout,
                network_retries=network_retries,
                lock_timeout=lock_budget,
            )
        finally:
            self._request_lock.release()

    def _call_locked(self, api: str, version: str, data: dict,
                     retry: bool = True, *, request_timeout: float = 20.0,
                     network_retries: int | None = None,
                     lock_timeout: float | None = None) -> dict:
        global _LAST_CALL_AT
        data_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        token = self._extract_token(self.cookie)
        resp = None
        last_error: Exception | None = None
        retry_count = (
            NETWORK_RETRIES if network_retries is None
            else max(0, int(network_retries)))
        for attempt in range(retry_count + 1):
            # 每次网络重试都重新签名，避免复用已经过期的毫秒时间戳。
            url, params = self._build_url(api, version, data_json, token)
            try:
                if lock_timeout is None:
                    _CALL_LOCK.acquire()
                elif not _CALL_LOCK.acquire(timeout=lock_timeout):
                    raise MtopError("请求队列繁忙，未在边界读取时限内取得全局锁")
                try:
                    wait_for = MIN_CALL_INTERVAL_SEC - (time.monotonic() - _LAST_CALL_AT)
                    if wait_for > 0:
                        time.sleep(wait_for)
                    try:
                        resp = self.session.get(
                            url, params=params, headers={"Cookie": self.cookie},
                            timeout=request_timeout)
                    finally:
                        _LAST_CALL_AT = time.monotonic()
                finally:
                    _CALL_LOCK.release()
                break
            except requests.RequestException as exc:
                last_error = exc
                if attempt < retry_count:
                    time.sleep(0.5 * (2 ** attempt))
        if resp is None:
            # 统一转成接口层错误，让 watcher 进入退避/告警逻辑，避免每分钟打印整段堆栈。
            detail = str(last_error or "unknown network error").split(" url:", 1)[0]
            raise MtopError(f"网络请求失败（{type(last_error).__name__}）：{detail[:180]}")
        try:
            payload = resp.json()
        except ValueError:
            raise MtopError(f"响应非 JSON: {resp.status_code} {resp.text[:200]}")

        ret = payload.get("ret", [""])[0] if isinstance(payload.get("ret"), list) else str(payload.get("ret", ""))

        # 首次无 token：服务端下发后重试一次
        # ret 是完整字符串（如 "FAIL_SYS_TOKEN_EXOIRED::令牌过期"），按前缀匹配；
        # 注意淘宝实际拼写是 EXOIRED（少个 P），用 FAIL_SYS_TOKEN 前缀统一覆盖
        if ret.startswith("FAIL_SYS_TOKEN"):
            new_token = self._new_token(resp.cookies)
            if new_token and retry:
                self.cookie = self._merge_new_cookies(resp)
                log.info("已从服务端刷新 token（含 enc/sgcookie 全套），重试一次")
                return self.call(
                    api, version, data, retry=False,
                    request_timeout=request_timeout,
                    network_retries=retry_count,
                    lock_timeout=lock_timeout,
                )
            raise MtopError(f"token 获取失败: {ret}")

        if ret.startswith("FAIL_SYS_SESSION_EXPIRED"):
            from ..taobao_session.signal import publish_auth_failure
            publish_auth_failure("session_expired", api, epoch=self.auth_epoch)
            raise MtopSessionExpired(
                "淘宝登录状态已过期", auth_epoch=self.auth_epoch)
        if "USER_VALIDATE" in ret:
            from ..taobao_session.signal import publish_auth_failure
            publish_auth_failure(
                "user_validation_required", api, epoch=self.auth_epoch)
            raise MtopUserValidationRequired(
                f"风控拦截（{ret}）：接口要求滑块验证，多为瞬时风控；"
                "系统会自动复核当前登录态，仅当复核持续失败时才需人工重新登录",
                auth_epoch=self.auth_epoch,
            )
        if "FAIL" in ret and "BIZ" not in ret:
            # 业务失败（如直播间不存在）直接返回，由上层判断
            return payload
        return payload

    def _build_url(self, api: str, version: str, data_json: str, token: str):
        t = str(int(time.time() * 1000))
        sign = self._sign(token, t, self.app_key, data_json)
        params = {
            "jsv": "2.7.5",
            "appKey": self.app_key,
            "t": t,
            "sign": sign,
            "api": api,
            "v": version,
            "dataType": "json",
            "type": "json",
            "data": data_json,
        }
        return f"{MTOP_HOST}/{api}/{version}/", params

    # ---------- 直播流 ----------
    def query_live_detail(self, live_id: str, **call_options) -> dict:
        """查询直播间详情（含流地址）。live_id 为直播间 id"""
        return self.call("mtop.roomstudio.live.detail.get", "1.0",
                         {"liveId": live_id}, **call_options)

    def probe(self, live_id: str, user_id: str = "", **call_options) -> dict:
        """探测模式：返回 (is_live, stream_urls, extra)。对响应做防御性解析"""
        payload = {}
        tried = []
        if live_id:
            tried.append("liveId")
            # 交通/鉴权异常必须传给 watcher。空 payload 与“明确
            # 下播”语义不同：吞掉异常会让网络抖动进入下播计数。
            payload = self.query_live_detail(live_id, **call_options)

        ret = payload.get("ret", [""])[0] if isinstance(payload.get("ret"), list) else str(payload.get("ret", ""))
        data = payload.get("data") or {}
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                data = {}

        # 优先用 roomstudio 接口的已知字段精确解析（排除 replayUrl 回放地址）
        stream_urls, is_live = self._extract_roomstudio(data)
        lifecycle = self._roomstudio_lifecycle(data)
        if lifecycle is not False and not stream_urls and not is_live:
            # 兜底：递归扫描所有 .flv/.m3u8 URL
            stream_urls = []
            self._scan_urls(data, stream_urls)
            # 去重保序
            seen, urls = set(), []
            for u in stream_urls:
                if u not in seen:
                    seen.add(u)
                    urls.append(u)
            stream_urls = urls
            is_live = bool(stream_urls) or self._looks_live(data)

        return {
            "is_live": is_live,
            "lifecycle": ("live" if lifecycle is True else
                          "ended" if lifecycle is False else "unknown"),
            "stream_urls": stream_urls,
            "tried": tried,
            "ret": ret,
            "raw": payload,
        }


    @staticmethod
    def _extract_roomstudio(data: dict) -> tuple[list[str], bool]:
        """roomstudio.live.detail.get 响应解析：
        在播判断：拿到直播流地址即视为在播（liveUrl/liveUrlHls/liveUrlList 都是
        直播流字段，下播时为空；replayUrl 回放地址不在此列，不会误判）。
        status 字段（streamStatus/roomStatus）仅作兜底，值可能随平台变化。"""
        if not isinstance(data, dict):
            return [], False
        urls: list[str] = []
        candidates: list[str] = []
        for key in ("liveUrlHls", "liveUrl"):
            v = data.get(key)
            if isinstance(v, str) and v:
                candidates.append(v)
        lst = data.get("liveUrlList")
        if isinstance(lst, list):
            for item in lst:
                if isinstance(item, dict):
                    for k in ("hlsUrl", "flvUrl"):
                        v = item.get(k)
                        if isinstance(v, str) and v:
                            candidates.append(v)
        for u in candidates:
            if u not in urls:
                urls.append(u)
        lifecycle = MtopClient._roomstudio_lifecycle(data)
        # 淘宝会在下播详情中继续保留已经失效的 liveUrl/liveUrlHls。明确的结束状态
        # 必须覆盖这些残留地址，否则 watcher 会把同一场永远判定为在播。
        if lifecycle is False:
            return [], False
        if lifecycle is True:
            return urls, True
        return urls, bool(urls)

    @staticmethod
    def _roomstudio_lifecycle(data: dict) -> bool | None:
        """解析 roomstudio 生命周期：True=在播、False=已结束、None=字段未知。

        实测口径：在播 streamStatus=1/roomStatus=1；下播
        streamStatus=0/roomStatus=2。通用 status 在两种状态下含义相反，禁止使用。
        """
        if not isinstance(data, dict):
            return None
        stream_status = str(data.get("streamStatus", "")).strip().lower()
        if stream_status:
            if stream_status in {"1", "live", "living", "true"}:
                return True
            if stream_status in {"0", "2", "end", "ended", "false", "offline"}:
                return False
        room_status = str(data.get("roomStatus", "")).strip().lower()
        if room_status:
            if room_status in {"1", "live", "living", "true"}:
                return True
            # roomStatus=0 在不同版本接口中也表示未知/待开播，不能用它
            # 覆盖仍可能有效的直播状态；明确结束值只有 2/end/offline。
            if room_status in {"2", "end", "ended", "false", "offline"}:
                return False
        destroyed = str(data.get("liveIsdestroy", "")).strip().lower()
        if destroyed in {"1", "true"}:
            return False
        return None

    @staticmethod
    def _scan_urls(obj, out: list[str]) -> None:
        if isinstance(obj, dict):
            for key, v in obj.items():
                # 回放/切片 URL 不是直播流，禁止拿来兜底判定在播。
                key_l = str(key).lower()
                if "replay" in key_l or "playback" in key_l or "tidbits" in key_l:
                    continue
                MtopClient._scan_urls(v, out)
        elif isinstance(obj, list):
            for v in obj:
                MtopClient._scan_urls(v, out)
        elif isinstance(obj, str):
            for m in URL_RE.finditer(obj):
                out.append(m.group(0))

    @staticmethod
    def _looks_live(data) -> bool:
        """无流地址时看状态字段兜底判断是否在播"""
        if not isinstance(data, dict):
            return False
        lifecycle = MtopClient._roomstudio_lifecycle(data)
        if lifecycle is not None:
            return lifecycle
        for key in ("liveStatus", "liveState", "isLive", "playing"):
            if key in data:
                v = data[key]
                return str(v) in ("1", "2", "true", "True", "LIVE", "live")
        return False


def public_live_media_urls(live_id: str) -> tuple[str, ...]:
    """Read current media URLs without using the authenticated session.

    This is a recorder-continuity fallback for an already known live identity.
    Callers must not use URL presence as end-of-live evidence.
    """
    target = str(live_id or "").strip()
    if not target:
        return ()
    result = MtopClient(cookie="").probe(
        target,
        request_timeout=2.0,
        network_retries=0,
        lock_timeout=0.25,
    )
    ret = str(result.get("ret") or "")
    urls = result.get("stream_urls")
    if (not ret.startswith("SUCCESS")
            or result.get("lifecycle") != "live"
            or not isinstance(urls, list)):
        return ()
    return tuple(str(url) for url in urls if isinstance(url, str) and url)


def shared_client(cfg: dict, *, default_referer: str = "https://h5.m.taobao.com/") -> MtopClient:
    """返回进程内共享的淘宝客户端。

    watcher、分钟指标、每日指标和场次发现共享同一套刷新后的 token/cookie，
    避免各自持有旧 token 后轮流触发刷新与风控。配置 cookie 改变时自然创建新实例。
    """
    t = cfg.get("taobao", {}) or {}
    cookie = str(t.get("cookie") or "")
    app_key = str(t.get("app_key") or "12574478")
    user_agent = str(t.get("user_agent") or "")
    referer = str(t.get("referer") or default_referer)
    key = (cookie, app_key, user_agent, referer)
    with _CLIENT_CACHE_LOCK:
        client = _CLIENT_CACHE.get(key)
        if client is None:
            client = MtopClient(
                cookie=cookie, app_key=app_key,
                user_agent=user_agent or None, referer=referer,
                auth_epoch=_AUTH_EPOCH,
            )
            _CLIENT_CACHE[key] = client
        return client


def activate_shared_cookie(cfg: dict, cookie: str) -> MtopClient:
    """在候选 Cookie 已通过业务验证后热切换共享客户端。"""
    global _AUTH_EPOCH
    cfg.setdefault("taobao", {})["cookie"] = str(cookie)
    with _CLIENT_CACHE_LOCK:
        _AUTH_EPOCH += 1
        _CLIENT_CACHE.clear()
    return shared_client(cfg)


def current_auth_epoch() -> int:
    """返回当前共享登录态版本，用于忽略旧客户端的延迟失败。"""
    with _CLIENT_CACHE_LOCK:
        return int(_AUTH_EPOCH)
