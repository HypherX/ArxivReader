"""Zotero 导入。

本机库（读 zotero.sqlite，不改原库）：
  POST /api/zotero/scan    {path?} → 库统计（不导入）
  POST /api/zotero/import  {path?, use_collections} → 启动后台导入，返回任务视图
  GET  /api/zotero/job     → 当前任务状态（含结果统计）
  POST /api/zotero/cancel  → 请求取消

在线个人文库（API Key 只留在本机，响应里不回传）：
  GET    /api/zotero           → 连接状态与同步进度
  PUT    /api/zotero/key       → 保存密钥
  DELETE /api/zotero/key       → 清除密钥
  POST   /api/zotero/sync      → 按文库版本增量同步
"""

import logging
import threading
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import database, zotero_import
from ..zotero_sync import ZoteroError, forget_key, save_key, start_sync, status

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/zotero", tags=["zotero"])

_LOCK = threading.Lock()
_JOB = None
_JOB_SEQ = 0


class ScanBody(BaseModel):
    path: str = ""


class ImportBody(BaseModel):
    path: str = ""
    use_collections: bool = True


def _raise(exc):
    status = 409 if exc.code == "zotero.running" else 400
    raise HTTPException(status_code=status,
                        detail={"code": exc.code, "params": exc.params})


def _job_view(job):
    if job is None:
        return None
    return {k: v for k, v in job.items() if k != "cancel"}


def public_job():
    with _LOCK:
        return _job_view(_JOB)


@router.get("/default-path")
def get_default_path():
    import os
    path = zotero_import.default_dir()
    return {"path": path, "exists": os.path.isfile(os.path.join(path, "zotero.sqlite"))}


@router.post("/scan")
def scan(body: ScanBody, db: Session = Depends(database.get_db)):
    try:
        result = zotero_import.scan(body.path)
        known = zotero_import.count_existing(db, result["papers"])
    except zotero_import.ZoteroError as exc:
        _raise(exc)
    stats = dict(result["stats"])
    stats["already_in_library"] = known
    # 条目明细只在导入时用；扫描接口只回统计与前几条样例标题
    sample = [p["title"] for p in result["papers"][:5]]
    return {"path": result["path"], "stats": stats, "sample": sample}


@router.post("/import")
def start_import(body: ImportBody, db: Session = Depends(database.get_db)):
    global _JOB, _JOB_SEQ
    try:
        result = zotero_import.scan(body.path)          # 导入前重扫，拿到最新条目
    except zotero_import.ZoteroError as exc:
        _raise(exc)
    if not result["papers"]:
        raise HTTPException(status_code=400,
                            detail={"code": "zotero.no_papers", "params": {}})

    with _LOCK:
        if _JOB and _JOB["running"]:
            raise HTTPException(status_code=409,
                                detail={"code": "zotero.running", "params": {}})
        _JOB_SEQ += 1
        job = {"id": _JOB_SEQ, "running": True, "phase": "scan", "done": 0,
               "total": len(result["papers"]), "current": "", "ok": False,
               "error": "", "finished": False, "result": None,
               "cancel": threading.Event()}
        _JOB = job
        view = _job_view(job)

    def progress(phase, done, total, current):
        with _LOCK:
            job["phase"] = phase
            job["done"] = done
            job["total"] = total
            job["current"] = current or ""

    def runner():
        session = database.SessionLocal()
        try:
            stats = zotero_import.import_library(
                session, result["papers"], result["path"],
                body.use_collections, progress, job["cancel"])
            with _LOCK:
                job["ok"] = True
                job["result"] = stats
        except zotero_import.ZoteroError as exc:
            with _LOCK:
                job["error"] = exc.code
        except Exception as exc:  # noqa: BLE001
            logger.exception("Zotero 导入失败")
            with _LOCK:
                job["error"] = "zotero.failed"
                job["result"] = {"message": str(exc)[:200]}
        finally:
            session.close()
            with _LOCK:
                job["running"] = False
                job["finished"] = True

    threading.Thread(target=runner, name="arxiv-zotero-import", daemon=True).start()
    return view


@router.get("/job")
def get_job():
    return {"job": public_job()}


@router.post("/cancel")
def cancel():
    with _LOCK:
        if _JOB and _JOB["running"]:
            _JOB["cancel"].set()
    return {"job": public_job()}


def _sync_http(exc: ZoteroError):
    raise HTTPException(exc.status, detail={"code": exc.code, "params": exc.params})


@router.get("")
def get_sync_status():
    return status()


@router.put("/key")
def put_key(body: Dict[str, Any]):
    try:
        save_key(body.get("api_key"))
    except ZoteroError as exc:
        _sync_http(exc)
    return status()


@router.delete("/key")
def delete_key():
    try:
        forget_key()
    except ZoteroError as exc:
        _sync_http(exc)
    return status()


@router.post("/sync")
def sync_library():
    try:
        start_sync()
    except ZoteroError as exc:
        _sync_http(exc)
    return status()
