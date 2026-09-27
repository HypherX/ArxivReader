"""论文工具集：让模型"按需取内容"，而不是每轮请求都把全文背一遍。

四个工具覆盖了 LLM 领域论文精读的全部取数需求，且彼此不重叠：
  get_outline    章节清单（先看骨架，决定读哪里）
  read_section   整节原文（方法/实验细节）
  search_text    关键词检索片段（定位某个名词、超参、数字）
  read_page      指定页原文（表格、公式、脚注密集处）

上下文成本：四个工具的 schema 合计约 300 token，且只在技能/对话开启工具时随请求下发；
换来的是"首轮只花 1~2k token 看骨架"，避免长期为 30k+ token 的全文反复付费。
"""

from typing import Any, Dict

from .base import Tool, ToolContext, register_tool


@register_tool("get_outline")
def _get_outline(ctx: ToolContext) -> Tool:
    def handler(**_: Any) -> str:
        if not ctx.has_document:
            return "该论文没有可用的正文，只能依据标题与摘要作答。"
        return ctx.document.outline_text()

    return Tool(
        name="get_outline",
        description="返回论文的章节清单（规范名/原标题/页码/规模），用于决定接下来读哪一节。",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )


@register_tool("read_section")
def _read_section(ctx: ToolContext) -> Tool:
    def handler(section: str = "", **_: Any) -> str:
        if not ctx.has_document:
            return "该论文没有可用的正文。"
        if not section:
            return "缺少 section 参数。可用章节：{}".format(
                "、".join(i["key"] for i in ctx.document.outline()))
        return ctx.document.section_text(section, max_tokens=8000)

    return Tool(
        name="read_section",
        description="读取指定章节的原文（带 [p.N] 页码锚点）。section 可用规范名（abstract/method/"
                    "experiments/results/analysis/conclusion/related_work/appendix 等）或原标题片段。",
        parameters={
            "type": "object",
            "properties": {"section": {"type": "string", "description": "章节名或原标题片段"}},
            "required": ["section"],
        },
        handler=handler,
    )


@register_tool("search_text")
def _search_text(ctx: ToolContext) -> Tool:
    def handler(query: str = "", **_: Any) -> str:
        if not ctx.has_document:
            return "该论文没有可用的正文。"
        if not query:
            return "缺少 query 参数。"
        return ctx.document.search(query, k=4)

    return Tool(
        name="search_text",
        description="在正文中检索关键词，返回含 [p.N] 页码的原文片段。适合定位超参、数据集名、"
                    "指标数值、术语定义。query 尽量用论文里的英文原词。",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "检索词（英文原词优先）"}},
            "required": ["query"],
        },
        handler=handler,
    )


@register_tool("read_page")
def _read_page(ctx: ToolContext) -> Tool:
    def handler(page: int = 0, span: int = 1, **_: Any) -> str:
        if not ctx.has_document:
            return "该论文没有可用的正文。"
        if not page:
            return "缺少 page 参数。"
        return ctx.document.read_page(int(page), span=int(span) if span else 1)

    return Tool(
        name="read_page",
        description="读取指定页（可带 span 连续多页）的原文，适合看表格、公式、脚注密集的页面。",
        parameters={
            "type": "object",
            "properties": {
                "page": {"type": "integer", "description": "页码，从 1 开始"},
                "span": {"type": "integer", "description": "连续读取页数，默认 1"},
            },
            "required": ["page"],
        },
        handler=handler,
    )
