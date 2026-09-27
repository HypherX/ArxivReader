"""工具底座：Tool 数据结构 + 工厂注册表 + OpenAI function calling schema 生成。

设计要点：
  - 工具是**确定性、零 LLM** 的能力，只做"取数据/算结果"，不含任何 prompt；
  - 每个工具用工厂函数注册（工厂接收 ToolContext，返回绑定好数据的 Tool），
    这样同一套工具既能服务单篇论文，也能扩展到多篇对比等场景；
  - 技能（skills）只声明自己要用哪些工具名，运行器按名构建，工具与技能互不硬编码；
  - 工具返回值一律为字符串：模型可直接读，且便于统一做 token 上限保护。
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 单次工具返回值上限（字符）。超出即截断，防止一次检索把上下文顶爆。
MAX_RESULT_CHARS = 12000


@dataclass
class ToolContext:
    """工具运行上下文：当前论文的结构化视图与原始输入。"""

    paper: Any = None                 # harness.paper.PaperInput
    document: Any = None              # harness.document.PaperDocument

    @property
    def has_document(self) -> bool:
        return self.document is not None and bool(getattr(self.document, "blocks", None))


@dataclass
class Tool:
    """一个可被模型调用的工具。"""

    name: str
    description: str
    parameters: Dict[str, Any]
    handler: Callable[..., str]
    max_result_chars: int = MAX_RESULT_CHARS

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def run(self, arguments: Any) -> Tuple[str, bool]:
        """执行工具；返回 (结果文本, 是否成功)。参数解析/执行异常都转成可读文本。"""
        if isinstance(arguments, str):
            raw = arguments.strip()
            if raw:
                try:
                    args = json.loads(raw)
                except ValueError:
                    return ("参数不是合法 JSON：{}".format(raw[:200]), False)
            else:
                args = {}
        elif isinstance(arguments, dict):
            args = arguments
        else:
            args = {}
        if not isinstance(args, dict):
            return ("参数必须是 JSON 对象。", False)
        try:
            result = self.handler(**args)
        except TypeError as exc:              # 参数名/类型不符
            return ("参数不匹配：{}".format(str(exc)[:200]), False)
        except Exception as exc:              # noqa: BLE001  工具内部异常不影响主流程
            logger.warning("工具 %s 执行失败：%s", self.name, str(exc)[:300])
            return ("工具执行失败：{}".format(str(exc)[:200]), False)
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        if len(text) > self.max_result_chars:
            text = text[:self.max_result_chars] + "\n...(结果过长已截断)"
        return (text, True)


# ---------------------------------------------------------------- 工厂注册表
_FACTORIES: Dict[str, Callable[[ToolContext], Tool]] = {}


def register_tool(name: str) -> Callable[[Callable[[ToolContext], Tool]], Callable[[ToolContext], Tool]]:
    """注册工具工厂：函数签名 (ToolContext) -> Tool。"""

    def decorator(factory: Callable[[ToolContext], Tool]) -> Callable[[ToolContext], Tool]:
        if name in _FACTORIES:
            raise ValueError("工具名重复注册：{}".format(name))
        _FACTORIES[name] = factory
        return factory

    return decorator


def tool_names() -> List[str]:
    return sorted(_FACTORIES)


def build_tools(ctx: ToolContext, names: Optional[List[str]] = None) -> List[Tool]:
    """按名构建工具；未注册的名字直接跳过（技能声明与实现解耦，向后兼容）。

    无正文（抽取失败）时不暴露任何工具：此时工具只会浪费 schema token。
    """
    if not ctx.has_document:
        return []
    out: List[Tool] = []
    for name in (names if names is not None else tool_names()):
        factory = _FACTORIES.get(name)
        if factory is None:
            logger.warning("技能声明了未注册的工具：%s", name)
            continue
        out.append(factory(ctx))
    return out


def openai_schemas(tools: List[Tool]) -> List[Dict[str, Any]]:
    return [t.schema() for t in tools]
