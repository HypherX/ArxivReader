"""ArxivReader Agent Harness。

AI 能力通过技能接入，Web 层（app/）不做 prompt 拼接：

  Skills 一次完整的 AI 能力 = Prompt 模板 + 上下文：harness/skills/

配套：
  harness/llm.py        LLM 调用底座（参数下发、usage 统计）
  harness/document.py   论文正文的结构化视图（分节、页码锚点、预算渲染、全文渲染）
  harness/agent.py      流式回复
  harness/runner.py     技能执行器（装配 messages -> 调用 -> 返回文本与用量统计）
"""

__all__ = [
    "agent",
    "config",
    "document",
    "llm",
    "runner",
    "skills",
    "tokens",
    "tools",
]
