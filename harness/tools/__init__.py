"""工具注册表。

阅读和对话把论文放进上下文；对话（chat 窗口）额外开放一组工具：
联网（arXiv 检索 / 网页抓取）与库内检索，工具声明与实现解耦。
"""

from . import web  # noqa: F401  导入即注册 fetch_web_page
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
