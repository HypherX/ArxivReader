"""多轮对话（论文问答）：把整篇 PDF（含附录）放进上下文，由模型直接回答。

不提供工具调用。历史只保留最近 chat_history_messages 条（默认 12）。
"""

from typing import Any, Dict, List, Optional, Tuple

from . import config as harness_config
from . import tokens
from .language import choose, normalize_language, PROMPT_VERSION
from .paper import PaperInput
from .skills.base import SkillSpec, load_prompt, render_prompt
from .tools import Tool

# 对话不是"一次成型的产物"，因此不进技能注册表（避免出现在 /api/skills 列表里），
# 但仍以 SkillSpec 声明，保证与技能同样的可配置口径。
CHAT_SKILL = SkillSpec(
    name="chat_qa",
    title="论文问答",
    description="针对当前论文的多轮问答，依据全文（含附录）直接回答。",
    prompt="chat_qa.md",
    context_mode="complete",
    tools=(),
    max_tool_rounds=0,
    output_format="markdown",
    usage_hint="追问细节、核对数字、验证理解时用。",
)

_HISTORY_LIMIT_DEFAULT = 12


def history_limit(cfg: Optional[Dict[str, Any]] = None) -> int:
    cfg = cfg or harness_config.get_harness_config()
    try:
        return max(2, int(cfg.get("chat_history_messages") or _HISTORY_LIMIT_DEFAULT))
    except (TypeError, ValueError):
        return _HISTORY_LIMIT_DEFAULT


def _paper_block(paper: PaperInput, cfg: Dict[str, Any]) -> str:
    """对话使用整篇 PDF，包括附录，不按预算截掉后部。"""
    language = normalize_language(cfg.get("output_language"))
    doc = paper.document()
    if paper.has_body:
        rendered = doc.render_complete(language=language)
        if rendered.text:
            return "{}\n\n{}".format(
                choose(cfg, "## 论文全文（含正文与附录；`[p.N]` 为原 PDF 页码）",
                       "## Full PDF text, including appendices (`[p.N]` marks the source page)"),
                rendered.text)
    return choose(cfg, "## 论文全文\n\n（没有可用的 PDF 文本）", "## Full PDF text\n\n(No PDF text is available)")


def build_chat_messages(paper: Any, history: List[Dict[str, str]], question: str, *,
                        cfg: Optional[Dict[str, Any]] = None
                        ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """装配对话请求：system（规则 + 论文材料）+ 裁剪后的历史 + 当前提问。"""
    cfg = cfg or harness_config.get_harness_config()
    paper_input = paper if isinstance(paper, PaperInput) else PaperInput.from_any(paper)
    output_language = normalize_language(cfg.get("output_language"))

    system_prompt = render_prompt(load_prompt(CHAT_SKILL.prompt, output_language), output_language=output_language)
    system = "{}\n\n{}".format(system_prompt.strip(), paper_input.meta_block(output_language) + "\n\n"
                              + _paper_block(paper_input, cfg))

    limit = history_limit(cfg)
    kept = [m for m in (history or []) if m.get("role") in ("user", "assistant")][-limit:]
    messages: List[Dict[str, Any]] = [{"role": "system", "content": system}]
    messages.extend({"role": m["role"], "content": str(m.get("content") or "")} for m in kept)
    messages.append({"role": "user", "content": question})

    info = {
        "output_language": output_language,
        "prompt_version": PROMPT_VERSION,
        "source_coverage": "full_text" if paper_input.has_body else "abstract_only",
        "context_tokens_est": tokens.estimate_tokens(system),
        "history_messages": len(kept),
        "history_dropped": max(0, len(history or []) - len(kept)),
        "system_chars": len(system),
    }
    return messages, info


def build_chat_tools(paper: Any, cfg: Optional[Dict[str, Any]] = None) -> List[Tool]:
    """对话不使用工具。论文内容已经整篇放进上下文。"""
    return []


def max_tool_rounds(cfg: Optional[Dict[str, Any]] = None) -> int:
    return 0
