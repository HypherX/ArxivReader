"""工具注册表。

阅读和对话直接把论文放进上下文，当前不向模型开放工具。
技能若声明工具名，执行器仍通过这里按名装配。
"""

from .base import (  # noqa: F401
    Tool,
    ToolContext,
    build_tools,
    openai_schemas,
    register_tool,
    tool_names,
)

__all__ = [
    "Tool",
    "ToolContext",
    "build_tools",
    "openai_schemas",
    "register_tool",
    "tool_names",
]
