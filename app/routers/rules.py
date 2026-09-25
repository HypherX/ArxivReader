"""归档规则路由：CRUD。

规则按 priority 升序匹配（越小越先），命中即把新入库论文放入 folder_id。
"""

from typing import List

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from .. import database, schemas
from ..models import FilingRule, Folder

router = APIRouter(prefix="/api/rules", tags=["rules"])


def _check_folder(db: Session, folder_id):
    if folder_id is not None and db.get(Folder, folder_id) is None:
        raise HTTPException(status_code=404, detail="目标文件夹不存在")


@router.get("", response_model=List[schemas.RuleOut])
def list_rules(db: Session = Depends(database.get_db)):
    return (db.query(FilingRule)
            .order_by(FilingRule.priority, FilingRule.id)
            .all())


@router.post("", response_model=schemas.RuleOut, status_code=201)
def create_rule(body: schemas.RuleCreate, db: Session = Depends(database.get_db)):
    _check_folder(db, body.folder_id)
    rule = FilingRule(
        name=body.name or "",
        match_type=body.match_type,
        pattern=body.pattern.strip(),
        folder_id=body.folder_id,
        priority=body.priority,
        enabled=body.enabled,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


@router.patch("/{rule_id}", response_model=schemas.RuleOut)
def update_rule(rule_id: int, body: schemas.RuleUpdate,
                db: Session = Depends(database.get_db)):
    rule = db.get(FilingRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")

    fields = body.model_fields_set
    if "folder_id" in fields:
        _check_folder(db, body.folder_id)
        rule.folder_id = body.folder_id
    if "name" in fields and body.name is not None:
        rule.name = body.name
    if "match_type" in fields and body.match_type is not None:
        rule.match_type = body.match_type
    if "pattern" in fields and body.pattern is not None:
        rule.pattern = body.pattern.strip()
    if "priority" in fields and body.priority is not None:
        rule.priority = body.priority
    if "enabled" in fields and body.enabled is not None:
        rule.enabled = body.enabled

    db.commit()
    db.refresh(rule)
    return rule


@router.delete("/{rule_id}", status_code=204)
def delete_rule(rule_id: int, db: Session = Depends(database.get_db)):
    rule = db.get(FilingRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    db.delete(rule)
    db.commit()
    return Response(status_code=204)
