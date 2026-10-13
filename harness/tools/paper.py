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
            return ctx.text("该论文没有可用的正文，只能依据标题与摘要作答。", "No full text is available; use only the title and abstract.")
        return ctx.document.outline_text(language=ctx.output_language)

    return Tool(
        name="get_outline",
        description=ctx.text("返回论文的章节清单（规范名/原标题/页码/规模），用于决定接下来读哪一节。",
                             "Return the paper outline with section keys, titles, pages and sizes."),
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )


@register_tool("read_section")
def _read_section(ctx: ToolContext) -> Tool:
    def handler(section: str = "", **_: Any) -> str:
        if not ctx.has_document:
            return ctx.text("该论文没有可用的正文。", "No full text available.")
        if not section:
            return ctx.text("缺少 section 参数。可用章节：{}", "Missing section. Available sections: {}").format(
                ", ".join(i["key"] for i in ctx.document.outline(ctx.output_language)))
        return ctx.document.section_text(section, max_tokens=8000, language=ctx.output_language)

    return Tool(
        name="read_section",
        description=ctx.text("读取指定章节原文与页码，使用规范名或原标题片段。",
                             "Read a section with source page markers. Use keys such as method, experiments, results, conclusion, references or part of its title."),
        parameters={
            "type": "object",
            "properties": {"section": {"type": "string", "description": ctx.text("章节名或原标题片段", "Section key or title fragment")}},
            "required": ["section"],
        },
        handler=handler,
    )


@register_tool("search_text")
def _search_text(ctx: ToolContext) -> Tool:
    def handler(query: str = "", **_: Any) -> str:
        if not ctx.has_document:
            return ctx.text("该论文没有可用的正文。", "No full text available.")
        if not query:
            return ctx.text("缺少 query 参数。", "Missing query.")
        return ctx.document.search(query, k=4, language=ctx.output_language)

    return Tool(
        name="search_text",
        description=ctx.text("在正文中检索关键词并返回页码片段，适合定位参数、数据、指标和定义。",
                             "Search full text for parameters, datasets, metrics or definitions. Return source excerpts with page markers; prefer the paper's original terminology."),
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "description": ctx.text("检索词（英文原词优先）", "Search terms in the source language")}},
            "required": ["query"],
        },
        handler=handler,
    )


@register_tool("read_page")
def _read_page(ctx: ToolContext) -> Tool:
    def handler(page: int = 0, span: int = 1, **_: Any) -> str:
        if not ctx.has_document:
            return ctx.text("该论文没有可用的正文。", "No full text available.")
        if not page:
            return ctx.text("缺少 page 参数。", "Missing page.")
        return ctx.document.read_page(int(page), span=int(span) if span else 1, language=ctx.output_language)

    return Tool(
        name="read_page",
        description=ctx.text("读取指定页（可带 span 连续多页）的原文，适合看表格、公式、脚注密集的页面。",
                             "Read source pages, including tables, formulas and footnotes. span specifies consecutive pages."),
        parameters={
            "type": "object",
            "properties": {
                "page": {"type": "integer", "description": ctx.text("页码，从 1 开始", "Page number, starting at 1")},
                "span": {"type": "integer", "description": ctx.text("连续读取页数，默认 1", "Number of consecutive pages, default 1")},
            },
            "required": ["page"],
        },
        handler=handler,
    )
