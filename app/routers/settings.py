"""设置路由：读写 LLM 端点与采样参数，并提供连通性测试。

GET 返回时对 api_key 脱敏（只回预览与是否已设置），不回传完整 key；
PUT 时若未提供 api_key（或为空）则保留原值，避免误清除。
"""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import database, llm_client, schemas, settings_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _to_out(settings: llm_client.LLMSettings) -> schemas.LLMSettingsOut:
    return schemas.LLMSettingsOut(
        base_url=settings.base_url,
        model=settings.model,
        temperature=settings.temperature,
        max_tokens=settings.max_tokens,
        top_p=settings.top_p,
        has_api_key=bool(settings.api_key),
        api_key_preview=settings_store.mask_api_key(settings.api_key),
    )


@router.get("", response_model=schemas.LLMSettingsOut)
def get_settings(db: Session = Depends(database.get_db)):
    return _to_out(settings_store.get_llm_settings(db))


@router.put("", response_model=schemas.LLMSettingsOut)
def update_settings(body: schemas.LLMSettingsUpdate,
                    db: Session = Depends(database.get_db)):
    patch = body.model_dump(exclude_unset=True)
    # api_key 为空视为"不修改"，防止把已保存的 key 覆盖成空
    if "api_key" in patch and not str(patch.get("api_key") or "").strip():
        patch.pop("api_key")
    settings_store.update_llm_settings(db, patch)
    return _to_out(settings_store.get_llm_settings(db))


@router.post("/test", response_model=schemas.TestResult)
def test_settings(db: Session = Depends(database.get_db)):
    settings = settings_store.get_llm_settings(db)
    if not settings.base_url or not settings.model:
        return schemas.TestResult(ok=False, detail="base_url 或 model 未配置")
    if not settings.api_key:
        return schemas.TestResult(ok=False, detail="api_key 未配置")
    try:
        reply = llm_client.chat(
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
