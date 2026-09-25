"""LLM 设置的持久化层。

首次启动把 config.py 的 LLM 节"播种"到 SQLite settings 表（仅补缺、不覆盖）；
之后运行时读写数据库，网页"设置"里的修改即持久化于此，不回写 config.py，
也不读取任何环境变量。

存储的键：base_url / api_key / model / temperature / max_tokens / top_p /
request_timeout / max_retries，全部以字符串存入 Setting.value。
"""

from typing import Any, Dict

from sqlalchemy.orm import Session

from . import config_loader
from .llm_client import LLMSettings
from .models import Setting

_KEYS = (
    "base_url", "api_key", "model", "temperature",
    "max_tokens", "top_p", "request_timeout", "max_retries",
)


def _read_all(db: Session) -> Dict[str, str]:
    return {row.key: row.value for row in db.query(Setting).all()}


def seed_from_config(db: Session) -> None:
    """把 config.py 的 LLM 节写入 settings 表；已存在的键保持不变。"""
    cfg = config_loader.get_llm_config()
    existing = set(_read_all(db).keys())
    changed = False
    for key in _KEYS:
        if key in existing or key not in cfg:
            continue
        db.add(Setting(key=key, value=str(cfg[key])))
        changed = True
    if changed:
        db.commit()


def _pick(raw: Dict[str, str], cfg: Dict[str, Any], key: str, default: Any) -> Any:
    val = raw.get(key)
    if val is None:
        val = cfg.get(key, default)
    return val


def get_llm_settings(db: Session) -> LLMSettings:
    """从数据库组装 LLMSettings；缺失项回退到 config.py，再回退到内置默认。"""
    raw = _read_all(db)
    cfg = config_loader.get_llm_config()
    return LLMSettings(
        base_url=str(_pick(raw, cfg, "base_url", "") or ""),
        api_key=str(_pick(raw, cfg, "api_key", "") or ""),
        model=str(_pick(raw, cfg, "model", "") or ""),
        temperature=float(_pick(raw, cfg, "temperature", 0.3)),
        max_tokens=int(_pick(raw, cfg, "max_tokens", 4096)),
        top_p=float(_pick(raw, cfg, "top_p", 1.0)),
        request_timeout=float(_pick(raw, cfg, "request_timeout", 120.0)),
        max_retries=int(_pick(raw, cfg, "max_retries", 3)),
    )


def update_llm_settings(db: Session, patch: Dict[str, Any]) -> None:
    """写入设置项；值为 None 的键跳过（表示不修改，如未改动的 api_key）。"""
    for key, value in patch.items():
        if key not in _KEYS or value is None:
            continue
        row = db.get(Setting, key)
        if row is None:
            db.add(Setting(key=key, value=str(value)))
        else:
            row.value = str(value)
    db.commit()


def mask_api_key(key: str) -> str:
    """返回脱敏预览，如 sk-a****wxyz；不回传完整 key。"""
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return key[:4] + "****" + key[-4:]
