"""知识网络的 LLM 步骤：带 JSON 契约的结构化调用 + 稳健解析。

与 harness 的分工：harness 提供 LLM 底座与 prompt 加载；本模块只负责
"组装 messages -> 调用 -> 抠出 JSON -> 失败自修复重试"，prompt 统一放在 harness/prompts/。

为什么需要它：方向归类与关系抽取都要求严格 JSON（后续要写库），而模型常带解释文字、
代码块围栏或尾随逗号；这里用一个"解析失败就把错误回灌并要求只输出 JSON"的重试把成功率拉满，
并把原始输出、推理内容、usage、耗时一并回传（便于记进 pipeline 账本）。
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from harness import config as harness_config
from harness import llm
from harness.cancel import Cancelled
from harness.skills.base import load_prompt, render_prompt

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")
_REPAIR_HINT = "上一条输出不是合法 JSON。请只输出 JSON 本身，不要解释文字、不要代码块围栏。"


@dataclass
class StepOutput:
    """一次结构化调用的结果（ok=False 时看 error）。"""

    ok: bool
    data: Any = None
    text: str = ""
    reasoning: str = ""
    usage: Dict[str, int] = field(default_factory=dict)
    model: str = ""
    latency_ms: int = 0
    error: str = ""


def robust_json_load(text: str) -> Optional[Any]:
    """从模型输出中抠出 JSON：去围栏 -> 取首个开括号到末尾闭括号 -> 容忍尾随逗号。"""
    if not text:
        return None
    cleaned = _FENCE_RE.sub("", text.strip())
    positions = [(cleaned.find("{"), "{", "}"), (cleaned.find("["), "[", "]")]
    positions = [p for p in positions if p[0] >= 0]
    # 以最先出现的括号为准：`[{"a":1}]` 必须解析成列表而不是内层对象
    for _, open_ch, close_ch in sorted(positions):
        start = cleaned.find(open_ch)
        end = cleaned.rfind(close_ch)
        if start < 0 or end <= start:
            continue
        candidate = cleaned[start:end + 1]
        for attempt in (candidate, _TRAILING_COMMA_RE.sub(r"\1", candidate)):
            try:
                return json.loads(attempt)
            except ValueError:
                continue
    return None


def build_messages(prompt_name: str, user_text: str,
                   cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    """system = prompt 模板（规则 + 输出契约），user = 调用方组装的材料。"""
    cfg = cfg or harness_config.get_harness_config()
    system = render_prompt(load_prompt(prompt_name),
                           output_language=str(cfg.get("output_language") or "中文"))
    return [{"role": "system", "content": system},
            {"role": "user", "content": user_text}]


def call_structured(prompt_name: str, user_text: str, settings: llm.LLMSettings, *,
                    as_json: bool = True, cfg: Optional[Dict[str, Any]] = None,
                    json_retries: int = 2, should_stop: Optional[Any] = None) -> StepOutput:
    """结构化调用：as_json=True 时要求 JSON 并做解析自修复重试。

    should_stop（见 harness.cancel）在每次尝试前检查：用户已点终止就直接抛 Cancelled，
    不再发起重试（否则每次重试都在白烧 token）。
    """
    messages = build_messages(prompt_name, user_text, cfg)
    attempts = max(1, json_retries + 1) if as_json else 1
    last_error = ""

    for _ in range(attempts):
        if should_stop is not None and should_stop():
            raise Cancelled("已被用户终止")
        started = time.time()
        result = llm.complete(settings, messages)
        latency_ms = int((time.time() - started) * 1000)
        if result is None:
            last_error = "LLM 调用失败（重试耗尽）"
            continue
        if not as_json:
            return StepOutput(ok=True, text=result.text, reasoning=result.reasoning_content,
                              usage=dict(result.usage or {}), model=settings.model,
                              latency_ms=latency_ms)
        data = robust_json_load(result.text)
        if data is not None:
            return StepOutput(ok=True, data=data, text=result.text,
                              reasoning=result.reasoning_content,
                              usage=dict(result.usage or {}), model=settings.model,
                              latency_ms=latency_ms)
        last_error = "JSON 解析失败：{}".format((result.text or "")[:200])
        logger.warning("结构化输出解析失败，回灌纠错（%s）", prompt_name)
        messages.append({"role": "assistant", "content": result.text or ""})
        messages.append({"role": "user", "content": _REPAIR_HINT})

    return StepOutput(ok=False, error=last_error or "结构化调用失败")
