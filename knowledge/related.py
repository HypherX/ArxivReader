"""关联检索：读 Related Work 与 References 提取引用标题，arXiv 模糊检索，
再按标题匹配度过滤、用摘要直接判定相关度，保留高分候选供用户筛选。

失败与不相关分开保存；继续筛选和重试复用成功结果。任务固定语言快照，
取消传入模型流和 arXiv 请求队列，所有退出路径均写入明确终态。
"""

import datetime
import hashlib
import json
import logging
import os
import math
import re
import threading
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterator, List, Optional, Tuple

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app import arxiv_service, database, pdf_service, settings_store
from harness.config import get_pdf_dir
from app.models import Paper, RelatedCandidate, RelatedRun
from harness import config as harness_config
from harness.paper import PaperInput
from harness.cancel import Cancelled
from harness.language import choose, runtime_config, PROMPT_VERSION
from .review import split_text

from . import arxiv_search, llm_steps, store

logger = logging.getLogger(__name__)

STAGES: Tuple[str, ...] = ("collect", "extract", "search", "judge")
STAGE_TITLES = {
    "collect": "读取论文与引用材料", "extract": "提取引用标题",
    "search": "arXiv 模糊检索", "judge": "按摘要判定相关度",
}
STAGE_TITLES_EN = {
    "collect": "Collect source material", "extract": "Extract citation titles",
    "search": "Fuzzy arXiv search", "judge": "Judge relevance from abstracts",
}
RELATED_PIPELINE_STEPS: Tuple[str, ...] = ("tree", "deep", "graph")
_RELEVANCE = {"strong", "related", "uncertain", "unrelated"}
_RETAIN_SCORE_FLOOR = 0.6      # 相关度高于此值才保留给用户筛选，其余按不相关丢弃
_EXTRACT_BUDGET = 10000


def _utcnow() -> datetime.datetime:
    return datetime.datetime.utcnow()


# ---------------------------------------------------------------- 输入组装
def _extract_sections(paper: Paper) -> Tuple[str, str]:
    """取 Related Work 与 References 正文（章节识别失败返回空串）。"""
    doc = PaperInput.from_any(paper).document()
    def full_section(key):
        section = doc.match_section(key)
        return "\n".join(block.text for block in section.blocks) if section else ""
    return full_section("related_work"), full_section("references")


def _seed_input(paper: Paper, related_text: str, refs_text: str) -> str:
    doc = PaperInput.from_any(paper).document()
    method = doc.match_section("method")
    method_text = "\n".join(b.text for b in method.blocks) if method else ""
    method_parts = split_text(method_text, 2000)
    return json.dumps({"source": {"title": paper.title, "arxiv_id": paper.arxiv_id,
                                  "abstract": paper.abstract,
                                  "method": method_parts[0] if method_parts else "",
                                  "method_truncated": len(method_parts) > 1},
                       "related_work": related_text, "references": refs_text},
                       ensure_ascii=False)


def _judge_input(paper: Paper, rows: List[RelatedCandidate]) -> str:
    return json.dumps({"source": {"title": paper.title, "abstract": paper.abstract,
                                  "arxiv_id": paper.arxiv_id},
                       "candidates": [{"id": row.id, "title": row.title,
                                       "abstract": row.abstract, "authors": row.authors,
                                       "query_sources": json.loads(row.query_sources_json or "[]")}
                                      for row in rows]}, ensure_ascii=False)


def _clamp_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, score)) if math.isfinite(score) else 0.0


def _clean_seeds(payload: Any) -> List[Dict[str, str]]:
    """清洗模型输出的种子：去空 / 去重 / 限长。不设数量上限——引用列表多长就取多长。"""
    raw = payload.get("seeds") if isinstance(payload, dict) else None
    seeds: List[Dict[str, str]] = []
    seen: set = set()
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        try:
            aid = arxiv_service.strip_version(arxiv_service.normalize_arxiv_id(str(item.get("arxiv_id") or "")))
        except ValueError:
            aid = ""
        key = aid or title.lower()
        if (not aid and len(title) < 8) or key in seen:
            continue
        seen.add(key)
        seeds.append({"title": title[:300], "arxiv_id": aid,
                      "authors": str(item.get("authors") or "")[:120],
                      "reason": str(item.get("reason") or "")[:200]})
    return seeds


def _validate_verdicts(data: Any, rows: List[RelatedCandidate], language: str = "zh") -> Dict[int, Dict[str, Any]]:
    cfg = {"output_language": language}
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or len(items) != len(rows):
        raise ValueError(choose(cfg, "判定未覆盖本批全部候选", "Incomplete batch verdict"))
    expected, output = {row.id for row in rows}, {}
    for item in items:
        if not isinstance(item, dict) or type(item.get("id")) is not int:
            raise ValueError(choose(cfg, "候选 ID 非法", "Invalid candidate ID"))
        cid = item["id"]
        if cid not in expected or cid in output or item.get("relevance") not in _RELEVANCE:
            raise ValueError(choose(cfg, "候选 ID 或相关性枚举非法", "Invalid candidate ID or relevance enum"))
        if not isinstance(item.get("reason"), str) or not item["reason"].strip():
            raise ValueError(choose(cfg, "缺少判定理由", "Missing relevance rationale"))
        score = item.get("score")
        if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError(choose(cfg, "相关性分数非法", "Invalid relevance score"))
        output[cid] = item
    return output


# ---------------------------------------------------------------- 编排
@dataclass
class RelatedOutcome:
    run_id: Optional[int] = None
    status: str = "done"
    counters: Dict[str, int] = field(default_factory=dict)
    error: str = ""


def _cancelled(db: Session, run: RelatedRun) -> bool:
    db.refresh(run)
    return bool(run.cancel_requested)


def _finish(db: Session, run: RelatedRun, status: str, counters: Dict[str, int],
            *, error: str = "") -> Dict[str, Any]:
    """终结一轮关联检索：必须同时写 status + stage + finished_at。

    （沿用检索 pipeline 的教训：只写 stage 会让前端轮询永不停止。）
    """
    store.update_related_run(db, run, status=status, stage="done", error=error,
                             counters=counters, finished=True)
    return {"type": "final", "run_id": run.id, "status": status,
            "counters": counters, "error": error}


def iter_related_events(db: Session, run: RelatedRun, *,
                        hcfg: Optional[Dict[str, Any]] = None,
                        kcfg: Optional[Dict[str, Any]] = None,
                        settings: Any = None) -> Iterator[Dict[str, Any]]:
    """流式执行关联检索 pipeline：每步产出事件（STAGE_TITLES），最后给 final。

    事件：{"type":"stage","stage":..,"title":..,"status":"running|done|failed","detail":..}
          {"type":"progress","stage":"judge","done":..,"total":..}
          {"type":"final","run_id":..,"status":..,"counters":{...}}
    """
    kcfg = kcfg or harness_config.get_knowledge_config()
    settings = settings or settings_store.get_llm_settings(db)
    hcfg = runtime_config(settings, hcfg or settings_store.get_harness_settings(db))
    run.output_language = hcfg["output_language"]
    db.commit()
    paper = db.get(Paper, run.paper_id)
    if paper is None:
        yield _finish(db, run, "failed", {}, error=choose(hcfg, "论文不存在（可能已被删除）", "Source paper is missing or was deleted"))
        return

    # 不再设"种子数 / 候选数 / 保留数"上限：引用与 Related Work 里提到多少就取多少。
    # per_seed 是每个标题向 arXiv 取的模糊候选数（不是总量上限），batch_size 只是判定调用的
    # token 分批，二者都不是功能限制。
    per_seed = max(1, int(kcfg.get("related_title_matches") or 3))
    batch_size = max(1, min(8, int(kcfg.get("related_batch_size") or 8)))
    counters: Dict[str, Any] = dict(run.counters)
    titles = STAGE_TITLES_EN if hcfg["output_language"] == "en" else STAGE_TITLES

    def check():
        if _cancelled(db, run):
            raise Cancelled("已被用户终止")
        return False

    def failure(rows, stage, error):
        for row in rows:
            row.processing_error = stage + ": " + str(error)[:500]
        db.commit()
        counters["failed"] = counters.get("failed", 0) + len(rows)
        store.log_related(db, run, str(error)[:500], level="error")

    def _emit_stage(stage: str, status: str, detail: str = "", **extra: Any) -> Dict[str, Any]:
        record = {"type": "stage", "stage": stage, "title": titles[stage],
                  "status": status, "detail": detail, **extra}
        store.update_related_run(db, run,
                                 stage=stage if status == "running" else run.stage,
                                 counters=counters)
        store.log_related(db, run, detail, code="log.stage", params={},
                                  stage=stage, status=status, original=detail)
        return record

    try:
        check()
        mode = counters.get("mode", "search")
        if mode == "search" or counters.get("resume_search"):
            counters["search_complete"] = False
            yield _emit_stage("collect", "running")
            related_text, refs_text = _extract_sections(paper)
            counters.update(related_work_chars=len(related_text), references_chars=len(refs_text))
            if not related_text and not refs_text:
                store.log_related(db, run, choose(hcfg, "未识别到 Related Work 或参考文献章节，本轮没有可提取的引用标题。",
                                                  "No Related Work or References section found; no citation titles to extract."),
                                  level="warn")
            yield _emit_stage("collect", "done")
            yield _emit_stage("extract", "running")
            pieces = [("related_work", p) for p in split_text(related_text, _EXTRACT_BUDGET)]
            pieces += [("references", p) for p in split_text(refs_text, _EXTRACT_BUDGET)]
            seeds = []
            extraction_cache = counters.setdefault("extraction_cache", {})
            search_cache = counters.setdefault("search_cache", {})
            counters["extraction_failed"] = 0
            counters["extraction_batches"] = len(pieces) or 1
            for kind, text in pieces:
                check()
                material = _seed_input(paper, text if kind == "related_work" else "",
                                       text if kind == "references" else "")
                key = hashlib.sha256((PROMPT_VERSION + hcfg["output_language"] + material).encode()).hexdigest()
                data = extraction_cache.get(key)
                if data is None:
                    out = llm_steps.call_structured("related_seed_extract.md", material,
                                                   settings, cfg=hcfg, should_stop=check)
                    counters["total_tokens"] = counters.get("total_tokens", 0) + (out.usage or {}).get("total_tokens", 0)
                    if not out.ok or not isinstance(out.data, dict):
                        counters["extraction_failed"] += 1
                        store.log_related(db, run, out.error or choose(hcfg, "引用标题提取失败", "Citation title extraction failed"), level="error")
                        continue
                    data = out.data
                    extraction_cache[key] = data
                    store.update_related_run(db, run, counters=counters)
                extracted = _clean_seeds(data)
                # ID 必须能在提供的引用材料中找到，防止模型伪造确定引用。
                for seed in extracted:
                    if seed["arxiv_id"] and seed["arxiv_id"] not in text:
                        seed["arxiv_id"] = ""
                seeds += extracted
            seeds = _clean_seeds({"seeds": seeds})
            counters.update(seeds=len(seeds), query_plan={"seeds": seeds})
            yield _emit_stage("extract", "done")
            yield _emit_stage("search", "running")
            collected = {}
            counters.update(raw_hits=0, duplicated=0, self_excluded=0, search_failed=0)
            for seed in seeds:
                check()
                try:
                    key = hashlib.sha256(json.dumps([seed["title"], seed["arxiv_id"], per_seed],
                                                    sort_keys=True).encode()).hexdigest()
                    if key in search_cache:
                        hits = [arxiv_search.SearchHit(**hit) for hit in search_cache[key]]
                    else:
                        hits = arxiv_search.search_reference(seed, max_results=per_seed, should_stop=check)
                        search_cache[key] = [hit.as_dict() for hit in hits]
                        store.update_related_run(db, run, counters=counters)
                except Cancelled:
                    raise
                except Exception as exc:
                    counters["search_failed"] += 1
                    store.log_related(db, run, str(exc)[:300], level="error")
                    continue
                counters["raw_hits"] += len(hits)
                for hit in hits:
                    hit.arxiv_id = arxiv_service.strip_version(hit.arxiv_id)
                    if hit.arxiv_id == arxiv_service.strip_version(paper.arxiv_id):
                        counters["self_excluded"] += 1
                        continue
                    source = {"kind": "reference", "seed": seed, "exact_reference": False}
                    hit.query_sources = (hit.query_sources or []) + [source]
                    if hit.arxiv_id in collected:
                        old = collected[hit.arxiv_id][0]
                        old.query_sources += hit.query_sources
                        old.match_score = max(old.match_score, hit.match_score)
                        continue
                    collected[hit.arxiv_id] = (hit, seed["title"])
            counters["found"] = len(collected)
            counters["deduplicated_hits"] = counters["raw_hits"] - len(collected) - counters["self_excluded"]
            selected = sorted(collected.values(), key=lambda pair: -pair[0].match_score)
            for hit, seed_title in selected:
                row, created = store.upsert_related_candidate(db, source_paper_id=paper.id,
                    hit=hit, seed_title=seed_title, run_id=run.id)
                if not created:
                    counters["duplicated"] += 1
            counters["search_complete"] = not (counters.get("extraction_failed") or counters.get("search_failed"))
            yield _emit_stage("search", "done")
        else:
            for stage in ("collect", "extract", "search"):
                yield _emit_stage(stage, "skipped", choose(hcfg, "复用已保存候选", "Reuse saved candidates"))

        rows = store.list_related_candidates(db, paper.id, limit=100000)
        if counters.get("candidate_id"):
            rows = [r for r in rows if r.id == counters["candidate_id"]]
        if mode == "retry" and not counters.get("resume_search"):
            rows = [r for r in rows if r.processing_error or r.relevance == "uncertain"]
            for row in rows:
                if row.relevance == "uncertain" and not row.processing_error.startswith("summary:"):
                    row.relevance = ""
            db.commit()
        pending = [r for r in rows if not r.relevance or r.processing_error.startswith("judge:")]
        counters["selected"] = len(pending)
        yield _emit_stage("judge", "running")
        # 逐批判定，直到全部候选都有结论：batch_size 只是单次调用的 token 分批，不是保留上限
        processed = 0
        while processed < len(pending):
            check()
            batch = pending[processed:processed + batch_size]
            try:
                out = llm_steps.call_structured("related_judge.md", _judge_input(paper, batch),
                                                settings, cfg=hcfg, should_stop=check)
                counters["total_tokens"] = counters.get("total_tokens", 0) + (out.usage or {}).get("total_tokens", 0)
                if not out.ok:
                    raise ValueError(out.error or choose(hcfg, "相关性筛选失败", "Relevance screening failed"))
                verdicts = _validate_verdicts(out.data, batch, hcfg["output_language"])
                for row in batch:
                    item = verdicts[row.id]
                    row.output_language = hcfg["output_language"]
                    store.set_related_verdict(db, row, relevance=item["relevance"],
                                              score=item["score"], reason=item["reason"])
                    counters[row.relevance] = counters.get(row.relevance, 0) + 1
                    # 相关度不高于阈值的候选直接丢弃，不进入用户筛选列表（已入库的不动）。
                    if row.relevance_score <= _RETAIN_SCORE_FLOOR and row.status != store.RELATED_PROMOTED:
                        store.discard_related_candidate(db, row)
                        counters["dropped_low_score"] = counters.get("dropped_low_score", 0) + 1
            except Cancelled:
                raise
            except Exception as exc:
                db.rollback()
                failure(batch, "judge", exc)
            processed += len(batch)
            yield {"type": "progress", "stage": "judge", "done": processed,
                   "total": len(pending), "counters": dict(counters)}
        counters["remaining"] = len(pending) - processed
        yield _emit_stage("judge", "done")
        incomplete = any(counters.get(k) for k in ("failed", "search_failed", "extraction_failed"))
        yield _finish(db, run, "failed" if incomplete else "done", counters,
                      error=choose(hcfg, "部分阶段失败；结果已保留，可重试。", "Some stages failed; saved results can be retried.") if incomplete else "")
    except Cancelled:
        db.rollback()
        yield _finish(db, run, "cancelled", counters)
    except Exception as exc:
        db.rollback()
        store.log_related(db, run, str(exc)[:300], level="error")
        yield _finish(db, run, "failed", counters, error=str(exc)[:500])


def run_related_pipeline(db: Session, run: RelatedRun, *,
                         hcfg: Optional[Dict[str, Any]] = None,
                         kcfg: Optional[Dict[str, Any]] = None,
                         settings: Any = None) -> RelatedOutcome:
    """汇总式执行（后台线程 / 测试）：消费事件流并返回结果对象。

    三重兜底：事件流没有 final 帧时置 failed，绝不让 run 永久停在 running。
    """
    outcome = RelatedOutcome(run_id=run.id)
    got_final = False
    for event in iter_related_events(db, run, hcfg=hcfg, kcfg=kcfg, settings=settings):
        if event.get("type") == "final":
            got_final = True
            outcome.status = event.get("status", "done")
            outcome.counters = event.get("counters") or {}
            outcome.error = event.get("error") or ""
    if not got_final:
        outcome.status = "failed"
        outcome.error = "关联检索流程异常结束（未收到最终事件）"
    # error 传 None = 不覆写：失败原因已在 _finish 写过，不能被空字符串抹掉
    store.update_related_run(db, run, status=outcome.status, error=outcome.error or None,
                             counters=outcome.counters, finished=True)
    return outcome


# ---------------------------------------------------------------- 后台线程
def start_related_background(paper_id: int, *, run_id: Optional[int] = None,
                             mode: str = "search", candidate_id: Optional[int] = None) -> int:
    """在后台线程里跑关联检索（不阻塞前端）；返回 run_id。

    线程内使用独立 DB 会话；异常统一落到 related_runs.error，前端轮询即可看到。
    """
    db = database.SessionLocal()
    try:
        # SQLite 写锁内检查与认领，避免同时点击在多个进程创建重复任务。
        db.execute(sql_text("BEGIN IMMEDIATE"))
        active = store.find_running_related_run(db, paper_id)
        if active is not None and active.id != run_id:
            return active.id
        previous = store.latest_related_run(db, paper_id)
        previous_counters = previous.counters if previous else {}
        resume_search = mode == "retry" and not candidate_id and previous is not None and (
            previous.status in ("interrupted", "cancelled") or
            previous_counters.get("search_complete") is False or
            any(previous_counters.get(k) for k in ("search_failed", "extraction_failed")))
        counters = {"mode": mode, "candidate_id": candidate_id, "resume_search": resume_search}
        if resume_search:
            for key in ("extraction_cache", "search_cache"):
                counters[key] = previous_counters.get(key, {})
        run = store.get_related_run(db, run_id) if run_id else None
        if run is None:
            run = RelatedRun(paper_id=paper_id, status="running", stage="collect")
            db.add(run)
            db.flush()
        run_id = run.id
        settings = settings_store.get_llm_settings(db)
        candidate = db.get(RelatedCandidate, candidate_id) if candidate_id else None
        resume_language = (candidate.output_language if candidate else
                           previous.output_language if previous and mode in ("retry", "continue") else None)
        if candidate and resume_language not in ("zh", "en"):
            resume_language = candidate.stats.get("output_language")
        if resume_language in ("zh", "en"):
            settings = replace(settings, output_language=resume_language)
        hcfg = runtime_config(settings, settings_store.get_harness_settings(db))
        run.output_language = hcfg["output_language"]
        run.counters_json = json.dumps(counters)
        db.commit()
    finally:
        db.close()

    def _worker() -> None:
        worker_db = database.SessionLocal()
        try:
            run = store.get_related_run(worker_db, run_id)
            if run is None:
                return
            run_related_pipeline(worker_db, run, settings=settings, hcfg=hcfg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("关联检索后台任务异常（run=%s）：%s", run_id, str(exc)[:300])
            try:
                worker_db.rollback()
                run = store.get_related_run(worker_db, run_id)
                if run is not None:
                    store.update_related_run(worker_db, run, status="failed", stage="done",
                                             error=str(exc)[:500], finished=True)
            except Exception:  # noqa: BLE001
                pass
        finally:
            worker_db.close()

    try:
        threading.Thread(target=_worker, name="related-run-{}".format(run_id), daemon=True).start()
    except Exception as exc:
        with database.SessionLocal() as failed_db:
            run = failed_db.get(RelatedRun, run_id)
            store.update_related_run(failed_db, run, status="failed", stage="done",
                                     error=str(exc)[:500], finished=True)
        raise
    return run_id


def recover_interrupted(db: Session) -> None:
    for run in db.query(RelatedRun).filter_by(status="running").all():
        run.status, run.stage, run.finished_at = "interrupted", "done", _utcnow()
        run.error = choose({"output_language": run.output_language},
                           "服务重启导致中断，可重试；已完成结果保留", "Interrupted by a server restart; saved results can be retried")
    for row in db.query(RelatedCandidate).filter(RelatedCandidate.promote_state.in_(["queued", "running"])).all():
        row.promote_state = "failed"
        row.promote_error = choose({"output_language": row.output_language},
                                   "服务重启导致中断，可重试入库", "Import interrupted by a server restart; retry to continue")
    db.commit()


# ---------------------------------------------------------------- 候选 -> 正式库（加入详细阅读）
def promote_related_candidate(db: Session, row: RelatedCandidate, *,
                              settings: Any = None,
                              trigger_pipeline: bool = True) -> Dict[str, Any]:
    """把强相关候选迁入正式库：下载全文 -> 建 Paper -> 预写 summary 产物 -> 触发知识网络。

    流程衔接的关键：候选在检索阶段已有 quick_summary，这里把它预写为 paper_artifacts
    的 summary 产物，再按 `tree -> deep -> graph` 运行 pipeline，
    因此「跳过 summary 步骤」与现有存储/图谱能力完全复用（幂等、可断点续跑）。
    """
    settings = settings or settings_store.get_llm_settings(db)
    existing = db.query(Paper).filter(Paper.arxiv_id == row.arxiv_id).first()
    if existing is None:
        meta = arxiv_service.fetch_metadata(row.arxiv_id)
        # 先不落任何文件夹：提升后只属于"全部论文"，tree 步骤若跑过会自动移入方向文件夹
        dest_dir = os.path.join(get_pdf_dir(), arxiv_service.safe_dirname(arxiv_service.UNFILED_DIR))
        pdf_path = arxiv_service.download_pdf(meta, dest_dir)
        text = pdf_service.extract_text(str(pdf_path))
        existing = Paper(arxiv_id=meta.arxiv_id, title=meta.title or row.title,
                         authors_json=json.dumps(meta.authors or row.authors, ensure_ascii=False),
                         abstract=meta.abstract or row.abstract,
                         categories_json=json.dumps(meta.categories or row.categories,
                                                    ensure_ascii=False),
                         published=meta.published or row.published,
                         pdf_url=meta.pdf_url, pdf_path=str(pdf_path), folder_id=folder.id,
                         full_text=text)
        db.add(existing)
        db.commit()
        db.refresh(existing)
    if (row.summary_text or "").strip() and store.get_artifact(db, existing.id, store.ARTIFACT_SUMMARY) is None:
        stats = row.stats or {}
        store.upsert_artifact(db, existing.id, store.ARTIFACT_SUMMARY, row.summary_text,
                              stats=stats, model=str(stats.get("model") or ""),
                              reasoning_effort=stats.get("reasoning_effort"))
    row.status = store.RELATED_PROMOTED
    row.promote_state = "running" if trigger_pipeline else "done"
    row.promote_error = ""
    row.promoted_paper_id = existing.id
    row.updated_at = _utcnow()
    db.commit()

    from . import pipeline as knowledge_pipeline
    artifact = store.get_artifact(db, existing.id, store.ARTIFACT_SUMMARY)
    cfg = runtime_config(settings, settings_store.get_harness_settings(db))
    steps = RELATED_PIPELINE_STEPS if knowledge_pipeline._current_artifact(artifact, cfg) else ("summary",) + RELATED_PIPELINE_STEPS
    result: Dict[str, Any] = {"paper_id": existing.id, "arxiv_id": existing.arxiv_id,
                              "title": existing.title, "steps": list(steps)}
    if trigger_pipeline:
        outcome = knowledge_pipeline.run_pipeline(db, existing, steps=steps,
                                                  settings=settings)
        result["pipeline"] = {"run_id": outcome.run_id, "status": outcome.status,
                              "steps": outcome.steps}
        row.promote_state = "done" if outcome.status == "done" else "failed"
        if outcome.status != "done":            # 已入库但流程未完成：把原因留给前端提示
            row.promote_error = "已入库，但知识网络流程{}：{}".format(
                "被终止" if outcome.status == "cancelled" else "未完成",
                (outcome.error or "")[:300])
        db.commit()
    return result


def _set_related_promote_state(db: Session, row: RelatedCandidate, state: str,
                               error: str = "") -> None:
    row.promote_state = state
    row.promote_error = (error or "")[:1000]
    row.updated_at = _utcnow()
    db.commit()


def start_related_promote_background(candidate_id: int, *,
                                     trigger_pipeline: bool = True) -> str:
    """后台「加入详细阅读」（下载全文 + 建 Paper + 跑知识网络）；返回初始状态。

    前端通过 `GET /api/related/candidates/{id}` 的 `promote_state` 轮询进度
    （queued → running → done/failed，失败原因看 promote_error）。
    """
    db = database.SessionLocal()
    try:
        db.execute(sql_text("BEGIN IMMEDIATE"))
        row = store.get_related_candidate(db, candidate_id)
        if row is None:
            raise ValueError("候选记录不存在：id={}".format(candidate_id))
        if row.status == store.RELATED_PROMOTED and row.promoted_paper_id and row.promote_state == "done":
            return "done"
        if row.promote_state in ("queued", "running"):
            return row.promote_state
        settings = settings_store.get_llm_settings(db)
        _set_related_promote_state(db, row, "queued")
    finally:
        db.close()

    def _worker() -> None:
        worker_db = database.SessionLocal()
        try:
            row = store.get_related_candidate(worker_db, candidate_id)
            if row is None:
                return
            _set_related_promote_state(worker_db, row, "running")
            promote_related_candidate(worker_db, row, trigger_pipeline=trigger_pipeline, settings=settings)
        except Exception as exc:  # noqa: BLE001  失败写回状态，前端可见
            logger.warning("关联候选提升入库失败（candidate=%s）：%s", candidate_id, str(exc)[:300])
            try:
                worker_db.rollback()
                row = store.get_related_candidate(worker_db, candidate_id)
                if row is not None:
                    _set_related_promote_state(worker_db, row, "failed", str(exc))
            except Exception:  # noqa: BLE001
                pass
        finally:
            worker_db.close()

    try:
        threading.Thread(target=_worker, name="related-promote-{}".format(candidate_id), daemon=True).start()
    except Exception as exc:
        with database.SessionLocal() as failed_db:
            row = store.get_related_candidate(failed_db, candidate_id)
            if row:
                _set_related_promote_state(failed_db, row, "failed", str(exc))
        raise
    return "queued"
