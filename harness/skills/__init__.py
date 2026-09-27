"""技能注册入口：导入即完成注册（新增技能在此 import 一次即可）。"""

from . import deep_reading, quick_summary  # noqa: F401  导入即注册
from .base import (  # noqa: F401
    SkillSpec,
    get_skill,
    list_skills,
    load_prompt,
    register_skill,
    render_prompt,
    render_prompt_file,
    skill_names,
)

__all__ = [
    "SkillSpec",
    "get_skill",
    "list_skills",
    "load_prompt",
    "register_skill",
    "render_prompt",
    "render_prompt_file",
    "skill_names",
]
