"""同步凭据只留在本机 sync/ 目录，不进入云端副本。

Google 客户端与刷新令牌：sync/google.json
WebDAV 服务器与应用密码：sync/webdav.json
当前启用的后端：sync/provider（"google" | "webdav"）
"""

import json
import os
import threading

from harness import config

_LOCK = threading.Lock()
FIELDS = ("client_id", "client_secret", "refresh_token", "email", "folder_id", "manifest_id")
WEBDAV_FIELDS = ("url", "username", "password", "base_path")
PROVIDERS = ("google", "webdav")


def credential_path(data_dir=None):
    base = data_dir or config.get_base_dir()
    return os.path.join(base, "sync", "google.json")


def webdav_path(data_dir=None):
    base = data_dir or config.get_base_dir()
    return os.path.join(base, "sync", "webdav.json")


def provider_path(data_dir=None):
    base = data_dir or config.get_base_dir()
    return os.path.join(base, "sync", "provider")


def get_provider(data_dir=None):
    """当前启用的同步后端；没选过则按已配置的凭据推断（兼容老用户）。"""
    path = provider_path(data_dir)
    with _LOCK:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                name = fh.read().strip()
            if name in PROVIDERS:
                return name
        except (OSError, ValueError):
            pass
    if load().get("client_id"):
        return "google"
    if load_webdav().get("url"):
        return "webdav"
    return ""


def set_provider(name, data_dir=None):
    if name not in PROVIDERS:
        raise ValueError("unknown provider: {}".format(name))
    path = provider_path(data_dir)
    with _LOCK:
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        if os.name != "nt":
            os.chmod(directory, 0o700)
        temporary = path + ".partial"
        with open(temporary, "w", encoding="utf-8") as fh:
            fh.write(name)
        os.replace(temporary, path)


def _load_fields(path, fields):
    with _LOCK:
        if not os.path.isfile(path):
            return {key: "" for key in fields}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return {key: "" for key in fields}
        data = {key: "" for key in fields}
        if isinstance(raw, dict):
            for key in fields:
                value = raw.get(key)
                if isinstance(value, str):
                    data[key] = value
        return data


def _save_fields(data, path, fields):
    clean = {key: "" for key in fields}
    for key in fields:
        value = data.get(key) if isinstance(data, dict) else ""
        clean[key] = value if isinstance(value, str) else ""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    if os.name != "nt":
        os.chmod(directory, 0o700)
    temporary = path + ".partial"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(clean, fh, ensure_ascii=False)
            fh.write("\n")
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except Exception:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise
    return clean


def blank():
    return {key: "" for key in FIELDS}


def load(path=None):
    return _load_fields(path or credential_path(), FIELDS)


def save(data, path=None):
    return _save_fields(data, path or credential_path(), FIELDS)


def load_webdav(path=None):
    return _load_fields(path or webdav_path(), WEBDAV_FIELDS)


def save_webdav(data, path=None):
    return _save_fields(data, path or webdav_path(), WEBDAV_FIELDS)
