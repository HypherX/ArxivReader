"""Skill A：深度精读 —— 整篇 PDF（含附录）入上下文，按五维框架产出学术解读报告。

不使用工具调用。附录和正文一起放进同一次请求，模型直接根据全文回答。
"""

from .base import SkillSpec, register_skill

DEEP_READING = register_skill(SkillSpec(
    name="deep_reading",
    title="深度精读",
    description="通读全文，按五维框架输出深度解读：论文背景/详细方法/详细实验/"
                "Insight 与价值/不足与未来展望。",
    prompt="deep_reading.md",
    context_mode="complete",
    context_tokens=None,
    drop_sections=(),
    tools=(),
    max_tool_rounds=0,
    output_format="markdown",
    usage_hint="需要完整掌握一篇论文时用；信息最全，token 消耗也最高。",
    task_line=("请现在输出这篇论文的深度解读报告：按 system 中的五个维度依次展开，"
               "直接输出正文，不要复述本指令。"),
))
