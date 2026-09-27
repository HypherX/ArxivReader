"""ArXiv 检索 Pipeline：批量检索 -> 方向树预筛 -> 快速总结验证 -> 缓冲区落盘。

四步（与前端「ArXiv 检索」页一一对应）：
  search     只取元信息（title/abstract/作者/分类/日期），**不下载全文**
  screen     读现有方向树，对每篇打分并给建议路径（一次调用批式处理多篇，省 token）
  summarize  对通过预筛的论文跑 `quick_summary`（含"解决了什么/未来展望"），再用 `discovery_verify` 复核归属
  buffer     通过复核的进入待读缓冲区（status=pending，尚未深读、未入图谱）

可中断 / 可续跑：进度与日志写 `search_runs`；每个批次/每篇之前检查 cancel_requested；
已经落库的缓冲区结果不会因为中断而丢失（下次检索会被 arxiv_id 去重跳过）。

对外：
  iter_search_events(...)   流式产出事件（CLI 与后台线程共用）
  run_search_pipeline(...)  汇总式执行（后台线程用）
  promote_buffer(...)       缓冲区 -> 正式库：下载全文 + 建 Paper + 落位方向树文件夹 + 触发单篇 pipeline
"""

import datetime
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from app import arxiv_service, config_loader, database, pdf_service, settings_store
from app.models import BufferPaper, DirectionNode, Folder, Paper, SearchRun
from harness import config as harness_config, runner
from harness.paper import PaperInput

from . import arxiv_search, llm_steps, store

logger = logging.getLogger(__name__)

STAGES: Tuple[str, ...] = ("search", "screen", "summarize", "buffer")
STAGE_TITLES = {
    "search": "ArXiv 批量检索",
    "screen": "方向树预筛",
    "summarize": "快速总结验证",
    "buffer": "写入待读缓冲区",
}
_BUFFER_TEXT_LIMIT = 1200        # 单篇摘要送进 prompt 的截断上限
_SCREEN_REASON_LIMIT = 60


# ---------------------------------------------------------------- 输入组装
def _tree_paths(db: Session, limit: int) -> List[str]:
    tree = store.tree_payload(db)
    paths = [node["path"] for node in tree["nodes"] if node["paper_count"] > 0 or node["subtree_paper_count"] > 0]
    if not paths:      # 还没有任何论文时，用整棵树（可能为空）
        paths = [node["path"] for node in tree["nodes"]]
    return paths[: int(limit)]


def _screen_input(items: Sequence[BufferPaper], paths: Sequence[str]) -> str:
    blocks = ["## 现有方向树路径（优先复用）"]
    blocks.extend(("- " + p) for p in paths) if paths else blocks.append("（当前为空）")
    blocks.append("")
    blocks.append("## 候选论文（id | 标题 | 摘要）")
    for index, row in enumerate(items, start=1):
        abstract = (row.abstract or "").replace("\n", " ")[:_BUFFER_TEXT_LIMIT]
        blocks.append("### id={}".format(index))
        blocks.append("- 标题：{}".format(row.title))
        blocks.append("- 分类：{}".format(", ".join(row.categories)))
        blocks.append("- 摘要：{}".format(abstract or "（未取到摘要）"))
    return "\n".join(blocks)


def _verify_input(row: BufferPaper, summary_md: str, paths: Sequence[str]) -> str:
    blocks = ["## 论文", "- 标题：{}".format(row.title),
              "- arXiv：{}".format(row.arxiv_id),
              "- 分类：{}".format(", ".join(row.categories)),
              "- 摘要：{}".format((row.abstract or "").replace("\n", " ")[:_BUFFER_TEXT_LIMIT]),
              "", "## 快速总结", summary_md.strip(),
              "", "## 现有方向树路径"]
    blocks.extend(("- " + p) for p in paths) if paths else blocks.append("（当前为空）")
    blocks.append("")
    blocks.append("## 预筛建议：{}（分数 {:.2f}）".format(row.suggested_path or "无", row.match_score))
    return "\n".join(blocks)


def _clamp_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, score))


# ---------------------------------------------------------------- 各步实现
def _screen_batch(db: Session, rows: Sequence[BufferPaper], paths: Sequence[str],
                  settings: Any, hcfg: Dict[str, Any]) -> Dict[str, int]:
    """对一批候选做预筛：写回分数/建议路径/理由，返回 token 账本。"""
    out = llm_steps.call_structured("discovery_prefilter.md", _screen_input(rows, paths),
                                    settings, cfg=hcfg)
    if not out.ok:
        raise RuntimeError(out.error or "预筛失败")
    payload = out.data if isinstance(out.data, dict) else {}
    by_index = {int(item.get("id")): item for item in (payload.get("items") or [])
                if str(item.get("id", "")).strip().isdigit()}
    for index, row in enumerate(rows, start=1):
        item = by_index.get(index) or {}
        path = str(item.get("path") or "").strip()
        node = store.get_node_by_path(db, store.path_key(path)) if path else None
        store.set_buffer_screening(
            db, row, score=_clamp_score(item.get("score")),
            path=path, matched_node_id=node.id if node else None,
            reason=str(item.get("reason") or "")[:_SCREEN_REASON_LIMIT])
    return {"usage": dict(out.usage or {}), "reasoning_chars": len(out.reasoning or "")}


def _summarize_and_verify(db: Session, row: BufferPaper, paths: Sequence[str], settings: Any,
                          hcfg: Dict[str, Any]) -> Dict[str, Any]:
    """跑 quick_summary（仅摘要级上下文）+ 复核归属；返回 {fit, path, reason, usage}。"""
    paper_input = PaperInput(arxiv_id=row.arxiv_id, title=row.title, abstract=row.abstract,
                             authors=row.authors, categories=row.categories,
                             published=row.published, full_text="")
    summary = runner.run_skill("quick_summary", paper_input, settings, cfg=hcfg)
    if summary.error or not summary.output.strip():
        raise RuntimeError(summary.error or "快速总结为空")

    verify = llm_steps.call_structured("discovery_verify.md", _verify_input(row, summary.output, paths),
                                       settings, cfg=hcfg)
    if not verify.ok:
        raise RuntimeError(verify.error or "归属复核失败")
    payload = verify.data if isinstance(verify.data, dict) else {}
    fit = bool(payload.get("fit"))
    path = str(payload.get("path") or "").strip()
    store.set_buffer_summary(db, row, summary_text=summary.output,
                             stats={**summary.stats, "verify": {"fit": fit, "path": path,
                                                                "confidence": payload.get("confidence")}},
                             verified_path=path if fit else "",
                             verify_reason=str(payload.get("reason") or ""), fit=fit)
    return {"fit": fit, "path": path, "reason": str(payload.get("reason") or ""),
            "confidence": payload.get("confidence"),
            "usage": _merge_usage([summary.stats.get("usage") or {}, verify.usage or {}])}


def _merge_usage(usages: Iterable[Dict[str, int]]) -> Dict[str, int]:
    total: Dict[str, int] = {}
    for usage in usages:
        for key, value in (usage or {}).items():
            if isinstance(value, int):
                total[key] = total.get(key, 0) + value
    return total


# ---------------------------------------------------------------- 编排
@dataclass
class SearchOutcome:
    run_id: Optional[int] = None
    status: str = "done"
    counters: Dict[str, int] = field(default_factory=dict)
    error: str = ""


def _cancelled(db: Session, run: SearchRun) -> bool:
    db.refresh(run)
    return bool(run.cancel_requested)


def _finish(db: Session, run: SearchRun, status: str, counters: Dict[str, int],
            *, error: str = "") -> Dict[str, Any]:
    """终结一轮检索：**必须同时写 status 与 finished_at**。

    历史 bug：收尾只写 stage="done"，status 一直停在 "running"，
    前端轮询永不停止、按钮永久禁用。所有退出路径统一走这里。
    """
    store.update_search_run(db, run, status=status, stage="done", error=error,
                            counters=counters, finished=True)
    return {"type": "final", "run_id": run.id, "status": status,
            "counters": counters, "error": error}


def iter_search_events(db: Session, run: SearchRun, request: Dict[str, Any], *,
                       hcfg: Optional[Dict[str, Any]] = None,
                       kcfg: Optional[Dict[str, Any]] = None,
                       settings: Any = None) -> Iterator[Dict[str, Any]]:
    """流式执行检索 pipeline：每步产出事件（STAGE_TITLES），最后给 final。

    事件：{"type":"stage","stage":..,"title":..,"status":"running|done|skipped|failed","detail":..}
          {"type":"final","run_id":..,"status":..,"counters":{...}}
    """
    hcfg = hcfg or harness_config.get_harness_config()
    kcfg = kcfg or harness_config.get_knowledge_config()
    settings = settings or settings_store.get_llm_settings(db)

    batch_size = max(1, int(kcfg.get("discovery_batch_size") or 8))
    min_score = float(request.get("min_score") or kcfg.get("discovery_min_score") or 0.55)
    max_summarize = max(0, int(request.get("max_summarize")
                               if request.get("max_summarize") is not None
                               else (kcfg.get("discovery_max_summarize") or 12)))
    paths = _tree_paths(db, int(kcfg.get("max_tree_paths") or 200))
    counters = dict(run.counters)
    tokens = {"total_tokens": 0}

    def _emit_stage(stage: str, status: str, detail: str = "", **extra: Any) -> Dict[str, Any]:
        record = {"type": "stage", "stage": stage, "title": STAGE_TITLES[stage],
                  "status": status, "detail": detail, **extra}
        store.update_search_run(db, run, stage=stage if status == "running" else run.stage,
                                counters=counters)
        store.log_search(db, run, "[{}] {} {}".format(STAGE_TITLES[stage], status, detail).strip())
        return record

    try:
        # Step 1: 检索（只取元信息）
        yield _emit_stage("search", "running")
        hits = arxiv_search.search(
            request.get("categories") or [], request.get("keywords") or [],
            date_from=request.get("date_from"), date_to=request.get("date_to"),
            max_results=int(request.get("max_results") or 40),
            sort_by=request.get("sort_by") or "submitted",
            keyword_mode=request.get("keyword_mode") or "and")
        counters["found"] = len(hits)
        # 去重：已在正式库或缓冲区（pending）里的不再处理
        known_papers = {row[0] for row in db.query(Paper.arxiv_id).all()}
        known_buffer = {row[0] for row in db.query(BufferPaper.arxiv_id).all()}
        fresh = [h for h in hits if h.arxiv_id not in known_papers and h.arxiv_id not in known_buffer]
        counters["duplicated"] = len(hits) - len(fresh)
        yield _emit_stage("search", "done",
                          "命中 {} 篇（去重跳过 {} 篇）".format(len(hits), counters["duplicated"]),
                          count=len(hits))

        if not fresh:
            yield _finish(db, run, "done", counters)
            return
        if _cancelled(db, run):
            yield _finish(db, run, "cancelled", counters)
            return

        # Step 2: 预筛（批式，每批一次调用）
        rows = [store.upsert_buffer_paper(db, hit, search_run_id=run.id) for hit in fresh]
        store.update_search_run(db, run, counters=counters)
        yield _emit_stage("screen", "running")
        for start in range(0, len(rows), batch_size):
            if _cancelled(db, run):
                yield _finish(db, run, "cancelled", counters)
                return
            batch = rows[start:start + batch_size]
            stats = _screen_batch(db, batch, paths, settings, hcfg)
            tokens = _merge_usage([tokens, stats["usage"]])
            counters["screened"] = start + len(batch)
            for row in batch:      # 实时日志：每篇的预筛结果
                store.log_search(
                    db, run,
                    "预筛 {} → {:.2f} {}".format(row.arxiv_id, row.match_score,
                                                row.suggested_path or "（无建议路径）"),
                    level="info" if row.match_score >= min_score else "warn",
                    arxiv_id=row.arxiv_id)
            store.log_search(db, run, "预筛进度 {}-{}/{}".format(start + 1,
                                                              start + len(batch), len(rows)))
        passed = [row for row in rows if row.match_score >= min_score]
        dropped = [row for row in rows if row.match_score < min_score]
        for row in dropped:
            store.set_buffer_status(db, row, store.BUFFER_REJECTED)
            store.log_search(db, run,
                             "丢弃（预筛 {:.2f} 低于阈值）：{}".format(row.match_score, row.arxiv_id),
                             level="warn", arxiv_id=row.arxiv_id)
        counters["passed"] = len(passed)
        counters["dropped"] = counters.get("dropped", 0) + len(dropped)
        yield _emit_stage("screen", "done",
                          "预筛完成：{} 篇过线（阈值 {:.2f}），{} 篇丢弃".format(len(passed), min_score, len(dropped)),
                          passed=len(passed), dropped=len(dropped), tokens=tokens.get("total_tokens"))

        if not passed:
            store.log_search(db, run, "无过线论文（阈值 {:.2f}），本轮结束".format(min_score), level="warn")
            yield _finish(db, run, "done", counters)
            return

        # Step 3: 快速总结 + 归属复核（按分数从高到低，限流）
        candidates = sorted(passed, key=lambda r: r.match_score, reverse=True)[:max_summarize]
        yield _emit_stage("summarize", "running",
                          "对 {} 篇运行 quick_summary 并复核归属".format(len(candidates)))
        kept: List[BufferPaper] = []
        for index, row in enumerate(candidates, start=1):
            if _cancelled(db, run):
                yield _finish(db, run, "cancelled", counters)
                return
            try:
                outcome = _summarize_and_verify(db, row, paths, settings, hcfg)
                tokens = _merge_usage([tokens, outcome["usage"]])
                counters["summarized"] = counters.get("summarized", 0) + 1
                if outcome["fit"]:
                    kept.append(row)
                    store.log_search(db, run, "保留：{} → {}".format(row.arxiv_id, outcome["path"]),
                                     arxiv_id=row.arxiv_id)
                else:
                    store.set_buffer_status(db, row, store.BUFFER_REJECTED)
                    counters["dropped"] = counters.get("dropped", 0) + 1
                    store.log_search(db, run, "丢弃：{}（{}）".format(row.arxiv_id, outcome["reason"]),
                                     level="warn", arxiv_id=row.arxiv_id)
            except Exception as exc:  # noqa: BLE001  单篇失败不影响整轮
                counters["dropped"] = counters.get("dropped", 0) + 1
                store.set_buffer_status(db, row, store.BUFFER_REJECTED)
                store.log_search(db, run, "总结/复核失败：{}（{}）".format(row.arxiv_id, str(exc)[:120]),
                                 level="error", arxiv_id=row.arxiv_id)
            yield {"type": "progress", "stage": "summarize", "done": index,
                   "total": len(candidates), "arxiv_id": row.arxiv_id,
                   "counters": dict(counters, kept=len(kept))}
        yield _emit_stage("summarize", "done",
                          "复核完成：{} 篇保留 / {} 篇丢弃".format(len(kept), len(candidates) - len(kept)),
                          kept=len(kept), tokens=tokens.get("total_tokens"))

        # Step 4: 缓冲区落盘（保留状态；同时刷新对应节点的图谱 md）
        yield _emit_stage("buffer", "running")
        touched_nodes = set()
        for row in kept:
            store.set_buffer_status(db, row, store.BUFFER_PENDING)
            if row.matched_node_id:
                touched_nodes.add(row.matched_node_id)
        for node_id in touched_nodes:
            node = db.get(DirectionNode, node_id)
            if node is not None:
                store.export_node_graph_md(db, node)
        counters["buffered"] = counters.get("buffered", 0) + len(kept)
        yield _emit_stage("buffer", "done", "{} 篇进入待读缓冲区".format(len(kept)))

        yield _finish(db, run, "done", counters)
        return
    except Exception as exc:  # noqa: BLE001  整轮失败也要留下账本
        logger.warning("检索 pipeline 失败（run=%s）：%s", run.id, str(exc)[:300])
        store.log_search(db, run, "失败：{}".format(str(exc)[:200]), level="error")
        yield _finish(db, run, "failed", counters, error=str(exc)[:500])


def run_search_pipeline(db: Session, request: Dict[str, Any], *,
                        run: Optional[SearchRun] = None, **kwargs: Any) -> SearchOutcome:
    """汇总式执行（后台线程 / CLI / 测试）。

    三重兜底：即使事件流异常结束（没有 final 帧），也会把 run 置为失败，
    绝不让它永久停在 running 而把前端轮询挂死。
    """
    run = run or store.create_search_run(db, request)
    outcome = SearchOutcome(run_id=run.id)
    got_final = False
    for event in iter_search_events(db, run, request, **kwargs):
        if event.get("type") == "final":
            got_final = True
            outcome.status = event.get("status", "done")
            outcome.counters = event.get("counters") or {}
            outcome.error = event.get("error") or ""
    if not got_final:
        outcome.status = "failed"
        outcome.error = "检索流程异常结束（未收到最终事件）"
    # error 传 None = 不覆写：失败原因已在 _finish 里写过，不能被空字符串抹掉
    store.update_search_run(db, run, status=outcome.status, error=outcome.error or None,
                            counters=outcome.counters, finished=True)
    return outcome


# ---------------------------------------------------------------- 后台线程
def start_search_background(request: Dict[str, Any], *, run_id: Optional[int] = None) -> int:
    """在后台线程里跑检索 pipeline（不阻塞前端）；返回 run_id。

    线程内使用独立 DB 会话；异常统一落到 search_runs.error，前端轮询即可看到。
    """
    db = database.SessionLocal()
    try:
        run = store.get_search_run(db, run_id) if run_id else None
        if run is None:
            run = store.create_search_run(db, request)
        run_id = run.id
    finally:
        db.close()

    def _worker() -> None:
        worker_db = database.SessionLocal()
        try:
            run = store.get_search_run(worker_db, run_id)
            if run is None:
                return
            run_search_pipeline(worker_db, request, run=run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("检索后台任务异常（run=%s）：%s", run_id, str(exc)[:300])
            try:
                store.update_search_run(worker_db, run, status="failed", error=str(exc)[:500],
                                        finished=True)
            except Exception:  # noqa: BLE001
                pass
        finally:
            worker_db.close()

    threading.Thread(target=_worker, name="search-run-{}".format(run_id), daemon=True).start()
    return run_id


# ---------------------------------------------------------------- 缓冲区 -> 正式库
def promote_buffer(db: Session, row: BufferPaper, *, settings: Any = None,
                   trigger_pipeline: bool = True) -> Dict[str, Any]:
    """把缓冲区论文迁入正式库：下载全文 -> 建 Paper -> 落位方向文件夹 -> 触发单篇 pipeline。

    已是正式库论文（重复提升）时直接复用已有记录，只补状态。
    """
    settings = settings or settings_store.get_llm_settings(db)
    existing = db.query(Paper).filter(Paper.arxiv_id == row.arxiv_id).first()
    if existing is None:
        meta = arxiv_service.fetch_metadata(row.arxiv_id)
        node = db.get(DirectionNode, row.matched_node_id) if row.matched_node_id else None
        folder = store.sync_node_folders(db, node) if node is not None else None
        if folder is None:
            folder = database.ensure_inbox(db)
        dest_dir = os.path.join(config_loader.get_pdf_dir(), arxiv_service.safe_dirname(folder.name))
        pdf_path = arxiv_service.download_pdf(meta, dest_dir)
        text, truncated = pdf_service.extract_text_for_context(str(pdf_path))
        existing = Paper(arxiv_id=meta.arxiv_id, title=meta.title or row.title,
                         authors_json=json.dumps(meta.authors or row.authors, ensure_ascii=False),
                         abstract=meta.abstract or row.abstract,
                         categories_json=json.dumps(meta.categories or row.categories, ensure_ascii=False),
                         published=meta.published or row.published,
                         pdf_url=meta.pdf_url, pdf_path=str(pdf_path), folder_id=folder.id,
                         full_text=text, text_truncated=truncated)
        db.add(existing)
        db.commit()
        db.refresh(existing)
    store.set_buffer_status(db, row, store.BUFFER_READ)
    row.promoted_paper_id = existing.id
    row.promote_state = "done"
    row.promote_error = ""
    db.commit()
    result: Dict[str, Any] = {"paper_id": existing.id, "arxiv_id": existing.arxiv_id,
                              "title": existing.title}
    if trigger_pipeline:
        from . import pipeline as knowledge_pipeline
        outcome = knowledge_pipeline.run_pipeline(db, existing, settings=settings)
        result["pipeline"] = {"run_id": outcome.run_id, "status": outcome.status,
                              "steps": outcome.steps}
    return result


def _set_promote_state(db: Session, row: BufferPaper, state: str, error: str = "") -> None:
    row.promote_state = state
    row.promote_error = (error or "")[:1000]
    row.updated_at = datetime.datetime.utcnow()
    db.commit()


def start_promote_background(buffer_id: int, *, trigger_pipeline: bool = True) -> str:
    """后台提升入库（下载全文 + 建 Paper + 跑单篇 pipeline）；返回初始状态。

    前端通过 `GET /api/buffer/{id}` 的 `promote_state` 轮询进度
    （queued → running → done/failed，失败原因看 promote_error）。
    """
    db = database.SessionLocal()
    try:
        row = store.get_buffer(db, buffer_id)
        if row is None:
            raise ValueError("缓冲区记录不存在：id={}".format(buffer_id))
        if row.status == store.BUFFER_READ:
            return row.promote_state or "done"
        _set_promote_state(db, row, "queued")
    finally:
        db.close()

    def _worker() -> None:
        worker_db = database.SessionLocal()
        try:
            row = store.get_buffer(worker_db, buffer_id)
            if row is None:
                return
            _set_promote_state(worker_db, row, "running")
            promote_buffer(worker_db, row, trigger_pipeline=trigger_pipeline)
        except Exception as exc:  # noqa: BLE001  失败写回状态，前端可见
            logger.warning("提升入库失败（buffer=%s）：%s", buffer_id, str(exc)[:300])
            try:
                row = store.get_buffer(worker_db, buffer_id)
                if row is not None:
                    _set_promote_state(worker_db, row, "failed", str(exc))
            except Exception:  # noqa: BLE001
                pass
        finally:
            worker_db.close()

    threading.Thread(target=_worker, name="promote-buffer-{}".format(buffer_id),
                     daemon=True).start()
    return "queued"
