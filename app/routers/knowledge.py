"""知识网络路由：方向树 / 节点详情 / 论文知识视图 / pipeline 运行（含 SSE）。

本层零 prompt、零 SQL：序列化交给 `knowledge.store`，编排交给 `knowledge.pipeline`。

  GET    /api/knowledge/steps                  步骤清单（前端画流程用）
  GET    /api/knowledge/tree                   方向树（nodes + parent 边，D3/ECharts 可直接用）
  GET    /api/knowledge/nodes/{node_id}        节点详情：论文 + 局部图谱（节点/边）+ 综述历史
  PATCH  /api/knowledge/nodes/{node_id}        重命名方向节点（后代路径与镜像文件夹一起平移）
  DELETE /api/knowledge/nodes/{node_id}        删除误建节点（子节点上移，不删论文）
  GET    /api/papers/{paper_id}/knowledge      单论文：产物 / 归属 / 关联边 / pipeline 记录
  GET    /api/papers/{paper_id}/artifacts/{kind}  单份产物正文（summary / deep_reading；?raw=true 直接给 Markdown）
  POST   /api/papers/{paper_id}/knowledge/run  跑 pipeline；?stream=true 时返回 SSE 逐步事件

SSE 帧：
  {"type":"step","step":"summary","title":"快速总结","status":"running|done|skipped|failed",
   "detail":"...","elapsed_s":1.2,"usage":{...}}
  {"type":"error","message":"..."}
  {"type":"done","status":"done|failed","run_id":7,"steps":[...],"syntheses":[...]}
"""

import json
import logging
from typing import Any, Dict, Iterator, List

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from harness import config as harness_config, llm as harness_llm
from harness import cancel as cancel_registry
from knowledge import STEP_TITLES, STEPS, graphdoc, iter_pipeline_events, store

from .. import database, schemas, settings_store
from ..models import DirectionNode, Paper

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["knowledge"])

# 阅读产物：kind -> 中文名（前端页签与导出文件名共用）
ARTIFACT_TITLES = {store.ARTIFACT_SUMMARY: "快速总结", store.ARTIFACT_DEEP: "深度精读"}

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def _sse(payload: Dict[str, Any]) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _load_paper_or_404(db: Session, paper_id: int) -> Paper:
    paper = db.get(Paper, paper_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="论文不存在")
    return paper


def _load_node_or_404(db: Session, node_id: int) -> DirectionNode:
    node = db.get(DirectionNode, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="方向节点不存在")
    return node


def _knowledge_cfg() -> Dict[str, Any]:
    return harness_config.get_knowledge_config()


def _pipeline_event_stream(db: Session, paper: Paper, request: schemas.PipelineRunRequest) -> Iterator[str]:
    """把 pipeline 事件翻译成 SSE（每步一帧，前端可实时显示进度）。

    带 cancel_token 时支持「终止」：用户点终止 → 当前步被标 failed(已被用户终止)、
    整轮记 cancelled，已产出的产物不回滚（见 harness.cancel 与 knowledge.pipeline）。
    """
    token = request.cancel_token
    try:
        for event in iter_pipeline_events(db, paper, steps=request.steps, force=request.force,
                                          hcfg=harness_config.get_harness_config(),
                                          kcfg=_knowledge_cfg(),
                                          should_stop=cancel_registry.watcher(token)):
            kind = event.get("type")
            if kind == "step":
                yield _sse({"type": "step", **{k: v for k, v in event.items() if k != "type"}})
            elif kind == "final":
                yield _sse({"type": "done", "status": event.get("status"),
                            "run_id": event.get("run_id"), "steps": event.get("steps"),
                            "syntheses": event.get("syntheses")})
                return
    except Exception as exc:  # noqa: BLE001  兜底：让前端收到错误帧而不是断流
        logger.warning("pipeline 流式执行失败：%s", str(exc)[:300])
        yield _sse({"type": "error", "message": str(exc)[:300]})
    finally:
        cancel_registry.release(token)


@router.get("/knowledge/steps")
def list_steps():
    """pipeline 步骤清单（稳定顺序），供前端渲染流程节点。"""
    return {"steps": [{"name": name, "title": STEP_TITLES[name]} for name in STEPS],
            "synthesis_every": int(_knowledge_cfg().get("synthesis_every") or 5)}


@router.get("/knowledge/tree")
def get_tree(db: Session = Depends(database.get_db)):
    """方向树：nodes + parent 边（可视化友好）。"""
    return store.tree_payload(db)


@router.get("/knowledge/nodes/{node_id}")
def get_node(node_id: int, db: Session = Depends(database.get_db)):
    """节点详情：论文列表 + 局部图谱（节点/边）+ 阶段综述（最新 + 历史）。"""
    return store.node_payload(db, _load_node_or_404(db, node_id))


@router.get("/knowledge/nodes/{node_id}/graph.md")
def get_node_graph_md(node_id: int, db: Session = Depends(database.get_db)):
    """节点图谱文档（Markdown）：论文图谱以每个节点一份 md 的形式沉淀，可直接阅读/渲染。"""
    node = _load_node_or_404(db, node_id)
    return Response(content=graphdoc.render_graph_md(db, node),
                    media_type="text/markdown; charset=utf-8")


@router.post("/knowledge/export")
def export_all_graph_md(db: Session = Depends(database.get_db)):
    """重建知识库文件：把方向树同步成嵌套文件夹（🧭）并写出每个节点的图谱 md。

    用途：升级后补齐历史节点、或手工修复文件夹结构。默认路径 data/knowledge/<路径>/GRAPH.md。
    """
    all_nodes = db.query(DirectionNode).order_by(DirectionNode.path).all()
    store.refresh_nodes(db, all_nodes)          # 同步文件夹（左侧文件夹树 = 方向树）
    moved = store.repair_paper_folders(db)      # 历史数据：把已归属论文落到方向文件夹
    paths = store.export_all_graph_md(db)       # 每个节点一份 GRAPH.md
    return {"count": len(paths), "root": str(store.knowledge_md_root()),
            "moved_papers": moved, "paths": [str(p) for p in paths]}


@router.patch("/knowledge/nodes/{node_id}")
def rename_node(node_id: int, body: schemas.NodeUpdateRequest,
                db: Session = Depends(database.get_db)):
    """重命名方向节点：整棵子树的 path、镜像文件夹名、图谱 md 目录一起平移。"""
    node = _load_node_or_404(db, node_id)
    try:
        store.rename_node(db, node, body.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return store.node_payload(db, node)


@router.delete("/knowledge/nodes/{node_id}", status_code=204)
def delete_node(node_id: int, db: Session = Depends(database.get_db)):
    """删除误建的节点：子节点上移一层，节点的归属与关系边一并删除（论文本身不动）。"""
    store.delete_node(db, _load_node_or_404(db, node_id))
    return None


@router.get("/papers/{paper_id}/knowledge")
def get_paper_knowledge(paper_id: int, db: Session = Depends(database.get_db)):
    """单论文视角：产物清单 + 方向归属 + 关联关系边 + 最近 pipeline 记录。"""
    return store.paper_knowledge_payload(db, _load_paper_or_404(db, paper_id))


@router.get("/papers/{paper_id}/artifacts/{kind}")
def get_paper_artifact(paper_id: int, kind: str,
                       raw: bool = Query(False, description="true = 直接返回 Markdown 正文"),
                       db: Session = Depends(database.get_db)):
    """单份阅读产物正文（summary / deep_reading）。

    前端在「对话栏」的产物页签里取它；raw=true 供浏览器新窗口直接阅读/另存为 .md。
    """
    paper = _load_paper_or_404(db, paper_id)
    if kind not in ARTIFACT_TITLES:
        raise HTTPException(status_code=404,
                            detail="未知产物类型：{}（可选 {}）".format(kind, "/".join(ARTIFACT_TITLES)))
    row = store.get_artifact(db, paper.id, kind)
    if row is None or not (row.content_md or "").strip():
        raise HTTPException(status_code=404, detail="还没有「{}」：先跑一次知识网络 pipeline".format(
            ARTIFACT_TITLES[kind]))
    if raw:
        return Response(content=row.content_md, media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": 'inline; filename="{}-{}.md"'.format(
                            paper.arxiv_id or paper.id, kind)})
    return {
        "paper_id": paper.id,
        "arxiv_id": paper.arxiv_id,
        "kind": row.kind,
        "title": ARTIFACT_TITLES[kind],
        "content_md": row.content_md,
        "chars": len(row.content_md or ""),
        "model": row.model,
        "reasoning_effort": row.reasoning_effort,
        "reasoning_md": row.reasoning_md or "",
        "stats": row.stats,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.post("/papers/{paper_id}/knowledge/run")
def run_knowledge_pipeline(paper_id: int,
                           body: schemas.PipelineRunRequest = schemas.PipelineRunRequest(),
                           stream: bool = Query(False, description="true = SSE 逐步事件"),
                           db: Session = Depends(database.get_db)):
    """对一篇论文跑知识网络 pipeline（摘要 -> 方向树 -> 深读 -> 图谱 -> 综述）。"""
    paper = _load_paper_or_404(db, paper_id)
    settings = settings_store.get_llm_settings(db)
    if not harness_llm.is_configured(settings):
        raise HTTPException(status_code=400,
                            detail="尚未配置 LLM（base_url / model / api_key），请先在设置中填写并保存。")

    if stream:
        return StreamingResponse(_pipeline_event_stream(db, paper, body),
                                 media_type="text/event-stream", headers=_SSE_HEADERS)

    from knowledge import run_pipeline        # 延迟导入：避免非流式路径也构造事件流
    result = run_pipeline(db, paper, steps=body.steps, force=body.force,
                          settings=settings, hcfg=harness_config.get_harness_config(),
                          kcfg=_knowledge_cfg())
    return schemas.PipelineRunOut(paper_id=result.paper_id, run_id=result.run_id,
                                  status=result.status, steps=result.steps,
                                  syntheses=result.syntheses, error=result.error)


@router.get("/knowledge/papers/{paper_id}/runs", response_model=List[Dict[str, Any]])
def list_paper_runs(paper_id: int, db: Session = Depends(database.get_db)):
    """该论文的 pipeline 历史记录（含逐步账本），便于排查与重跑。"""
    return store.paper_knowledge_payload(db, _load_paper_or_404(db, paper_id))["runs"]
