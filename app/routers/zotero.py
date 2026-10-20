"""Zotero 个人文库导入。API Key 只留在本机，响应里不回传。"""

from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from ..zotero_sync import ZoteroError, forget_key, save_key, start_sync, status

router = APIRouter(prefix="/api/zotero", tags=["zotero"])


def _http(exc: ZoteroError):
    raise HTTPException(exc.status, detail={"code": exc.code, "params": exc.params})


@router.get("")
def get_status():
    return status()


@router.put("/key")
def put_key(body: Dict[str, Any]):
    try:
        save_key(body.get("api_key"))
    except ZoteroError as exc:
        _http(exc)
    return status()


@router.delete("/key")
def delete_key():
    try:
        forget_key()
    except ZoteroError as exc:
        _http(exc)
    return status()


@router.post("/sync")
def sync():
    try:
        start_sync()
    except ZoteroError as exc:
        _http(exc)
    return status()
