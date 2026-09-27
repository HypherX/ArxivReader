"""LLM 设置的持久化层。

首次启动把 config.py 的 LLM 节"播种"到 SQLite settings 表（仅补缺、不覆盖）；
之后运行时读写数据库，网页"设置"里的修改即持久化于此，不回写 config.py，
也不读取任何环境变量。

存储的键：base_url / api_key / model / temperature / max_tokens / top_p /
reasoning_effort / request_timeout / max_retries，全部以字符串存入 Setting.value。

参数语义（重要）：
  - 空（""）表示**不下发该参数**，沿用服务端（API）默认值；
  - 因此采样参数与 reasoning_effort 允许为空，读取时映射为 None（LLMSettings 的 Optional 字段）；
  - 写入空字符串即"清除该设置"，与"不修改"（键不出现）区分开。
"""

from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from harness.llm import LLMSettings

from . import config_loader
from .models import Setting

# 允许为空的键（空 = 用 API 默认值）
OPTIONAL_KEYS = ("temperature", "max_tokens", "top_p", "reasoning_effort")
_KEYS = (
    "base_url", "api_key", "model",
    "temperature", "max_tokens", "top_p", "reasoning_effort",
    "request_timeout", "max_retries",
)
# 不允许被清空的键（空值视为"不修改"）
_REQUIRED_KEYS = ("base_url", "model")


def _read_all(db: Session) -> Dict[str, str]:
    return {row.key: row.value for row in db.query(Setting).all()}


def seed_from_config(db: Session) -> None:
    """把 config.py 的 LLM 节写入 settings 表；已存在的键保持不变。

    config 中值为 None 的键（如采样参数留空）不写库：读取时会回退到 config，仍是 None，
    即"沿用 API 默认值"。
    """
    cfg = config_loader.get_llm_config()
    existing = set(_read_all(db).keys())
    changed = False
    for key in _KEYS:
        if key in existing or key not in cfg or cfg[key] is None:
            continue
        db.add(Setting(key=key, value=str(cfg[key])))
        changed = True
    if changed:
        db.commit()


def _raw_value(raw: Dict[str, str], cfg: Dict[str, Any], key: str) -> Optional[str]:
    """取原始字符串值；空串 / none / null 统一映射成 None（= 用 API 默认值）。"""
    value = raw.get(key, cfg.get(key))
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ("none", "null", "default"):
        return None
    return text


def _to_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def get_llm_settings(db: Session) -> LLMSettings:
    """从数据库组装 LLMSettings；缺失项回退到 config.py，再回退到内置默认。"""
    raw = _read_all(db)
    cfg = config_loader.get_llm_config()
    return LLMSettings(
        base_url=_raw_value(raw, cfg, "base_url") or "",
        api_key=_raw_value(raw, cfg, "api_key") or "",
        model=_raw_value(raw, cfg, "model") or "",
        temperature=_to_float(_raw_value(raw, cfg, "temperature")),
        max_tokens=_to_int(_raw_value(raw, cfg, "max_tokens")),
        top_p=_to_float(_raw_value(raw, cfg, "top_p")),
        reasoning_effort=_raw_value(raw, cfg, "reasoning_effort"),
        request_timeout=_to_float(_raw_value(raw, cfg, "request_timeout")) or 300.0,
        max_retries=_to_int(_raw_value(raw, cfg, "max_retries")) or 0,
    )


def update_llm_settings(db: Session, patch: Dict[str, Any]) -> None:
    """写入设置项。

    - 值为 None 的键跳过（= 不修改，如未改动的 api_key）；
    - 值为空串的键写入 ""（= 清除该设置，改用 API 默认值）；
    - base_url / model 的空值视为"不修改"，避免误清空导致不可用。
    """
    for key, value in patch.items():
        if key not in _KEYS or value is None:
            continue
        text = str(value).strip()
        if not text and key in _REQUIRED_KEYS:
            continue
        row = db.get(Setting, key)
        if row is None:
            db.add(Setting(key=key, value=text))
        else:
            row.value = text
    db.commit()


def mask_api_key(key: str) -> str:
    """返回脱敏预览，如 sk-a****wxyz；不回传完整 key。"""
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return key[:4] + "****" + key[-4:]
