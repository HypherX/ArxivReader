"""多轮对话（论文问答）：整篇 PDF（含附录）进上下文 + agent 工具调用。

工具策略：
  - 普通对话窗口（chat）开放工具：联网（arXiv 检索 / 论文查证 / 网页抓取）与
    库内检索 / 关联图谱 / 本文定位，模型按需调用，最多 max_tool_rounds 轮；
  - 快速总结 / 深度精读的追问窗口不开放工具（它们的 prompt 约定只依据
    论文全文与该份产物回答）——由路由层按 scope 决定是否传 tools。
历史只保留最近 chat_history_messages 条（默认 12）。
"""

from typing import Any, Dict, List, Optional, Tuple

from . import config as harness_config
from . import tokens
from .language import choose, normalize_language, PROMPT_VERSION
from .paper import PaperInput
from .skills.base import SkillSpec, load_prompt, render_prompt
from .tools import Tool, ToolContext, build_tools

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


def build_followup_messages(paper: Any, history: List[Dict[str, str]], question: str,
                            artifact_md: str, *, scope: str,
                            cfg: Optional[Dict[str, Any]] = None
                            ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """在全文之外放进当前这份总结或精读。历史只来自调用方传入的这一页。"""
    cfg = cfg or harness_config.get_harness_config()
    messages, info = build_chat_messages(paper, history, question, cfg=cfg)
    if scope == "deep_reading":
        header = choose(cfg, "## 当前窗口：深度精读", "## This window: deep reading")
        note = choose(cfg, "读者正在就下面这份精读继续追问。只依据论文全文和这份精读回答，不要引用其他窗口里的提问或回复。",
                      "The reader is asking about the deep reading below. Use only the full PDF and this note. Do not use questions or replies from any other window.")
    else:
        header = choose(cfg, "## 当前窗口：快速总结", "## This window: quick summary")
        note = choose(cfg, "读者正在就下面这份总结继续追问。只依据论文全文和这份总结回答，不要引用其他窗口里的提问或回复。",
                      "The reader is asking about the summary below. Use only the full PDF and this summary. Do not use questions or replies from any other window.")
    messages[0]["content"] = "{}\n\n{}\n\n{}\n\n{}".format(
        messages[0]["content"].rstrip(), header, note, (artifact_md or "").strip())
    info["followup_scope"] = scope
    info["context_tokens_est"] = tokens.estimate_tokens(messages[0]["content"])
    return messages, info


def build_chat_tools(paper: Any, cfg: Optional[Dict[str, Any]] = None) -> List[Tool]:
    """对话窗口的 agent 工具：联网检索 / 网页抓取 / 库内检索 / 关联图谱 / 本文定位。

    cfg["chat_tools"]=False 可整体关闭（回到纯全文直读模式）。
    """
    cfg = cfg or harness_config.get_harness_config()
    if cfg.get("chat_tools") is False:
        return []
    paper_input = paper if isinstance(paper, PaperInput) else PaperInput.from_any(paper)
    ctx = ToolContext(paper=paper_input, document=paper_input.document(),
                      output_language=normalize_language(cfg.get("output_language")))
    return build_tools(ctx, list(CHAT_TOOL_NAMES))


def max_tool_rounds(cfg: Optional[Dict[str, Any]] = None) -> int:
    cfg = cfg or harness_config.get_harness_config()
    try:
        return max(0, int(cfg.get("max_tool_rounds") or 0))
    except (TypeError, ValueError):
        return 0


# 对话可用工具名（实现分别在 harness.tools.web 与 app.chat_tools 注册）
CHAT_TOOL_NAMES = ("search_arxiv", "get_arxiv_paper", "fetch_web_page",
                   "search_library", "related_papers", "find_in_paper")
