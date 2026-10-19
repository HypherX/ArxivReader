"""WebDAV 云端副本（坚果云 / Nextcloud / ownCloud 等，Zotero 式自定义节点）。

与 Google Drive 后端的差别：WebDAV 天生按路径寻址，manifest 里的文件 id 直接就是
相对路径（inventory.parse_manifest 用 safe_rel 校验，天然防路径穿越）。
账号 + 应用密码只走 HTTPS Basic（本机 127.0.0.1 测试允许 HTTP），只留在本机
sync/webdav.json，不进入云端副本。

服务端布局：<服务器>/<base_path>/manifest.json + pdfs/... + knowledge/... + library.db，
与本机资料目录同构，恢复时逐个校验 sha256 后才替换本地。
"""

import json
import logging
import os
import tempfile
import time
import urllib.parse
import xml.etree.ElementTree as ET

import requests

from .errors import SyncError, public_message

logger = logging.getLogger(__name__)

DEFAULT_BASE_PATH = "ArxivReader"
_CHUNK = 1024 * 1024
_TIMEOUT = (20.0, 300.0)          # (连接, 读取)：大 PDF 在慢网盘上给足读取时间
_RETRY_STATUS = (429, 500, 502, 503, 504)
_MAX_RELPATH_ID = 1024            # 与 inventory.safe_rel 的上限一致
_UA = "ArxivReader/1.0 (+https://github.com/HypherX/ArxivReader)"

# 解析 PROPFIND 返回时的 DAV 命名空间
_PROPFIND_BODY = ('<?xml version="1.0" encoding="utf-8"?>'
                  '<propfind xmlns="DAV:"><prop><resourcetype/></prop></propfind>')


def _is_loopback(host):
    return (host or "").lower() in ("127.0.0.1", "localhost", "::1", "[::1]")


def validate_config(url, username, base_path):
    """清洗并校验 WebDAV 配置；返回 (url, username, base_path)，不合格抛 SyncError。

    只允许 https；http 仅放行本机回环（开发与测试用假服务器）。"""
    url = (url or "").strip()
    username = (username or "").strip()
    base_path = (base_path or "").strip().strip("/")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("https", "http") or not parsed.netloc or parsed.query or parsed.fragment:
        raise SyncError("sync.webdav_invalid", status=422)
    if parsed.scheme == "http" and not _is_loopback(parsed.hostname):
        raise SyncError("sync.webdav_invalid", status=422)
    if parsed.scheme == "http":
        logger.warning("WebDAV 使用明文 HTTP（仅应在本机回环测试中使用）：%s", parsed.hostname)
    if not 1 <= len(username) <= 200:
        raise SyncError("sync.webdav_invalid", status=422)
    parts = [p for p in base_path.split("/") if p]
    if any(p in (".", "..") for p in parts) or len(base_path) > 200:
        raise SyncError("sync.webdav_invalid", status=422)
    base_path = "/".join(parts) or DEFAULT_BASE_PATH
    # 去掉 query/fragment，保留服务器前缀路径（坚果云是 /dav/）
    clean_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
    return clean_url, username, base_path


def _quote_path(path):
    """把 a/b/c 逐段 URL 编码（服务器上前缀与文件名都可能含非 ASCII）。"""
    return "/".join(urllib.parse.quote(part) for part in path.split("/") if part)


class _CancelReader:
    """带取消检查与已知长度的文件读取器：requests 走 identity 编码（坚果云拒 chunked）。"""

    def __init__(self, path, cancel):
        self._fh = open(path, "rb")
        self._cancel = cancel
        self._size = os.path.getsize(path)

    def __len__(self):
        return self._size

    def read(self, size=-1):
        if self._cancel is not None and self._cancel.is_set():
            self._fh.close()
            raise SyncError("sync.cancelled")
        return self._fh.read(size)

    def close(self):
        self._fh.close()


class WebDavStore:
    """对齐 GoogleStore 的接口：put/get/manifest/list/delete/close。"""

    def __init__(self, url, username, password, base_path=DEFAULT_BASE_PATH, http=None):
        self.base_url, self.username, self.base_path = validate_config(url, username, base_path)
        self.password = password or ""
        self.manifest_id = "manifest.json"     # WebDAV 的 id 即相对路径
        self._own_http = http is None
        self.http = http or requests.Session()
        if self._own_http:
            self.http.headers.update({"User-Agent": _UA})
        self.auth = (self.username, self.password)

    def close(self):
        if self._own_http:
            self.http.close()

    # ---------------------------------------------------------- 基础请求
    def _url(self, rel):
        rel = (rel or "").strip("/")
        full = "/".join(part for part in (self.base_path, rel) if part)
        return self._raw_url(full)

    def _raw_url(self, full_rel):
        """相对服务器前缀的完整路径 → URL（ensure_dir 逐级建目录用，避免重复拼 base_path）。"""
        return self.base_url + "/" + _quote_path(full_rel)

    def _send(self, method, url, **kwargs):
        kwargs.setdefault("timeout", _TIMEOUT)
        kwargs.setdefault("auth", self.auth)
        response = None
        for attempt in range(4):
            try:
                response = self.http.request(method, url, **kwargs)
            except requests.exceptions.RequestException as exc:
                if attempt < 3:
                    time.sleep(min(8, 1.5 * (attempt + 1)))
                    continue
                raise SyncError("sync.webdav_connect",
                                {"message": public_message(type(exc).__name__)})
            if response.status_code in _RETRY_STATUS and attempt < 3:
                time.sleep(min(8, 1.5 * (attempt + 1)))
                continue
            return response
        return response

    def _raise(self, response):
        if response.status_code < 400:
            return
        if response.status_code in (401, 403):
            raise SyncError("sync.webdav_auth")
        message = public_message(response.text[:300] if response.text else "")
        if message:
            raise SyncError("sync.failed", {"message": message})
        raise SyncError("sync.failed_plain")

    # ---------------------------------------------------------- 目录与连通性
    def ensure_dir(self, rel_dir=""):
        """逐级 MKCOL（405=已存在，容忍）；顺便充当凭据/连通性检查。"""
        full = "/".join(p for p in (self.base_path, rel_dir.strip("/")) if p)
        current = ""
        for part in [p for p in full.split("/") if p]:
            current = "/".join([current, part]) if current else part
            response = self._send("MKCOL", self._raw_url(current))
            if response.status_code in (200, 201, 204, 301, 302, 405):
                continue
            self._raise(response)
        return True

    def check_connection(self):
        """连接按钮用：建好根目录并 PROPFIND 一次，确认账号与路径都可用。"""
        self.ensure_dir("")
        response = self._send("PROPFIND", self._url(""),
                              headers={"Depth": "0", "Content-Type": "application/xml"},
                              data=_PROPFIND_BODY)
        if response.status_code == 404:
            raise SyncError("sync.webdav_invalid", status=422)
        self._raise(response)
        return True

    # ---------------------------------------------------------- 文件读写
    def put_file(self, relpath, local_path, sha256, cancel=None):
        if cancel is not None and cancel.is_set():
            raise SyncError("sync.cancelled")
        relpath = relpath.replace("\\", "/")
        if len(relpath) > _MAX_RELPATH_ID:
            raise SyncError("sync.failed_plain")
        parent = relpath.rsplit("/", 1)[0] if "/" in relpath else ""
        self.ensure_dir(parent)
        url = self._url(relpath)
        response = None
        for attempt in range(3):
            reader = _CancelReader(local_path, cancel)
            try:
                response = self.http.put(url, data=reader, auth=self.auth, timeout=_TIMEOUT)
            except SyncError:
                raise
            except requests.exceptions.RequestException as exc:
                if attempt < 2:
                    time.sleep(min(8, 1.5 * (attempt + 1)))
                    continue
                raise SyncError("sync.webdav_connect",
                                {"message": public_message(type(exc).__name__)})
            finally:
                reader.close()
            if response.status_code in _RETRY_STATUS and attempt < 2:
                time.sleep(min(8, 1.5 * (attempt + 1)))
                continue
            break
        self._raise(response)
        return relpath                      # WebDAV 的文件 id 就是相对路径

    def get_file(self, file_id, dest, cancel=None):
        tmp = dest + ".partial"
        try:
            with self.http.get(self._url(file_id), auth=self.auth, timeout=_TIMEOUT,
                               stream=True) as response:
                if response.status_code in (401, 403):
                    raise SyncError("sync.webdav_auth")
                if response.status_code == 404:
                    raise SyncError("sync.webdav_not_found")
                if response.status_code >= 400:
                    self._raise(response)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(tmp, "wb") as fh:
                    for chunk in response.iter_content(_CHUNK):
                        if cancel is not None and cancel.is_set():
                            raise SyncError("sync.cancelled")
                        if chunk:
                            fh.write(chunk)
            os.replace(tmp, dest)
        except requests.exceptions.RequestException as exc:
            raise SyncError("sync.webdav_connect",
                            {"message": public_message(type(exc).__name__)})
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def delete_file(self, file_id):
        response = self._send("DELETE", self._url(file_id))
        if response.status_code in (200, 202, 204, 404):
            return
        self._raise(response)

    def list_file_ids(self):
        """PROPFIND 逐层遍历基目录，返回全部文件的相对路径（即文件 id）。"""
        found = []
        queue = [""]
        for _ in range(10000):
            if not queue:
                break
            rel_dir = queue.pop(0)
            response = self._send("PROPFIND", self._url(rel_dir),
                                  headers={"Depth": "1", "Content-Type": "application/xml"},
                                  data=_PROPFIND_BODY)
            if response.status_code == 404:
                continue
            self._raise(response)
            for rel, is_dir in _parse_propfind(response.content, self.base_path, rel_dir):
                if is_dir:
                    queue.append(rel)
                else:
                    found.append(rel)
        return found

    # ---------------------------------------------------------- manifest
    def load_manifest(self):
        handle, tmp = tempfile.mkstemp(prefix="arxiv-manifest-")
        os.close(handle)
        try:
            try:
                self.get_file("manifest.json", tmp)
            except SyncError as exc:
                if exc.code == "sync.webdav_not_found":
                    return None            # 服务器上还没有备份
                raise
            if os.path.getsize(tmp) == 0:
                return None
            try:
                with open(tmp, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                raise SyncError("sync.cloud_invalid")
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        if not isinstance(data, dict):
            raise SyncError("sync.cloud_invalid")
        return data

    def save_manifest(self, manifest):
        raw = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        handle, tmp = tempfile.mkstemp(prefix="arxiv-manifest-", suffix=".json")
        try:
            with os.fdopen(handle, "wb") as fh:
                fh.write(raw)
            import hashlib
            self.put_file("manifest.json", tmp, hashlib.sha256(raw).hexdigest())
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        self.manifest_id = "manifest.json"


def _parse_propfind(content, base_path, rel_dir):
    """解析 Depth:1 的 multistatus：返回 [(相对基目录路径, 是否目录)]，跳过自身。"""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        raise SyncError("sync.failed_plain")
    base_prefix = base_path.strip("/")
    current = "/".join(part for part in (base_prefix, rel_dir.strip("/")) if part)
    items = []
    for response in root.iter():
        if not response.tag.endswith("response"):
            continue
        href = ""
        is_dir = False
        for child in response.iter():
            if child.tag.endswith("href") and child.text:
                href = urllib.parse.unquote(child.text.strip())
            elif child.tag.endswith("collection"):
                is_dir = True
        if not href:
            continue
        # href 可能是绝对路径或完整 URL；统一剥掉服务器前缀与基目录前缀
        path = urllib.parse.urlsplit(href).path if "://" in href else href
        path = path.strip("/")
        marker = base_prefix + "/"
        index = path.find(marker)
        if index < 0:
            continue
        rel = path[index + len(marker):]
        if not rel or rel == rel_dir.strip("/"):
            continue
        items.append((rel, is_dir))
    return items
