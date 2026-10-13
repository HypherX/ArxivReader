"""设置路由：读写 LLM 端点与采样参数，并提供连通性测试。

空值语义（与 settings_store 一致）：
  - 字段缺省 = 不修改；
  - 字段为空串 = 清除该设置，改用服务端（API）默认值；
  - api_key 为空视为"不修改"，防止把已保存的 key 覆盖成空。

GET 时对 api_key 脱敏（只回预览与是否已设置），不回传完整 key。
连接检查仅在内存中使用表单参数，不隐式保存，也不返回模型回复或密钥。
"""

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from harness import llm as harness_llm

from .. import database, schemas, settings_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])

_CLEAR_WORDS = ("none", "null", "default")


def _to_out(settings: harness_llm.LLMSettings) -> schemas.LLMSettingsOut:
    return schemas.LLMSettingsOut(
        base_url=settings.base_url,
        model=settings.model,
        output_language=settings.output_language or "zh",
        temperature=settings.temperature,
        max_tokens=settings.max_tokens,
        top_p=settings.top_p,
        reasoning_effort=settings.reasoning_effort,
        has_api_key=bool(settings.api_key),
        api_key_preview=settings_store.mask_api_key(settings.api_key),
        request_timeout=settings.request_timeout,
        max_retries=settings.max_retries,
    )


def _normalize_patch(body: schemas.LLMSettingsUpdate) -> Dict[str, Any]:
    """把"缺省/空串/数值"归一成 settings_store 认识的形式。"""
    patch: Dict[str, Any] = {}
    for key, value in body.model_dump(exclude_unset=True).items():
        if value is None:
            continue
        if isinstance(value, str):
            text = value.strip()
            if key == "api_key":
                if text:
                    patch[key] = text
                continue
            if not text or text.lower() in _CLEAR_WORDS:
                patch[key] = ""          # 显式清空 -> 用 API 默认值
                continue
        patch[key] = value
    from urllib.parse import urlsplit
    url = patch.get("base_url")
    if url:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            raise HTTPException(422, detail={"code": "settings.invalid_url", "params": {}})
    for key, lower, upper in (("temperature", 0, 2), ("top_p", 0, 1), ("max_tokens", 1, 2000000)):
        value = patch.get(key)
        if value not in (None, "") and not lower <= value <= upper:
            raise HTTPException(422, detail={"code": "settings.invalid_parameter", "params": {"name": key}})
    return patch


@router.get("", response_model=schemas.LLMSettingsOut)
def get_settings(db: Session = Depends(database.get_db)):
    return _to_out(settings_store.get_llm_settings(db))


@router.put("", response_model=schemas.LLMSettingsOut)
def update_settings(body: schemas.LLMSettingsUpdate,
                    db: Session = Depends(database.get_db)):
    settings_store.update_llm_settings(db, _normalize_patch(body))
    return _to_out(settings_store.get_llm_settings(db))


@router.post("/test", response_model=schemas.TestResult)
def test_settings(body: schemas.LLMSettingsUpdate, db: Session = Depends(database.get_db)):
    settings = settings_store.get_llm_settings(db, _normalize_patch(body))
    if not settings.base_url or not settings.model or not settings.api_key:
        return schemas.TestResult(ok=False, code="settings.not_configured")
    try:
        reply = harness_llm.chat(
            settings,
            [{"role": "user", "content": "Reply with OK only."}],
            max_retries=0,
        )
    except Exception:
        return schemas.TestResult(ok=False, code="settings.connection_failed")
    if reply is None:
        return schemas.TestResult(ok=False, code="settings.connection_failed")
    return schemas.TestResult(ok=True, code="settings.connected")


@router.get("/preferences")
def get_preferences(db: Session = Depends(database.get_db)):
    import json
    from ..models import Setting
    row = db.get(Setting, "ui.preferences")
    try:
        value = json.loads(row.value) if row else {}
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


@router.put("/preferences")
def save_preferences(body: Dict[str, str], db: Session = Depends(database.get_db)):
    import json
    from ..models import Setting
    if len(body) > 20 or any(not k.startswith("arxivreader.") or len(v) > 50000 for k, v in body.items()):
        raise HTTPException(422, detail={"code": "settings.invalid_parameter", "params": {"name": "preferences"}})
    value = get_preferences(db)
    value.update(body)
    row = db.get(Setting, "ui.preferences")
    if row is None:
        row = Setting(key="ui.preferences", value="{}")
        db.add(row)
    row.value = json.dumps(value, ensure_ascii=False)
    db.commit()
    return value
