"""Skill B：快速总结 —— 只读摘要/引言/结果/结论，产出 220 字内的核心 Insight。

取值策略：compact 模式（定章节 + 每节取首段与末段），无工具、单轮调用。
token 目标是精读的 1/6 以内，用于大批量筛选（例如每日 arXiv 扫读）。
"""

from .base import SkillSpec, register_skill

QUICK_SUMMARY = register_skill(SkillSpec(
    name="quick_summary",
    title="快速总结",
    description="只读摘要+引言+结果+结论，输出 6 行总结：核心 Insight、关键数字与精读建议（不设字数上限，但不写细节）。",
    prompt="quick_summary.md",
    context_mode="compact",
    context_tokens=None,          # 取 HARNESS.quick_context_tokens
    sections=("abstract", "introduction", "results", "conclusion"),
    per_section_paras=3,          # 每节取首段 + 末段（引言末段常是贡献列表）
    drop_sections=("references", "acknowledgements"),
    tools=(),                     # 不开放工具：速度优先，单轮出结果
    max_tool_rounds=0,
    output_format="markdown",
    usage_hint="每日扫读、批量筛选时用；只给 insight/贡献/结论层，细节留给精读。",
    task_line="请现在输出快速总结：严格按 system 中的 6 行结构与标签，只写 insight/贡献/结论层。",
))
