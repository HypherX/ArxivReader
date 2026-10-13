"""应用内部默认值和路径；模型信息仅由应用设置提供，不加载用户代码。"""

import os
import sys
from typing import Any, Dict, Optional

# harness/ 的上一级即项目根 paper_reader/
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")

_DEFAULT_LLM: Dict[str, Any] = {
    # 首次启动保持未配置状态，用户只需在设置中填写。
    "base_url": "",
    "api_key": "",
    "model": "",
    "temperature": None,
    "max_tokens": None,
    "top_p": None,
    "reasoning_effort": None,
    "request_timeout": 300.0,
    "max_retries": 2,
}

_DEFAULT_HARNESS: Dict[str, Any] = {
    "skill_context_tokens": 40000,
    "quick_context_tokens": 3000,
    "chat_context_tokens": 24000,
    "chat_tools": True,
    "max_tool_rounds": 3,
    "output_language": "zh",
}

_data_dir: Optional[str] = None


def set_data_dir(path: str) -> None:
    """由桌面启动器在导入数据库之前指定资料目录。"""
    global _data_dir
    _data_dir = os.path.abspath(path)

_DEFAULT_KNOWLEDGE: Dict[str, Any] = {
    "relation_max_papers": 20,
    "max_tree_paths": 200,
    "store_reasoning": False,
    "auto_run_on_import": False,
    "reuse_artifacts": True,
    "discovery_batch_size": 8,
    "discovery_min_score": 0.55,
    "discovery_max_summarize": 12,
    "discovery_default_days": 14,
    # 智能关联检索（Related Work -> 候选论文）
    "related_max_seeds": 20,
    "related_title_matches": 3,
    "related_topic_queries": 6,
    "related_max_candidates": 120,
    "related_batch_size": 8,
    "related_max_results": 30,
}


def get_llm_config() -> Dict[str, Any]:
    """返回空模型信息和应用默认参数的副本。"""
    return dict(_DEFAULT_LLM)


def get_harness_config() -> Dict[str, Any]:
    return dict(_DEFAULT_HARNESS)


def get_knowledge_config() -> Dict[str, Any]:
    """阅读与检索配置；旧自动综述阈值不再生效。"""
    return dict(_DEFAULT_KNOWLEDGE)


def get_base_dir() -> str:
    """源码模式保留原资料库；桌面模式由启动器提供用户资料路径。"""
    if _data_dir:
        return _data_dir
    if getattr(sys, "frozen", False):
        from platformdirs import user_data_dir
        return os.path.join(user_data_dir("ArxivReader", appauthor=False), "data")
    return os.path.join(ROOT_DIR, "data")


def get_db_path() -> str:
    return os.path.join(get_base_dir(), "library.db")


def get_pdf_dir() -> str:
    return os.path.join(get_base_dir(), "pdfs")

