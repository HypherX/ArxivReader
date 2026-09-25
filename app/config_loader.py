"""加载项目根目录的 config.py（全系统不读取任何环境变量）。

优先级：
  1) paper_reader/config.py（用户真实配置，已 gitignore）
  2) paper_reader/config.example.py（入库模板，占位值）
  3) 本模块内置默认值

用 importlib 按绝对路径加载，因此与启动时的工作目录无关（无论从
paper_reader/ 还是其父目录执行 uvicorn 都能正确定位）。

对外暴露：
  get_llm_config()      -> dict   LLM 节（与内置默认值合并）
  get_storage_config()  -> dict   STORAGE 节
  get_base_dir()        -> str    数据库与 PDF 的根目录
  get_db_path()         -> str    SQLite 文件绝对路径
  get_pdf_dir()         -> str    PDF 存放根目录
"""

import importlib.util
import os
from typing import Any, Dict, Optional

# app/ 的上一级即项目根 paper_reader/
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_DEFAULT_LLM: Dict[str, Any] = {
    "base_url": "https://api.openai.com/v1",
    "api_key": "",
    "model": "gpt-4o-mini",
    "temperature": 0.3,
    "max_tokens": 4096,
    "top_p": 1.0,
    "request_timeout": 120.0,
    "max_retries": 3,
}

_DEFAULT_STORAGE: Dict[str, Any] = {
    "base_dir": "",
}


def _load_module_from_path(path: str) -> Optional[Any]:
    spec = importlib.util.spec_from_file_location("arxivreader_config", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_config_module() -> Optional[Any]:
    for name in ("config.py", "config.example.py"):
        path = os.path.join(ROOT_DIR, name)
        if os.path.isfile(path):
            try:
                return _load_module_from_path(path)
            except Exception:
                # 配置写错时不阻断启动，回退到默认值
                continue
    return None


_MODULE = _load_config_module()


def _section(name: str, default: Dict[str, Any]) -> Dict[str, Any]:
    raw = getattr(_MODULE, name, None) if _MODULE is not None else None
    merged = dict(default)
    if isinstance(raw, dict):
        merged.update(raw)
    return merged


def get_llm_config() -> Dict[str, Any]:
    return _section("LLM", _DEFAULT_LLM)


def get_storage_config() -> Dict[str, Any]:
    return _section("STORAGE", _DEFAULT_STORAGE)


def get_base_dir() -> str:
    """数据库与 PDF 的根目录；STORAGE.base_dir 为空则用 paper_reader/data/。"""
    base = str(get_storage_config().get("base_dir") or "").strip()
    if not base:
        base = os.path.join(ROOT_DIR, "data")
    return os.path.abspath(base)


def get_db_path() -> str:
    return os.path.join(get_base_dir(), "library.db")


def get_pdf_dir() -> str:
    return os.path.join(get_base_dir(), "pdfs")
