"""知识网络的存储层：方向树 / 论文归属 / 产物 / 关系边 / 综述 / pipeline 账本。

只暴露"业务动作"（建节点、挂论文、写边、取树…），Web 层与 pipeline 都不直接拼 SQL。
所有图结构统一序列化为「节点 + 边」JSON，可直接喂 D3 / ECharts：
  tree_payload()          方向树：nodes + parent 边
  node_payload()          单节点：论文节点 + 关系边（局部图谱）+ 综述历史
  paper_knowledge_payload() 单论文：产物、归属、关联边、pipeline 记录
"""

import datetime
import json
import math
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from harness.config import get_base_dir
from app.models import (DirectionNode, Folder, FolderReview, NodeSynthesis, Paper,
                        PaperArtifact, PaperArtifactVersion, PaperDirection, PaperRelation, PipelineRun,
                        RelatedCandidate, RelatedRun)

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
    return PATH_SEP.join(normalize_path(path))


def get_node_by_path(db: Session, path: str) -> Optional[DirectionNode]:
    return db.query(DirectionNode).filter(DirectionNode.path == path).first()


def get_or_create_node(db: Session, path: Sequence[str], description: str = "", *, commit: bool = True) -> Optional[DirectionNode]:
    """按路径逐层建点（缺哪层补哪层），返回最深节点；路径非法则返回 None。"""
    parts = normalize_path(path)
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
            db.flush()
        elif description and depth == len(parts) - 1 and not node.description:
            node.description = description
        parent = node
    if commit:
        db.commit()
    return node


def recount_node(db: Session, node: DirectionNode) -> int:
    """重算节点直接挂载的论文数（归属变化后必须调用，保证图谱节点大小正确）。"""
    count = db.query(PaperDirection).filter(PaperDirection.node_id == node.id).count()
    if node.paper_count != count:
        node.paper_count = count
        db.commit()
    return count


def set_paper_memberships(db: Session, paper: Paper, memberships: Sequence[Dict[str, Any]], *,
                          language: str = "unknown", source: str = "auto", commit: bool = True) -> List[PaperDirection]:
    """先完整校验，再在同一事务内更新唯一分类、位置、计数与失效关系。"""
    if not isinstance(memberships, (list, tuple)) or len(memberships) != 1 or not isinstance(memberships[0], dict):
        raise ValueError("每篇论文必须且只能选择一个分类；旧分类未修改")
    item = memberships[0]
    path = normalize_path(item.get("path"))
    node = None
    if item.get("node_id") is not None:
        if isinstance(item["node_id"], bool) or not str(item["node_id"]).isdigit():
            raise ValueError("分类 node_id 非法")
        node = db.get(DirectionNode, int(item["node_id"]))
        if node is None:
            raise ValueError("分类节点不存在；旧分类未修改")
    elif not path or any("/" in part or part in (".", "..") for part in path):
        raise ValueError("分类路径非法；旧分类未修改")
    try:
        confidence = float(item.get("confidence") or 0.0)
    except (TypeError, ValueError):
        raise ValueError("分类置信度必须是数字")
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("分类置信度必须在 0～1 之间")
    try:
        node = _resolve_membership_node(db, node, path)
        folder = sync_node_folders(db, node, commit=False)
        row = db.query(PaperDirection).filter_by(paper_id=paper.id).first()
        old_id = row.node_id if row else None
        if row is None:
            row = PaperDirection(paper_id=paper.id, node_id=node.id)
            db.add(row)
        elif old_id != node.id:
            row.node_id = node.id
            row.relations_at = None
            row.relations_count = 0
        row.role = "primary"
        row.reason = str(item.get("reason") or "")[:4000]
        row.confidence = confidence
        row.output_language = language
        paper.folder_id = folder.id
        db.flush()
        _clean_local_edges(db, paper.id, node.id)
        _recount_ids(db, {old_id, node.id})
        if commit:
            db.commit()
        db.expire(paper, ["directions", "folder"])
        db.expire(row, ["node"])
        return [row]
    except Exception:
        db.rollback()
        raise


def _resolve_membership_node(db: Session, node: Optional[DirectionNode], path: List[str]) -> DirectionNode:
    """已有节点只是更粗的上级、而路径又向下延伸时，建子类而不是停在上级。"""
    if node is not None and path:
        current = normalize_path(node.path)
        if path[:len(current)] == current and len(path) > len(current):
            created = get_or_create_node(db, path, commit=False)
            if created is not None:
                return created
        return node
    if node is not None:
        return node
    if not path or any(part in (".", "..") for part in path):
        raise ValueError("分类路径非法；旧分类未修改")
    created = get_or_create_node(db, path, commit=False)
    if created is None:
        raise ValueError("分类路径非法；旧分类未修改")
    return created


def _recount_ids(db: Session, ids: Iterable[Optional[int]]) -> None:
    db.flush()
    for node_id in set(ids) - {None}:
        node = db.get(DirectionNode, node_id)
        if node:
            node.paper_count = db.query(PaperDirection).filter_by(node_id=node_id).count()


def _clean_local_edges(db: Session, paper_id: int, node_id: Optional[int]) -> None:
    query = db.query(PaperRelation).filter(or_(PaperRelation.src_paper_id == paper_id,
                                               PaperRelation.dst_paper_id == paper_id))
    if node_id is not None:
        query = query.filter(PaperRelation.node_id != node_id)
    query.delete(synchronize_session="fetch")


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


def subtree_node_ids(db: Session, node_id: int) -> List[int]:
    """节点 + 全部后代节点 id。"""
    ids = [int(node_id)]
    queue = [int(node_id)]
    while queue:
        current = queue.pop()
        for (child_id,) in db.query(DirectionNode.id).filter(DirectionNode.parent_id == current).all():
            ids.append(child_id)
            queue.append(child_id)
    return ids


def get_subtree_papers(db: Session, node: DirectionNode, exclude_paper_id: Optional[int] = None,
                       limit: Optional[int] = None) -> List[Paper]:
    """节点子树内的论文：直属论文在前，其余后代节点论文按入库时间倒序接在后面。

    关系抽取用：只对比同节点会漏掉父子/兄弟方向的近邻论文（实测浅分类的论文
    与深层节点里的高度相关论文完全遇不到，关系图因此过稀）。
    """
    direct = get_node_papers(db, node, exclude_paper_id=exclude_paper_id)
    direct_ids = {p.id for p in direct}
    query = (db.query(Paper)
             .join(PaperDirection, PaperDirection.paper_id == Paper.id)
             .filter(PaperDirection.node_id.in_(subtree_node_ids(db, node.id))))
    if exclude_paper_id is not None:
        query = query.filter(Paper.id != exclude_paper_id)
    deeper = [p for p in query.order_by(Paper.added_at.desc(), Paper.id.desc()).all()
              if p.id not in direct_ids]
    merged = direct + deeper
    return merged[:int(limit)] if limit else merged


def paper_memberships(db: Session, paper_id: int) -> List[PaperDirection]:
    """论文的唯一方向归属；普通文件夹中的论文可以没有方向节点。"""
    rows = (db.query(PaperDirection)
            .filter(PaperDirection.paper_id == paper_id)
            .order_by(PaperDirection.id).all())
    return sorted(rows, key=lambda r: (0 if r.role == "primary" else 1, r.id))


def folder_subtree_ids(db: Session, folder_id: int) -> List[int]:
    """按邻接表迭代遍历，支持普通目录与方向目录，防止损坏数据成环。"""
    rows = db.query(Folder.id, Folder.parent_id).all()
    children: Dict[int, List[int]] = {}
    for fid, parent in rows:
        children.setdefault(parent, []).append(fid)
    if folder_id not in {fid for fid, _ in rows}:
        raise ValueError("文件夹不存在")
    seen, stack = set(), [folder_id]
    while stack:
        fid = stack.pop()
        if fid in seen:
            continue
        seen.add(fid)
        stack.extend(children.get(fid, []))
    return sorted(seen)


def folder_path(db: Session, folder: Folder) -> str:
    parts, seen = [], set()
    while folder is not None:
        if folder.id in seen:
            raise ValueError("目录存在环")
        seen.add(folder.id)
        parts.append(folder.name)
        folder = db.get(Folder, folder.parent_id) if folder.parent_id else None
    return "/".join(reversed(parts))


def folder_papers(db: Session, folder_id: int, recursive: bool = True) -> List[Paper]:
    ids = folder_subtree_ids(db, folder_id) if recursive else [folder_id]
    return db.query(Paper).filter(Paper.folder_id.in_(ids)).order_by(Paper.id).all()


def update_folder_location(db: Session, folder: Folder, name: str, parent_id: Optional[int], *, commit: bool = True) -> None:
    """移动或改名后同步整棵子树的方向物化路径，不移动论文/PDF 文件。"""
    name = name.strip()
    if not name or "/" in name or name in (".", ".."):
        raise ValueError("文件夹名不能为空或包含路径分隔符")
    subtree = folder_subtree_ids(db, folder.id)
    if parent_id in subtree:
        raise ValueError("不能移动到自身或后代目录")
    if parent_id is not None and db.get(Folder, parent_id) is None:
        raise ValueError("父文件夹不存在")
    clash = db.query(Folder).filter(Folder.parent_id == parent_id, Folder.name == name,
                                    Folder.id != folder.id).first()
    if clash:
        raise ValueError("同级已存在同名文件夹")
    folder.name, folder.parent_id = name, parent_id
    db.flush()
    nodes = _sync_direction_paths(db, subtree)
    if commit:
        db.commit()
        for node in nodes:
            export_node_graph_md(db, node)


def _sync_direction_paths(db: Session, subtree: Sequence[int]) -> List[DirectionNode]:
    affected = db.query(Folder).filter(Folder.id.in_(subtree), Folder.direction_node_id.isnot(None)).all()
    nodes = {f.id: db.get(DirectionNode, f.direction_node_id) for f in affected}
    paths = {f.id: folder_path(db, f) for f in affected}
    affected_ids = {n.id for n in nodes.values() if n}
    for path in paths.values():
        conflict = db.query(DirectionNode).filter(DirectionNode.path == path,
                                                  ~DirectionNode.id.in_(affected_ids)).first()
        if conflict:
            db.rollback()
            raise ValueError("目标路径已存在方向节点")
    for f in affected:
        node = nodes[f.id]
        if node is None:
            continue
        ancestor = db.get(Folder, f.parent_id) if f.parent_id else None
        while ancestor and not ancestor.direction_node_id:
            ancestor = db.get(Folder, ancestor.parent_id) if ancestor.parent_id else None
        node.name, node.path = f.name, paths[f.id]
        node.depth = paths[f.id].count("/")
        node.parent_id = ancestor.direction_node_id if ancestor else None
    db.flush()
    return [node for node in nodes.values() if node]


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
    else:
        # 重跑覆盖当前结果，不保留上一版。
        (db.query(PaperArtifactVersion)
         .filter(PaperArtifactVersion.paper_id == paper_id, PaperArtifactVersion.kind == kind)
         .delete(synchronize_session=False))
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


def mark_relations(db: Session, direction: PaperDirection, count: int, signature: str = "") -> None:
    """标记该（论文, 节点）已完成关系抽取（即使 0 条边），供下一轮幂等跳过。"""
    direction.relations_at = _utcnow()
    direction.relations_count = int(count)
    direction.relations_signature = signature
    db.commit()


def upsert_edges(db: Session, node_id: int, src_paper_id: int,
                 edges: Sequence[Dict[str, Any]], *, allowed_ids: Optional[Iterable[int]] = None,
                 language: str = "unknown", commit: bool = True) -> int:
    """写入以 src 为起点的关系边（同 (node,src,dst,relation) 覆盖更新）。"""
    written = 0
    valid_relations = {"cites", "extends", "improves", "contradicts", "complements", "applies", "baseline_of", "uses"}
    allowed = set(allowed_ids) if allowed_ids is not None else None
    seen = set()
    for item in edges or []:
        if not isinstance(item, dict):
            continue
        try:
            dst = int(item.get("other_paper_id") or item.get("dst_paper_id") or 0)
        except (TypeError, ValueError):
            continue
        relation = str(item.get("relation") or "").strip().lower()
        if (not dst or dst == src_paper_id or dst in seen or relation not in valid_relations
                or (allowed is not None and dst not in allowed) or db.get(Paper, dst) is None
                or str(item.get("direction") or "->") not in ("->", "<-", "<->")):
            continue
        if allowed is not None and (type(item.get("other_paper_id")) is not int
                or not isinstance(item.get("rationale"), str) or not item["rationale"].strip()
                or not isinstance(item.get("evidence"), str) or not item["evidence"].strip()):
            continue
        strength = item.get("strength", 0.5)
        if type(strength) not in (int, float) or not math.isfinite(strength) or not 0 <= strength <= 1:
            continue
        seen.add(dst)
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
        row.output_language = language
        row.strength = strength
        row.rationale = str(item.get("rationale") or "")
        row.evidence = str(item.get("evidence") or "")
        written += 1
    if commit:
        db.commit()
    return written


def replace_edges(db: Session, direction: PaperDirection, edges, allowed_ids, language, signature):
    """完整结果校验后事务替换此次比对范围，任何非法边都保留原关系。"""
    allowed = set(allowed_ids)
    # 比对范围是方向子树（见 get_subtree_papers）：一致性校验也按子树成员算。
    node = db.get(DirectionNode, direction.node_id)
    subtree = subtree_node_ids(db, direction.node_id) if node is not None else [direction.node_id]
    current = {d.paper_id for d in db.query(PaperDirection)
               .filter(PaperDirection.node_id.in_(subtree)).all()}
    if direction.paper_id not in current or not allowed <= current:
        raise ValueError("论文归属在关系抽取期间变化，请重试")
    try:
        db.query(PaperRelation).filter(PaperRelation.node_id == direction.node_id,
            PaperRelation.src_paper_id == direction.paper_id,
            PaperRelation.dst_paper_id.in_(allowed)).delete(synchronize_session="fetch")
        count = upsert_edges(db, direction.node_id, direction.paper_id, edges,
                             allowed_ids=allowed, language=language, commit=False)
        if count != len(edges):
            raise ValueError("关系包含非法 ID、类型、强度或缺少原文证据")
        direction.relations_at = _utcnow()
        direction.relations_count, direction.relations_signature = count, signature
        db.commit()
        return count
    except Exception:
        db.rollback()
        raise


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


def synthesis_history(db: Session, node_id: int, limit: Optional[int] = 10) -> List[NodeSynthesis]:
    query = (db.query(NodeSynthesis).filter(NodeSynthesis.node_id == node_id)
             .order_by(NodeSynthesis.created_at.desc(), NodeSynthesis.id.desc()))
    return (query.limit(limit) if limit is not None else query).all()


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
            "total_papers": db.query(PaperDirection.paper_id).distinct().count()}


def node_payload(db: Session, node: DirectionNode) -> Dict[str, Any]:
    """单节点详情：论文列表 + 局部图谱（节点/边）+ 综述历史。

    关系抽取按子树比对（见 get_subtree_papers），边的另一端常在后代节点里；
    图谱节点因此取「直属论文 ∪ 边的端点论文」，外部端点标 external=True。
    """
    directions = (db.query(PaperDirection).filter(PaperDirection.node_id == node.id)
                  .order_by(PaperDirection.created_at.desc()).all())
    papers = [d.paper for d in directions if d.paper is not None]
    paper_ids = {p.id for p in papers}

    all_edges = list_edges(db, node.id)
    endpoint_ids = {e.src_paper_id for e in all_edges} | {e.dst_paper_id for e in all_edges}
    extra_ids = endpoint_ids - paper_ids
    extras = db.query(Paper).filter(Paper.id.in_(extra_ids)).all() if extra_ids else []

    graph_nodes = [{
        "id": p.id,
        "label": p.title or p.arxiv_id,
        "arxiv_id": p.arxiv_id,
        "year": (p.published or "")[:4],
        "role": next((d.role for d in directions if d.paper_id == p.id), "secondary"),
        "external": False,
        "added_at": _iso(p.added_at),
    } for p in papers] + [{
        "id": p.id,
        "label": p.title or p.arxiv_id,
        "arxiv_id": p.arxiv_id,
        "year": (p.published or "")[:4],
        "role": "external",
        "external": True,
        "added_at": _iso(p.added_at),
    } for p in extras]

    graph_node_ids = paper_ids | {p.id for p in extras}
    edges = [e for e in all_edges
             if e.src_paper_id in graph_node_ids and e.dst_paper_id in graph_node_ids]
    graph_edges = [{
        "id": e.id,
        "source": e.src_paper_id,
        "target": e.dst_paper_id,
        "relation": e.relation,
        "direction": e.direction,
        "strength": e.strength,
        "rationale": e.rationale,
        "evidence": e.evidence,
        "output_language": e.output_language,
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
    # 图谱要画出对端是谁：把边两端论文的标题/编号一次查出来带上，前端不必再逐条请求
    endpoint_ids = {value for edge in edges for value in (edge.src_paper_id, edge.dst_paper_id)}
    endpoints = {row.id: row for row in db.query(Paper).filter(Paper.id.in_(endpoint_ids)).all()} \
        if endpoint_ids else {}
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
            "source_title": (endpoints.get(e.src_paper_id).title if endpoints.get(e.src_paper_id) else ""),
            "source_arxiv_id": (endpoints.get(e.src_paper_id).arxiv_id if endpoints.get(e.src_paper_id) else ""),
            "target_title": (endpoints.get(e.dst_paper_id).title if endpoints.get(e.dst_paper_id) else ""),
            "target_arxiv_id": (endpoints.get(e.dst_paper_id).arxiv_id if endpoints.get(e.dst_paper_id) else ""),
            "evidence": e.evidence, "output_language": e.output_language,
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
    cleaned = (cleaned or "untitled")[:80].rstrip(". ")
    if re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", cleaned):
        cleaned = "_" + cleaned
    return cleaned


def _child_folder(db: Session, parent: Optional[Folder], name: str) -> Optional[Folder]:
    query = db.query(Folder).filter(Folder.name == name)
    query = query.filter(Folder.parent_id.is_(None)) if parent is None else query.filter(Folder.parent_id == parent.id)
    return query.first()


def sync_node_folders(db: Session, node: DirectionNode, *, commit: bool = True,
                      assign_papers: bool = True) -> Optional[Folder]:
    """将方向节点（含祖先）镜像成同名嵌套文件夹，返回该节点对应的文件夹。

    左侧文件夹树因此就是方向树的可视化体现；已存在的同名文件夹会被“认领”
    （补上 direction_node_id），不改变用户已有的文件夹结构。
    逐层认领：中间层只要本身也是方向节点，它的文件夹同样挂上 🧭，
    否则点父目录看不到该层的论文图谱。
    """
    existing = folder_of_node(db, node.id)
    if existing is not None:
        return existing
    parent = db.get(DirectionNode, node.parent_id) if node.parent_id else None
    parent_folder = sync_node_folders(db, parent, commit=False, assign_papers=assign_papers) if parent else None
    folder = _child_folder(db, parent_folder, node.name)
    if folder is None or folder.direction_node_id not in (None, node.id):
        folder = Folder(name=node.name, parent_id=parent_folder.id if parent_folder else None)
        db.add(folder)
    folder.direction_node_id = node.id
    db.flush()
    touched = {node.id}
    papers = db.query(Paper).filter(Paper.folder_id == folder.id).all() if assign_papers else []
    for paper in papers:
        member = db.query(PaperDirection).filter_by(paper_id=paper.id).first()
        if member is None:
            db.add(PaperDirection(paper_id=paper.id, node_id=node.id, role="primary"))
        elif member.node_id != node.id:
            touched.add(member.node_id)
            member.node_id, member.relations_at, member.relations_count = node.id, None, 0
            _clean_local_edges(db, paper.id, node.id)
    _recount_ids(db, touched)
    db.flush()
    if commit:
        db.commit()
    return folder


def move_paper_to_folder(db: Session, paper: Paper, folder: Optional[Folder], *,
                         source: str = "manual", commit: bool = True) -> None:
    """所有移动入口共用：方向目录更新唯一归属，普通目录清除自动归属。"""
    if folder is not None and db.get(Folder, folder.id) is None:
        raise ValueError("目标文件夹不存在")
    if folder is not None and folder.direction_node_id:
        set_paper_memberships(db, paper, [{"node_id": folder.direction_node_id,
                                          "reason": "", "confidence": 1}],
                              source=source, commit=commit)
        return
    old = paper_memberships(db, paper.id)
    old_ids = {d.node_id for d in old}
    for d in old:
        db.delete(d)
    paper.folder_id = folder.id if folder else None
    db.flush()
    _clean_local_edges(db, paper.id, None)
    _recount_ids(db, old_ids)
    if commit:
        db.commit()
    db.expire(paper, ["directions", "folder"])


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
    # 这次分类明确指定了目标时，论文跟过去，包括它原来放在用户文件夹里的情况。
    target = move_paper_node or (nodes[0] if nodes else None)
    if move_paper is not None and target is not None:
        folder = sync_node_folders(db, target)
        if move_paper.folder_id != folder.id:
            move_paper_to_folder(db, move_paper, folder, source="auto")
    return exported


# ---------------------------------------------------------------- 图谱 Markdown 落盘
KNOWLEDGE_DIR_NAME = "knowledge"


def knowledge_md_root() -> Path:
    """图谱 md 的根目录（默认 data/knowledge/，与 library.db 同层）。"""
    return Path(get_base_dir()) / KNOWLEDGE_DIR_NAME


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
    """节点与文件夹共用同一事务路径更新规则。"""
    folder = sync_node_folders(db, node)
    old_dir = _node_md_dir(node)
    try:
        update_folder_location(db, folder, new_name, folder.parent_id, commit=False)
        db.commit()
    except Exception:
        db.rollback()
        raise
    _relocate_md_dir(old_dir, _node_md_dir(node))
    for item in [node] + _descendant_nodes(db, node):
        export_node_graph_md(db, item)
    return node


def _archive_node_reports(db: Session, node: DirectionNode, folder: Folder) -> None:
    """目录删除前把每版旧报告原文及元信息转存到独立报告历史。"""
    history = synthesis_history(db, node.id, limit=None)
    records = [(s.content_md, s.created_at, {"legacy_synthesis_id": s.id, "paper_ids": s.paper_ids,
                "trigger": s.trigger, "stats": json.loads(s.stats_json or "{}"), "paper_count": s.paper_count}) for s in history]
    if node.synthesis_md and not any(text == node.synthesis_md for text, _, _ in records):
        records.append((node.synthesis_md, node.synthesis_at or _utcnow(), {"paper_count": node.synthesis_paper_count}))
    for text, created, meta in records:
        db.add(FolderReview(folder_id=folder.id, folder_path=node.path,
            title=node.path + " · 历史阶段综述", focus="", output_language="unknown",
            prompt_version="legacy", status="done", content_md=text,
            params_json=json.dumps({"legacy_node_id": node.id, **meta}, ensure_ascii=False),
            created_at=created, finished_at=created))


def delete_folder(db: Session, folder: Folder, *, keep_folder: bool = False) -> int:
    """事务上移子目录与论文，方向归属同步；保留 PDF、对话及全部报告版本。"""
    target = db.get(Folder, folder.parent_id) if folder.parent_id else None
    target_id = target.id if target else None
    children = db.query(Folder).filter_by(parent_id=folder.id).all()
    for child in children:
        if db.query(Folder).filter(Folder.parent_id == target_id, Folder.name == child.name,
                                   Folder.id != child.id).first():
            raise ValueError("上移后存在同名文件夹，请先重命名：" + child.name)
    try:
        node = db.get(DirectionNode, folder.direction_node_id) if folder.direction_node_id else None
        if node:
            _archive_node_reports(db, node, folder)
        folder.direction_node_id = None
        db.flush()
        for paper in db.query(Paper).filter_by(folder_id=folder.id).all():
            move_paper_to_folder(db, paper, folder if keep_folder else target, commit=False)
        affected = []
        for child in children:
            child.parent_id = target_id
            db.flush()
            affected += _sync_direction_paths(db, folder_subtree_ids(db, child.id))
        if node:
            db.query(PaperRelation).filter_by(node_id=node.id).delete(synchronize_session="fetch")
            # ORM 集合可能在前面的单分类替换前已加载，刷新后再级联清理。
            db.expire(node, ["children", "directions", "syntheses"])
            db.delete(node)
        if not keep_folder:
            db.expire(folder, ["children", "papers"])
            db.delete(folder)
        db.commit()
    except Exception:
        db.rollback()
        raise
    for node in affected:
        export_node_graph_md(db, node)
    return len(children)


def delete_node(db: Session, node: DirectionNode) -> int:
    """旧节点入口只解除方向定义，保留普通文件夹；子目录同步上移。"""
    folder = sync_node_folders(db, node)
    return delete_folder(db, folder, keep_folder=True)


def _direction_subtree_empty(db: Session, node: DirectionNode) -> bool:
    """这个方向和它的子方向都没有论文，并且下面没有用户自建文件夹。"""
    ids = subtree_node_ids(db, node.id)
    if db.query(PaperDirection).filter(PaperDirection.node_id.in_(ids)).count():
        return False
    folder = folder_of_node(db, node.id)
    if folder is None:
        return True
    for fid in folder_subtree_ids(db, folder.id):
        child = db.get(Folder, fid)
        if child is not None and child.id != folder.id and not child.direction_node_id:
            return False
    return True


def prune_empty_directions(db: Session) -> List[str]:
    """删掉重新分类后已经没有任何论文的方向。仍有子类论文的上级保留。"""
    removed: List[str] = []
    while True:
        target = None
        for node in (db.query(DirectionNode)
                     .order_by(DirectionNode.depth.desc(), DirectionNode.id.desc())
                     .all()):
            if _direction_subtree_empty(db, node):
                target = node
                break
        if target is None:
            break
        path = target.path
        parent_id = target.parent_id
        folder = folder_of_node(db, target.id)
        md = _node_md_path(target)
        if md.is_file():
            md.unlink()
            try:
                md.parent.rmdir()
            except OSError:
                pass
        if folder is None:
            db.query(PaperRelation).filter_by(node_id=target.id).delete(synchronize_session=False)
            db.delete(target)
            db.commit()
        else:
            delete_folder(db, folder, keep_folder=False)
        removed.append(path)
        parent = db.get(DirectionNode, parent_id) if parent_id else None
        if parent is not None:
            export_node_graph_md(db, parent)
    return removed


def repair_paper_folders(db: Session) -> int:
    """把已归属方向树的论文移到其 primary 方向文件夹下（升级/手工修复用）。

    返回移动的论文数。正常流程里单篇 pipeline 的 tree 步骤会自动落位，
    这里只用于处理"先有归属、后才有文件夹镜像"的历史数据。
    """
    moved = 0
    for direction in db.query(PaperDirection).order_by(PaperDirection.id).all():
        paper = direction.paper
        if (direction.role != "primary" or direction.node is None or paper is None
                or (paper.folder and not paper.folder.direction_node_id)):
            continue
        folder = sync_node_folders(db, direction.node)
        if folder is not None and direction.paper.folder_id != folder.id:
            direction.paper.folder_id = folder.id
            moved += 1
    if moved:
        db.commit()
    return moved


# ---------------------------------------------------------------- 长任务日志与状态
RUN_LOG_TAIL = 300


# 长任务的进度日志统一走这里：写满即截尾，字段口径与前端进度面板对齐。
def _append_run_log(db: Session, target: Any, message: str, *, level: str = "info",
                    arxiv_id: str = "", **structured: Any) -> None:
    from .progress import normalize
    logs = target.logs
    logs.append(normalize({"ts": _utcnow().strftime("%H:%M:%S"), "level": level,
                 "message": str(message)[:400], "arxiv_id": arxiv_id, **structured}))
    target.log_json = json.dumps(logs[-RUN_LOG_TAIL:], ensure_ascii=False)
    db.commit()


def _update_run_record(db: Session, run: Any, *, stage: Optional[str] = None,
                       status: Optional[str] = None, error: Optional[str] = None,
                       counters: Optional[Dict[str, Any]] = None,
                       finished: bool = False) -> Any:
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


# ---------------------------------------------------------------- 智能关联检索（Related Work）
RELATED_PENDING = "pending"
RELATED_DISCARDED = "discarded"
RELATED_PROMOTED = "promoted"


def create_related_run(db: Session, paper_id: int) -> RelatedRun:
    run = RelatedRun(paper_id=int(paper_id), status="running", stage="collect",
                     log_json="[]", counters_json="{}")
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def get_related_run(db: Session, run_id: int) -> Optional[RelatedRun]:
    return db.get(RelatedRun, run_id)


def latest_related_run(db: Session, paper_id: int) -> Optional[RelatedRun]:
    return (db.query(RelatedRun).filter(RelatedRun.paper_id == int(paper_id))
            .order_by(RelatedRun.id.desc()).first())


def find_running_related_run(db: Session, paper_id: int) -> Optional[RelatedRun]:
    return (db.query(RelatedRun)
            .filter(RelatedRun.paper_id == int(paper_id), RelatedRun.status == "running")
            .order_by(RelatedRun.id.desc()).first())


def log_related(db: Session, run: RelatedRun, message: str, *, level: str = "info",
                arxiv_id: str = "", **structured: Any) -> None:
    _append_run_log(db, run, message, level=level, arxiv_id=arxiv_id, **structured)


def update_related_run(db: Session, run: RelatedRun, *, stage: Optional[str] = None,
                       status: Optional[str] = None, error: Optional[str] = None,
                       counters: Optional[Dict[str, Any]] = None,
                       finished: bool = False) -> RelatedRun:
    return _update_run_record(db, run, stage=stage, status=status, error=error,
                              counters=counters, finished=finished)


def related_run_payload(run: Optional[RelatedRun], *, log_tail: int = 80) -> Optional[Dict[str, Any]]:
    if run is None:
        return None
    from .progress import normalize
    return {
        "id": run.id, "paper_id": run.paper_id, "status": run.status, "stage": run.stage,
        "counters": run.counters, "error": run.error,
        "cancel_requested": bool(run.cancel_requested),
        "started_at": _iso(run.started_at), "finished_at": _iso(run.finished_at),
        "logs": [normalize(row) for row in run.logs[-max(1, int(log_tail)):]],
                "stages_done": list({row.get("stage") for row in map(normalize, run.logs)
                                     if row.get("status") == "done" and row.get("stage")}),
        "log_total": len(run.logs),
    }


def upsert_related_candidate(db: Session, *, source_paper_id: int, hit: Any, seed_title: str,
                             run_id: Optional[int] = None) -> Tuple[RelatedCandidate, bool]:
    """按 (source_paper_id, arxiv_id) 写入/更新候选；返回 (row, 是否新建)。

    已判定（summary/verdict）的候选只刷新元信息与命中来源，不重置判定结果；
    match_score 取历史与本次的较大值（多个种子命中同一篇时用最高分）。
    """
    data = hit.as_dict() if hasattr(hit, "as_dict") else dict(hit)
    from app.arxiv_service import strip_version
    arxiv_id = strip_version(str(data.get("arxiv_id") or "").strip())
    row = (db.query(RelatedCandidate)
           .filter(RelatedCandidate.source_paper_id == int(source_paper_id),
                   RelatedCandidate.arxiv_id == arxiv_id).first())
    created = row is None
    if row is None:
        row = RelatedCandidate(source_paper_id=int(source_paper_id), arxiv_id=arxiv_id,
                               status=RELATED_PENDING)
        db.add(row)
    row.title = str(data.get("title") or "")[:1000]
    row.abstract = str(data.get("abstract") or "")
    row.authors_json = json.dumps(data.get("authors") or [], ensure_ascii=False)
    row.categories_json = json.dumps(data.get("categories") or [], ensure_ascii=False)
    row.published = data.get("published")
    row.seed_title = str(seed_title or "")[:1024]
    sources = json.loads(row.query_sources_json or "[]")
    for source in data.get("query_sources") or []:
        if source not in sources:
            sources.append(source)
    row.query_sources_json = json.dumps(sources, ensure_ascii=False)
    row.match_score = max(row.match_score or 0.0, float(data.get("match_score") or 0.0))
    if run_id:
        row.run_id = int(run_id)
    row.updated_at = _utcnow()
    db.commit()
    db.refresh(row)
    return row, created


def get_related_candidate(db: Session, candidate_id: int) -> Optional[RelatedCandidate]:
    return db.get(RelatedCandidate, candidate_id)


def list_related_candidates(db: Session, source_paper_id: int, *,
                            limit: int = 200) -> List[RelatedCandidate]:
    """候选列表。已丢弃的记录只用于去重，不再返回给界面。"""
    query = (db.query(RelatedCandidate)
             .filter(RelatedCandidate.source_paper_id == int(source_paper_id),
                     RelatedCandidate.status != RELATED_DISCARDED))
    return (query.order_by(RelatedCandidate.relevance_score.desc(),
                           RelatedCandidate.match_score.desc(),
                           RelatedCandidate.id.desc())
            .limit(max(1, int(limit))).all())


def set_related_summary(db: Session, row: RelatedCandidate, *, summary_text: str,
                        stats: Optional[Dict[str, Any]]) -> RelatedCandidate:
    row.summary_text = summary_text or ""
    row.summary_stats_json = json.dumps(stats or {}, ensure_ascii=False)
    row.updated_at = _utcnow()
    db.commit()
    return row


def set_related_verdict(db: Session, row: RelatedCandidate, *, relevance: str,
                        score: float, reason: str) -> RelatedCandidate:
    """四档判定与处理错误分离，不将材料或接口失败当作不相关。"""
    if relevance not in ("strong", "related", "uncertain", "unrelated"):
        raise ValueError("相关性枚举非法")
    row.relevance = relevance
    row.processing_error = ""
    try:
        row.relevance_score = max(0.0, min(1.0, float(score or 0.0)))
    except (TypeError, ValueError):
        row.relevance_score = 0.0
    row.relevance_reason = (reason or "")[:2000]
    if row.status != RELATED_PROMOTED:
        row.status = RELATED_DISCARDED if row.relevance == "unrelated" else RELATED_PENDING
    row.updated_at = _utcnow()
    db.commit()
    return row


def discard_related_candidate(db: Session, row: RelatedCandidate) -> RelatedCandidate:
    row.status = RELATED_DISCARDED
    row.updated_at = _utcnow()
    db.commit()
    return row


def delete_related_candidate(db: Session, row: RelatedCandidate) -> None:
    db.delete(row)
    db.commit()


def related_counts(db: Session, source_paper_id: int) -> Dict[str, int]:
    counts = {RELATED_PENDING: 0, RELATED_PROMOTED: 0, RELATED_DISCARDED: 0}
    for status, total in (db.query(RelatedCandidate.status, func.count(RelatedCandidate.id))
                          .filter(RelatedCandidate.source_paper_id == int(source_paper_id))
                          .group_by(RelatedCandidate.status).all()):
        counts[status] = int(total)
    for relevance, total in (db.query(RelatedCandidate.relevance, func.count(RelatedCandidate.id))
                             .filter(RelatedCandidate.source_paper_id == int(source_paper_id))
                             .group_by(RelatedCandidate.relevance).all()):
        counts[relevance or "unprocessed"] = int(total)
    counts["failed"] = db.query(RelatedCandidate).filter(
        RelatedCandidate.source_paper_id == int(source_paper_id),
        RelatedCandidate.processing_error != "").count()
    counts["total"] = sum(counts.get(k, 0)
                          for k in (RELATED_PENDING, RELATED_PROMOTED, RELATED_DISCARDED))
    return counts


def _library_paper_ids(db: Session, arxiv_ids: List[str]) -> Dict[str, int]:
    if not arxiv_ids:
        return {}
    return {arxiv_id: paper_id for paper_id, arxiv_id in
            db.query(Paper.id, Paper.arxiv_id).filter(Paper.arxiv_id.in_(arxiv_ids)).all()}


def _related_candidate_payload(row: RelatedCandidate, library_paper_id: Optional[int]) -> Dict[str, Any]:
    return {
        "id": row.id, "arxiv_id": row.arxiv_id, "title": row.title,
        "abstract": row.abstract, "authors": row.authors, "categories": row.categories,
        "published": row.published, "seed_title": row.seed_title,
        "match_score": row.match_score, "summary_text": row.summary_text,
        "relevance": row.relevance, "relevance_score": row.relevance_score,
        "relevance_reason": row.relevance_reason, "status": row.status,
        "output_language": row.output_language,
        "summary_language": (row.stats or {}).get("output_language", "unknown"),
        "query_sources": json.loads(row.query_sources_json or "[]"),
        "processing_error": row.processing_error,
        "retryable": bool(row.processing_error or row.relevance == "uncertain"),
        "promote_state": row.promote_state, "promote_error": row.promote_error,
        "promoted_paper_id": row.promoted_paper_id,
        "library_paper_id": library_paper_id,
        "in_library": library_paper_id is not None,
        "created_at": _iso(row.created_at), "updated_at": _iso(row.updated_at),
    }


def related_candidates_payload(db: Session, source_paper_id: int) -> List[Dict[str, Any]]:
    """待筛选与已入库的候选（不含已丢弃）+ 文献库中的论文 id。"""
    rows = list_related_candidates(db, source_paper_id)
    library = _library_paper_ids(db, [row.arxiv_id for row in rows])
    return [_related_candidate_payload(row, library.get(row.arxiv_id)) for row in rows]


def related_candidate_payload_single(db: Session, row: RelatedCandidate) -> Dict[str, Any]:
    """单条候选 payload（前端轮询入库状态时用）。"""
    library = _library_paper_ids(db, [row.arxiv_id])
    return _related_candidate_payload(row, library.get(row.arxiv_id))
