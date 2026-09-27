"""技能底座：SkillSpec 声明 + 注册表 + prompt 模板加载。

一个"技能"= 一次完整的 AI 能力，由四件事定义清楚，全部可声明、可复用、可 A/B：

  1. prompt        提示词模板（harness/prompts/*.md，纯文本，便于迭代对比）
  2. context       送入模型的论文上下文如何取（全文/紧凑/仅大纲 + token 预算）
  3. tools         允许调用的工具白名单（function calling，不写进 prompt）
  4. output        输出契约（格式约束写在 prompt 里，这里只声明格式类型）

新增技能 = 新建一个 SkillSpec + 一个 prompt 文件，无需改 Web 层与 LLM 层。
"""

import logging
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from .. import config as harness_config

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


@dataclass(frozen=True)
class SkillSpec:
    """技能声明（不可变）：prompt 模板 + 上下文预算 + 工具白名单的一次性描述。"""

    name: str                                   # 技能标识（API 路径用）
    title: str                                  # 中文名
    description: str                            # 一句话说明（API 列表用）
    prompt: str                                 # prompts/ 下的文件名（可含 .md）
    context_mode: str = "full"                  # full | compact | outline
    context_tokens: Optional[int] = None        # None -> 取 HARNESS.skill_context_tokens
    sections: Tuple[str, ...] = ()              # compact 模式下要取的章节
    per_section_paras: Optional[int] = None     # compact 模式每节保留段数（首段 + 末段）
    drop_sections: Tuple[str, ...] = ("references", "acknowledgements", "future_work")
    tools: Tuple[str, ...] = ()                 # 工具白名单（按名构建）
    max_tool_rounds: int = 0                    # 0 = 不给模型调用工具的机会
    output_format: str = "markdown"
    usage_hint: str = ""                        # 何时用它（展示给用户/前端）
    task_line: str = ""                         # 放在 user 消息末尾的执行指令（一句，尽量短）
    # 可选：只对本技能覆盖推理强度（low/medium/high/max）；None = 用全局设置。
    # 用途：精读这类长输出任务若因 max 推理过慢，可只给它降级，而不影响其他技能。
    reasoning_effort: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "context_mode": self.context_mode,
            "context_tokens": self.context_tokens,
            "tools": list(self.tools),
            "max_tool_rounds": self.max_tool_rounds,
            "output_format": self.output_format,
            "reasoning_effort": self.reasoning_effort,
            "usage_hint": self.usage_hint,
        }


_SKILLS: Dict[str, SkillSpec] = {}


def register_skill(spec: SkillSpec) -> SkillSpec:
    """注册技能（模块导入时调用）。"""
    if spec.name in _SKILLS:
        raise ValueError("技能名重复注册：{}".format(spec.name))
    _SKILLS[spec.name] = spec
    logger.debug("注册技能：%s", spec.name)
    return spec


def get_skill(name: str) -> SkillSpec:
    spec = _SKILLS.get((name or "").strip())
    if spec is None:
        raise KeyError("未知技能：{!r}（可用：{}）".format(name, ", ".join(sorted(_SKILLS))))
    return spec


def list_skills() -> List[SkillSpec]:
    return [_SKILLS[k] for k in sorted(_SKILLS)]


def skill_names() -> List[str]:
    return sorted(_SKILLS)


# ---------------------------------------------------------------- prompt 模板
@lru_cache(maxsize=32)
def load_prompt(name: str) -> str:
    """读取 prompt 模板。name 可为 prompts/ 下的文件名，或任意绝对/相对路径。

    进程内缓存：服务运行时改 prompt 需重启（`uvicorn --reload` 会自动重载）；
    实验台每次都是新进程，因此 A/B 迭代不受影响。
    """
    path = name if os.path.isabs(name) or os.sep in name else harness_config.get_prompt_path(name)
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def render_prompt(template: str, **values: Any) -> str:
    """替换模板中的 {{placeholder}}；未提供值的占位符原样保留（便于发现漏填）。"""
    def _sub(match: "re.Match[str]") -> str:
        key = match.group(1)
        if key not in values or values[key] is None:
            return match.group(0)
        return str(values[key])

    return _PLACEHOLDER_RE.sub(_sub, template)


def render_prompt_file(name: str, **values: Any) -> str:
    return render_prompt(load_prompt(name), **values)
