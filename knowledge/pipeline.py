"""阅读 pipeline：快速总结 -> 唯一分类 -> 深度精读 -> 论文关系。

设计要点
  - **分步可观测**：每步产出事件（running / done / skipped / failed + 用时 + token 账本），
    逐条落 PipelineRun.steps_json，失败也能看到"卡在哪一步、为什么"。
  - **幂等可重跑**：产物（artifact）/ 归属（membership）/ 关系边存在即跳过；force=True 强制重跑。
  - **失败不掩盖**：某步失败只标记该步，后续依赖步骤标 skipped，整轮状态置 failed；
    已成功的步骤不回滚，下次运行直接复用（配合 reuse_artifacts）。
  - **零 prompt 泄漏**：prompt 全在 harness/prompts/，输入由本模块按步组装。
  - **人类可读优先**：方向树存的是自然语言 reason/description；图谱边带 rationale 供悬停展示。

对外：
  STEPS / iter_pipeline_events()  流式事件（CLI 与 SSE 接口共用）
  run_pipeline()                  汇总式调用（后台任务、脚本、测试用）
"""

import datetime
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from app import settings_store
from app.models import Paper, PaperDirection, PipelineRun
from harness import config as harness_config, runner
from harness.cancel import Cancelled
from harness.paper import PaperInput
from harness.language import runtime_config, PROMPT_VERSION, choose

from . import llm_steps, store
from .arxiv_search import title_similarity

logger = logging.getLogger(__name__)

STEPS: Tuple[str, ...] = ("summary", "tree", "deep", "graph")
STEP_TITLES = {
    "summary": "快速总结",
    "tree": "归入方向树",
    "deep": "深度精读",
    "graph": "构建局部图谱",
}
STEP_TITLES_EN = {"summary": "Quick summary", "tree": "Classification", "deep": "Deep reading", "graph": "Paper relationships"}
# 单步送入模型的材料上限（字符），防止 prompt 随知识库增长而失控
_DEEP_NOTE_LIMIT = 14000


def _utcnow() -> datetime.datetime:
    return datetime.datetime.utcnow()


def _iso(value: Optional[datetime.datetime]) -> Optional[str]:
    return value.isoformat() if value else None


@dataclass
class PipelineResult:
    """一轮 pipeline 的结果（steps 为逐步账本）。"""

    paper_id: int
    run_id: Optional[int] = None
    status: str = "done"
    steps: List[Dict[str, Any]] = field(default_factory=list)
    error: str = ""
    syntheses: List[Dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------- 输入组装
def _tree_input(paper: Paper, summary_md: str, paths: Sequence[Any]) -> str:
    return json.dumps({"title": paper.title, "abstract": paper.abstract,
                       "summary": summary_md, "existing_nodes": list(paths)}, ensure_ascii=False)


def _verify_edge_pairing(edges: List[Any], batch_others: Sequence[Dict[str, Any]],
                         src_paper_id: int, node_path: str) -> List[Dict[str, Any]]:
    """用回显标题校验「ID ↔ 理由」配对：多论文一批时模型可能把理由配错 ID。

    - ID 不在本批、但回显标题能定位到批次内论文（≥0.5）：按标题救回；
    - 回显与批次内另一篇更匹配（≥0.5）：以标题为准修正 other_paper_id；
    - 回显与任何标题都对不上（<0.3）：配对无法核实，丢弃该边；
    - 中间地带或无回显：仅当 ID 本身合法才保留。
    """
    titles = {int(p["id"]): str(p.get("title") or "") for p in batch_others}
    checked: List[Dict[str, Any]] = []
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        raw_id = edge.get("other_paper_id")
        echo = str(edge.get("other_title") or "").strip()
        scored = sorted(((title_similarity(echo, title), pid) for pid, title in titles.items()),
                        key=lambda pair: -pair[0]) if echo else []
        best_score, best_id = scored[0] if scored else (0.0, 0)
        if echo and best_score >= 0.5:
            if best_id != raw_id:
                logger.warning("关系边配对纠正（paper=%s node=%s）：ID %r -> %s（回显标题 %r）",
                               src_paper_id, node_path, raw_id, best_id, echo[:60])
                edge["other_paper_id"] = best_id
            checked.append(edge)
        elif raw_id in titles and (not echo or best_score < 0.3 or (best_id == raw_id and best_score >= 0.3)):
            # ID 合法且回显没有明确指向另一篇（回显缺失/不可识别/确证本人）→ 保留
            checked.append(edge)
        else:
            logger.warning("关系边无法核实，丢弃（paper=%s node=%s）：id=%r echo=%r",
                           src_paper_id, node_path, raw_id, echo[:60])
    return checked


def _relation_input(paper: Paper, deep_md: str, others: Sequence[Paper], language: str = "zh") -> str:
    def material(p: Paper, note: str = "") -> Dict[str, Any]:
        rendered = PaperInput.from_any(p).document().render(tokens_budget=4000, language=language)
        notes = note or "\n\n".join(a.content_md for a in p.artifacts)
        return {"id": p.id, "title": p.title, "abstract": p.abstract,
                "notes": notes[:_DEEP_NOTE_LIMIT], "notes_truncated": len(notes) > _DEEP_NOTE_LIMIT,
                "source_text": rendered.text, "context_truncated": rendered.truncated,
                "coverage": "full_text" if p.full_text else "abstract_only"}
    return json.dumps({"source": material(paper, deep_md),
                       "others": [material(p) for p in others]}, ensure_ascii=False)


def _current_artifact(artifact: Any, hcfg: Dict[str, Any]) -> bool:
    return bool(artifact and artifact.stats.get("output_language") == hcfg.get("output_language")
                and artifact.stats.get("prompt_version") == PROMPT_VERSION)


# ---------------------------------------------------------------- 各步实现
def _step_summary(db: Session, paper: Paper, *, force: bool, settings: Any,
                  hcfg: Dict[str, Any], kcfg: Dict[str, Any],
                  should_stop: Optional[Any] = None) -> Dict[str, Any]:
    existing = store.get_artifact(db, paper.id, store.ARTIFACT_SUMMARY)
    if _current_artifact(existing, hcfg) and kcfg.get("reuse_artifacts", True) and not force:
        return {"status": "skipped", "detail": choose(hcfg, "复用当前语言摘要", "Reused current-language summary")}
    result = runner.run_skill("quick_summary", paper, settings, cfg=hcfg,
                              should_stop=should_stop)
    if result.error or not result.output.strip():
        raise RuntimeError(result.error or choose(hcfg, "摘要为空", "Summary is empty"))
    store.upsert_artifact(db, paper.id, store.ARTIFACT_SUMMARY, result.output,
                          reasoning_md=_reasoning_text(result, kcfg),
                          stats=result.stats, model=result.stats.get("model", ""),
                          reasoning_effort=result.stats.get("reasoning_effort"))
    return {"status": "done", "detail": choose(hcfg, "摘要 {} 字", "Summary: {} characters").format(len(result.output)),
            "usage": result.stats.get("usage"), "reasoning_chars": len(result.reasoning_content or "")}


def _step_tree(db: Session, paper: Paper, *, force: bool, settings: Any,
               hcfg: Dict[str, Any], kcfg: Dict[str, Any],
               should_stop: Optional[Any] = None) -> Dict[str, Any]:
    artifact = store.get_artifact(db, paper.id, store.ARTIFACT_SUMMARY)
    if artifact is None:
        raise RuntimeError(choose(hcfg, "缺少摘要产物：请先运行 summary 步骤", "Missing summary; run the summary step first"))
    existing = store.paper_memberships(db, paper.id)
    if paper.folder is not None and not paper.folder.direction_node_id and not force:
        return {"status": "skipped", "detail": choose(hcfg, "保留用户指定的文件夹位置", "Preserved the user-selected folder")}
    if existing and kcfg.get("reuse_artifacts", True) and not force:
        return {"status": "skipped",
                "detail": choose(hcfg, "保留已有唯一分类", "Preserved existing classification"),
                "memberships": [{"node_id": d.node_id, "path": d.node.path if d.node else ""}
                                for d in existing]}

    tree = store.tree_payload(db)
    paths = [{"node_id": n["id"], "path": n["path"]} for n in tree["nodes"]][: int(kcfg.get("max_tree_paths") or 200)]
    out = llm_steps.call_structured("direction_assign.md",
                                    _tree_input(paper, artifact.content_md, paths),
                                    settings, cfg=hcfg, should_stop=should_stop)
    if not out.ok:
        raise RuntimeError(out.error or choose(hcfg, "方向归类失败", "Classification failed"))
    payload = out.data if isinstance(out.data, dict) else {}
    memberships = payload.get("memberships") or []
    created = store.set_paper_memberships(db, paper, memberships, language=hcfg["output_language"])
    nodes = [row.node for row in created if row.node is not None]
    primary = next((row.node for row in created
                    if row.role == "primary" and row.node is not None),
                   nodes[0] if nodes else None)
    # 方向树 -> 文件夹树镜像（左侧面板即方向树）+ 把论文挂到 primary 方向文件夹 + 刷新图谱 md
    exported = store.refresh_nodes(db, nodes, move_paper=paper, move_paper_node=primary)
    return {"status": "done",
            "detail": choose(hcfg, "归入 {} 个方向节点：{}", "Assigned to {} node: {}").format(
                len(created), "；".join((row.node.path if row.node else "") for row in created)),
            "memberships": [{"node_id": row.node_id, "path": row.node.path if row.node else "",
                             "role": row.role, "reason": row.reason,
                             "confidence": row.confidence} for row in created],
            "graph_md": exported,
            "usage": out.usage, "reasoning_chars": len(out.reasoning or "")}


def _step_deep(db: Session, paper: Paper, *, force: bool, settings: Any,
               hcfg: Dict[str, Any], kcfg: Dict[str, Any],
               should_stop: Optional[Any] = None) -> Dict[str, Any]:
    existing = store.get_artifact(db, paper.id, store.ARTIFACT_DEEP)
    if _current_artifact(existing, hcfg) and kcfg.get("reuse_artifacts", True) and not force:
        return {"status": "skipped", "detail": choose(hcfg, "复用当前语言精读", "Reused current-language deep reading")}
    result = runner.run_skill("deep_reading", paper, settings, cfg=hcfg,
                              should_stop=should_stop)
    if result.error or not result.output.strip():
        raise RuntimeError(result.error or choose(hcfg, "深读笔记为空", "Deep reading is empty"))
    store.upsert_artifact(db, paper.id, store.ARTIFACT_DEEP, result.output,
                          reasoning_md=_reasoning_text(result, kcfg),
                          stats=result.stats, model=result.stats.get("model", ""),
                          reasoning_effort=result.stats.get("reasoning_effort"))
    return {"status": "done", "detail": choose(hcfg, "深读笔记 {} 字", "Deep reading: {} characters").format(len(result.output)),
            "usage": result.stats.get("usage"), "reasoning_chars": len(result.reasoning_content or "")}


def _step_graph(db: Session, paper: Paper, *, force: bool, settings: Any,
                hcfg: Dict[str, Any], kcfg: Dict[str, Any],
                should_stop: Optional[Any] = None) -> Dict[str, Any]:
    artifact = store.get_artifact(db, paper.id, store.ARTIFACT_DEEP)
    if artifact is None:
        raise RuntimeError(choose(hcfg, "缺少深读笔记：请先运行 deep 步骤", "Missing deep reading; run the deep step first"))
    directions = store.paper_memberships(db, paper.id)
    if not directions:
        return {"status": "skipped", "detail": choose(hcfg, "论文尚未归属任何方向节点", "No classification assigned")}

    limit = int(kcfg.get("relation_max_papers") or 20)
    reuse = bool(kcfg.get("reuse_artifacts", True))
    written = 0
    called = 0
    per_node: List[Dict[str, Any]] = []
    usages: List[Dict[str, int]] = []
    for direction in directions:
        node = direction.node
        if node is None:
            continue
        others = store.get_subtree_papers(db, node, exclude_paper_id=paper.id, limit=limit)
        inputs = [_relation_input(paper, artifact.content_md, others[start:start + 4], hcfg["output_language"])
                  for start in range(0, len(others), 4)]
        signature = hashlib.sha256(json.dumps([PROMPT_VERSION, hcfg["output_language"], inputs],
                                               ensure_ascii=False).encode()).hexdigest()
        if direction.relations_signature == signature and reuse and not force:
            per_node.append({"node": node.path, "edges": 0,
                             "detail": choose(hcfg, "材料与语言未变，复用已有关系", "Unchanged materials and language; reused relationships")})
            continue
        if not others:
            store.mark_relations(db, direction, 0, signature)
            per_node.append({"node": node.path, "edges": 0, "detail": choose(hcfg, "该节点下暂无其他论文", "No other papers in this node")})
            continue
        all_edges = []
        for user_text in inputs:
            out = llm_steps.call_structured("paper_relation.md", user_text,
                                           settings, cfg=hcfg, should_stop=should_stop)
            if not out.ok:
                raise RuntimeError(choose(hcfg, "关系抽取失败（{}）：{}", "Relationship extraction failed ({}): {}").format(node.path, out.error))
            payload = out.data if isinstance(out.data, dict) else {}
            if not isinstance(payload.get("edges"), list):
                raise ValueError(choose(hcfg, "关系结果必须含 edges 数组", "Relationship result requires an edges array"))
            batch_others = json.loads(user_text)["others"]
            # 逐边校验（坏边只丢不弃整批）：ID 合法性 + 回显标题配对纠正在内部完成
            all_edges.extend(_verify_edge_pairing(payload["edges"], batch_others, paper.id, node.path))
            called += 1
            usages.append(out.usage or {})
        if should_stop and should_stop():
            raise Cancelled("已被用户终止")
        count = store.replace_edges(db, direction, all_edges, [p.id for p in others], hcfg["output_language"], signature)
        written += count
        per_node.append({"node": node.path, "edges": count,
                         "detail": choose(hcfg, "对比 {} 篇已有论文", "Compared {} existing papers").format(len(others))})
    if written:
        detail = choose(hcfg, "新增/更新关系边 {} 条", "Created or updated {} relationships").format(written)
    elif called:
        detail = choose(hcfg, "已对比但未提炼出关系边（模型判断无实质关系）", "No evidence-supported relationships found")
    else:
        detail = choose(hcfg, "无可抽取对象：该节点暂无其他论文，或已抽取过（跳过）", "No comparison candidates, or unchanged relationships reused")
    status = "done" if written else "skipped"
    # 图谱有变化 -> 刷新该节点的图谱 md（论文图谱以节点文件夹里的 md 形式沉淀）
    store.refresh_nodes(db, [d.node for d in directions if d.node is not None])
    return {"status": status, "detail": detail, "edges": written, "per_node": per_node,
            "usage": _merge_usage(usages)}


_STEP_FUNCS = {
    "summary": _step_summary,
    "tree": _step_tree,
    "deep": _step_deep,
    "graph": _step_graph,
}


def _reasoning_text(result: Any, kcfg: Dict[str, Any]) -> str:
    """按配置决定是否落库推理过程（体积大，默认只留空串）。"""
    if not kcfg.get("store_reasoning", False):
        return ""
    return result.reasoning_content or ""


def _merge_usage(usages: Sequence[Dict[str, int]]) -> Dict[str, int]:
    total: Dict[str, int] = {}
    for usage in usages:
        for key, value in (usage or {}).items():
            if isinstance(value, int):
                total[key] = total.get(key, 0) + value
    return total


# ---------------------------------------------------------------- 编排
def iter_pipeline_events(db: Session, paper: Paper, *, steps: Optional[Iterable[str]] = None,
                         force: bool = False, settings: Any = None,
                         hcfg: Optional[Dict[str, Any]] = None,
                         kcfg: Optional[Dict[str, Any]] = None,
                         should_stop: Optional[Any] = None) -> Iterator[Dict[str, Any]]:
    """流式执行 pipeline：逐步产出事件，最后给一个 final（含完整账本）。

    事件：{"type":"step","step":..,"title":..,"status":"running|done|skipped|failed",...}
          {"type":"final","status":..,"steps":[...],"syntheses":[...],"run_id":..}

    should_stop（见 harness.cancel）：用户点了「终止」时，当前步会被标成
    failed（detail=已被用户终止）、run.status 记 cancelled，已经产出的产物不回滚。
    """
    kcfg = kcfg or harness_config.get_knowledge_config()
    settings = settings or settings_store.get_llm_settings(db)
    hcfg = runtime_config(settings, hcfg or settings_store.get_harness_settings(db))
    wanted = list(steps) if steps else list(STEPS)
    if "synthesis" in wanted:
        raise ValueError(choose(hcfg, "自动阶段综述已移除，请从文件夹菜单手动生成综述", "Automatic reviews were removed; generate a review from the folder menu"))
    if any(s not in STEPS for s in wanted):
        raise ValueError(choose(hcfg, "未知的阅读步骤", "Unknown reading step"))
    wanted = list(dict.fromkeys(wanted))

    run = PipelineRun(paper_id=paper.id, status="running", steps_json="[]",
                      output_language=hcfg["output_language"])
    db.add(run)
    db.commit()
    db.refresh(run)

    records: List[Dict[str, Any]] = []
    failed = False
    syntheses: List[Dict[str, Any]] = []

    def _persist(record: Dict[str, Any]) -> Dict[str, Any]:
        records.append(record)
        run.steps_json = json.dumps(records, ensure_ascii=False)
        run.current_step = record.get("step", "")
        db.commit()
        return record

    running = None
    try:
        for name in wanted:
            running = {"step": name, "title": choose(hcfg, STEP_TITLES[name], STEP_TITLES_EN[name]), "status": "running",
                       "detail": "", "started_at": _iso(_utcnow())}
            run.current_step = name
            run.steps_json = json.dumps(records + [running], ensure_ascii=False)
            db.commit()
            yield {"type": "step", **running}
            if failed:
                record = _persist(dict(running, status="skipped", detail=choose(hcfg, "前序步骤失败，已跳过", "Skipped after a previous step failed"),
                                       finished_at=_iso(_utcnow()), elapsed_s=0.0))
                yield {"type": "step", **record}
                continue

            started = time.time()
            try:
                if should_stop is not None and should_stop():
                    raise Cancelled("已被用户终止")
                outcome = _STEP_FUNCS[name](db, paper, force=force, settings=settings,
                                            hcfg=hcfg, kcfg=kcfg, should_stop=should_stop)
                status = outcome.pop("status", "done")
                detail = outcome.pop("detail", "")
                record = _persist(dict(running, status=status, detail=detail,
                                       finished_at=_iso(_utcnow()),
                                       elapsed_s=round(time.time() - started, 2), **outcome))
            except Cancelled:
                db.rollback()
                message = choose(hcfg, "已被用户终止", "Stopped by the user")
                record = _persist(dict(running, status="failed", detail=message,
                                       finished_at=_iso(_utcnow()),
                                       elapsed_s=round(time.time() - started, 2)))
                run.status, run.current_step, run.error = "cancelled", "", message
                run.finished_at = _utcnow()
                db.commit()
                yield {"type": "step", **record}
                yield {"type": "final", "paper_id": paper.id, "run_id": run.id,
                       "status": "cancelled", "steps": records, "syntheses": syntheses}
                return
            except Exception as exc:
                db.rollback()
                failed = True
                logger.warning("pipeline 步骤 %s 失败（paper=%s）：%s", name, paper.id, str(exc)[:300])
                record = _persist(dict(running, status="failed", detail=str(exc)[:500],
                                       finished_at=_iso(_utcnow()),
                                       elapsed_s=round(time.time() - started, 2)))
            yield {"type": "step", **record}

        status = "failed" if failed else "done"
        run.status = status
        run.current_step = ""
        run.error = next((r["detail"] for r in records if r.get("status") == "failed"), "")[:1000]
        run.finished_at = _utcnow()
        db.commit()
        yield {"type": "final", "paper_id": paper.id, "run_id": run.id, "status": status,
               "steps": records, "syntheses": syntheses}
    finally:
        if run.status == "running":
            db.rollback()
            stopped = bool(should_stop and should_stop())
            message = choose(hcfg, "已被用户终止", "Stopped by the user") if stopped else choose(
                hcfg, "连接中断；已完成产物保留，可重新运行", "Connection interrupted; completed artifacts preserved, rerun to continue")
            if running and not any(r["step"] == running["step"] for r in records):
                _persist(dict(running, status="failed", detail=message, finished_at=_iso(_utcnow())))
            run.status, run.current_step, run.error = "cancelled" if stopped else "interrupted", "", message
            run.finished_at = _utcnow()
            db.commit()


def recover_interrupted(db: Session) -> None:
    """服务重启后只结束遗留运行，不回滚已完成产物。"""
    for run in db.query(PipelineRun).filter_by(status="running").all():
        run.status, run.current_step = "interrupted", ""
        run.error = choose({"output_language": run.output_language},
                           "服务重启导致中断，可重新运行并复用已完成产物",
                           "Interrupted by a server restart; rerun to reuse completed artifacts")
        run.finished_at = _utcnow()
        try:
            records = json.loads(run.steps_json or "[]")
            if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
                raise ValueError("Invalid step ledger")
        except (ValueError, TypeError):
            run.error += choose({"output_language": run.output_language},
                                "；步骤记录损坏，原始记录已保留", "; damaged step ledger preserved unchanged")
            continue
        for record in records:
            if record.get("status") == "running":
                record.update(status="failed", detail=run.error, finished_at=_iso(run.finished_at))
        run.steps_json = json.dumps(records, ensure_ascii=False)
    db.commit()


def run_pipeline(db: Session, paper: Paper, *, steps: Optional[Iterable[str]] = None,
                 force: bool = False, settings: Any = None,
                 hcfg: Optional[Dict[str, Any]] = None,
                 kcfg: Optional[Dict[str, Any]] = None,
                 should_stop: Optional[Any] = None) -> PipelineResult:
    """汇总式执行（脚本/后台任务/测试）：消费事件流并返回结果对象。"""
    result = PipelineResult(paper_id=paper.id)
    for event in iter_pipeline_events(db, paper, steps=steps, force=force, settings=settings,
                                      hcfg=hcfg, kcfg=kcfg, should_stop=should_stop):
        if event.get("type") == "final":
            result.run_id = event.get("run_id")
            result.status = event.get("status", "done")
            result.steps = event.get("steps") or []
            result.syntheses = event.get("syntheses") or []
            result.error = next((s.get("detail", "") for s in result.steps
                                 if s.get("status") == "failed"), "")
    return result
