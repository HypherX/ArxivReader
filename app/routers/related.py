"""智能关联检索路由：Related Work 检索 -> 候选判定 -> 加入详细阅读。

本层零 prompt、零 SQL：编排在 `knowledge.related`，序列化在 `knowledge.store`。

  POST   /api/papers/{paper_id}/related/run       启动关联检索（后台线程），返回 run 供轮询
  GET    /api/papers/{paper_id}/related            步骤清单 + 最新 run + 强相关候选 + 计数
  POST   /api/papers/{paper_id}/related/cancel     请求中断（当前阶段/当前论文后停止）
  GET    /api/related/candidates/{cid}             单条候选（轮询 promote 状态用）
  POST   /api/related/candidates/{cid}/promote     加入详细阅读（跳过 summary，进知识网络）
  POST   /api/related/candidates/{cid}/discard     丢弃候选（保留记录，不再出现在列表里）
  DELETE /api/related/candidates/{cid}             删除候选记录
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from harness import llm as harness_llm
from knowledge import related, store

from .. import database, schemas, settings_store
from ..models import Paper, RelatedCandidate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["related"])


def _settings_or_400(db: Session):
    settings = settings_store.get_llm_settings(db)
    if not harness_llm.is_configured(settings):
        raise HTTPException(status_code=400,
                            detail="尚未配置 LLM（base_url / model / api_key），请先在设置中填写并保存。")
    return settings


def _paper_or_404(db: Session, paper_id: int) -> Paper:
    paper = db.get(Paper, paper_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="论文不存在")
    return paper


def _candidate_or_404(db: Session, candidate_id: int) -> RelatedCandidate:
    row = store.get_related_candidate(db, candidate_id)
    if row is None:
        raise HTTPException(status_code=404, detail="候选记录不存在")
    return row


@router.post("/papers/{paper_id}/related/run", response_model=schemas.RelatedRunOut)
def start_related(paper_id: int, mode: str = Query("search", pattern="^(search|continue|retry)$"),
                  db: Session = Depends(database.get_db)):
    """启动关联检索（后台异步）：已在运行则幂等返回当前 run，不重复起线程。"""
    paper = _paper_or_404(db, paper_id)
    _settings_or_400(db)
    running = store.find_running_related_run(db, paper.id)
    if running is not None:
        return store.related_run_payload(running)
    run_id = related.start_related_background(paper.id, mode=mode)
    db.expire_all()
    run = store.get_related_run(db, run_id)
    return store.related_run_payload(run)


@router.get("/papers/{paper_id}/related", response_model=schemas.RelatedViewOut)
def get_related_view(paper_id: int, include_discarded: bool = False,
                     db: Session = Depends(database.get_db)):
    """关联检索视图：步骤清单（与后端 STAGE_TITLES 同源）+ 最新 run + 强相关候选。"""
    paper = _paper_or_404(db, paper_id)
    run = store.latest_related_run(db, paper.id)
    language = run.output_language if run and run.output_language in ("zh", "en") else settings_store.get_output_language(db)
    titles = related.STAGE_TITLES_EN if language == "en" else related.STAGE_TITLES
    return schemas.RelatedViewOut(
        paper_id=paper.id,
        stages=[schemas.RelatedStageOut(name=name, title=titles[name])
                for name in related.STAGES],
        run=store.related_run_payload(run) if run is not None else None,
        candidates=store.related_candidates_payload(db, paper.id, include_discarded=include_discarded),
        counts=store.related_counts(db, paper.id),
    )


@router.post("/papers/{paper_id}/related/cancel", response_model=schemas.RelatedRunOut)
def cancel_related(paper_id: int, db: Session = Depends(database.get_db)):
    """请求中断：当前阶段/当前论文结束后停止；已判定结果不会丢。"""
    paper = _paper_or_404(db, paper_id)
    run = store.find_running_related_run(db, paper.id) or store.latest_related_run(db, paper.id)
    if run is None:
        raise HTTPException(status_code=404, detail="没有关联检索记录")
    if run.status == "running":
        run.cancel_requested = True
        db.commit()
        store.log_related(db, run, "收到中断请求，正在关闭模型流 / Cancellation requested", level="warn")
    return store.related_run_payload(run)


@router.get("/related/candidates/{candidate_id}", response_model=schemas.RelatedCandidateOut)
def get_related_candidate(candidate_id: int, db: Session = Depends(database.get_db)):
    return store.related_candidate_payload_single(db, _candidate_or_404(db, candidate_id))


@router.post("/related/candidates/{candidate_id}/promote", response_model=schemas.RelatedPromoteOut)
def promote_related_candidate_endpoint(
        candidate_id: int,
        background: bool = Query(True, description="true = 后台执行，用 promote_state 轮询"),
        trigger_pipeline: bool = Query(True, description="是否同时跑知识网络 pipeline"),
        db: Session = Depends(database.get_db)):
    """加入详细阅读：下载全文 -> 建 Paper -> 预写 summary 产物 -> 跑知识网络（跳过 summary）。

    流程衔接：候选在检索阶段已生成 quick_summary，这里把它补成正式产物后按
    tree -> deep -> graph 运行，与缓冲区「加入详细阅读」共用同一套
    pipeline 实现（幂等、可断点续跑）。
    """
    row = _candidate_or_404(db, candidate_id)
    if row.status == store.RELATED_PROMOTED and row.promoted_paper_id and row.promote_state == "done":
        return schemas.RelatedPromoteOut(candidate_id=row.id,
                                         promote_state=row.promote_state or "done",
                                         paper_id=row.promoted_paper_id)
    _settings_or_400(db)
    if background:
        state = related.start_related_promote_background(candidate_id,
                                                         trigger_pipeline=trigger_pipeline)
        return schemas.RelatedPromoteOut(candidate_id=row.id, promote_state=state)
    try:
        result = related.promote_related_candidate(db, row, trigger_pipeline=trigger_pipeline)
    except Exception as exc:  # noqa: BLE001
        logger.warning("关联候选提升入库失败（candidate=%s）：%s", candidate_id, str(exc)[:300])
        raise HTTPException(status_code=502, detail="加入详细阅读失败：{}".format(str(exc)[:200]))
    return schemas.RelatedPromoteOut(candidate_id=row.id, promote_state=row.promote_state,
                                     paper_id=result.get("paper_id"),
                                     pipeline=result.get("pipeline"))


@router.post("/related/candidates/{candidate_id}/discard",
             response_model=schemas.RelatedCandidateOut)
def discard_related_candidate_endpoint(candidate_id: int,
                                        db: Session = Depends(database.get_db)):
    """丢弃候选（保留记录便于回看与去重，不再出现在强相关列表里）。"""
    row = store.discard_related_candidate(db, _candidate_or_404(db, candidate_id))
    return store.related_candidate_payload_single(db, row)


@router.post("/related/candidates/{candidate_id}/retry", response_model=schemas.RelatedRunOut)
def retry_related_candidate(candidate_id: int, db: Session = Depends(database.get_db)):
    row = _candidate_or_404(db, candidate_id)
    _settings_or_400(db)
    running = store.find_running_related_run(db, row.source_paper_id)
    if running:
        raise HTTPException(status_code=409, detail="请等待当前检索完成后重试")
    if not row.processing_error and row.relevance != "uncertain":
        raise HTTPException(status_code=409, detail="该条目没有失败或待核实步骤")
    run_id = related.start_related_background(row.source_paper_id, mode="retry", candidate_id=row.id)
    db.expire_all()
    return store.related_run_payload(store.get_related_run(db, run_id))


@router.delete("/related/candidates/{candidate_id}", status_code=204)
def delete_related_candidate_endpoint(candidate_id: int,
                                       db: Session = Depends(database.get_db)):
    """删除候选记录（已提升入库的论文本体不受影响）。"""
    store.delete_related_candidate(db, _candidate_or_404(db, candidate_id))
    return None
