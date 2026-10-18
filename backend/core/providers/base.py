"""数据源公共设施：限速、重试、HTTP 会话、统一异常。

设计约束：
* 免费数据源无 SLA，所有网络调用必须带重试与退避，且**单只失败不能中断整体任务**；
* baostock 是全局单连接库，必须串行；东财是 HTTP 接口，可限速后并发；
* 异常一律包成 ``ProviderError``，上层只处理这一种。
"""
from __future__ import annotations

import functools
import logging
import threading
import time
from typing import Any, Callable, Optional, TypeVar

import requests

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class ProviderError(RuntimeError):
    """数据源调用失败（网络、解析、限流等）。"""


class RateLimiter:
    """最小间隔限速器（线程安全）。

    免费接口没有官方限流承诺，靠自觉：默认每 0.2s 一次请求。
    """

    def __init__(self, min_interval: float = 0.2):
        self.min_interval = max(0.0, float(min_interval))
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            gap = time.monotonic() - self._last
            if gap < self.min_interval:
                time.sleep(self.min_interval - gap)
            self._last = time.monotonic()


def retry(
    times: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
    on_error: Optional[Callable[[int, BaseException], None]] = None,
):
    """重试装饰器：指数退避，最后一次仍失败则抛出。"""

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            attempt = 0
            wait = delay
            while True:
                try:
                    return fn(*args, **kwargs)
                except exceptions as exc:
                    attempt += 1
                    if on_error:
                        on_error(attempt, exc)
                    if attempt >= times:
                        raise
                    logger.warning(
                        "%s 第 %d/%d 次失败：%s，%.1fs 后重试",
                        getattr(fn, "__name__", "call"),
                        attempt,
                        times,
                        exc,
                        wait,
                    )
                    time.sleep(wait)
                    wait *= backoff

        return wrapper

    return decorator


def make_session(
    user_agent: str = DEFAULT_UA,
    timeout: float = 20.0,
    pool_size: int = 8,
) -> requests.Session:
    """构造带默认 UA 与超时的 requests 会话。"""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": user_agent,
            "Accept": "*/*",
            "Connection": "keep-alive",
        }
    )
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=pool_size,
        pool_maxsize=pool_size,
        max_retries=0,  # 重试由 retry 装饰器统一控制
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.request_timeout = timeout  # type: ignore[attr-defined]
    return session


def get_json(
    session: requests.Session,
    url: str,
    params: Optional[dict] = None,
    timeout: Optional[float] = None,
) -> dict:
    """GET 并解析 JSON，任何异常统一包成 ProviderError。"""
    try:
        response = session.get(
            url,
            params=params,
            timeout=timeout or getattr(session, "request_timeout", 20.0),
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise ProviderError(f"请求失败 {url}: {exc}") from exc
    except ValueError as exc:
        raise ProviderError(f"响应不是合法 JSON {url}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProviderError(f"响应结构异常 {url}: {type(payload).__name__}")
    return payload


class Progress:
    """进度回调封装：把 (done, total, message) 统一成可空回调。"""

    def __init__(self, callback: Optional[Callable[[float, str], None]] = None):
        self.callback = callback

    def __call__(self, done: int, total: int, message: str = "") -> None:
        if not self.callback:
            return
        ratio = 0.0 if not total else max(0.0, min(1.0, done / total))
        self.callback(ratio, message)

    def stage(self, message: str) -> None:
        """不推进进度，只更新说明文字。"""
        if self.callback:
            self.callback(-1.0, message)
