"""按需翻译 API：无自动批量调用，不改动原分类、路径或关系。"""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from knowledge import translation
from .. import database, settings_store
from ..models import Paper

router = APIRouter(prefix="/api/translations", tags=["translations"])


class TranslationRequest(BaseModel):
    output_language: Optional[Literal["zh", "en"]] = None


class BlockTranslationRequest(TranslationRequest):
    text: str = Field(..., min_length=1, max_length=translation.MAX_BLOCK_CHARS,
                      description="待翻译的一段原文（PDF 划词或重排段落）")


@router.post("/paper/{paper_id}")
def translate_paper_block(paper_id: int, body: BlockTranslationRequest,
                          db: Session = Depends(database.get_db)):
    """翻译属于某篇论文的一段原文：划词翻译与重排段落翻译共用这一个入口。

    必须声明在 /{kind}/{target_id} 之前：那条路由会先匹配到 /paper/123，
    再因为 kind 不在 Literal 里返回 422。
    """
    if db.get(Paper, paper_id) is None:
        raise HTTPException(status_code=404, detail="论文不存在")
    return _run(db, lambda: translation.translate_paper_block(
        db, paper_id, body.text, settings_store.get_llm_settings(db), body.output_language))


@router.post("/{kind}/{target_id}")
def translate(kind: Literal["node", "relation"], target_id: int,
              body: TranslationRequest = TranslationRequest(),
              db: Session = Depends(database.get_db)):
    return _run(db, lambda: translation.translate(
        db, kind, target_id, settings_store.get_llm_settings(db), body.output_language))


def _run(db, call):
    """异常 -> 界面可读的状态码；ValueError 既可能是入参问题也可能是"未配置 LLM"。"""
    try:
        return call()
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
