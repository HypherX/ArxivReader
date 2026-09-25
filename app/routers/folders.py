"""文件夹路由：树形展示与增删改。

删除文件夹时，其子文件夹与论文上移到父级（顶层则移入 Inbox），不做级联删除，
避免误删论文。Inbox 为系统默认文件夹，禁止删除。
"""

from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import database, schemas
from ..models import Folder, Paper

router = APIRouter(prefix="/api/folders", tags=["folders"])


def _paper_counts(db: Session) -> Dict[int, int]:
    rows = db.query(Paper.folder_id, func.count(Paper.id)).group_by(Paper.folder_id).all()
    counts: Dict[int, int] = {}
    for folder_id, cnt in rows:
        if folder_id is not None:
            counts[folder_id] = cnt
    return counts


def _build_tree(folders: List[Folder], counts: Dict[int, int]) -> List[schemas.FolderNode]:
    nodes = {
        f.id: schemas.FolderNode(
            id=f.id, name=f.name, parent_id=f.parent_id,
            paper_count=counts.get(f.id, 0), children=[],
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

    def sort_rec(items: List[schemas.FolderNode]) -> None:
        items.sort(key=lambda n: n.name.lower())
        for n in items:
            sort_rec(n.children)

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
    return _build_tree(folders, _paper_counts(db))


@router.post("", response_model=schemas.FolderOut, status_code=201)
def create_folder(body: schemas.FolderCreate, db: Session = Depends(database.get_db)):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="文件夹名不能为空")
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
    if "name" in fields and body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="文件夹名不能为空")
        folder.name = name
    if "parent_id" in fields:
        new_parent = body.parent_id
        if new_parent == folder_id:
            raise HTTPException(status_code=400, detail="不能把文件夹设为自己的子文件夹")
        if new_parent is not None:
            if db.get(Folder, new_parent) is None:
                raise HTTPException(status_code=404, detail="父文件夹不存在")
            if _is_descendant(db, new_parent, folder_id):
                raise HTTPException(status_code=400, detail="不能移动到自身的子文件夹下")
        folder.parent_id = new_parent

    db.commit()
    db.refresh(folder)
    return folder


@router.delete("/{folder_id}", status_code=204)
def delete_folder(folder_id: int, db: Session = Depends(database.get_db)):
    folder = db.get(Folder, folder_id)
    if folder is None:
        raise HTTPException(status_code=404, detail="文件夹不存在")
    if folder.name == database.INBOX_NAME and folder.parent_id is None:
        raise HTTPException(status_code=400, detail="Inbox 为系统默认文件夹，不能删除")

    new_parent_id = folder.parent_id
    if new_parent_id is None:
        inbox = database.ensure_inbox(db)
        new_parent_id = inbox.id if inbox.id != folder_id else None

    db.query(Folder).filter(Folder.parent_id == folder_id).update(
        {"parent_id": new_parent_id}, synchronize_session=False)
    db.query(Paper).filter(Paper.folder_id == folder_id).update(
        {"folder_id": new_parent_id}, synchronize_session=False)
    db.delete(folder)
    db.commit()
    return Response(status_code=204)
