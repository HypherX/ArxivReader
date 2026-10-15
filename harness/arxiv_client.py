"""全进程共用 arXiv API 客户端：串行请求、统一限速与重试。"""

import threading
from typing import Any, Callable, List, Optional

import arxiv
import requests

from .cancel import Cancelled

_LOCK = threading.Lock()
_CLIENT = None

# requests 默认完全无超时：arXiv 限流时会晾着连接不放（实测一个请求挂 60 秒以上），
# 必须自己兜底；超时/连接错误在 arxiv 库的重试名单之外，会立刻抛给上层（正好快速失败）。
_REQUEST_TIMEOUT = 20.0


class _TimeoutSession(requests.Session):
    """给每个请求补默认 timeout 的 Session（arxiv.Client 不接受 timeout 参数）。"""

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", _REQUEST_TIMEOUT)
        return super().request(method, url, **kwargs)


def make_client() -> arxiv.Client:
    """构造带超时兜底的客户端。num_retries=2：429/503 不会在几秒内恢复，
    原地重试只是继续挨打；真正的限流恢复由上层冷却后再整轮重试。"""
    client = arxiv.Client(page_size=100, delay_seconds=3.0, num_retries=2)
    client._session = _TimeoutSession()
    return client


def results(request: Any, *, client: Any = None,
            should_stop: Optional[Callable[[], bool]] = None) -> List[Any]:
    """限速由同一客户端维护；等待队列和分页间均可取消。"""
    global _CLIENT

    def check():
        if should_stop and should_stop():
            raise Cancelled("已被用户终止")

    check()
    while not _LOCK.acquire(timeout=0.2):
        check()
    try:
        check()
        if client is None:
            if _CLIENT is None:
                _CLIENT = make_client()
            client = _CLIENT
        output = []
        for item in client.results(request):
            check()
            output.append(item)
        check()
        return output
    finally:
        _LOCK.release()
