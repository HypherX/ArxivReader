"""设置路由：读写 LLM 端点与采样参数，并提供连通性测试。

空值语义（与 settings_store 一致）：
  - 字段缺省 = 不修改；
  - 字段为空串 = 清除该设置，改用服务端（API）默认值；
  - api_key 为空视为"不修改"，防止把已保存的 key 覆盖成空。

GET 时对 api_key 脱敏（只回预览与是否已设置），不回传完整 key。
reasoning_effort（low/medium/high）在此已可读写，作为"用户自定义推理强度"的接口预留，
前端 UI 本期未接入。
"""

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends
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
        temperature=settings.temperature,
        max_tokens=settings.max_tokens,
        top_p=settings.top_p,
        reasoning_effort=settings.reasoning_effort,
        has_api_key=bool(settings.api_key),
        api_key_preview=settings_store.mask_api_key(settings.api_key),
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
def test_settings(db: Session = Depends(database.get_db)):
    settings = settings_store.get_llm_settings(db)
    if not settings.base_url or not settings.model:
        return schemas.TestResult(ok=False, detail="base_url 或 model 未配置")
    if not settings.api_key:
        return schemas.TestResult(ok=False, detail="api_key 未配置")
    try:
        reply = harness_llm.chat(
            settings,
            [{"role": "user", "content": "请只回复两个字：在的"}],
            max_retries=0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("连通性测试失败：%s", str(exc)[:300])
        return schemas.TestResult(ok=False, detail=str(exc)[:300])
    if reply is None:
        return schemas.TestResult(ok=False, detail="调用失败（无响应）")
    return schemas.TestResult(ok=True, detail="已连接，模型回复：%s" % reply[:80])
