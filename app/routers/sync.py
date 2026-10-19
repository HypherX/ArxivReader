"""云同步接口。令牌、客户端密钥和应用密码只留在本机，响应里不回传。"""

from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from ..sync import service
from ..sync.errors import SyncError

router = APIRouter(prefix="/api/sync", tags=["sync"])


def _http(exc: SyncError):
    raise HTTPException(exc.status, detail={"code": exc.code, "params": exc.params})


@router.get("")
def get_status():
    return service.status()


@router.put("/client")
def save_client(body: Dict[str, Any]):
    try:
        service.save_client(body.get("client_id"), body.get("client_secret"))
    except SyncError as exc:
        _http(exc)
    return service.status()


@router.put("/webdav")
def save_webdav(body: Dict[str, Any]):
    try:
        service.save_webdav_config(body.get("url"), body.get("username"),
                                   body.get("password"), body.get("base_path"))
    except SyncError as exc:
        _http(exc)
    return service.status()


@router.post("/connect")
def connect():
    try:
        service.start_connect()
    except SyncError as exc:
        _http(exc)
    return service.status()


@router.post("/disconnect")
def disconnect():
    try:
        service.disconnect()
    except SyncError as exc:
        _http(exc)
    return service.status()


@router.post("/cloud")
def refresh_cloud():
    try:
        service.refresh_cloud()
    except SyncError:
        pass
    return service.status()


@router.post("/push")
def push():
    try:
        service.start_push()
    except SyncError as exc:
        _http(exc)
    return service.status()


@router.post("/pull")
def pull(body: Dict[str, Any]):
    if body.get("confirm") is not True:
        raise HTTPException(400, detail={"code": "sync.confirm_required", "params": {}})
    try:
        service.start_pull()
    except SyncError as exc:
        _http(exc)
    return service.status()


@router.post("/cancel")
def cancel():
    return service.cancel()
