"""协作式取消：支撑界面上「终止」按钮的登记表。

为什么需要它（不是加个 abort 就够）：SSE 流是**同步生成器**，跑在 worker 线程里，
客户端断开连接并不能中断这个线程——它会继续把模型输出读到底，白烧 token。
因此前端点「终止」时先调 `POST /api/cancel/{token}` 置位标记；
流式循环在事件/步骤之间 `check()` / `should_stop()` 命中标记就抛 `Cancelled` 提前退出，
由上层负责收尾（对话落库已生成部分、pipeline 把当前步与整轮记成 cancelled）。

令牌由**前端生成**（每个流式请求一个），服务端懒登记：
即使终止请求先到、任务请求后到（竞态），标记也已经置好，任务一开始就能被拦住。

    token = "abc123"            # 前端 crypto.randomUUID()
    ...
    should_stop = cancel.watcher(token)     # 传给流式循环（None 表示不支持终止）
    try:
        ...
    finally:
        cancel.release(token)               # 一定要释放，避免登记表无限增长
    # 另一个请求线程：cancel.request_stop(token) → POST /api/cancel/{token}
"""

import threading
import time
import uuid
from typing import Callable, Dict, Optional, Tuple

MAX_PENDING = 200        # 登记表上限（异常路径漏 release 时兜底）
STALE_SECONDS = 600.0    # 超过这个时间的登记项按“已无人等待”清理


class Cancelled(Exception):
    """协作式取消信号：调用方应捕获它并做收尾，而不是当成错误上报。"""


_registry: Dict[str, Tuple[threading.Event, float]] = {}
_lock = threading.Lock()


def new_token() -> str:
    """生成一个令牌（前端等价于 crypto.randomUUID()，服务端脚本/测试用）。"""
    return uuid.uuid4().hex


def _purge_locked(now: float) -> None:
    """清理过期项与超限项（调用方需持锁）。"""
    if len(_registry) >= MAX_PENDING:
        for token, (_, created) in list(_registry.items()):
            if now - created > STALE_SECONDS:
                _registry.pop(token, None)
    while len(_registry) >= MAX_PENDING:
        oldest = min(_registry.items(), key=lambda kv: kv[1][1])[0]
        _registry.pop(oldest, None)


def request_stop(token: Optional[str]) -> bool:
    """请求取消（懒登记：未知 token 也会被记住）。

    返回 True 表示这次调用置位了标记（任务若还在跑就会被拦住）；
    返回 False 只可能是 token 为空。
    """
    if not token:
        return False
    now = time.time()
    with _lock:
        entry = _registry.get(token)
        if entry is None:
            _registry[token] = (threading.Event(), now)
            entry = _registry[token]
        event = entry[0]
        event.set()
        _purge_locked(now)
    return True


def is_stopping(token: Optional[str]) -> bool:
    if not token:
        return False
    with _lock:
        entry = _registry.get(token)
    return bool(entry is not None and entry[0].is_set())


def release(token: Optional[str]) -> None:
    """任务结束时释放令牌（幂等）。"""
    if not token:
        return
    with _lock:
        _registry.pop(token, None)


def check(token: Optional[str]) -> None:
    """若已请求取消则抛 Cancelled；token 为空时什么也不做（非流式路径）。"""
    if is_stopping(token):
        raise Cancelled("已被用户终止")


def watcher(token: Optional[str]) -> Optional[Callable[[], bool]]:
    """把 token 转成 `should_stop()` 回调，供底层流式循环逐块检查。"""
    if not token:
        return None
    return lambda: is_stopping(token)


def pending_count() -> int:
    """当前登记数（测试用）。"""
    with _lock:
        return len(_registry)
