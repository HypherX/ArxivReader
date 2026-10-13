"""ArxivReader Agent Harness。

所有 AI 能力只通过两条通道接入，Web 层（app/）不做任何 prompt 拼接：

  Tools  确定性、零 LLM 的能力（检索、取节、取页、大纲）：harness/tools/
  Skills 一次完整的"AI 能力" = Prompt 模板 + 上下文预算 + 工具白名单：harness/skills/

配套：
  harness/llm.py        LLM 调用底座（参数下发口径、function calling、usage 统计）
  harness/document.py   论文正文的结构化视图（分节、页码锚点、检索、预算渲染）
  harness/agent.py      工具调用循环（function calling，非流式/流式两套）
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
