"""知识网络 pipeline 编排：读一篇论文 -> 摘要 -> 归入方向树 -> 深读 -> 局部图谱 -> 阶段综述。

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

from . import llm_steps, store

logger = logging.getLogger(__name__)

STEPS: Tuple[str, ...] = ("summary", "tree", "deep", "graph", "synthesis")
STEP_TITLES = {
    "summary": "快速总结",
    "tree": "归入方向树",
    "deep": "深度精读",
    "graph": "构建局部图谱",
    "synthesis": "阶段综述",
}
# 单步送入模型的材料上限（字符），防止 prompt 随知识库增长而失控
_DEEP_NOTE_LIMIT = 14000
_BRIEF_LIMIT = 600
_SYNTHESIS_PAPER_LIMIT = 40


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
def _paper_brief(paper: Paper, limit: int = _BRIEF_LIMIT) -> str:
    pa = PaperInput.from_any(paper)
    text = pa.meta_block()
    if limit and len(text) > limit:
        text = text[:limit] + "…"
    return text


def _tree_input(paper: Paper, summary_md: str, paths: Sequence[str]) -> str:
    blocks = ["## 新论文", _paper_brief(paper, 0), "", "## 论文摘要（含\"解决了什么\"与\"未来展望\"）", summary_md]
    blocks.append("")
    blocks.append("## 现有方向树路径（优先复用；确实无法表达才新建）")
    if paths:
        blocks.extend("- " + p for p in paths)
    else:
        blocks.append("（当前为空：这是第一篇论文，请自建根节点与最必要的分支）")
    return "\n".join(blocks)


def _relation_input(paper: Paper, deep_md: str, others: Sequence[Paper]) -> str:
    note = deep_md if len(deep_md) <= _DEEP_NOTE_LIMIT else deep_md[:_DEEP_NOTE_LIMIT] + "…（笔记过长已截断）"
    blocks = ["## 新论文", "- id={} | {}".format(paper.id, paper.title or paper.arxiv_id),
              "", "## 新论文深读笔记", note, "", "## 该方向节点下的已有论文"]
    for other in others:
        blocks.append("- id={} | {}".format(other.id, (other.title or other.arxiv_id)))
        blocks.append("  一句话结论：{}".format((other.abstract or "未提供").replace("\n", " ")[:240]))
    return "\n".join(blocks)


def _synthesis_input(node_path: str, description: str, papers: Sequence[Paper],
                     edges: Sequence[Any]) -> str:
    blocks = ["## 方向节点", "- 路径：{}".format(node_path)]
    if description:
        blocks.append("- 说明：{}".format(description))
    blocks.append("- 论文数：{}".format(len(papers)))
    blocks.append("")
    blocks.append("## 论文列表（摘要要点）")
    for paper in papers:
        blocks.append("- id={} | {} | {}".format(
            paper.id, paper.title or paper.arxiv_id, (paper.published or "")[:10]))
        blocks.append("  {}".format((paper.abstract or "未提供").replace("\n", " ")[:300]))
    blocks.append("")
    blocks.append("## 已提炼的论文关系（src --relation--> dst）")
    if edges:
        for edge in edges:
            blocks.append("- {} --{}--> {}: {}".format(
                edge.src_paper_id, edge.relation, edge.dst_paper_id,
                (edge.rationale or "")[:200]))
    else:
        blocks.append("（暂未提炼出边：请主要依据论文摘要组织综述）")
    return "\n".join(blocks)


# ---------------------------------------------------------------- 各步实现
def _step_summary(db: Session, paper: Paper, *, force: bool, settings: Any,
                  hcfg: Dict[str, Any], kcfg: Dict[str, Any],
                  should_stop: Optional[Any] = None) -> Dict[str, Any]:
    existing = store.get_artifact(db, paper.id, store.ARTIFACT_SUMMARY)
    if existing and kcfg.get("reuse_artifacts", True) and not force:
        return {"status": "skipped", "detail": "已有摘要产物（{} 字）".format(len(existing.content_md))}
    result = runner.run_skill("quick_summary", paper, settings, cfg=hcfg,
                              should_stop=should_stop)
    if result.error or not result.output.strip():
        raise RuntimeError(result.error or "摘要为空")
    store.upsert_artifact(db, paper.id, store.ARTIFACT_SUMMARY, result.output,
                          reasoning_md=_reasoning_text(result, kcfg),
                          stats=result.stats, model=result.stats.get("model", ""),
                          reasoning_effort=result.stats.get("reasoning_effort"))
    return {"status": "done", "detail": "摘要 {} 字".format(len(result.output)),
            "usage": result.stats.get("usage"), "reasoning_chars": len(result.reasoning_content or "")}


def _step_tree(db: Session, paper: Paper, *, force: bool, settings: Any,
               hcfg: Dict[str, Any], kcfg: Dict[str, Any],
               should_stop: Optional[Any] = None) -> Dict[str, Any]:
    artifact = store.get_artifact(db, paper.id, store.ARTIFACT_SUMMARY)
    if artifact is None:
        raise RuntimeError("缺少摘要产物：请先运行 summary 步骤")
    existing = store.paper_memberships(db, paper.id)
    if existing and kcfg.get("reuse_artifacts", True) and not force:
        return {"status": "skipped",
                "detail": "已归属 {} 个方向节点".format(len(existing)),
                "memberships": [{"node_id": d.node_id, "path": d.node.path if d.node else ""}
                                for d in existing]}

    tree = store.tree_payload(db)
    paths = [n["path"] for n in tree["nodes"]][: int(kcfg.get("max_tree_paths") or 200)]
    out = llm_steps.call_structured("direction_assign.md",
                                    _tree_input(paper, artifact.content_md, paths),
                                    settings, cfg=hcfg, should_stop=should_stop)
    if not out.ok:
        raise RuntimeError(out.error or "方向归类失败")
    payload = out.data if isinstance(out.data, dict) else {}
    memberships = payload.get("memberships") or []
    for item in payload.get("new_nodes") or []:
        if isinstance(item, dict):
            store.get_or_create_node(db, item.get("path"), description=str(item.get("description") or ""))
    created = store.set_paper_memberships(db, paper, memberships)
    nodes = [row.node for row in created if row.node is not None]
    primary = next((row.node for row in created
                    if row.role == "primary" and row.node is not None),
                   nodes[0] if nodes else None)
    # 方向树 -> 文件夹树镜像（左侧面板即方向树）+ 把论文挂到 primary 方向文件夹 + 刷新图谱 md
    exported = store.refresh_nodes(db, nodes, move_paper=paper, move_paper_node=primary)
    return {"status": "done",
            "detail": "归属 {} 个方向节点：{}".format(
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
    if existing and kcfg.get("reuse_artifacts", True) and not force:
        return {"status": "skipped", "detail": "已有深读笔记（{} 字）".format(len(existing.content_md))}
    result = runner.run_skill("deep_reading", paper, settings, cfg=hcfg,
                              should_stop=should_stop)
    if result.error or not result.output.strip():
        raise RuntimeError(result.error or "深读笔记为空")
    store.upsert_artifact(db, paper.id, store.ARTIFACT_DEEP, result.output,
                          reasoning_md=_reasoning_text(result, kcfg),
                          stats=result.stats, model=result.stats.get("model", ""),
                          reasoning_effort=result.stats.get("reasoning_effort"))
    return {"status": "done", "detail": "深读笔记 {} 字".format(len(result.output)),
            "usage": result.stats.get("usage"), "reasoning_chars": len(result.reasoning_content or "")}


def _step_graph(db: Session, paper: Paper, *, force: bool, settings: Any,
                hcfg: Dict[str, Any], kcfg: Dict[str, Any],
                should_stop: Optional[Any] = None) -> Dict[str, Any]:
    artifact = store.get_artifact(db, paper.id, store.ARTIFACT_DEEP)
    if artifact is None:
        raise RuntimeError("缺少深读笔记：请先运行 deep 步骤")
    directions = store.paper_memberships(db, paper.id)
    if not directions:
        return {"status": "skipped", "detail": "论文尚未归属任何方向节点"}

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
        if direction.relations_at and reuse and not force:
            per_node.append({"node": node.path, "edges": 0, "detail": "已抽取过关系（跳过）"})
            continue
        others = store.get_node_papers(db, node, exclude_paper_id=paper.id, limit=limit)
        if not others:
            store.mark_relations(db, direction, 0)   # 无可比对对象也标记，避免反复空跑
            per_node.append({"node": node.path, "edges": 0, "detail": "该节点下暂无其他论文"})
            continue
        out = llm_steps.call_structured("paper_relation.md",
                                        _relation_input(paper, artifact.content_md, others),
                                        settings, cfg=hcfg, should_stop=should_stop)
        if not out.ok:
            raise RuntimeError("关系抽取失败（{}）：{}".format(node.path, out.error))
        payload = out.data if isinstance(out.data, dict) else {}
        edges = payload.get("edges") or []
        count = store.upsert_edges(db, node.id, paper.id, edges)
        store.mark_relations(db, direction, count)
        written += count
        called += 1
        usages.append(out.usage or {})
        per_node.append({"node": node.path, "edges": count,
                         "detail": "对比 {} 篇已有论文".format(len(others))})
    if written:
        detail = "新增/更新关系边 {} 条".format(written)
    elif called:
        detail = "已对比但未提炼出关系边（模型判断无实质关系）"
    else:
        detail = "无可抽取对象：该节点暂无其他论文，或已抽取过（跳过）"
    status = "done" if written else "skipped"
    # 图谱有变化 -> 刷新该节点的图谱 md（论文图谱以节点文件夹里的 md 形式沉淀）
    store.refresh_nodes(db, [d.node for d in directions if d.node is not None])
    return {"status": status, "detail": detail, "edges": written, "per_node": per_node,
            "usage": _merge_usage(usages)}


def _step_synthesis(db: Session, paper: Paper, *, force: bool, settings: Any,
                    hcfg: Dict[str, Any], kcfg: Dict[str, Any],
                    should_stop: Optional[Any] = None) -> Dict[str, Any]:
    every = max(1, int(kcfg.get("synthesis_every") or 5))
    cap = int(kcfg.get("synthesis_max_papers") or _SYNTHESIS_PAPER_LIMIT)
    directions = store.paper_memberships(db, paper.id)
    if not directions:
        return {"status": "skipped", "detail": "论文尚未归属任何方向节点"}

    produced: List[Dict[str, Any]] = []
    usages: List[Dict[str, int]] = []
    for direction in directions:
        node = direction.node
        if node is None:
            continue
        count = store.recount_node(db, node)
        since = count - (node.synthesis_paper_count or 0)
        due = bool(node.synthesis_md and since >= every) \
            or (not node.synthesis_md and count >= every) \
            or (force and count > 0)
        if not due:
            continue
        papers = store.get_node_papers(db, node, limit=cap)
        out = llm_steps.call_structured(
            "field_synthesis.md",
            _synthesis_input(node.path, node.description, papers, store.list_edges(db, node.id)),
            settings, as_json=False, cfg=hcfg, should_stop=should_stop)
        if not out.ok or not out.text.strip():
            raise RuntimeError("阶段综述失败（{}）：{}".format(node.path, out.error))
        store.save_synthesis(db, node, out.text, [p.id for p in papers],
                             "papers+{}".format(every),
                             {"usage": out.usage, "model": out.model,
                              "latency_ms": out.latency_ms})
        usages.append(out.usage or {})
        produced.append({"node_id": node.id, "path": node.path,
                         "paper_count": len(papers), "chars": len(out.text)})

    detail = "生成 {} 版综述：{}".format(
        len(produced), "；".join("{}（{} 篇）".format(x["path"], x["paper_count"]) for x in produced)) \
        if produced else "未达触发阈值（每 +{} 篇）".format(every)
    if produced:
        store.refresh_nodes(db, [direction.node for direction in directions
                                 if direction.node is not None])
    return {"status": "done" if produced else "skipped", "detail": detail,
            "syntheses": produced, "usage": _merge_usage(usages)}


_STEP_FUNCS = {
    "summary": _step_summary,
    "tree": _step_tree,
    "deep": _step_deep,
    "graph": _step_graph,
    "synthesis": _step_synthesis,
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
    hcfg = hcfg or harness_config.get_harness_config()
    kcfg = kcfg or harness_config.get_knowledge_config()
    settings = settings or settings_store.get_llm_settings(db)
    wanted = [s for s in (list(steps) if steps else list(STEPS)) if s in STEPS]

    run = PipelineRun(paper_id=paper.id, status="running", steps_json="[]")
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

    for name in wanted:
        running = {"step": name, "title": STEP_TITLES[name], "status": "running",
                   "detail": "", "started_at": _iso(_utcnow())}
        yield {"type": "step", **running}
        if failed:
            record = _persist(dict(running, status="skipped", detail="前序步骤失败，已跳过",
                                   finished_at=_iso(_utcnow()), elapsed_s=0.0))
            yield {"type": "step", **record}
            continue

        started = time.time()
        try:
            outcome = _STEP_FUNCS[name](db, paper, force=force, settings=settings,
                                        hcfg=hcfg, kcfg=kcfg, should_stop=should_stop)
            status = outcome.pop("status", "done")
            detail = outcome.pop("detail", "")
            record = _persist(dict(running, status=status, detail=detail,
                                   finished_at=_iso(_utcnow()),
                                   elapsed_s=round(time.time() - started, 2), **outcome))
            if name == "synthesis":
                syntheses = record.get("syntheses") or []
        except Cancelled:
            # 用户终止：当前步记账 + 整轮记 cancelled，剩余步骤交给前端显示“已终止”
            record = _persist(dict(running, status="failed", detail="已被用户终止",
                                   finished_at=_iso(_utcnow()),
                                   elapsed_s=round(time.time() - started, 2)))
            yield {"type": "step", **record}
            run.status = "cancelled"
            run.current_step = ""
            run.error = "已被用户终止"
            run.finished_at = _utcnow()
            db.commit()
            logger.info("pipeline 被用户终止（paper=%s，停在 %s）", paper.id, name)
            yield {"type": "final", "paper_id": paper.id, "run_id": run.id,
                   "status": "cancelled", "steps": records, "syntheses": syntheses}
            return
        except Exception as exc:  # noqa: BLE001  单步失败不中断账本记录
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
