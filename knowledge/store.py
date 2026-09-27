"""知识网络的存储层：方向树 / 论文归属 / 产物 / 关系边 / 综述 / pipeline 账本。

只暴露"业务动作"（建节点、挂论文、写边、取树…），Web 层与 pipeline 都不直接拼 SQL。
所有图结构统一序列化为「节点 + 边」JSON，可直接喂 D3 / ECharts：
  tree_payload()          方向树：nodes + parent 边
  node_payload()          单节点：论文节点 + 关系边（局部图谱）+ 综述历史
  paper_knowledge_payload() 单论文：产物、归属、关联边、pipeline 记录
"""

import datetime
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import config_loader
from app.models import (BufferPaper, DirectionNode, Folder, NodeSynthesis, Paper,
                        PaperArtifact, PaperDirection, PaperRelation, PipelineRun,
                        SearchRun)

from . import graphdoc

ARTIFACT_SUMMARY = "summary"          # Step 1 快速总结
ARTIFACT_DEEP = "deep_reading"        # Step 3 深度精读
PATH_SEP = "/"
DEFAULT_NODE_DESCRIPTION = ""


def _utcnow() -> datetime.datetime:
    return datetime.datetime.utcnow()


def _iso(value: Optional[datetime.datetime]) -> Optional[str]:
    return value.isoformat() if value else None


# ---------------------------------------------------------------- 方向树
def normalize_path(path: Any) -> List[str]:
    """清洗路径：去空白 / 去分隔符 / 去空层，最长 6 层（防止模型给出过深噪声）。"""
    if isinstance(path, str):
        raw: Iterable[Any] = path.split(PATH_SEP)
    elif isinstance(path, (list, tuple)):
        raw = path
    else:
        return []
    parts = []
    for item in raw:
        name = str(item or "").strip().strip(PATH_SEP).strip()
        if name:
            parts.append(name[:120])
    return parts[:6]


def path_key(path: Sequence[str]) -> str:
    return PATH_SEP.join(normalize_path(list(path)))


def get_node_by_path(db: Session, path: str) -> Optional[DirectionNode]:
    return db.query(DirectionNode).filter(DirectionNode.path == path).first()


def get_or_create_node(db: Session, path: Sequence[str], description: str = "") -> Optional[DirectionNode]:
    """按路径逐层建点（缺哪层补哪层），返回最深节点；路径非法则返回 None。"""
    parts = normalize_path(list(path))
    if not parts:
        return None
    parent: Optional[DirectionNode] = None
    acc: List[str] = []
    node: Optional[DirectionNode] = None
    for depth, name in enumerate(parts):
        acc.append(name)
        key = path_key(acc)
        node = get_node_by_path(db, key)
        if node is None:
            node = DirectionNode(parent_id=parent.id if parent else None, name=name,
                                 path=key, depth=depth, description=description if depth == len(parts) - 1 else "")
            db.add(node)
            db.commit()
            db.refresh(node)
        elif description and depth == len(parts) - 1 and not node.description:
            node.description = description
            db.commit()
        parent = node
    return node


def recount_node(db: Session, node: DirectionNode) -> int:
    """重算节点直接挂载的论文数（归属变化后必须调用，保证图谱节点大小正确）。"""
    count = db.query(PaperDirection).filter(PaperDirection.node_id == node.id).count()
    if node.paper_count != count:
        node.paper_count = count
        db.commit()
    return count


def set_paper_memberships(db: Session, paper: Paper, memberships: Sequence[Dict[str, Any]]) -> List[PaperDirection]:
    """覆盖式写入论文归属（多归属）：先清空旧归属，再按新结果建点+挂载。"""
    old_nodes = [d.node for d in db.query(PaperDirection).filter(PaperDirection.paper_id == paper.id).all()]
    for d in list(db.query(PaperDirection).filter(PaperDirection.paper_id == paper.id).all()):
        db.delete(d)
    db.commit()

    seen: set = set()
    created: List[PaperDirection] = []
    primary_assigned = False
    for item in memberships or []:
        path = normalize_path(item.get("path"))
        if len(path) < 1:
            continue
        node = get_or_create_node(db, path)
        if node is None or node.id in seen:
            continue
        seen.add(node.id)
        role = str(item.get("role") or "secondary").lower()
        if role == "primary" and primary_assigned:
            role = "secondary"
        primary_assigned = primary_assigned or role == "primary"
        try:
            confidence = float(item.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        row = PaperDirection(paper_id=paper.id, node_id=node.id, role=role,
                             reason=str(item.get("reason") or "")[:2000],
                             confidence=max(0.0, min(1.0, confidence)))
        db.add(row)
        created.append(row)
    db.commit()

    for node in {o.id: o for o in old_nodes + [r.node for r in created]}.values():
        recount_node(db, node)
    return created


def get_node_papers(db: Session, node: DirectionNode, exclude_paper_id: Optional[int] = None,
                    limit: Optional[int] = None) -> List[Paper]:
    """节点下的论文（按入库时间倒序），可排除某篇、可限量。"""
    query = (db.query(Paper)
             .join(PaperDirection, PaperDirection.paper_id == Paper.id)
             .filter(PaperDirection.node_id == node.id))
    if exclude_paper_id is not None:
        query = query.filter(Paper.id != exclude_paper_id)
    query = query.order_by(Paper.added_at.desc(), Paper.id.desc())
    if limit:
        query = query.limit(int(limit))
    return query.all()


def paper_memberships(db: Session, paper_id: int) -> List[PaperDirection]:
    """论文的方向归属：primary 排在前（同一篇可多归属）。"""
    rows = (db.query(PaperDirection)
            .filter(PaperDirection.paper_id == paper_id)
            .order_by(PaperDirection.id).all())
    return sorted(rows, key=lambda r: (0 if r.role == "primary" else 1, r.id))


# ---------------------------------------------------------------- 产物
def get_artifact(db: Session, paper_id: int, kind: str) -> Optional[PaperArtifact]:
    return (db.query(PaperArtifact)
            .filter(PaperArtifact.paper_id == paper_id, PaperArtifact.kind == kind).first())


def upsert_artifact(db: Session, paper_id: int, kind: str, content_md: str, *,
                    reasoning_md: str = "", stats: Optional[Dict[str, Any]] = None,
                    model: str = "", reasoning_effort: Optional[str] = None) -> PaperArtifact:
    row = get_artifact(db, paper_id, kind)
    if row is None:
        row = PaperArtifact(paper_id=paper_id, kind=kind)
        db.add(row)
    row.content_md = content_md or ""
    row.reasoning_md = reasoning_md or ""
    row.stats_json = json.dumps(stats or {}, ensure_ascii=False)
    row.model = model or ""
    row.reasoning_effort = reasoning_effort
    row.updated_at = _utcnow()
    db.commit()
    db.refresh(row)
    return row


# ---------------------------------------------------------------- 关系边
def has_edges(db: Session, node_id: int, src_paper_id: int) -> bool:
    return (db.query(PaperRelation)
            .filter(PaperRelation.node_id == node_id,
                    PaperRelation.src_paper_id == src_paper_id).count()) > 0


def mark_relations(db: Session, direction: PaperDirection, count: int) -> None:
    """标记该（论文, 节点）已完成关系抽取（即使 0 条边），供下一轮幂等跳过。"""
    direction.relations_at = _utcnow()
    direction.relations_count = int(count)
    db.commit()


def upsert_edges(db: Session, node_id: int, src_paper_id: int,
                 edges: Sequence[Dict[str, Any]]) -> int:
    """写入以 src 为起点的关系边（同 (node,src,dst,relation) 覆盖更新）。"""
    written = 0
    for item in edges or []:
        try:
            dst = int(item.get("other_paper_id") or item.get("dst_paper_id") or 0)
        except (TypeError, ValueError):
            continue
        relation = str(item.get("relation") or "").strip().lower()
        if not dst or dst == src_paper_id or not relation:
            continue
        row = (db.query(PaperRelation)
               .filter(PaperRelation.node_id == node_id,
                       PaperRelation.src_paper_id == src_paper_id,
                       PaperRelation.dst_paper_id == dst,
                       PaperRelation.relation == relation).first())
        if row is None:
            row = PaperRelation(node_id=node_id, src_paper_id=src_paper_id,
                                 dst_paper_id=dst, relation=relation)
            db.add(row)
        row.direction = str(item.get("direction") or "->")
        try:
            row.strength = max(0.0, min(1.0, float(item.get("strength") or 0.5)))
        except (TypeError, ValueError):
            row.strength = 0.5
        row.rationale = str(item.get("rationale") or "")[:2000]
        row.evidence = str(item.get("evidence") or "")[:1000]
        written += 1
    db.commit()
    return written


def list_edges(db: Session, node_id: int) -> List[PaperRelation]:
    return (db.query(PaperRelation).filter(PaperRelation.node_id == node_id)
            .order_by(PaperRelation.id).all())


# ---------------------------------------------------------------- 阶段综述
def save_synthesis(db: Session, node: DirectionNode, content_md: str,
                   paper_ids: Sequence[int], trigger: str,
                   stats: Optional[Dict[str, Any]] = None) -> NodeSynthesis:
    row = NodeSynthesis(node_id=node.id, content_md=content_md,
                        paper_count=len(list(paper_ids)),
                        paper_ids_json=json.dumps([int(p) for p in paper_ids]),
                        trigger=trigger,
                        stats_json=json.dumps(stats or {}, ensure_ascii=False))
    db.add(row)
    node.synthesis_md = content_md
    node.synthesis_at = _utcnow()
    node.synthesis_paper_count = len(list(paper_ids))
    db.commit()
    db.refresh(row)
    return row


def synthesis_history(db: Session, node_id: int, limit: int = 10) -> List[NodeSynthesis]:
    return (db.query(NodeSynthesis).filter(NodeSynthesis.node_id == node_id)
            .order_by(NodeSynthesis.created_at.desc(), NodeSynthesis.id.desc())
            .limit(limit).all())


# ---------------------------------------------------------------- 序列化（可视化友好）
def _subtree_count(db: Session, node: DirectionNode) -> int:
    """节点及其子树的论文总数（可视化时用作节点权重）。"""
    total = node.paper_count
    for child in db.query(DirectionNode).filter(DirectionNode.parent_id == node.id).all():
        total += _subtree_count(db, child)
    return total


def tree_payload(db: Session) -> Dict[str, Any]:
    """方向树 -> {nodes, edges}：nodes 带子树论文数，edges 为 parent 边。"""
    nodes = db.query(DirectionNode).order_by(DirectionNode.depth, DirectionNode.path).all()
    by_id = {n.id: n for n in nodes}
    subtree = {n.id: n.paper_count for n in nodes}
    for node in sorted(nodes, key=lambda n: -n.depth):
        if node.parent_id and node.parent_id in subtree:
            subtree[node.parent_id] += subtree[node.id]

    out_nodes = [{
        "id": n.id,
        "name": n.name,
        "path": n.path,
        "depth": n.depth,
        "parent_id": n.parent_id,
        "description": n.description,
        "paper_count": n.paper_count,
        "subtree_paper_count": subtree.get(n.id, 0),
        "has_synthesis": bool(n.synthesis_md),
        "synthesis_at": _iso(n.synthesis_at),
        "synthesis_paper_count": n.synthesis_paper_count,
        "updated_at": _iso(n.updated_at),
    } for n in nodes]
    out_edges = [{"source": n.parent_id, "target": n.id, "type": "parent"}
                 for n in nodes if n.parent_id in by_id]
    return {"nodes": out_nodes, "edges": out_edges,
            "total_papers": max([n.paper_count for n in nodes], default=0)}


def node_payload(db: Session, node: DirectionNode) -> Dict[str, Any]:
    """单节点详情：论文列表 + 局部图谱（节点/边）+ 综述历史。"""
    directions = (db.query(PaperDirection).filter(PaperDirection.node_id == node.id)
                  .order_by(PaperDirection.created_at.desc()).all())
    papers = [d.paper for d in directions if d.paper is not None]
    paper_ids = {p.id for p in papers}

    graph_nodes = [{
        "id": p.id,
        "label": p.title or p.arxiv_id,
        "arxiv_id": p.arxiv_id,
        "year": (p.published or "")[:4],
        "role": next((d.role for d in directions if d.paper_id == p.id), "secondary"),
        "added_at": _iso(p.added_at),
    } for p in papers]

    edges = [e for e in list_edges(db, node.id)
             if e.src_paper_id in paper_ids and e.dst_paper_id in paper_ids]
    graph_edges = [{
        "id": e.id,
        "source": e.src_paper_id,
        "target": e.dst_paper_id,
        "relation": e.relation,
        "direction": e.direction,
        "strength": e.strength,
        "rationale": e.rationale,
        "evidence": e.evidence,
    } for e in edges]

    children = (db.query(DirectionNode).filter(DirectionNode.parent_id == node.id)
                .order_by(DirectionNode.name).all())
    return {
        "node": {
            "id": node.id,
            "name": node.name,
            "path": node.path,
            "depth": node.depth,
            "parent_id": node.parent_id,
            "description": node.description,
            "paper_count": node.paper_count,
            "subtree_paper_count": _subtree_count(db, node),
            "has_synthesis": bool(node.synthesis_md),
            "synthesis_at": _iso(node.synthesis_at),
            "synthesis_paper_count": node.synthesis_paper_count,
            "graph_md_path": node.graph_md_path,
            "graph_md_at": _iso(node.graph_md_at),
            "is_leaf": node.is_leaf,
        },
        "papers": [{
            "id": d.paper_id,
            "arxiv_id": d.paper.arxiv_id if d.paper else "",
            "title": d.paper.title if d.paper else "",
            "role": d.role,
            "reason": d.reason,
            "confidence": d.confidence,
            "added_at": _iso(d.paper.added_at) if d.paper else None,
        } for d in directions],
        "graph": {"nodes": graph_nodes, "edges": graph_edges},
        "synthesis": {
            "latest": node.synthesis_md,
            "at": _iso(node.synthesis_at),
            "paper_count": node.synthesis_paper_count,
            "history": [{"id": s.id, "created_at": _iso(s.created_at),
                         "paper_count": s.paper_count, "trigger": s.trigger,
                         "content_md": s.content_md} for s in synthesis_history(db, node.id)],
        },
        "children": [{"id": c.id, "name": c.name, "path": c.path,
                      "paper_count": c.paper_count,
                      "subtree_paper_count": _subtree_count(db, c)} for c in children],
    }


def paper_knowledge_payload(db: Session, paper: Paper) -> Dict[str, Any]:
    """单论文视角：产物清单 + 归属 + 关联边 + pipeline 记录。"""
    artifacts = (db.query(PaperArtifact).filter(PaperArtifact.paper_id == paper.id)
                 .order_by(PaperArtifact.kind).all())
    directions = paper_memberships(db, paper.id)
    edges = (db.query(PaperRelation)
             .filter((PaperRelation.src_paper_id == paper.id)
                     | (PaperRelation.dst_paper_id == paper.id))
             .order_by(PaperRelation.id.desc()).all())
    runs = (db.query(PipelineRun).filter(PipelineRun.paper_id == paper.id)
            .order_by(PipelineRun.id.desc()).limit(5).all())
    return {
        "paper": {"id": paper.id, "arxiv_id": paper.arxiv_id, "title": paper.title},
        "artifacts": [{
            "kind": a.kind, "chars": len(a.content_md or ""),
            "model": a.model, "reasoning_effort": a.reasoning_effort,
            "updated_at": _iso(a.updated_at), "stats": a.stats,
        } for a in artifacts],
        "memberships": [{
            "node_id": d.node_id, "path": d.node.path if d.node else "",
            "name": d.node.name if d.node else "", "role": d.role,
            "reason": d.reason, "confidence": d.confidence,
        } for d in directions],
        "edges": [{
            "id": e.id, "node_id": e.node_id, "relation": e.relation,
            "direction": e.direction, "strength": e.strength, "rationale": e.rationale,
            "source": e.src_paper_id, "target": e.dst_paper_id,
        } for e in edges],
        "runs": [{
            "id": r.id, "status": r.status, "current_step": r.current_step,
            "started_at": _iso(r.started_at), "finished_at": _iso(r.finished_at),
            "error": r.error, "steps": r.steps,
        } for r in runs],
    }


# ---------------------------------------------------------------- 方向树 -> 文件夹树镜像
_UNSAFE_NAME_RE = re.compile(r"[^\w\-\u4e00-\u9fff ]")


def safe_name(name: str) -> str:
    """文件夹名清洗：去掉路径分隔符与奇怪字符（保持可读）。"""
    cleaned = _UNSAFE_NAME_RE.sub("_", (name or "").strip()).strip(". ")
    return (cleaned or "untitled")[:80]


def _child_folder(db: Session, parent: Optional[Folder], name: str) -> Optional[Folder]:
    query = db.query(Folder).filter(Folder.name == name)
    query = query.filter(Folder.parent_id.is_(None)) if parent is None else query.filter(Folder.parent_id == parent.id)
    return query.first()


def sync_node_folders(db: Session, node: DirectionNode) -> Optional[Folder]:
    """将方向节点（含祖先）镜像成同名嵌套文件夹，返回该节点对应的文件夹。

    左侧文件夹树因此就是方向树的可视化体现；已存在的同名文件夹会被“认领”
    （补上 direction_node_id），不改变用户已有的文件夹结构。
    逐层认领：中间层只要本身也是方向节点，它的文件夹同样挂上 🧭，
    否则点父目录看不到该层的论文图谱。
    """
    parts = node.path.split(PATH_SEP)
    parent_folder: Optional[Folder] = None
    acc: List[str] = []
    for name in parts:
        acc.append(name)
        folder = _child_folder(db, parent_folder, name)
        if folder is None:
            folder = Folder(name=name, parent_id=parent_folder.id if parent_folder else None)
            db.add(folder)
            db.commit()
            db.refresh(folder)
        owner = get_node_by_path(db, path_key(acc))
        if owner is not None and folder.direction_node_id != owner.id \
                and folder.direction_node_id is None:
            folder.direction_node_id = owner.id
            db.commit()
        parent_folder = folder
    return parent_folder


def move_paper_to_folder(db: Session, paper: Paper, folder: Optional[Folder]) -> None:
    """把论文挂到方向文件夹下（多归属时以调用方传入的 primary 为准）。"""
    if folder is None or paper.folder_id == folder.id:
        return
    paper.folder_id = folder.id
    db.commit()


def refresh_nodes(db: Session, nodes: Sequence[DirectionNode], *,
                  move_paper: Optional[Paper] = None,
                  move_paper_node: Optional[DirectionNode] = None) -> List[str]:
    """节点变化后的统一收尾：镜像文件夹 + 导出图谱 md（可选把论文移入其方向文件夹）。"""
    exported: List[str] = []
    seen: set = set()
    for node in nodes:
        if node is None or node.id in seen:
            continue
        seen.add(node.id)
        sync_node_folders(db, node)
        exported.append(str(export_node_graph_md(db, node)))
    if move_paper is not None:
        target = move_paper_node or (nodes[0] if nodes else None)
        if target is not None:
            move_paper_to_folder(db, move_paper, sync_node_folders(db, target))
    return exported


# ---------------------------------------------------------------- 图谱 Markdown 落盘
KNOWLEDGE_DIR_NAME = "knowledge"


def knowledge_md_root() -> Path:
    """图谱 md 的根目录（默认 data/knowledge/，与 library.db 同层）。"""
    return Path(config_loader.get_base_dir()) / KNOWLEDGE_DIR_NAME


def _node_md_dir(node: DirectionNode) -> Path:
    parts = [safe_name(p) for p in node.path.split(PATH_SEP) if p.strip()]
    return knowledge_md_root().joinpath(*parts) if parts else knowledge_md_root()


def _node_md_path(node: DirectionNode) -> Path:
    return _node_md_dir(node) / graphdoc.GRAPH_MD_FILENAME


def export_node_graph_md(db: Session, node: DirectionNode) -> Path:
    """渲染并写入该节点的图谱 md（每个节点一份；叶子节点必有）。"""
    target = _node_md_path(node)
    target.parent.mkdir(parents=True, exist_ok=True)
    content = graphdoc.render_graph_md(db, node)
    target.write_text(content, encoding="utf-8")
    node.graph_md_path = str(target)
    node.graph_md_at = _utcnow()
    db.commit()
    return target


def export_all_graph_md(db: Session) -> List[Path]:
    """导出全部节点（含空节点）的图谱 md，便于整体刷新/迁移。"""
    nodes = db.query(DirectionNode).order_by(DirectionNode.path).all()
    return [export_node_graph_md(db, node) for node in nodes]


def _like_prefix(value: str) -> str:
    """LIKE 前缀匹配：节点名可能含 _ / % ，必须转义，否则会误伤兄弟节点。"""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


def _descendant_nodes(db: Session, node: DirectionNode) -> List[DirectionNode]:
    """该节点的全部后代（按深度升序）：物化路径前缀匹配，改名/删除时整体平移。"""
    return (db.query(DirectionNode)
            .filter(DirectionNode.path.like(_like_prefix(node.path + PATH_SEP), escape="\\"),
                    DirectionNode.id != node.id)
            .order_by(DirectionNode.depth, DirectionNode.path).all())


def _join_path(*parts: str) -> str:
    return PATH_SEP.join([p for p in parts if p])


def _relocate_md_dir(old_dir: Path, new_dir: Path) -> None:
    """改名后搬迁图谱 md 目录（保留其中文件）；目标已存在则不动，交给重新导出。"""
    try:
        if old_dir != new_dir and old_dir.exists() and not new_dir.exists():
            new_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old_dir), str(new_dir))
    except OSError:                     # 搬迁失败不影响主流程：导出会把 md 写到新位置
        pass


def folder_of_node(db: Session, node_id: int) -> Optional[Folder]:
    return db.query(Folder).filter(Folder.direction_node_id == node_id).first()


def rename_node(db: Session, node: DirectionNode, new_name: str) -> DirectionNode:
    """重命名方向节点：后代 path/depth、镜像文件夹名、图谱 md 目录一起平移。

    方向树的 path 是物化路径，改名必须整棵子树同步，否则父子关系会错位；
    同级重名直接拒绝（path 有唯一约束）。
    """
    name = str(new_name or "").strip().strip(PATH_SEP).strip()[:120]
    if not name:
        raise ValueError("方向节点名不能为空")
    if name == node.name:
        return node
    parent_path = node.path.rsplit(PATH_SEP, 1)[0] if PATH_SEP in node.path else ""
    new_path = _join_path(parent_path, name)
    clash = (db.query(DirectionNode)
             .filter(DirectionNode.path == new_path, DirectionNode.id != node.id).first())
    if clash is not None:
        raise ValueError("同级已存在同名方向：{}".format(new_path))

    descendants = _descendant_nodes(db, node)
    old_dir = _node_md_dir(node)
    old_prefix, new_prefix = node.path + PATH_SEP, new_path + PATH_SEP
    node.name, node.path = name, new_path
    for child in descendants:
        child.path = new_prefix + child.path[len(old_prefix):]
        child.depth = child.path.count(PATH_SEP)
    db.commit()

    folder = folder_of_node(db, node.id)
    if folder is not None and folder.name != name:
        folder.name = name
        db.commit()

    _relocate_md_dir(old_dir, _node_md_dir(node))
    for item in [node] + descendants:            # path 写进了 md 头部，整棵子树重渲染
        export_node_graph_md(db, item)
    parent = db.get(DirectionNode, node.parent_id) if node.parent_id else None
    if parent is not None:
        export_node_graph_md(db, parent)         # 父节点 md 里含子方向列表
    return node


def delete_node(db: Session, node: DirectionNode) -> int:
    """删除误建节点：子节点上移一层（含后代 path 平移），清掉归属/关系/绑定与 md。

    论文本身、以及用户手动建的文件夹都不删除；返回上移的子节点数。
    """
    node_id = node.id
    parent = db.get(DirectionNode, node.parent_id) if node.parent_id else None
    parent_path = parent.path if parent is not None else ""
    descendants = _descendant_nodes(db, node)
    old_prefix = node.path + PATH_SEP
    for child in descendants:
        child.path = _join_path(parent_path, child.path[len(old_prefix):])
        child.depth = child.path.count(PATH_SEP)
        if child.parent_id == node_id:
            child.parent_id = parent.id if parent is not None else None
    db.commit()

    md_file = _node_md_dir(node) / graphdoc.GRAPH_MD_FILENAME
    # 关系边没有 ORM 级联，必须显式清掉，避免留下悬空 node_id
    (db.query(PaperRelation).filter(PaperRelation.node_id == node_id)
     .delete(synchronize_session=False))
    db.delete(node)                              # 归属 / 综述按 ORM 级联删除
    db.commit()
    try:
        if md_file.exists():
            md_file.unlink()
    except OSError:
        pass

    folder = folder_of_node(db, node.id)
    if folder is not None:                       # 文件夹保留给用户（可能已放论文），只解绑
        folder.direction_node_id = None
        db.commit()
    if parent is not None:
        export_node_graph_md(db, parent)
    return len([c for c in descendants if c.parent_id == (parent.id if parent else None)])


def repair_paper_folders(db: Session) -> int:
    """把已归属方向树的论文移到其 primary 方向文件夹下（升级/手工修复用）。

    返回移动的论文数。正常流程里单篇 pipeline 的 tree 步骤会自动落位，
    这里只用于处理"先有归属、后才有文件夹镜像"的历史数据。
    """
    moved = 0
    for direction in db.query(PaperDirection).order_by(PaperDirection.id).all():
        if direction.role != "primary" or direction.node is None or direction.paper is None:
            continue
        folder = sync_node_folders(db, direction.node)
        if folder is not None and direction.paper.folder_id != folder.id:
            direction.paper.folder_id = folder.id
            moved += 1
    if moved:
        db.commit()
    return moved


# ---------------------------------------------------------------- 缓冲区（待读区）
BUFFER_PENDING = "pending"
BUFFER_READ = "read"
BUFFER_REJECTED = "rejected"


def upsert_buffer_paper(db: Session, hit: Any, *, match_score: float = 0.0,
                        suggested_path: str = "", matched_node_id: Optional[int] = None,
                        screen_reason: str = "", search_run_id: Optional[int] = None,
                        source: str = "search") -> BufferPaper:
    """写入/更新缓冲区条目（按 arxiv_id 去重，不覆写已完成的总结与状态）。"""
    data = hit.as_dict() if hasattr(hit, "as_dict") else dict(hit)
    arxiv_id = str(data.get("arxiv_id") or "").strip()
    row = db.query(BufferPaper).filter(BufferPaper.arxiv_id == arxiv_id).first()
    if row is None:
        row = BufferPaper(arxiv_id=arxiv_id, status=BUFFER_PENDING)
        db.add(row)
    row.title = str(data.get("title") or "")[:1000]
    row.abstract = str(data.get("abstract") or "")
    row.authors_json = json.dumps(data.get("authors") or [], ensure_ascii=False)
    row.categories_json = json.dumps(data.get("categories") or [], ensure_ascii=False)
    row.published = data.get("published")
    if match_score:
        row.match_score = float(match_score)
    if suggested_path:
        row.suggested_path = suggested_path[:1024]
    if matched_node_id:
        row.matched_node_id = int(matched_node_id)
    if screen_reason:
        row.screen_reason = str(screen_reason)[:2000]
    if search_run_id:
        row.search_run_id = int(search_run_id)
    row.source = source
    row.updated_at = _utcnow()
    db.commit()
    db.refresh(row)
    return row


def list_buffer(db: Session, *, status: str = BUFFER_PENDING, q: Optional[str] = None,
                limit: int = 200) -> List[BufferPaper]:
    query = db.query(BufferPaper)
    if status and status != "all":
        query = query.filter(BufferPaper.status == status)
    keyword = (q or "").strip()
    if keyword:
        like = "%{}%".format(keyword)
        query = query.filter((BufferPaper.title.ilike(like)) | (BufferPaper.abstract.ilike(like))
                             | (BufferPaper.arxiv_id.ilike(like)))
    return (query.order_by(BufferPaper.match_score.desc(), BufferPaper.id.desc())
            .limit(max(1, int(limit))).all())


def get_buffer(db: Session, buffer_id: int) -> Optional[BufferPaper]:
    return db.get(BufferPaper, buffer_id)


def set_buffer_screening(db: Session, row: BufferPaper, *, score: float, path: str,
                         matched_node_id: Optional[int], reason: str) -> BufferPaper:
    row.match_score = float(score)
    row.suggested_path = (path or "")[:1024]
    row.matched_node_id = matched_node_id
    row.screen_reason = (reason or "")[:2000]
    row.updated_at = _utcnow()
    db.commit()
    return row


def set_buffer_summary(db: Session, row: BufferPaper, *, summary_text: str,
                       stats: Optional[Dict[str, Any]], verified_path: str,
                       verify_reason: str, fit: bool) -> BufferPaper:
    row.summary_text = summary_text or ""
    row.summary_stats_json = json.dumps(stats or {}, ensure_ascii=False)
    row.verified_path = (verified_path or "")[:1024]
    row.verify_reason = (verify_reason or "")[:2000]
    if fit:
        row.status = BUFFER_PENDING
    row.updated_at = _utcnow()
    db.commit()
    return row


def set_buffer_status(db: Session, row: BufferPaper, status: str) -> BufferPaper:
    row.status = status
    row.updated_at = _utcnow()
    db.commit()
    return row


def buffer_stats(db: Session) -> Dict[str, int]:
    counts = {BUFFER_PENDING: 0, BUFFER_READ: 0, BUFFER_REJECTED: 0}
    for status, total in (db.query(BufferPaper.status, func.count(BufferPaper.id))
                          .group_by(BufferPaper.status).all()):
        counts[status] = int(total)
    counts["total"] = sum(counts.values())
    return counts


# ---------------------------------------------------------------- 检索 run（异步任务状态）
SEARCH_LOG_TAIL = 300


def create_search_run(db: Session, request: Dict[str, Any]) -> SearchRun:
    run = SearchRun(status="running", stage="search",
                    request_json=json.dumps(request, ensure_ascii=False), log_json="[]",
                    counters_json=json.dumps({"found": 0, "screened": 0, "summarized": 0,
                                              "buffered": 0, "dropped": 0}, ensure_ascii=False))
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def log_search(db: Session, run: SearchRun, message: str, *, level: str = "info",
               arxiv_id: str = "") -> None:
    logs = run.logs
    logs.append({"ts": _utcnow().strftime("%H:%M:%S"), "level": level,
                 "message": str(message)[:400], "arxiv_id": arxiv_id})
    run.log_json = json.dumps(logs[-SEARCH_LOG_TAIL:], ensure_ascii=False)
    db.commit()


def update_search_run(db: Session, run: SearchRun, *, stage: Optional[str] = None,
                      status: Optional[str] = None, error: Optional[str] = None,
                      counters: Optional[Dict[str, Any]] = None,
                      finished: bool = False) -> SearchRun:
    if stage:
        run.stage = stage
    if status:
        run.status = status
    if error is not None:
        run.error = str(error)[:1000]
    if counters is not None:
        merged = dict(run.counters)
        merged.update(counters)
        run.counters_json = json.dumps(merged, ensure_ascii=False)
    if finished:
        run.finished_at = _utcnow()
    db.commit()
    return run


def get_search_run(db: Session, run_id: int) -> Optional[SearchRun]:
    return db.get(SearchRun, run_id)


def recent_search_runs(db: Session, limit: int = 10) -> List[SearchRun]:
    return (db.query(SearchRun).order_by(SearchRun.id.desc()).limit(max(1, int(limit))).all())


def search_run_payload(run: SearchRun, *, log_tail: int = 80) -> Dict[str, Any]:
    return {
        "id": run.id, "status": run.status, "stage": run.stage,
        "counters": run.counters, "error": run.error,
        "cancel_requested": bool(run.cancel_requested),
        "started_at": _iso(run.started_at), "finished_at": _iso(run.finished_at),
        "request": run.request,
        "logs": run.logs[-max(1, int(log_tail)):],
        "log_total": len(run.logs),
    }
