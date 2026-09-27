"""ArXiv 检索 Pipeline 与待读缓冲区路由。

本层零 prompt、零 SQL：检索编排在 `knowledge.discovery`，序列化在 `knowledge.store`。

  POST /api/search/run              提交检索（异步后台跑），返回 run_id 供轮询
  GET  /api/search/status           轮询进度（stage / counters / logs 实时日志）
  POST /api/search/cancel           请求中断（当前批次结束后停止，已处理结果保留）
  GET  /api/search/runs             历史检索记录

  GET  /api/buffer                  待读缓冲区列表（含各状态计数）
  GET  /api/buffer/{id}             单条详情（title + abstract + 预筛理由 + 快速总结 + 建议路径）
  POST /api/buffer/{id}/summarize   对某条按需补跑「快速总结 + 归属复核」（预览用）
  POST /api/buffer/{id}/promote     加入详细阅读：迁入正式库 + 触发单篇 pipeline（默认后台）
  POST /api/buffer/{id}/reject      丢弃该条
  DELETE /api/buffer/{id}           删除缓冲区记录（已提升的论文不受影响）
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from harness import config as harness_config, llm as harness_llm
from knowledge import discovery, store

from .. import database, schemas, settings_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["search"])


def _knowledge_cfg():
    return harness_config.get_knowledge_config()


def _settings_or_400(db: Session):
    settings = settings_store.get_llm_settings(db)
    if not harness_llm.is_configured(settings):
        raise HTTPException(status_code=400,
                            detail="尚未配置 LLM（base_url / model / api_key），请先在设置中填写并保存。")
    return settings


def _buffer_or_404(db: Session, buffer_id: int):
    row = store.get_buffer(db, buffer_id)
    if row is None:
        raise HTTPException(status_code=404, detail="缓冲区记录不存在")
    return row


# ---------------------------------------------------------------- 检索
@router.post("/search/run", response_model=schemas.SearchRunOut)
def start_search(body: schemas.SearchRunRequest, db: Session = Depends(database.get_db)):
    """提交检索：后台异步执行（不阻塞前端），返回 run 状态供轮询。"""
    _settings_or_400(db)
    request = body.model_dump()
    if not (request.get("categories") or request.get("keywords")
            or request.get("date_from") or request.get("date_to")):
        raise HTTPException(status_code=400, detail="至少填写一个检索条件（分类 / 关键词 / 日期范围）")
    run_id = discovery.start_search_background(request)
    db.expire_all()
    run = store.get_search_run(db, run_id)
    return store.search_run_payload(run)


@router.get("/search/status", response_model=schemas.SearchRunOut)
def search_status(run_id: Optional[int] = Query(None, description="不填则返回最近一次"),
                  db: Session = Depends(database.get_db)):
    """轮询进度：stage（search/screen/summarize/buffer）+ counters + 实时日志尾部。"""
    run = store.get_search_run(db, run_id) if run_id else \
        next(iter(store.recent_search_runs(db, limit=1)), None)
    if run is None:
        raise HTTPException(status_code=404, detail="没有检索记录")
    return store.search_run_payload(run)


@router.post("/search/cancel", response_model=schemas.SearchRunOut)
def cancel_search(run_id: Optional[int] = Query(None, description="不填则取消最近一次进行中的检索"),
                  db: Session = Depends(database.get_db)):
    """请求中断：当前批次/当前论文结束后停止；已写入缓冲区的结果不会丢。"""
    run = store.get_search_run(db, run_id) if run_id else \
        next((r for r in store.recent_search_runs(db, limit=5) if r.status == "running"), None)
    if run is None:
        raise HTTPException(status_code=404, detail="没有可取消的检索")
    run.cancel_requested = True
    db.commit()
    store.log_search(db, run, "收到中断请求：将在当前批次结束后停止", level="warn")
    return store.search_run_payload(run)


@router.get("/search/runs", response_model=List[schemas.SearchRunOut])
def list_search_runs(limit: int = Query(10, ge=1, le=50), db: Session = Depends(database.get_db)):
    return [store.search_run_payload(run, log_tail=20) for run in store.recent_search_runs(db, limit)]


# ---------------------------------------------------------------- 缓冲区
@router.get("/buffer", response_model=schemas.BufferListOut)
def list_buffer(status: str = Query("pending", description="pending/read/rejected/all"),
                q: Optional[str] = None, limit: int = Query(200, ge=1, le=500),
                db: Session = Depends(database.get_db)):
    rows = store.list_buffer(db, status=status, q=q, limit=limit)
    return schemas.BufferListOut(
        items=[schemas.BufferPaperOut.model_validate(row) for row in rows],
        counts=store.buffer_stats(db))


@router.get("/buffer/{buffer_id}", response_model=schemas.BufferPaperOut)
def get_buffer(buffer_id: int, db: Session = Depends(database.get_db)):
    return _buffer_or_404(db, buffer_id)


@router.post("/buffer/{buffer_id}/summarize", response_model=schemas.BufferPaperOut)
def summarize_buffer(buffer_id: int, db: Session = Depends(database.get_db)):
    """按需补跑「快速总结 + 归属复核」（用于预览；不下载全文）。"""
    row = _buffer_or_404(db, buffer_id)
    settings = _settings_or_400(db)
    paths = discovery._tree_paths(db, int(_knowledge_cfg().get("max_tree_paths") or 200))
    try:
        discovery._summarize_and_verify(db, row, paths, settings,
                                        harness_config.get_harness_config())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail="总结/复核失败：{}".format(str(exc)[:200]))
    db.refresh(row)
    return row


@router.post("/buffer/{buffer_id}/promote", response_model=schemas.PromoteOut)
def promote_buffer(buffer_id: int,
                   background: bool = Query(True, description="true = 后台执行，用 promote_state 轮询"),
                   trigger_pipeline: bool = Query(True, description="是否同时跑单篇知识网络 pipeline"),
                   db: Session = Depends(database.get_db)):
    """加入详细阅读：下载全文 -> 建 Paper -> 挂到方向文件夹 -> 触发单篇 pipeline。"""
    row = _buffer_or_404(db, buffer_id)
    if row.status == store.BUFFER_READ and row.promoted_paper_id:
        return schemas.PromoteOut(buffer_id=row.id, promote_state=row.promote_state or "done",
                                  paper_id=row.promoted_paper_id)
    _settings_or_400(db)
    if background:
        state = discovery.start_promote_background(buffer_id, trigger_pipeline=trigger_pipeline)
        return schemas.PromoteOut(buffer_id=row.id, promote_state=state)
    try:
        result = discovery.promote_buffer(db, row, trigger_pipeline=trigger_pipeline)
    except Exception as exc:  # noqa: BLE001
        logger.warning("提升入库失败（buffer=%s）：%s", buffer_id, str(exc)[:300])
        raise HTTPException(status_code=502, detail="提升入库失败：{}".format(str(exc)[:200]))
    return schemas.PromoteOut(buffer_id=row.id, promote_state="done",
                              paper_id=result.get("paper_id"), pipeline=result.get("pipeline"))


@router.post("/buffer/{buffer_id}/reject", response_model=schemas.BufferPaperOut)
def reject_buffer(buffer_id: int, db: Session = Depends(database.get_db)):
    """丢弃该条（保留记录便于回看，不参与后续流程）。"""
    row = _buffer_or_404(db, buffer_id)
    store.set_buffer_status(db, row, store.BUFFER_REJECTED)
    return row


@router.delete("/buffer/{buffer_id}", status_code=204)
def delete_buffer(buffer_id: int, db: Session = Depends(database.get_db)):
    """删除缓冲区记录（若已提升为正式论文，论文本体不受影响）。"""
    row = _buffer_or_404(db, buffer_id)
    db.delete(row)
    db.commit()
    return None
