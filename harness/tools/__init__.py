"""工具层：确定性、零 LLM 的能力集合。

导入本包即完成所有工具的注册（各模块顶部用 @register_tool 装饰工厂函数）。
新增工具只需在本目录加一个模块并在下方 import，技能侧用工具名声明即可使用。
"""

from . import paper  # noqa: F401  导入即注册论文工具
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
