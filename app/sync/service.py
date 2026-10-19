"""同步任务：同一时间只跑一个，界面轮询进度。

后端二选一（sync/provider 文件记录）：Google Drive（OAuth）或 WebDAV（坚果云等，
账号 + 应用密码）。两家的凭据各自独立保存，保存哪家的配置就把哪家设为当前后端。
"""

import json
import logging
import os
import re
import threading

from harness import config

from . import credentials, google_drive, inventory, webdav
from .errors import SyncError, public_message

logger = logging.getLogger(__name__)

CLIENT_RE = re.compile(r"^[A-Za-z0-9_-]{10,}\.apps\.googleusercontent\.com$")
_LOCK = threading.Lock()
_JOB = None
_JOB_SEQ = 0
_CLOUD_ERROR = None


def _public_params(params):
    clean = {}
    if not isinstance(params, dict):
        return clean
    for key in ("message", "path"):
        value = params.get(key)
        if isinstance(value, str) and value:
            clean[key] = public_message(value)
    return clean


def _state_path():
    return os.path.join(config.get_base_dir(), "sync", "state.json")


def _load_state():
    path = _state_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state):
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".partial"
    with open(temporary, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False)
    os.replace(temporary, path)


def library_busy():
    return inventory._library_busy()


def save_client(client_id, client_secret):
    if client_id is None:
        client_id = ""
    if client_secret is None:
        client_secret = ""
    if not isinstance(client_id, str) or not isinstance(client_secret, str):
        raise SyncError("sync.invalid_client", status=422)
    client_id = client_id.strip()
    client_secret = client_secret.strip()
    if not CLIENT_RE.fullmatch(client_id):
        raise SyncError("sync.invalid_client", status=422)
    creds = credentials.load()
    changed = creds.get("client_id") != client_id
    if client_secret:
        if not 8 <= len(client_secret) <= 256 or any(ch.isspace() for ch in client_secret):
            raise SyncError("sync.invalid_client", status=422)
        creds["client_secret"] = client_secret
    elif changed or not creds.get("client_secret"):
        raise SyncError("sync.not_configured")
    if changed:
        for key in ("refresh_token", "email", "folder_id", "manifest_id"):
            creds[key] = ""
    creds["client_id"] = client_id
    credentials.save(creds)
    credentials.set_provider("google")
    return creds


def save_webdav_config(url, username, password, base_path):
    """保存 WebDAV 配置并设为当前后端；密码留空表示沿用已保存的。"""
    if not isinstance(password, str):
        raise SyncError("sync.webdav_invalid", status=422)
    url, username, base_path = webdav.validate_config(url, username, base_path)
    password = password.strip()
    if password and (len(password) > 256):
        raise SyncError("sync.webdav_invalid", status=422)
    creds = credentials.load_webdav()
    changed = creds.get("url") != url or creds.get("username") != username
    if password:
        creds["password"] = password
    elif changed or not creds.get("password"):
        # 换了服务器/账号后旧密码无意义，必须重新给
        if changed:
            creds["password"] = ""
        raise SyncError("sync.webdav_invalid", status=422)
    creds.update({"url": url, "username": username, "base_path": base_path})
    credentials.save_webdav(creds)
    credentials.set_provider("webdav")
    return creds


def disconnect():
    _ensure_idle()
    provider = credentials.get_provider()
    if provider == "webdav":
        creds = credentials.load_webdav()
        creds["password"] = ""
        credentials.save_webdav(creds)
    else:
        creds = credentials.load()
        creds["refresh_token"] = ""
        creds["email"] = ""
        credentials.save(creds)


def _ensure_idle():
    with _LOCK:
        if _JOB and _JOB["running"]:
            raise SyncError("sync.running", status=409)


def _require_configured():
    provider = credentials.get_provider()
    if provider == "webdav":
        creds = credentials.load_webdav()
        if not creds.get("url") or not creds.get("username"):
            raise SyncError("sync.not_configured")
        return creds
    creds = credentials.load()
    if not creds.get("client_id") or not creds.get("client_secret"):
        raise SyncError("sync.not_configured")
    return creds


def _require_connected():
    provider = credentials.get_provider()
    if provider == "webdav":
        creds = credentials.load_webdav()
        if not creds.get("url") or not creds.get("username") or not creds.get("password"):
            raise SyncError("sync.not_connected")
        return creds
    creds = _require_configured()
    if not creds.get("refresh_token"):
        raise SyncError("sync.not_connected")
    return creds


def _make_store():
    """按当前后端构造 store；调用方负责 close。"""
    provider = credentials.get_provider()
    if provider == "webdav":
        creds = credentials.load_webdav()
        return webdav.WebDavStore(creds.get("url"), creds.get("username"),
                                  creds.get("password"), creds.get("base_path"))
    creds = credentials.load()
    return google_drive.GoogleStore(creds, credentials.credential_path())


def _remember_cloud(manifest):
    state = _load_state()
    state["cloud"] = inventory.summarize_manifest(manifest)
    _save_state(state)


def refresh_cloud():
    global _CLOUD_ERROR
    provider = credentials.get_provider()
    connected = bool(credentials.load().get("refresh_token")) if provider == "google" \
        else bool(provider == "webdav" and credentials.load_webdav().get("password"))
    if not connected:
        _CLOUD_ERROR = None
        state = _load_state()
        state["cloud"] = None
        _save_state(state)
        return
    store = _make_store()
    try:
        manifest = store.load_manifest()
        if manifest is not None:
            inventory.parse_manifest(manifest)
        _remember_cloud(manifest)
        _CLOUD_ERROR = None
    except SyncError as exc:
        _CLOUD_ERROR = {"code": exc.code, "params": _public_params(exc.params)}
        raise
    finally:
        store.close()


def _job_view(job):
    return {
        "id": job["id"],
        "running": job["running"],
        "op": job["op"],
        "phase": job["phase"],
        "done": job["done"],
        "total": job["total"],
        "current": (job["current"] or "")[:180],
        "ok": job["ok"],
        "error": job["error"],
        "error_params": _public_params(job["error_params"]),
        "finished": job["finished"],
    }


def public_job():
    with _LOCK:
        if not _JOB:
            return None
        return _job_view(_JOB)


def status():
    provider = credentials.get_provider()
    google = credentials.load()
    dav = credentials.load_webdav()
    if provider == "webdav":
        configured = bool(dav.get("url") and dav.get("username"))
        connected = configured and bool(dav.get("password"))
        account = dav.get("username") or ""
    else:
        configured = bool(google.get("client_id") and google.get("client_secret"))
        connected = bool(google.get("refresh_token"))
        account = google.get("email") or ""
    state = _load_state()
    cloud = state.get("cloud")
    if not isinstance(cloud, dict):
        cloud = None
    return {
        "provider": provider or "google",
        "configured": configured,
        "connected": connected,
        "email": account,
        "client_id": google.get("client_id") or "",
        "has_client_secret": bool(google.get("client_secret")),
        "webdav": {
            "url": dav.get("url") or "",
            "username": dav.get("username") or "",
            "base_path": dav.get("base_path") or webdav.DEFAULT_BASE_PATH,
            "has_password": bool(dav.get("password")),
        },
        "local": inventory.summarize_local(config.get_base_dir()),
        "cloud": cloud,
        "cloud_error": _CLOUD_ERROR,
        "job": public_job(),
    }


def _progress(job):
    def inner(phase, done, total, current):
        with _LOCK:
            job["phase"] = phase
            job["done"] = done
            job["total"] = total
            job["current"] = current or ""
    return inner


def _start(op, work):
    global _JOB, _JOB_SEQ
    with _LOCK:
        if _JOB and _JOB["running"]:
            raise SyncError("sync.running", status=409)
        _JOB_SEQ += 1
        job = {
            "id": _JOB_SEQ,
            "running": True,
            "op": op,
            "phase": "scan",
            "done": 0,
            "total": 0,
            "current": "",
            "ok": False,
            "error": "",
            "error_params": {},
            "finished": False,
            "cancel": threading.Event(),
        }
        _JOB = job
        view = _job_view(job)

        def runner():
            try:
                work(job, job["cancel"])
                with _LOCK:
                    job["ok"] = True
            except SyncError as exc:
                logger.info("同步未完成：%s", exc.code)
                with _LOCK:
                    job["error"] = exc.code
                    job["error_params"] = _public_params(exc.params)
            except Exception as exc:
                logger.exception("同步失败")
                with _LOCK:
                    job["error"] = "sync.failed"
                    job["error_params"] = {"message": public_message(exc)}
            finally:
                with _LOCK:
                    job["running"] = False
                    job["finished"] = True

    threading.Thread(target=runner, name="arxiv-sync", daemon=True).start()
    return view


def start_connect():
    _require_configured()
    if library_busy():
        raise SyncError("sync.busy", status=409)

    if credentials.get_provider() == "webdav":
        def work_webdav(job, cancel):
            if cancel.is_set():
                raise SyncError("sync.cancelled")
            store = _make_store()
            try:
                store.check_connection()
            finally:
                store.close()
            try:
                refresh_cloud()
            except SyncError:
                logger.info("已连接 WebDAV，但暂时读不到云端备份")

        return _start("connect", work_webdav)

    def work(job, cancel):
        creds = credentials.load()
        path = credentials.credential_path()
        google_drive.connect(creds, path, _progress(job), cancel)
        try:
            refresh_cloud()
        except SyncError:
            logger.info("已连接 Google，但暂时读不到云端备份")

    return _start("connect", work)


def start_push():
    _require_connected()
    if library_busy():
        raise SyncError("sync.busy", status=409)

    def work(job, cancel):
        if library_busy():
            raise SyncError("sync.busy", status=409)
        store = _make_store()
        try:
            manifest = inventory.push_library(config.get_base_dir(), store, _progress(job), cancel)
            _remember_cloud(manifest)
        finally:
            store.close()

    return _start("push", work)


def start_pull():
    _require_connected()
    if library_busy():
        raise SyncError("sync.busy", status=409)

    def work(job, cancel):
        store = _make_store()
        try:
            manifest = inventory.pull_library(config.get_base_dir(), store, _progress(job), cancel)
            _remember_cloud(manifest)
        finally:
            store.close()

    return _start("pull", work)


def cancel():
    with _LOCK:
        if _JOB and _JOB["running"]:
            _JOB["cancel"].set()
    return status()
