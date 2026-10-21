"""文件夹路由：树形展示与增删改。

删除文件夹时，其子文件夹与论文上移到父级；顶层文件夹删除后论文回到"全部论文"。
不做级联删除，避免误删论文。

方向文件夹通过稳定节点 ID 关联分类；移动、改名、删除统一维护子树路径和唯一归属。
"""

from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from knowledge import store as knowledge_store, translation

from .. import database, schemas, settings_store
from .. import folder_classify
from ..models import DirectionNode, Folder, Paper

router = APIRouter(prefix="/api/folders", tags=["folders"])


def _paper_counts(db: Session) -> Dict[int, int]:
    rows = db.query(Paper.folder_id, func.count(Paper.id)).group_by(Paper.folder_id).all()
    counts: Dict[int, int] = {}
    for folder_id, cnt in rows:
        if folder_id is not None:
            counts[folder_id] = cnt
    return counts


def _build_tree(folders: List[Folder], counts: Dict[int, int], aliases=None) -> List[schemas.FolderNode]:
    nodes = {
        f.id: schemas.FolderNode(
            id=f.id, name=f.name, parent_id=f.parent_id,
            paper_count=counts.get(f.id, 0), direction_node_id=f.direction_node_id,
            children=[],
        )
        for f in folders
    }
    roots: List[schemas.FolderNode] = []
    for f in folders:
        node = nodes[f.id]
        if f.parent_id is not None and f.parent_id in nodes:
            nodes[f.parent_id].children.append(node)
        else:
            roots.append(node)

    def sort_rec(items: List[schemas.FolderNode], prefix: str = "") -> None:
        items.sort(key=lambda n: n.name.lower())
        for n in items:
            n.path = (prefix + "/" if prefix else "") + n.name
            n.display_name = (aliases or {}).get(n.direction_node_id, n.name)
            sort_rec(n.children, n.path)
            n.subtree_paper_count = n.paper_count + sum(c.subtree_paper_count for c in n.children)

    sort_rec(roots)
    return roots


def _is_descendant(db: Session, node_id: int, ancestor_id: int) -> bool:
    """node_id 是否为 ancestor_id 的后代（用于防止移动成环）。"""
    current = node_id
    seen = set()
    while current is not None and current not in seen:
        if current == ancestor_id:
            return True
        seen.add(current)
        folder = db.get(Folder, current)
        current = folder.parent_id if folder else None
    return False


@router.get("", response_model=List[schemas.FolderNode])
def list_folders(db: Session = Depends(database.get_db)):
    folders = db.query(Folder).all()
    aliases = translation.node_aliases(db, settings_store.get_output_language(db))
    return _build_tree(folders, _paper_counts(db), aliases)


@router.post("", response_model=schemas.FolderOut, status_code=201)
def create_folder(body: schemas.FolderCreate, db: Session = Depends(database.get_db)):
    name = body.name.strip()
    if not name or "/" in name or name in (".", ".."):
        raise HTTPException(status_code=400, detail="文件夹名不能为空或包含路径分隔符")
    if db.query(Folder).filter_by(name=name, parent_id=body.parent_id).first():
        raise HTTPException(status_code=409, detail="同级已存在同名文件夹")
    if body.parent_id is not None and db.get(Folder, body.parent_id) is None:
        raise HTTPException(status_code=404, detail="父文件夹不存在")
    folder = Folder(name=name, parent_id=body.parent_id)
    db.add(folder)
    db.commit()
    db.refresh(folder)
    return folder


@router.patch("/{folder_id}", response_model=schemas.FolderOut)
def update_folder(folder_id: int, body: schemas.FolderUpdate,
                  db: Session = Depends(database.get_db)):
    folder = db.get(Folder, folder_id)
    if folder is None:
        raise HTTPException(status_code=404, detail="文件夹不存在")

    fields = body.model_fields_set
    name = body.name if "name" in fields and body.name is not None else folder.name
    parent_id = body.parent_id if "parent_id" in fields else folder.parent_id
    try:
        knowledge_store.update_folder_location(db, folder, name, parent_id)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    db.refresh(folder)
    return folder


@router.post("/{folder_id}/auto-classify")
def start_auto_classify(folder_id: int, db: Session = Depends(database.get_db)):
    """一键自动分小类：后台线程跑一次 LLM 分组并移动当前层论文，返回任务状态供轮询。"""
    try:
        return folder_classify.start_task(folder_id)
    except folder_classify.ClassifyError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "params": exc.params})


@router.get("/{folder_id}/auto-classify")
def get_auto_classify(folder_id: int):
    """轮询自动分小类任务状态；从未运行过时 state 为 idle。"""
    task = folder_classify.get_task(folder_id)
    if task is None:
        return {"folder_id": folder_id, "state": "idle", "total": 0, "error": "",
                "error_code": "", "error_params": {}, "groups": [], "moved": 0, "skipped": 0}
    return task


@router.delete("/{folder_id}", status_code=204)
def delete_folder(folder_id: int, db: Session = Depends(database.get_db)):
    folder = db.get(Folder, folder_id)
    if folder is None:
        raise HTTPException(status_code=404, detail="文件夹不存在")
    try:
        knowledge_store.delete_folder(db, folder)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc))
    return Response(status_code=204)
