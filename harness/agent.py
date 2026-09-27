"""工具调用循环（OpenAI function calling），统一走 llm.stream_events。

两个入口，共用同一套协议细节：
  run_agent         非流式（对外）：跑完"模型请求工具 -> 本地执行 -> 回灌结果 -> 继续"，
                    返回最终文本 **与完整推理内容**（技能后台调用用这一条）
  iter_agent_events 流式（对外）：逐事件产出 delta / tool / error / final（对话与流式技能用）

事件协议（与 llm.stream_events 对齐，**每个 delta 事件同时携带 content 与 reasoning_content**）：
  {"type": "delta",  "content": <增量正文>, "reasoning_content": <增量思考>}
  {"type": "tool",   "name": 工具名, "ok": bool, "result_chars": n}
  {"type": "final",  "content": 完整正文, "reasoning_content": 完整思考,
                     "rounds": n, "calls": [ToolCallRecord...], "usage": {...}}
  {"type": "error",  "message": 错误信息}

配额与协议约定：
  - 工具通过请求体的 tools 参数下发（不拼进 prompt），schema 每轮重复计费，故保持精简；
  - 回灌时 assistant 消息必须保留 tool_calls、工具结果用 role=tool + tool_call_id；
  - max_rounds 限制"允许调用工具的轮数"，用尽后强制最后一轮不带工具，保证一定有文本产出；
  - usage 取自流式最终 chunk（stream_options.include_usage），累计后作为真实 token 账本。
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from . import llm
from .cancel import Cancelled
from .tools.base import Tool, openai_schemas

logger = logging.getLogger(__name__)

_REASONING_SEPARATOR = "\n\n"


@dataclass
class ToolCallRecord:
    """一次工具调用的账本记录。"""

    name: str
    arguments: str = ""
    ok: bool = True
    result_chars: int = 0


@dataclass
class AgentRun:
    """非流式运行结果。"""

    text: str = ""
    reasoning_content: str = ""
    rounds: int = 0
    calls: List[ToolCallRecord] = field(default_factory=list)
    usage: Dict[str, int] = field(default_factory=dict)
    error: str = ""


def _add_usage(total: Dict[str, int], part: Dict[str, int]) -> None:
    for key, value in (part or {}).items():
        if isinstance(value, int):
            total[key] = total.get(key, 0) + value


def _assistant_message(text: str, tool_calls: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "role": "assistant",
        "content": text or "",
        "tool_calls": [
            {
                "id": call.get("id") or "call_{}".format(idx),
                "type": "function",
                "function": {"name": call.get("name", ""), "arguments": call.get("arguments") or "{}"},
            }
            for idx, call in enumerate(tool_calls)
        ],
    }


def _run_tool(tool_map: Dict[str, Tool], call: Dict[str, Any]) -> Tuple[str, ToolCallRecord]:
    name = call.get("name", "")
    tool = tool_map.get(name)
    if tool is None:
        text = "未知工具：{}（可用：{}）".format(name, ", ".join(sorted(tool_map)) or "无")
        return text, ToolCallRecord(name=name, arguments=call.get("arguments", ""), ok=False,
                                   result_chars=len(text))
    text, ok = tool.run(call.get("arguments"))
    return text, ToolCallRecord(name=name, arguments=call.get("arguments", ""), ok=ok,
                               result_chars=len(text))


def _merge_tool_delta(pending: Dict[int, Dict[str, str]], event: Dict[str, Any]) -> None:
    slot = pending.setdefault(int(event.get("index") or 0),
                              {"id": "", "name": "", "arguments": ""})
    if event.get("id"):
        slot["id"] = str(event["id"])
    if event.get("name"):
        slot["name"] = str(event["name"])
    if event.get("arguments"):
        slot["arguments"] += str(event["arguments"])


def iter_agent_events(settings: llm.LLMSettings, messages: List[Dict[str, Any]],
                      tools: Optional[List[Tool]] = None,
                      max_rounds: int = 0,
                      should_stop: Optional[Any] = None) -> Iterator[Dict[str, Any]]:
    """流式跑完整个工具循环，逐事件产出（协议见模块 docstring）。

    should_stop：用户终止回调（见 harness.cancel）；命中时抛 Cancelled 给上层收尾。
    """
    tools = tools or []
    tool_map = {t.name: t for t in tools}
    schemas = openai_schemas(tools) if tools else None
    convo = list(messages)
    calls: List[ToolCallRecord] = []
    usage: Dict[str, int] = {}
    content_all: List[str] = []
    reasoning_all: List[str] = []
    rounds = 0
    tool_rounds = 0

    while True:
        if should_stop is not None and should_stop():
            raise Cancelled("已被用户终止")
        allow_tools = bool(schemas) and tool_rounds < max_rounds
        rounds += 1
        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        pending: Dict[int, Dict[str, str]] = {}
        try:
            for event in llm.stream_events(settings, convo,
                                           tools=schemas if allow_tools else None,
                                           should_stop=should_stop):
                kind = event.get("type")
                if kind == "delta":
                    content = event.get("content") or ""
                    reasoning = event.get("reasoning_content") or ""
                    if content:
                        content_parts.append(content)
                    if reasoning:
                        reasoning_parts.append(reasoning)
                    if content or reasoning:
                        yield event          # 原样透传（含 content 与 reasoning_content 两字段）
                elif kind == "tool_call":
                    _merge_tool_delta(pending, event)
                elif kind == "usage":
                    _add_usage(usage, event.get("usage") or {})
        except Cancelled:
            raise                    # 用户终止：交给上层收尾，不能当成普通错误帧
        except Exception as exc:  # noqa: BLE001  建连或流中断
            logger.warning("流式工具循环失败：%s", str(exc)[:300])
            yield {"type": "error", "message": str(exc)[:300]}
            return

        text = "".join(content_parts).strip()
        reasoning = "".join(reasoning_parts).strip()
        if text:
            content_all.append(text)
        if reasoning:
            reasoning_all.append(reasoning)

        round_calls = [pending[k] for k in sorted(pending)]
        if not (allow_tools and round_calls):
            yield {
                "type": "final",
                "content": text,
                "reasoning_content": reasoning,
                "rounds": rounds,
                "calls": calls,
                "usage": dict(usage),
                "full_content": _REASONING_SEPARATOR.join(content_all),
                "full_reasoning_content": _REASONING_SEPARATOR.join(reasoning_all),
            }
            return

        tool_rounds += 1
        convo.append(_assistant_message(text, round_calls))
        for call in round_calls:
            result_text, record = _run_tool(tool_map, call)
            calls.append(record)
            yield {"type": "tool", "name": record.name, "ok": record.ok,
                   "result_chars": record.result_chars}
            convo.append({"role": "tool", "tool_call_id": call.get("id") or "", "content": result_text})


def run_agent(settings: llm.LLMSettings, messages: List[Dict[str, Any]],
              tools: Optional[List[Tool]] = None, max_rounds: int = 0) -> AgentRun:
    """非流式封装：消费 iter_agent_events 并汇总（保留完整推理内容与真实 usage）。"""
    run = AgentRun()
    for event in iter_agent_events(settings, messages, tools=tools, max_rounds=max_rounds):
        kind = event.get("type")
        if kind == "tool":
            run.calls.append(ToolCallRecord(name=event.get("name", ""),
                                            ok=bool(event.get("ok")),
                                            result_chars=int(event.get("result_chars") or 0)))
        elif kind == "error":
            run.error = event.get("message", "调用失败")
            return run
        elif kind == "final":
            run.text = event.get("full_content") or event.get("content") or ""
            run.reasoning_content = event.get("full_reasoning_content") or ""
            run.rounds = int(event.get("rounds") or 0)
            run.usage = dict(event.get("usage") or {})
            return run
    if not run.error:
        run.error = "未收到最终结果（流意外结束）"
    return run
