"""应用自更新：检查 GitHub 最新 Release、后台下载安装包、macOS 下换包重启。

设计要点：
  - 只读公共 Release 信息，不需要 token；检查走 api.github.com（github.com 主域
    在部分网络下不可达），下载优先走 API 资产地址（302 到 release-assets CDN，
    同样可通），浏览器直链作为兜底。
  - 下载在后台线程进行，前端轮询进度；完成后校验官方 .sha256 再允许应用。
  - 应用更新（仅 macOS 打包形态）：生成一个换包脚本，等桌面主进程退出后
    用 ditto 解包、原子替换 .app、去 quarantine、重新打开。开发模式与非
    macOS 平台只提示手动下载，不做任何替换。
  - “稍后提醒”按版本号记忆（Setting: update.dismissed），同一版本不再打扰。
"""

import hashlib
import json
import logging
import os
import platform
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

from harness import config

from . import database
from .models import Setting
from .version import VERSION

logger = logging.getLogger(__name__)

REPO = "HypherX/ArxivReader"
API = "https://api.github.com/repos/" + REPO
_UA = {"User-Agent": "ArxivReader-updater", "Accept": "application/vnd.github+json"}
_CHECK_TTL = 6 * 3600          # 检查结果缓存 6 小时；手动检查强制刷新
_MAX_NOTES = 4000              # 发给前端的更新说明长度上限

_LOCK = threading.Lock()
_CACHE = {"at": 0.0, "result": None}
_TASK = None                   # 全局只有一个下载任务


class UpdateError(ValueError):
    """可预期的更新失败（网络、无匹配资产、非打包形态等），code 走前端 i18n。"""

    def __init__(self, code, params=None):
        super().__init__(code)
        self.code = code
        self.params = params or {}


def parse_version(text):
    """'v1.2.3-beta' -> (1, 2, 3)；无法解析时返回 None。"""
    if not text:
        return None
    match = re.match(r"^v?(\d+(?:\.\d+){0,3})", str(text).strip())
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def has_update(latest, current=VERSION):
    new, old = parse_version(latest), parse_version(current)
    if new is None or old is None:
        return False
    length = max(len(new), len(old))
    return new + (0,) * (length - len(new)) > old + (0,) * (length - len(old))


def _asset_for_platform(assets):
    """按当前平台挑安装包与校验文件。命名约定：ArxivReader-<ver>-macOS-arm64.zip。"""
    os_name = "macOS" if sys.platform == "darwin" else "Windows"
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    wanted, digest = None, None
    for asset in assets or []:
        name = asset.get("name") or ""
        if not name.startswith("ArxivReader-"):
            continue
        if name.endswith(".sha256") and os_name in name and arch in name:
            digest = asset
        elif name.endswith(".zip") and os_name in name and arch in name:
            wanted = asset
    return wanted, digest


def _fetch_json(url, timeout=20):
    request = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def check_latest(force=False):
    """查询最新 Release。网络失败不抛错，返回 error 字段由前端提示。"""
    now = time.time()
    with _LOCK:
        if not force and _CACHE["result"] is not None and now - _CACHE["at"] < _CHECK_TTL:
            return dict(_CACHE["result"])
    try:
        release = _fetch_json(API + "/releases/latest")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.info("检查更新失败：%s", str(exc)[:200])
        return {"ok": False, "error": "update.check_failed", "current": VERSION}

    tag = release.get("tag_name") or ""
    asset, digest = _asset_for_platform(release.get("assets"))
    result = {
        "ok": True,
        "current": VERSION,
        "latest": tag.lstrip("v"),
        "tag": tag,
        "name": release.get("name") or tag,
        "notes": (release.get("body") or "")[:_MAX_NOTES],
        "page_url": release.get("html_url") or "",
        "published_at": release.get("published_at") or "",
        "has_update": has_update(tag),
        "asset": _asset_payload(asset),
        "sha256_asset": _asset_payload(digest),
        "frozen": bool(getattr(sys, "frozen", False)),
        "platform": sys.platform,
        "checked_at": now,
    }
    with _LOCK:
        _CACHE.update(at=now, result=result)
    return dict(result)


def _asset_payload(asset):
    if not asset:
        return None
    return {"id": asset.get("id"), "name": asset.get("name"), "size": asset.get("size") or 0,
            "api_url": asset.get("url"), "browser_url": asset.get("browser_download_url") or ""}


def _dismissed_version():
    try:
        with database.SessionLocal() as db:
            row = db.get(Setting, "update.dismissed")
            return row.value if row else ""
    except Exception:  # noqa: BLE001  读取失败视为未忽略，宁可多提示一次
        return ""


def get_auto():
    """启动时是否自动检查更新（默认开）。"""
    try:
        with database.SessionLocal() as db:
            row = db.get(Setting, "update.auto")
            return row.value != "0" if row else True
    except Exception:  # noqa: BLE001
        return True


def set_auto(enabled):
    with database.SessionLocal() as db:
        row = db.get(Setting, "update.auto")
        value = "1" if enabled else "0"
        if row is None:
            db.add(Setting(key="update.auto", value=value))
        else:
            row.value = value
        db.commit()


def check_status(force=False):
    """给前端的完整状态：检查结果 + 忽略标记 + 自动检查开关 + 下载任务。"""
    result = check_latest(force=force)
    result["dismissed"] = bool(result.get("latest")) and result["latest"] == _dismissed_version()
    result["auto"] = get_auto()
    task = download_status()
    if task:
        result["download"] = task
    return result


def dismiss(version):
    text = str(version or "").strip()[:40]
    with database.SessionLocal() as db:
        row = db.get(Setting, "update.dismissed")
        if row is None:
            db.add(Setting(key="update.dismissed", value=text))
        else:
            row.value = text
        db.commit()


# ---------------------------------------------------------------- 下载

def download_status():
    with _LOCK:
        return dict(_TASK) if _TASK else None


def _updates_dir():
    path = Path(config.get_base_dir()) / "updates"
    path.mkdir(parents=True, exist_ok=True)
    return path


def start_download(asset, sha256_asset):
    """启动（或幂等返回）安装包下载任务。asset/sha256_asset 用 check 返回的载荷。"""
    if not asset or not asset.get("api_url"):
        raise UpdateError("update.no_asset")
    with _LOCK:
        global _TASK
        if _TASK and _TASK.get("state") == "running":
            return dict(_TASK)
        _TASK = {"state": "running", "name": asset["name"], "size": asset.get("size") or 0,
                 "downloaded": 0, "error": "", "path": "", "started_at": time.time()}
    threading.Thread(target=_download, args=(asset, sha256_asset), daemon=True,
                     name="update-download").start()
    return download_status()


def _stream_download(url, target, task, extra_headers=None):
    """流式下载到 target；实时更新 task 进度。跟随重定向（API 资产 302 到 CDN）。"""
    headers = dict(_UA)
    if extra_headers:
        headers.update(extra_headers)
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        total = int(response.headers.get("Content-Length") or 0)
        if total:
            task["size"] = total
        with open(target, "wb") as out:
            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                out.write(chunk)
                task["downloaded"] += len(chunk)


def _download(asset, sha256_asset):
    task = _TASK
    try:
        target = _updates_dir() / asset["name"]
        partial = target.with_suffix(target.suffix + ".part")
        try:
            # API 资产地址优先：302 到 release-assets CDN，github.com 不可达时也能下
            _stream_download(asset["api_url"], partial, task,
                             {"Accept": "application/octet-stream"})
        except (urllib.error.URLError, OSError) as exc:
            if not asset.get("browser_url"):
                raise
            logger.info("API 资产下载失败，改用浏览器直链：%s", str(exc)[:160])
            task["downloaded"] = 0
            _stream_download(asset["browser_url"], partial, task)

        expected = _expected_sha256(sha256_asset)
        if expected:
            digest = hashlib.sha256()
            with open(partial, "rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != expected:
                partial.unlink(missing_ok=True)
                raise UpdateError("update.bad_sha256")
        os.replace(partial, target)
        task.update(state="done", path=str(target))
        logger.info("更新包下载完成：%s", target)
    except UpdateError as exc:
        task.update(state="failed", error=exc.code)
    except Exception as exc:  # noqa: BLE001  网络/磁盘错误统一落进任务状态
        logger.warning("更新包下载失败：%s", str(exc)[:300])
        task.update(state="failed", error="update.download_failed")


def _expected_sha256(sha256_asset):
    """官方 .sha256 文件内容：'<hex>  <文件名>'。拿不到则不校验（返回 None）。"""
    if not sha256_asset or not sha256_asset.get("api_url"):
        return None
    try:
        request = urllib.request.Request(sha256_asset["api_url"],
                                         headers={**_UA, "Accept": "application/octet-stream"})
        with urllib.request.urlopen(request, timeout=30) as response:
            text = response.read().decode("utf-8", "replace")
        match = re.match(r"^([0-9a-f]{64})", text.strip())
        return match.group(1) if match else None
    except (urllib.error.URLError, OSError):
        return None


# ---------------------------------------------------------------- 应用更新（macOS 打包形态）

def apply_ready():
    """下载完成且形态支持时返回换包所需信息；否则抛 UpdateError。"""
    task = download_status()
    if not task or task.get("state") != "done" or not task.get("path"):
        raise UpdateError("update.not_downloaded")
    if not getattr(sys, "frozen", False):
        raise UpdateError("update.dev_mode")
    if sys.platform != "darwin":
        raise UpdateError("update.manual_only")
    executable = Path(sys.executable).resolve()
    bundle = executable.parents[2]          # …/ArxivReader.app/Contents/MacOS/ArxivReader
    if bundle.suffix != ".app" or not (bundle / "Contents" / "Info.plist").is_file():
        raise UpdateError("update.no_bundle")
    return {"zip": task["path"], "app": str(bundle), "host_pid": os.getppid()}


def start_apply():
    """生成换包脚本并分离启动；脚本等桌面主进程退出后换包并重新打开。"""
    info = apply_ready()
    work = Path(tempfile.mkdtemp(prefix="arxivreader-update-"))
    script = work / "apply.sh"
    script.write_text(_APPLY_SCRIPT, encoding="utf-8")
    log = _updates_dir() / "apply.log"
    with open(log, "ab") as logfile:
        subprocess.Popen(["/bin/bash", str(script), str(info["host_pid"]), info["app"],
                          info["zip"], str(work)],
                         stdin=subprocess.DEVNULL, stdout=logfile, stderr=subprocess.STDOUT,
                         start_new_session=True, close_fds=True)
    logger.info("换包脚本已启动：等主进程 %s 退出后替换 %s", info["host_pid"], info["app"])
    return {"ok": True, "log": str(log)}


_APPLY_SCRIPT = """#!/bin/bash
# ArxivReader 自更新换包：$1=主进程PID $2=.app路径 $3=安装包zip $4=工作目录
set -u
PID="$1"; APP="$2"; ZIP="$3"; WORK="$4"
echo "[$(date '+%H:%M:%S')] 等待主进程 $PID 退出后替换 $APP"
for _ in $(seq 1 200); do
  kill -0 "$PID" 2>/dev/null || break
  sleep 0.25
done
# v1.1.3 及更早版本的主进程退出时会卡在 Py_FinalizeEx（等待常驻线程），永远退不完；
# 等不到就主动终结。quit_app 流程已先销毁窗口并关闭后端，残留的只是一个卡在
# 解释器退出阶段的空壳，终结它不会丢数据。
if kill -0 "$PID" 2>/dev/null; then
  echo "[$(date '+%H:%M:%S')] 主进程 50s 未退出，发送 TERM（含残留后端子进程）"
  pkill -TERM -P "$PID" 2>/dev/null
  kill -TERM "$PID" 2>/dev/null
  sleep 3
fi
if kill -0 "$PID" 2>/dev/null; then
  echo "[$(date '+%H:%M:%S')] 主进程仍未退出，发送 KILL"
  kill -KILL "$PID" 2>/dev/null
  sleep 1
fi
kill -0 "$PID" 2>/dev/null && { echo "主进程无法终结，放弃换包"; exit 1; }
echo "[$(date '+%H:%M:%S')] 解包 $ZIP"
EXTRACT="$WORK/extract"
rm -rf "$EXTRACT"; mkdir -p "$EXTRACT"
/usr/bin/ditto -x -k "$ZIP" "$EXTRACT" || { echo "ditto 解包失败"; exit 1; }
NEWAPP="$EXTRACT/ArxivReader.app"
[ -d "$NEWAPP" ] || { echo "包内没有 ArxivReader.app"; exit 1; }
BACKUP="$WORK/ArxivReader-old.app"
rm -rf "$BACKUP"
mv "$APP" "$BACKUP" || { echo "旧版本移出失败"; exit 1; }
if mv "$NEWAPP" "$APP"; then
  /usr/bin/xattr -dr com.apple.quarantine "$APP" 2>/dev/null
  echo "[$(date '+%H:%M:%S')] 换包完成，重新打开 $APP"
  /usr/bin/open "$APP"
  rm -f "$ZIP"
else
  mv "$BACKUP" "$APP"                          # 换包失败，回滚旧版本
  echo "新版本落位失败，已回滚旧版本"
  exit 1
fi
"""
