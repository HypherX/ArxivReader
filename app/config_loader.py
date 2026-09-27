"""config.py 加载入口（历史 API 保持不变，实现下沉到 harness.config）。

重构为 Agent Harness 后，配置加载统一由 `harness/config.py` 负责——harness 是下层，
不依赖 Web 层，因此 Web（app/）与知识网络编排（knowledge/）共用同一份配置口径。
本模块只做转发，保证既有 `config_loader.xxx` 调用点（含测试 monkeypatch）无需改动。
"""

from harness.config import (  # noqa: F401
    ROOT_DIR,
    get_base_dir,
    get_db_path,
    get_harness_config,
    get_knowledge_config,
    get_llm_config,
    get_pdf_dir,
    get_prompt_path,
    get_storage_config,
)

__all__ = [
    "ROOT_DIR",
    "get_base_dir",
    "get_db_path",
    "get_harness_config",
    "get_knowledge_config",
    "get_llm_config",
    "get_pdf_dir",
    "get_prompt_path",
    "get_storage_config",
]
