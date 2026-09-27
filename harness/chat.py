"""多轮对话（论文问答）：对话与技能共用同一套"prompt + 上下文预算 + 工具"三件套。

与技能的差别只有三点：
  1. 技能是一次性任务，对话要带上多轮历史；
  2. 对话默认流式输出（SSE 逐字返回）；
  3. 对话允许在回答前用工具取证（function calling），因此适合"细节追问"。

上下文策略（HARNESS.chat_context_tokens）：
  >0  压缩全文入 system（默认，质量优先；相比旧版"全文直塞"已剔除参考文献与版式噪声）
  0   摘要 + 章节大纲入 system，正文全部靠工具按需取（token 最省，适合长对话）

历史裁剪：只带最近 chat_history_messages 条（默认 12），避免长会话线性膨胀。
"""

from typing import Any, Dict, List, Optional, Tuple

from . import config as harness_config
from . import tokens
from .paper import PaperInput
from .skills.base import SkillSpec, load_prompt, render_prompt
from .tools import Tool, ToolContext, build_tools

# 对话不是"一次成型的产物"，因此不进技能注册表（避免出现在 /api/skills 列表里），
# 但仍以 SkillSpec 声明，保证与技能同样的可配置口径。
CHAT_SKILL = SkillSpec(
    name="chat_qa",
    title="论文问答",
    description="针对当前论文的多轮问答，回答前可按需调用工具取证。",
    prompt="chat_qa.md",
    context_mode="full",
    tools=("search_text", "read_section", "read_page", "get_outline"),
    max_tool_rounds=3,
    output_format="markdown",
    usage_hint="追问细节、核对数字、验证理解时用。",
)

_HISTORY_LIMIT_DEFAULT = 12
_CONTEXT_HEADER = "## 论文材料（`[p.N]` 为原 PDF 页码；`###` 为章节标题）"


def history_limit(cfg: Optional[Dict[str, Any]] = None) -> int:
    cfg = cfg or harness_config.get_harness_config()
    try:
        return max(2, int(cfg.get("chat_history_messages") or _HISTORY_LIMIT_DEFAULT))
    except (TypeError, ValueError):
        return _HISTORY_LIMIT_DEFAULT


def _paper_block(paper: PaperInput, cfg: Dict[str, Any]) -> str:
    """按预算渲染对话用的论文材料：全文 or 摘要 + 大纲。"""
    doc = paper.document()
    budget = int(cfg.get("chat_context_tokens") or 0)
    if budget > 0 and paper.has_body:
        rendered = doc.render(tokens_budget=budget)
        if rendered.text:
            block = "{}\n\n{}".format(_CONTEXT_HEADER, rendered.text)
            if rendered.note:
                block += "\n\n" + rendered.note
            return block
    outline = doc.outline_text() if paper.has_body else "（无可用正文）"
    return ("{}\n\n## 章节大纲（正文未预置，需要哪部分请用工具取回）\n\n{}"
            .format(_CONTEXT_HEADER, outline))


def build_chat_messages(paper: Any, history: List[Dict[str, str]], question: str, *,
                        cfg: Optional[Dict[str, Any]] = None
                        ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """装配对话请求：system（规则 + 论文材料）+ 裁剪后的历史 + 当前提问。"""
    cfg = cfg or harness_config.get_harness_config()
    paper_input = paper if isinstance(paper, PaperInput) else PaperInput.from_any(paper)
    output_language = str(cfg.get("output_language") or "中文")

    system_prompt = render_prompt(load_prompt(CHAT_SKILL.prompt), output_language=output_language)
    system = "{}\n\n{}".format(system_prompt.strip(), paper_input.meta_block() + "\n\n"
                              + _paper_block(paper_input, cfg))

    limit = history_limit(cfg)
    kept = [m for m in (history or []) if m.get("role") in ("user", "assistant")][-limit:]
    messages: List[Dict[str, Any]] = [{"role": "system", "content": system}]
    messages.extend({"role": m["role"], "content": str(m.get("content") or "")} for m in kept)
    messages.append({"role": "user", "content": question})

    info = {
        "context_tokens_est": tokens.estimate_tokens(system),
        "history_messages": len(kept),
        "history_dropped": max(0, len(history or []) - len(kept)),
        "system_chars": len(system),
    }
    return messages, info


def build_chat_tools(paper: Any, cfg: Optional[Dict[str, Any]] = None) -> List[Tool]:
    """对话可用的工具（无正文或开关关闭时返回空列表）。"""
    cfg = cfg or harness_config.get_harness_config()
    if not cfg.get("chat_tools", True):
        return []
    paper_input = paper if isinstance(paper, PaperInput) else PaperInput.from_any(paper)
    if not paper_input.has_body:
        return []
    ctx = ToolContext(paper=paper_input, document=paper_input.document())
    return build_tools(ctx, list(CHAT_SKILL.tools))


def max_tool_rounds(cfg: Optional[Dict[str, Any]] = None) -> int:
    cfg = cfg or harness_config.get_harness_config()
    try:
        return max(0, int(cfg.get("max_tool_rounds") or 0))
    except (TypeError, ValueError):
        return 0
