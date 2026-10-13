"""Skill A：深度精读 —— 全文入上下文，按五维框架产出学术解读报告。

取值策略：single-pass（一次性给全文）而非多轮取节。
原因是精读要覆盖全部 5 个维度，多轮工具调用会把已累积的上下文反复重发，
token 反而更高；只在正文超预算被裁剪时，才启用工具把省略部分取回（最多 1 轮）。
"""

from .base import SkillSpec, register_skill

DEEP_READING = register_skill(SkillSpec(
    name="deep_reading",
    title="深度精读",
    description="通读全文，按五维框架输出深度解读：论文背景/详细方法/详细实验/"
                "Insight 与价值/不足与未来展望。",
    prompt="deep_reading.md",
    context_mode="full",
    context_tokens=None,          # 取 HARNESS.skill_context_tokens
    drop_sections=("references", "acknowledgements"),
    tools=("read_section", "search_text"),
    max_tool_rounds=1,            # 正文已整篇入上下文；只在被裁剪时才允许取回一次
    output_format="markdown",
    usage_hint="需要完整掌握一篇论文时用；信息最全，token 消耗也最高。",
    task_line=("请现在输出这篇论文的深度解读报告：按 system 中的五个维度依次展开，"
               "直接输出正文，不要复述本指令。"),
))
