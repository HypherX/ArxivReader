"""LLM 调用底座（Agent Harness 的统一出入口）。

三种调用形态，共用同一套参数口径：
  complete      非流式，返回 Completion（文本 + **推理内容** + 工具调用 + usage）
  chat          非流式，仅返回文本（连通性测试等轻量场景）
  stream_events 真正的流式（SSE 语义）：逐事件产出，**每个事件同时携带 content 与
                reasoning_content 字段**（推理模型把思考过程放在 reasoning_content，
                部分实现叫 reasoning；两者都解析）

流式事件协议（供上层 agent 与 Web 层直接转发）：
  {"type": "delta", "content": <增量正文>, "reasoning_content": <增量思考>}
  {"type": "tool_call", "index": i, "id": ..., "name": ..., "arguments": <增量参数>}
  {"type": "usage", "usage": {...}}        # 仅当服务端回传（stream_options.include_usage）

缓存/重试/参数口径不变：按 (base_url, api_key) 缓存客户端；采样参数为 None 就不下发；
reasoning_effort 经 extra_body 顶层下发（low/medium/high/max，None 则不发）；
不读取任何环境变量。
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from openai import OpenAI

from .cancel import Cancelled

logger = logging.getLogger(__name__)

_REASONING_ATTRS = ("reasoning_content", "reasoning")


@dataclass
class LLMSettings:
    """一次 LLM 调用所需的全部参数（默认端点由 config/设置填充）。"""

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None
    reasoning_effort: Optional[str] = None
    request_timeout: float = 300.0
    max_retries: int = 2
    stream_usage: bool = True     # 流式请求是否带 stream_options.include_usage

    @classmethod
    def from_mapping(cls, cfg: Dict[str, Any]) -> "LLMSettings":
        """由 dict 构造：只取本 dataclass 认识的键，值缺失/为空则用默认。"""
        fields = {f for f in cls.__dataclass_fields__}
        kwargs = {k: v for k, v in (cfg or {}).items() if k in fields and v is not None}
        return cls(**kwargs)


@dataclass
class Completion:
    """一次非流式调用的结果。"""

    text: str = ""
    reasoning_content: str = ""          # 推理过程（无推理模型时为空串）
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)   # [{id,name,arguments}]
    usage: Dict[str, int] = field(default_factory=dict)
    finish_reason: str = ""

    @property
    def total_tokens(self) -> int:
        return int(self.usage.get("total_tokens") or 0)

    @property
    def reasoning_tokens(self) -> int:
        """部分服务端在 usage 里单列推理 token（completion_tokens_details.reasoning_tokens）。"""
        return int(self.usage.get("reasoning_tokens") or 0)


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


def is_configured(settings: LLMSettings) -> bool:
    return bool(settings.base_url and settings.model and settings.api_key)


def build_params(settings: LLMSettings, messages: List[Dict[str, Any]],
                 tools: Optional[List[Dict[str, Any]]] = None,
                 stream: bool = False,
                 include_usage: bool = False) -> Dict[str, Any]:
    """构造 chat.completions 请求体：只下发显式设置过的参数。"""
    params: Dict[str, Any] = {"model": settings.model, "messages": messages}
    if settings.temperature is not None:
        params["temperature"] = settings.temperature
    if settings.max_tokens is not None:
        params["max_tokens"] = settings.max_tokens
    if settings.top_p is not None:
        params["top_p"] = settings.top_p
    if settings.reasoning_effort:
        # OpenAI SDK 会把 extra_body 的键合并到请求体顶层
        params["extra_body"] = {"reasoning_effort": settings.reasoning_effort}
    if tools:
        params["tools"] = tools
    if stream:
        params["stream"] = True
        if include_usage:
            params["stream_options"] = {"include_usage": True}
    return params


# ---------------------------------------------------------------- 解析辅助
def _parse_usage(payload: Any) -> Dict[str, int]:
    """解析 usage；顺带把推理 token 明细拉平到 reasoning_tokens。"""
    usage = getattr(payload, "usage", None)
    if usage is None:
        return {}
    out: Dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = getattr(usage, key, None)
        if isinstance(value, int):
            out[key] = value
    details = getattr(usage, "completion_tokens_details", None)
    reasoning = getattr(details, "reasoning_tokens", None) if details is not None else None
    if isinstance(reasoning, int):
        out["reasoning_tokens"] = reasoning
    return out


def _parse_reasoning(obj: Any) -> str:
    """取推理内容：字段名在不同实现里叫 reasoning_content 或 reasoning。"""
    for attr in _REASONING_ATTRS:
        value = getattr(obj, attr, None)
        if isinstance(value, str) and value.strip():
            return value
    extra = getattr(obj, "model_extra", None)
    if isinstance(extra, dict):
        for attr in _REASONING_ATTRS:
            value = extra.get(attr)
            if isinstance(value, str) and value.strip():
                return value
    return ""


def _parse_tool_calls(message: Any) -> List[Dict[str, Any]]:
    calls = []
    for call in (getattr(message, "tool_calls", None) or []):
        fn = getattr(call, "function", None)
        calls.append({
            "id": getattr(call, "id", "") or "",
            "name": getattr(fn, "name", "") or "",
            "arguments": getattr(fn, "arguments", "") or "",
        })
    return calls


def _is_unsupported_param_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(word in text for word in ("stream_options", "unknown parameter", "unsupported",
                                        "invalid_request", "extra fields"))


# ---------------------------------------------------------------- 非流式
def complete(settings: LLMSettings, messages: List[Dict[str, Any]],
             tools: Optional[List[Dict[str, Any]]] = None,
             tool_choice: Optional[str] = None,
             max_retries: Optional[int] = None) -> Optional[Completion]:
    """非流式调用；重试耗尽返回 None。支持 function calling（tools）与推理内容解析。"""
    client = get_client(settings)
    retries = settings.max_retries if max_retries is None else max_retries
    params = build_params(settings, messages, tools=tools)
    if tools and tool_choice:
        params["tool_choice"] = tool_choice

    for attempt in range(retries + 1):
        try:
            resp = client.chat.completions.create(**params)
            choice = resp.choices[0]
            message = choice.message
            return Completion(
                text=(getattr(message, "content", None) or "").strip(),
                reasoning_content=_parse_reasoning(message),
                tool_calls=_parse_tool_calls(message),
                usage=_parse_usage(resp),
                finish_reason=getattr(choice, "finish_reason", "") or "",
            )
        except Exception as exc:  # noqa: BLE001  网络/鉴权/限流等统一重试
            logger.warning("LLM 调用失败（第 %d/%d 次）：%s",
                           attempt + 1, retries + 1, str(exc)[:300])
            if attempt < retries:
                time.sleep(min(2 ** attempt, 30))
    return None


def chat(settings: LLMSettings, messages: List[Dict[str, Any]],
         max_retries: Optional[int] = None) -> Optional[str]:
    """非流式调用，仅返回文本（重试耗尽返回 None）。"""
    result = complete(settings, messages, max_retries=max_retries)
    return None if result is None else result.text


# ---------------------------------------------------------------- 流式（SSE 语义）
def stream_events(settings: LLMSettings, messages: List[Dict[str, Any]],
                  tools: Optional[List[Dict[str, Any]]] = None,
                  tool_choice: Optional[str] = None,
                  include_usage: Optional[bool] = None,
                  should_stop: Optional[Callable[[], bool]] = None) -> Iterator[Dict[str, Any]]:
    """流式调用，逐事件产出（事件协议见模块 docstring）。

    不做重试：流一旦开始难以安全续传；建连阶段的异常向上抛出，由上层转成错误事件。
    若服务端不接受 stream_options，则自动去掉该参数重连一次（此时没有 usage 事件）。

    should_stop：用户点了「终止」时返回 True。逐块检查，命中就抛 Cancelled 并关掉
    底层流（否则请求会挂到模型自己结束，白烧 token）。
    """
    client = get_client(settings)
    if should_stop is not None and should_stop():
        raise Cancelled("已被用户终止")          # 建连前先看一眼：上一步刚被终止就不发请求了
    use_usage = settings.stream_usage if include_usage is None else include_usage
    params = build_params(settings, messages, tools=tools, stream=True, include_usage=use_usage)
    if tools and tool_choice:
        params["tool_choice"] = tool_choice

    try:
        stream = client.chat.completions.create(**params)
    except Exception as exc:  # noqa: BLE001  可能是 stream_options 不被支持
        if use_usage and _is_unsupported_param_error(exc):
            logger.warning("服务端不支持 stream_options，回退为不带 usage 的流式：%s", str(exc)[:200])
            params.pop("stream_options", None)
            stream = client.chat.completions.create(**params)
        else:
            raise

    try:
        for chunk in stream:
            if should_stop is not None and should_stop():
                raise Cancelled("已被用户终止")
            usage = _parse_usage(chunk)
            if usage:
                yield {"type": "usage", "usage": usage}
            for choice in (getattr(chunk, "choices", None) or []):
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue
                content = getattr(delta, "content", None) or ""
                reasoning = _parse_reasoning(delta)
                if content or reasoning:
                    yield {"type": "delta", "content": content, "reasoning_content": reasoning}
                for tc in (getattr(delta, "tool_calls", None) or []):
                    fn = getattr(tc, "function", None)
                    yield {
                        "type": "tool_call",
                        "index": getattr(tc, "index", 0) or 0,
                        "id": getattr(tc, "id", "") or "",
                        "name": (getattr(fn, "name", "") or "") if fn is not None else "",
                        "arguments": (getattr(fn, "arguments", "") or "") if fn is not None else "",
                    }
    finally:
        # 提前退出（含用户终止）时必须主动关掉底层 HTTP 流，否则请求会一直读到模型结束
        _close_stream(stream)


def _close_stream(stream: Any) -> None:
    close = getattr(stream, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception as exc:  # noqa: BLE001  关流失败不影响上层收尾
        logger.debug("关闭流失败：%s", str(exc)[:200])
