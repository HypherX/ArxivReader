"""Google 客户端和刷新令牌只留在本机 sync/google.json，不进入云端副本。"""

import json
import os
import threading

from harness import config

_LOCK = threading.Lock()
FIELDS = ("client_id", "client_secret", "refresh_token", "email", "folder_id", "manifest_id")


def credential_path(data_dir=None):
    base = data_dir or config.get_base_dir()
    return os.path.join(base, "sync", "google.json")


def blank():
    return {key: "" for key in FIELDS}


def load(path=None):
    path = path or credential_path()
    with _LOCK:
        if not os.path.isfile(path):
            return blank()
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return blank()
        data = blank()
        if isinstance(raw, dict):
            for key in FIELDS:
                value = raw.get(key)
                if isinstance(value, str):
                    data[key] = value
        return data


def save(data, path=None):
    path = path or credential_path()
    clean = blank()
    for key in FIELDS:
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
