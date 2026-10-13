"""全进程共用 arXiv API 客户端：串行请求、统一限速与重试。"""

import threading
from typing import Any, Callable, List, Optional

import arxiv

from .cancel import Cancelled

_LOCK = threading.Lock()
_CLIENT = None


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
                _CLIENT = arxiv.Client(page_size=100, delay_seconds=3.0, num_retries=3)
            client = _CLIENT
        output = []
        for item in client.results(request):
            check()
            output.append(item)
        check()
        return output
    finally:
        _LOCK.release()
