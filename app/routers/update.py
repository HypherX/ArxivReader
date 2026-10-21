"""应用自更新：检查最新 Release、下载进度、应用更新与"稍后提醒"。

错误约定：可预期失败统一 400 + {"code", "params"}，前端按 i18n code 出文案；
网络类失败在 check 里不抛错，以 {"ok": false, "error": code} 返回。
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import update_service

router = APIRouter(prefix="/api/update", tags=["update"])


class DismissBody(BaseModel):
    version: str = Field(default="", max_length=40)


class PrefsBody(BaseModel):
    auto: bool = True


@router.get("/check")
def check(force: bool = False):
    """检查最新版本（6 小时缓存；force 强制刷新）。网络失败返回 ok=false。"""
    return update_service.check_status(force=force)


@router.post("/preferences")
def preferences(body: PrefsBody):
    """保存"启动时自动检查更新"开关。"""
    update_service.set_auto(body.auto)
    return {"ok": True, "auto": update_service.get_auto()}


@router.get("/preferences")
def get_preferences():
    """启动时先读本开关再决定是否发起联网检查，关掉就完全不请求。"""
    return {"auto": update_service.get_auto()}


@router.post("/dismiss")
def dismiss(body: DismissBody):
    """"稍后提醒"：记住已忽略的版号，同一版本不再弹横幅。"""
    update_service.dismiss(body.version)
    return {"ok": True}


@router.post("/download")
def download():
    """开始（或幂等返回进行中的）安装包下载任务。资产信息取自最新检查结果。"""
    status = update_service.check_status()
    if not status.get("ok"):
        raise HTTPException(status_code=400, detail={"code": "update.check_failed", "params": {}})
    if not status.get("has_update") or not status.get("asset"):
        raise HTTPException(status_code=400, detail={"code": "update.no_asset", "params": {}})
    try:
        return update_service.start_download(status["asset"], status.get("sha256_asset"))
    except update_service.UpdateError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "params": exc.params})


@router.get("/download")
def download_progress():
    """轮询下载进度；从未下载过时 state 为 idle。"""
    task = update_service.download_status()
    return task or {"state": "idle", "downloaded": 0, "size": 0, "error": "", "path": ""}


@router.post("/apply")
def apply():
    """启动换包脚本（仅 macOS 打包形态）。脚本等应用退出后换包并自动重开。"""
    try:
        return update_service.start_apply()
    except update_service.UpdateError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "params": exc.params})
