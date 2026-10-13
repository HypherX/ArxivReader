"""按需翻译 API：无自动批量调用，不改动原分类、路径或关系。"""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from knowledge import translation
from .. import database, settings_store

router = APIRouter(prefix="/api/translations", tags=["translations"])


class TranslationRequest(BaseModel):
    output_language: Optional[Literal["zh", "en"]] = None


@router.post("/{kind}/{target_id}")
def translate(kind: Literal["node", "relation"], target_id: int,
              body: TranslationRequest = TranslationRequest(),
              db: Session = Depends(database.get_db)):
    try:
        return translation.translate(db, kind, target_id, settings_store.get_llm_settings(db), body.output_language)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
