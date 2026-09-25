"""OpenAI 兼容 LLM 客户端。

对齐仓库既有约定（ACEBench/NLEnv/StableRubrics）：
  - 按 (base_url, api_key) 缓存 OpenAI 客户端，避免每请求重建；
  - 失败按指数退避重试；
  - 不读取任何环境变量，全部参数来自传入的 LLMSettings。

对外：
  LLMSettings   承载连接与采样参数的 dataclass
  get_client    取（并缓存）OpenAI 客户端
  chat          一次性返回完整文本
  chat_stream   生成器，逐块产出增量文本（用于 SSE 流式对话）
"""

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Tuple

from openai import OpenAI

logger = logging.getLogger(__name__)


@dataclass
class LLMSettings:
    """一次 LLM 调用所需的全部参数（不含硬编码默认端点，由 settings_store 填充）。"""

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = 0.3
    max_tokens: int = 4096
    top_p: float = 1.0
    request_timeout: float = 120.0
    max_retries: int = 3


_CLIENT_CACHE: Dict[Tuple[str, str], OpenAI] = {}


def get_client(settings: LLMSettings) -> OpenAI:
    key = (settings.base_url, settings.api_key)
    client = _CLIENT_CACHE.get(key)
    if client is None:
        client = OpenAI(
            base_url=settings.base_url or None,
            api_key=settings.api_key or "EMPTY",
            timeout=settings.request_timeout,
            max_retries=0,  # 重试在本模块内做，SDK 层不重复重试
        )
        _CLIENT_CACHE[key] = client
    return client


def _build_params(settings: LLMSettings, messages: List[Dict[str, str]]) -> Dict[str, Any]:
    return {
        "model": settings.model,
        "messages": messages,
        "temperature": settings.temperature,
        "max_tokens": settings.max_tokens,
        "top_p": settings.top_p,
    }


def chat(settings: LLMSettings, messages: List[Dict[str, str]],
         max_retries: Optional[int] = None) -> Optional[str]:
    """非流式调用；重试耗尽返回 None。"""
    client = get_client(settings)
    retries = settings.max_retries if max_retries is None else max_retries
    params = _build_params(settings, messages)

    for attempt in range(retries + 1):
        try:
            resp = client.chat.completions.create(**params)
            content = resp.choices[0].message.content or ""
            return content.strip()
        except Exception as exc:  # noqa: BLE001  网络/鉴权/限流等统一重试
            logger.warning("LLM 调用失败（第 %d/%d 次）：%s",
                           attempt + 1, retries + 1, str(exc)[:300])
            if attempt < retries:
                time.sleep(min(2 ** attempt, 30))
    return None


def chat_stream(settings: LLMSettings, messages: List[Dict[str, str]]) -> Iterator[str]:
    """流式调用，逐块 yield 增量文本（delta.content）。

    不做重试：流一旦开始难以安全续传；建连阶段的异常向上抛出，由路由层
    捕获并以 SSE 错误事件反馈给前端。
    """
    client = get_client(settings)
    params = _build_params(settings, messages)
    params["stream"] = True

    stream = client.chat.completions.create(**params)
    for chunk in stream:
        choices = getattr(chunk, "choices", None)
        if not choices:
            continue
        delta = choices[0].delta
        piece = getattr(delta, "content", None)
        if piece:
            yield piece
