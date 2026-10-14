"""LLM 设置的持久化层。

模型信息仅通过应用设置写入 SQLite；缺失项使用空值和内部安全默认值。
不执行用户配置文件，也不从环境变量读取模型信息。

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

from harness import config
from .models import Setting

# 允许为空的键（空 = 用 API 默认值）
OPTIONAL_KEYS = ("temperature", "max_tokens", "top_p", "reasoning_effort")
_KEYS = (
    "base_url", "api_key", "model",
    "temperature", "max_tokens", "top_p", "reasoning_effort",
    "request_timeout", "max_retries", "output_language",
)
# 不允许被清空的键（空值视为"不修改"）
_REQUIRED_KEYS = ("base_url", "model")


def _read_all(db: Session) -> Dict[str, str]:
    return {row.key: row.value for row in db.query(Setting).all()}


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


def get_llm_settings(db: Session, overrides: Optional[Dict[str, Any]] = None) -> LLMSettings:
    """读取已保存设置；连接检查可用内存覆盖，绝不隐式写库。"""
    raw = _read_all(db)
    raw.update({k: v for k, v in (overrides or {}).items() if k in _KEYS and v is not None})
    cfg = config.get_llm_config()
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
        output_language=raw.get("output_language") or get_output_language(db),
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


def get_output_language(db: Session) -> str:
    from harness.language import normalize_language
    from harness.config import get_harness_config
    row = db.get(Setting, "output_language")
    return normalize_language(row.value if row else get_harness_config().get("output_language"))


def get_harness_settings(db: Session) -> Dict[str, Any]:
    from harness.config import get_harness_config
    return {**get_harness_config(), "output_language": get_output_language(db)}


def mask_api_key(key: str) -> str:
    """返回脱敏预览，如 sk-a****wxyz；不回传完整 key。"""
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return key[:4] + "****" + key[-4:]


# ---------------------------------------------------------------- 划词翻译自定义端点
# 翻译调用频率高，不适合一直走昂贵的主模型：seltr.* 任一字段非空即覆盖主设置的
# 对应字段，留空的字段继续跟随主设置（per-field fallback）。
SELTR_KEYS = ("seltr.base_url", "seltr.model", "seltr.api_key")


def get_selection_translation_raw(db: Session) -> Dict[str, str]:
    raw = _read_all(db)
    return {key: (raw.get(key) or "").strip() for key in SELTR_KEYS}


def update_selection_translation(db: Session, patch: Dict[str, Any]) -> None:
    """写入划词翻译端点设置；空串 = 清除该自定义字段（回退跟随主设置）。"""
    for key in SELTR_KEYS:
        if key not in patch or patch[key] is None:
            continue
        text = str(patch[key]).strip()
        row = db.get(Setting, key)
        if row is None:
            db.add(Setting(key=key, value=text))
        else:
            row.value = text
    db.commit()


def get_selection_translation_settings(db: Session) -> LLMSettings:
    """划词翻译实际使用的 LLM 设置。

    组合规则（防止"主端点 + 残留自定义 Key"这种错配）：
      - base_url 与 model 都未自定义 → 完全跟随主设置（忽略残留的 seltr.api_key）；
      - 只自定义 model（同端点换便宜模型）→ 用主设置的 Key；
      - 自定义了 base_url（换端点）→ 优先用自定义 Key，未设则先试主 Key。
    """
    from dataclasses import replace
    main = get_llm_settings(db)
    raw = get_selection_translation_raw(db)
    custom_url = raw["seltr.base_url"]
    custom_model = raw["seltr.model"]
    if not custom_url and not custom_model:
        return main
    api_key = main.api_key
    if custom_url and raw["seltr.api_key"]:
        api_key = raw["seltr.api_key"]
    return replace(main,
                   base_url=custom_url or main.base_url,
                   model=custom_model or main.model,
                   api_key=api_key)
