"""PDF 全文翻译路由：翻译设置、引擎安装、任务启动/轮询/取消、产物下载。

  GET    /api/pdf-translation/settings            翻译设置（api_key 脱敏）
  PUT    /api/pdf-translation/settings            更新翻译设置
  GET    /api/pdf-translation/engine              引擎状态（是否安装/版本/安装日志）
  POST   /api/pdf-translation/engine/install      后台安装 pdf2zh_next（幂等）
  POST   /api/papers/{paper_id}/pdf-translation/run    启动一次全文翻译
  GET    /api/papers/{paper_id}/pdf-translation        最新任务 + 历史列表
  GET    /api/pdf-translation/tasks/{task_id}          任务详情（轮询）
  POST   /api/pdf-translation/tasks/{task_id}/cancel   取消
  GET    /api/pdf-translation/tasks/{task_id}/file/{kind}  取 mono/dual PDF（inline）
  DELETE /api/pdf-translation/tasks/{task_id}          删除记录与产物
"""

import logging
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import database
from ..models import Paper, PdfTranslationTask
from ..pdftranslate import env as env_mod
from ..pdftranslate import settings as tr_settings
from ..pdftranslate import tasks

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["pdf-translation"])


# ---------------------------------------------------------------- 设置
class TranslationSettingsUpdate(BaseModel):
    """字段缺省 = 不修改；空串 = 清除（api_key 空 = 不修改）。"""

    service: Optional[str] = None
    model: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    lang_in: Optional[str] = None
    lang_out: Optional[str] = None
    qps: Optional[int] = Field(None, ge=1, le=100)
    output_mode: Optional[str] = None
    dual_mode: Optional[str] = None
    dual_translate_first: Optional[bool] = None
    translate_table_text: Optional[bool] = None
    ocr_workaround: Optional[bool] = None
    enhance_compatibility: Optional[bool] = None
    use_mirror: Optional[bool] = None


@router.get("/pdf-translation/settings")
def get_pdf_translation_settings(db: Session = Depends(database.get_db)):
    return tr_settings.public_view(tr_settings.get_translation_settings(db))


@router.put("/pdf-translation/settings")
def update_pdf_translation_settings(body: TranslationSettingsUpdate,
                                    db: Session = Depends(database.get_db)):
    patch: Dict[str, Any] = body.model_dump(exclude_unset=True)
    if patch.get("service") and patch["service"] not in tr_settings.SERVICES:
        raise HTTPException(status_code=422, detail="不支持的翻译服务")
    if patch.get("output_mode") and patch["output_mode"] not in tr_settings.OUTPUT_MODES:
        raise HTTPException(status_code=422, detail="输出方式无效")
    if patch.get("dual_mode") and patch["dual_mode"].upper() not in tr_settings.DUAL_MODES:
        raise HTTPException(status_code=422, detail="对照排版无效")
    tr_settings.update_translation_settings(db, patch)
    return tr_settings.public_view(tr_settings.get_translation_settings(db))


# ---------------------------------------------------------------- 引擎安装
@router.get("/pdf-translation/engine")
def engine_status():
    return env_mod.status()


@router.post("/pdf-translation/engine/install")
def install_engine(db: Session = Depends(database.get_db)):
    if env_mod.status()["installed"]:
        return {"started": False, "installed": True}
    use_mirror = tr_settings.get_translation_settings(db).get("use_mirror", False)
    started = env_mod.install_background(use_mirror=use_mirror)
    return {"started": started, "installed": False}


# ---------------------------------------------------------------- 任务
class RunRequest(BaseModel):
    """本次覆盖项；缺省全部沿用保存的翻译设置。"""

    output_mode: Optional[str] = None
    dual_mode: Optional[str] = None
    lang_in: Optional[str] = None
    lang_out: Optional[str] = None
    qps: Optional[int] = Field(None, ge=1, le=100)
    pages: Optional[str] = None
    dual_translate_first: Optional[bool] = None
    translate_table_text: Optional[bool] = None
    ocr_workaround: Optional[bool] = None
    enhance_compatibility: Optional[bool] = None


def _paper_or_404(db: Session, paper_id: int) -> Paper:
    paper = db.get(Paper, paper_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="论文不存在")
    return paper


def _task_or_404(db: Session, task_id: int) -> PdfTranslationTask:
    task = db.get(PdfTranslationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="翻译任务不存在")
    return task


@router.post("/papers/{paper_id}/pdf-translation/run", status_code=201)
def run_pdf_translation(paper_id: int, body: RunRequest = RunRequest(),
                        db: Session = Depends(database.get_db)):
    paper = _paper_or_404(db, paper_id)
    running = tasks.running_task(db, paper.id)
    if running is not None:   # 幂等：在跑就返回当前任务，前端继续轮询
        return tasks.task_payload(running)
    try:
        task = tasks.start_task(db, paper, body.model_dump(exclude_unset=True))
    except LookupError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        message = str(exc)
        # 形如 pdftr.* 的消息是 i18n 错误码，按字典 detail 透传给前端本地化
        if message.startswith("pdftr."):
            raise HTTPException(status_code=400, detail={"code": message, "params": {}})
        raise HTTPException(status_code=400, detail=message)
    return tasks.task_payload(task)


@router.get("/papers/{paper_id}/pdf-translation")
def pdf_translation_view(paper_id: int, db: Session = Depends(database.get_db)):
    _paper_or_404(db, paper_id)
    latest = tasks.latest_task(db, paper_id)
    return {
        "latest": tasks.task_payload(latest) if latest else None,
        "tasks": tasks.history(db, paper_id),
        "engine": env_mod.status(),
    }


@router.get("/pdf-translation/tasks/{task_id}")
def get_pdf_translation_task(task_id: int, db: Session = Depends(database.get_db)):
    return tasks.task_payload(_task_or_404(db, task_id))


@router.post("/pdf-translation/tasks/{task_id}/cancel")
def cancel_pdf_translation_task(task_id: int, db: Session = Depends(database.get_db)):
    task = _task_or_404(db, task_id)
    return tasks.task_payload(tasks.request_cancel(db, task))


@router.get("/pdf-translation/tasks/{task_id}/file/{kind}")
def pdf_translation_file(task_id: int, kind: str,
                         db: Session = Depends(database.get_db)):
    task = _task_or_404(db, task_id)
    if kind not in ("mono", "dual"):
        raise HTTPException(status_code=404, detail="未知的文件类型")
    path = task.mono_path if kind == "mono" else task.dual_path
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="译文文件不存在")
    base = os.path.abspath(env_mod.output_dir())
    if not os.path.abspath(path).startswith(base + os.sep):
        raise HTTPException(status_code=403, detail="不允许访问该路径")
    return FileResponse(path, media_type="application/pdf",
                        filename=os.path.basename(path),
                        content_disposition_type="inline")


@router.delete("/pdf-translation/tasks/{task_id}", status_code=204)
def delete_pdf_translation_task(task_id: int, db: Session = Depends(database.get_db)):
    tasks.delete_task(db, _task_or_404(db, task_id))
    return None
