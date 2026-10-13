"""手动文件夹综述 API；后台任务使用独立数据库会话。"""
import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import database, settings_store
from app.models import FolderReview
from harness import cancel, llm
from harness.language import choose
from knowledge import review

router = APIRouter(prefix="/api", tags=["reviews"])


class ReviewRequest(BaseModel):
    title: str = Field("", max_length=500)
    focus: str = Field("", max_length=8000)
    output_language: Optional[Literal["zh", "en"]] = None


def _row(db: Session, review_id: int) -> FolderReview:
    row = db.get(FolderReview, review_id)
    if row is None:
        raise HTTPException(status_code=404, detail="综述不存在")
    return row


def _settings(db: Session):
    settings = settings_store.get_llm_settings(db)
    if not llm.is_configured(settings):
        raise HTTPException(status_code=400, detail="请先配置 LLM")
    return settings


@router.get("/folders/{folder_id}/review-preview")
def preview_folder(folder_id: int, db: Session = Depends(database.get_db)):
    try:
        return review.preview(db, folder_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/folders/{folder_id}/reviews", status_code=201)
def create_folder_review(folder_id: int, body: ReviewRequest, db: Session = Depends(database.get_db)):
    settings = _settings(db)
    try:
        row = review.create_review(db, folder_id, title=body.title, focus=body.focus, language=body.output_language)
        review.start_background(row.id, settings)
    except review.ReviewStartError:
        raise HTTPException(status_code=503, detail=choose({"output_language": row.output_language},
            "执行器启动失败；报告 #{} 已保留，请从历史续跑", "Worker failed to start; report #{} was preserved, resume it from history").format(row.id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.refresh(row)
    return review.payload(row)


@router.get("/reviews")
def list_reviews(folder_id: Optional[int] = None, limit: int = Query(100, ge=1, le=500),
                 db: Session = Depends(database.get_db)):
    query = db.query(FolderReview)
    if folder_id is not None:
        query = query.filter(FolderReview.folder_id == folder_id)
    return [review.payload(row) for row in query.order_by(FolderReview.id.desc()).limit(limit).all()]


@router.get("/reviews/{review_id}")
def get_review(review_id: int, db: Session = Depends(database.get_db)):
    return review.payload(_row(db, review_id), content=True)


@router.get("/reviews/{review_id}/export")
def export_review(review_id: int, db: Session = Depends(database.get_db)):
    row = _row(db, review_id)
    if not row.content_md:
        raise HTTPException(status_code=409, detail="报告尚未生成，查看进度或续跑失败批次")
    return Response(row.content_md, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="review-{}.md"'.format(row.id)})


@router.post("/reviews/{review_id}/cancel")
def cancel_review(review_id: int, db: Session = Depends(database.get_db)):
    row = _row(db, review_id)
    message = choose({"output_language": row.output_language}, "已取消；可续跑", "Cancelled; resume when ready")
    queued = db.query(FolderReview).filter_by(id=row.id, status="queued").update(
        {"cancel_requested": True, "status": "cancelled", "error": message,
         "finished_at": datetime.datetime.utcnow()}, synchronize_session=False)
    running = db.query(FolderReview).filter_by(id=row.id, status="running").update(
        {"cancel_requested": True}, synchronize_session=False) if not queued else 0
    db.commit()
    if running:
        cancel.request_stop("review:{}".format(row.id))
    db.refresh(row)
    return review.payload(row)


@router.post("/reviews/{review_id}/resume")
def resume_review(review_id: int, db: Session = Depends(database.get_db)):
    row = _row(db, review_id)
    try:
        review.start_background(row.id, _settings(db), resume=True)
    except review.ReviewStartError:
        raise HTTPException(status_code=503, detail=choose({"output_language": row.output_language},
            "执行器启动失败；报告 #{} 已保留，请稍后续跑", "Worker failed to start; report #{} was preserved, retry later").format(row.id))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    db.refresh(row)
    return review.payload(row)
