"""输出语言与提示词版本：无数据库依赖，任务开始时解析一次。"""
from typing import Any, Dict, Optional

PROMPT_VERSION = "2026-10-readable-v1"


def normalize_language(value: Any) -> str:
    text = str(value or "zh").strip().lower()
    return "en" if text in ("en", "english", "英文", "英语") else "zh"


def runtime_config(settings: Any, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    from .config import get_harness_config
    result = dict(get_harness_config())
    result.update(cfg or {})
    language = getattr(settings, "output_language", None)
    if language:
        result["output_language"] = language
    result["output_language"] = normalize_language(result.get("output_language"))
    return result


def choose(cfg: Dict[str, Any], zh: str, en: str) -> str:
    return en if normalize_language(cfg.get("output_language")) == "en" else zh
