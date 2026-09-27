"""技能执行器：把"技能声明 + 论文 + LLM 设置"跑成一次结果，并留下完整 token 账本。

职责边界：
  - 只做"装配 + 调用 + 记账"，不含任何业务规则（规则在 SkillSpec 与 prompt 文件里）；
  - 上下文（meta + 正文）只在这里拼一次，禁止各调用点各自拼 prompt —— 这是 Web 层零 prompt 的保证；
  - 内部统一走 harness.agent 的流式事件循环，因此**推理内容（reasoning_content）与真实
    usage 都能拿到**，再由下面两个入口分别对外：
      iter_skill_events  流式（事件级，供 SSE 直连与推理轨迹落盘）
      run_skill          汇总式（供 HTTP 非流式接口与后台 pipeline）
"""

import time
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

from . import config as harness_config
from . import tokens
from .agent import iter_agent_events
from .paper import PaperInput
from .skills.base import SkillSpec, get_skill, load_prompt, render_prompt
from .tools import ToolContext, build_tools, openai_schemas

_CONTEXT_HEADER = "## 论文正文（按预算整理，`[p.N]` 为原 PDF 页码；`###` 为章节标题）"


@dataclass
class SkillResult:
    """一次技能调用的结果。"""

    skill: str
    output: str = ""
    reasoning_content: str = ""
    stats: Dict[str, Any] = field(default_factory=dict)
    error: str = ""


def resolve_budget(spec: SkillSpec, cfg: Optional[Dict[str, Any]] = None) -> int:
    """确定本次调用的上下文 token 预算（技能显式值 > 全局默认；紧凑模式有独立默认）。"""
    cfg = cfg or harness_config.get_harness_config()
    if spec.context_tokens:
        return int(spec.context_tokens)
    if spec.context_mode == "compact":
        return int(cfg.get("quick_context_tokens") or 3000)
    if spec.context_mode == "outline":
        return 0
    return int(cfg.get("skill_context_tokens") or 40000)


def settings_for_skill(spec: SkillSpec, settings: Any) -> Any:
    """技能可对推理强度做局部覆盖（未声明则沿用全局设置）。"""
    effort = getattr(spec, "reasoning_effort", None)
    current = getattr(settings, "reasoning_effort", None)
    if effort and effort != current:
        return replace(settings, reasoning_effort=effort)
    return settings


def build_skill_messages(spec: SkillSpec, paper: PaperInput, *,
                         cfg: Optional[Dict[str, Any]] = None,
                         budget: Optional[int] = None
                         ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """装配一次技能调用的 messages，并返回本次上下文账本。"""
    cfg = cfg or harness_config.get_harness_config()
    budget = resolve_budget(spec, cfg) if budget is None else int(budget)
    output_language = str(cfg.get("output_language") or "中文")

    system = render_prompt(load_prompt(spec.prompt), output_language=output_language)

    doc = paper.document()
    rendered = doc.render(tokens_budget=budget, drop=spec.drop_sections,
                          sections=spec.sections or None,
                          per_section_paras=spec.per_section_paras)

    parts = [paper.meta_block()]
    if rendered.text:
        parts.append("{}\n\n{}".format(_CONTEXT_HEADER, rendered.text))
    else:
        parts.append("## 论文正文\n\n（无可用正文，请仅依据元信息作答，并说明正文缺失。）")
    if rendered.note:
        parts.append(rendered.note)
    if spec.task_line:
        parts.append(spec.task_line)

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(parts)},
    ]
    info = {
        "context_mode": spec.context_mode,
        "context_budget_tokens": budget,
        "context_tokens_est": tokens.estimate_tokens(rendered.text),
        "context_sections": rendered.sections,
        "context_omitted": ["{}≈{}t".format(k, t) for k, t in rendered.omitted],
        "context_truncated": rendered.truncated,
        "context_note": rendered.note,
        "system_chars": len(system),
        "user_chars": len(messages[1]["content"]),
    }
    return messages, info


def _build_stats(info: Dict[str, Any], doc: Any, tools: List[Any], messages: List[Dict[str, Any]], *,
                 rounds: int, calls: List[Any], usage: Dict[str, int],
                 text: str, reasoning: str, latency_ms: int, settings: Any) -> Dict[str, Any]:
    """统一的 token 账本：估算值（预算视角）+ API 真实值（计费视角）。"""
    schema_tokens = tokens.estimate_tools(openai_schemas(tools)) if tools else 0
    stats: Dict[str, Any] = dict(info)
    stats.update({
        "prompt_tokens_est": tokens.estimate_messages(messages) + schema_tokens,
        "tool_schema_tokens_est": schema_tokens,
        "tools": [getattr(t, "name", "") for t in tools],
        "llm_rounds": rounds,
        "tool_calls": [c if isinstance(c, dict) else asdict(c) for c in calls],
        "usage": dict(usage or {}),
        "model": getattr(settings, "model", ""),
        "reasoning_effort": getattr(settings, "reasoning_effort", None),
        "output_chars": len(text or ""),
        "output_tokens_est": tokens.estimate_tokens(text or ""),
        "reasoning_chars": len(reasoning or ""),
        "reasoning_tokens_est": tokens.estimate_tokens(reasoning or ""),
        "latency_ms": latency_ms,
        "doc": doc.stats(),
    })
    return stats


def _prepare(spec: SkillSpec, paper: Any, settings: Any, cfg: Dict[str, Any],
             budget: Optional[int]) -> Dict[str, Any]:
    """技能调用的公共准备：messages / 工具 / 上下文统计 / 覆盖后的设置。"""
    paper_input = paper if isinstance(paper, PaperInput) else PaperInput.from_any(paper)
    doc = paper_input.document()
    messages, info = build_skill_messages(spec, paper_input, cfg=cfg, budget=budget)
    tools = build_tools(ToolContext(paper=paper_input, document=doc), list(spec.tools))
    if not paper_input.has_body:
        tools = []      # 无正文时不给工具（也避免模型空转）
    return {
        "paper": paper_input,
        "doc": doc,
        "messages": messages,
        "info": info,
        "tools": tools,
        "settings": settings_for_skill(spec, settings),
    }


def iter_skill_events(skill: Union[str, SkillSpec], paper: Any, settings: Any, *,
                      cfg: Optional[Dict[str, Any]] = None,
                      budget: Optional[int] = None,
                      should_stop: Optional[Any] = None) -> Iterator[Dict[str, Any]]:
    """流式执行技能：原样透传 agent 事件，最后补一个带 output/stats 的 final 事件。

    事件序列：delta* (tool*) -> final | error；should_stop（见 harness.cancel）
    命中时向上抛 Cancelled，由 SSE 层收尾（本函数不吞掉它）。
    """
    cfg = cfg or harness_config.get_harness_config()
    spec = skill if isinstance(skill, SkillSpec) else get_skill(skill)
    ctx = _prepare(spec, paper, settings, cfg, budget)
    started = time.time()

    for event in iter_agent_events(ctx["settings"], ctx["messages"],
                                   tools=ctx["tools"], max_rounds=spec.max_tool_rounds,
                                   should_stop=should_stop):
        kind = event.get("type")
        if kind in ("delta", "tool"):
            yield event
        elif kind == "error":
            yield event
            return
        elif kind == "final":
            latency_ms = int((time.time() - started) * 1000)
            output = event.get("full_content") or event.get("content") or ""
            reasoning = event.get("full_reasoning_content") or ""
            stats = _build_stats(ctx["info"], ctx["doc"], ctx["tools"], ctx["messages"],
                                 rounds=int(event.get("rounds") or 0),
                                 calls=list(event.get("calls") or []),
                                 usage=event.get("usage") or {},
                                 text=output, reasoning=reasoning,
                                 latency_ms=latency_ms, settings=ctx["settings"])
            yield {"type": "final", "skill": spec.name, "output": output,
                   "reasoning_content": reasoning, "stats": stats}
            return


def run_skill(skill: Union[str, SkillSpec], paper: Any, settings: Any, *,
              cfg: Optional[Dict[str, Any]] = None,
              budget: Optional[int] = None,
              should_stop: Optional[Any] = None) -> SkillResult:
    """执行一次技能并汇总结果。paper 可以是 ORM 对象、dict 或 PaperInput。"""
    spec = skill if isinstance(skill, SkillSpec) else get_skill(skill)
    result = SkillResult(skill=spec.name)
    for event in iter_skill_events(spec, paper, settings, cfg=cfg, budget=budget,
                                   should_stop=should_stop):
        kind = event.get("type")
        if kind == "error":
            result.error = event.get("message", "调用失败")
            return result
        if kind == "final":
            result.output = event.get("output") or ""
            result.reasoning_content = event.get("reasoning_content") or ""
            result.stats = event.get("stats") or {}
            return result
    if not result.error:
        result.error = "未收到最终结果（流意外结束）"
    return result
