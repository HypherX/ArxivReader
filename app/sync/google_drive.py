"""Google Drive 应用数据目录。令牌只和 Google 交换，不经过自建服务器。

桌面程序用本机 127.0.0.1 接收授权码（PKCE）。备份放在 Drive 的 appDataFolder，
只对这个 OAuth 客户端可见，并计入用户自己的网盘容量。
"""

import base64
import hashlib
import json
import os
import secrets
import tempfile
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

from . import credentials
from .errors import SyncError, public_message

TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
DRIVE = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
USERINFO = "https://www.googleapis.com/oauth2/v3/userinfo"
SCOPES = "openid email https://www.googleapis.com/auth/drive.appdata"
FOLDER = "application/vnd.google-apps.folder"
CHUNK = 8 * 1024 * 1024


def pkce_pair():
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def build_authorize_url(client_id, redirect_uri, state, challenge):
    query = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    })
    return AUTH_URL + "?" + query


def _q(value):
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/oauth2callback":
            self.send_response(404)
            self.end_headers()
            return
        query = urllib.parse.parse_qs(parsed.query)
        self.server.result = {key: values[0] for key, values in query.items() if values}
        body = (
            "<!DOCTYPE html><meta charset='utf-8'><title>ArxivReader</title>"
            "<p>Google account connected. You can close this tab and return to ArxivReader.</p>"
            "<p>Google 账号已连接，可以关闭此页并回到 ArxivReader。</p>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


def bind_oauth_server():
    httpd = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    httpd.result = None
    redirect = "http://127.0.0.1:{}/oauth2callback".format(httpd.server_address[1])
    return httpd, redirect


def serve_oauth(httpd, cancel, timeout=180):
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            if cancel is not None and cancel.is_set():
                raise SyncError("sync.cancelled")
            httpd.timeout = 0.5
            httpd.handle_request()
            if httpd.result is not None:
                return httpd.result
        raise SyncError("sync.oauth_timeout")
    finally:
        httpd.server_close()


def _token_error(response):
    text = ""
    try:
        text = response.text or ""
    except Exception:
        text = ""
    if "invalid_grant" in text:
        raise SyncError("sync.oauth_expired")
    message = ""
    try:
        payload = response.json()
        message = payload.get("error_description") or payload.get("error") or ""
    except Exception:
        message = text
    raise SyncError("sync.oauth_failed", {"message": public_message(message)})


def exchange_code(http, client_id, client_secret, code, verifier, redirect_uri):
    response = http.post(TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }, timeout=30)
    if response.status_code >= 400:
        _token_error(response)
    payload = response.json()
    if not payload.get("refresh_token"):
        raise SyncError("sync.oauth_failed", {"message": ""})
    return payload


def connect(creds, path, on_progress, cancel, opener=None):
    """打开系统浏览器完成授权，并把刷新令牌写进本机凭据。"""
    on_progress("auth", 0, 0, "")
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    # 先占住端口，授权地址里的端口必须和实际监听一致。
    httpd, redirect = bind_oauth_server()
    url = build_authorize_url(creds["client_id"], redirect, state, challenge)
    open_browser = opener or _open_browser
    try:
        if not open_browser(url):
            raise SyncError("sync.browser_failed")
    except Exception:
        httpd.server_close()
        raise
    captured = serve_oauth(httpd, cancel) or {}
    if captured.get("error") == "access_denied":
        raise SyncError("sync.oauth_denied")
    if captured.get("error"):
        raise SyncError("sync.oauth_failed", {"message": public_message(captured.get("error"))})
    code = captured.get("code") or ""
    got_state = captured.get("state") or ""
    if not secrets.compare_digest(got_state, state) or not _valid_code(code):
        raise SyncError("sync.oauth_failed", {"message": ""})
    with httpx.Client(timeout=30) as http:
        payload = exchange_code(http, creds["client_id"], creds["client_secret"], code, verifier, redirect)
    creds["refresh_token"] = payload["refresh_token"]
    credentials.save(creds, path)
    store = GoogleStore(creds, path)
    try:
        try:
            creds["email"] = store.user_email()
        except SyncError:
            creds["email"] = ""
        credentials.save(creds, path)
        store.ensure_root()
    finally:
        store.close()


def _valid_code(code):
    if not isinstance(code, str) or not 8 <= len(code) <= 512:
        return False
    return all(ch.isascii() and (ch.isalnum() or ch in "_.-/~+=") for ch in code)


def _open_browser(url):
    import webbrowser
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


class GoogleStore:
    def __init__(self, creds, path, http=None, upload_limit=5 * 1024 * 1024):
        self.creds = creds
        self.path = path
        self.upload_limit = upload_limit
        self.manifest_id = creds.get("manifest_id") or ""
        self._folders = {}
        self._token = ""
        self._exp = 0
        self._own_http = http is None
        self.http = http or httpx.Client(
            follow_redirects=True,
            headers={"User-Agent": "ArxivReader"},
            timeout=httpx.Timeout(30.0, read=300.0),
        )

    def close(self):
        if self._own_http:
            self.http.close()

    def _save(self):
        credentials.save(self.creds, self.path)

    def access_token(self, force=False):
        if not force and self._token and time.time() < self._exp - 30:
            return self._token
        response = self.http.post(TOKEN_URL, data={
            "client_id": self.creds.get("client_id") or "",
            "client_secret": self.creds.get("client_secret") or "",
            "refresh_token": self.creds.get("refresh_token") or "",
            "grant_type": "refresh_token",
        }, timeout=30)
        if response.status_code >= 400:
            _token_error(response)
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise SyncError("sync.oauth_failed", {"message": ""})
        self._token = token
        self._exp = time.time() + int(payload.get("expires_in") or 3600)
        return self._token

    def _send(self, method, url, **kwargs):
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = "Bearer " + self.access_token()
        kwargs["headers"] = headers
        response = None
        for attempt in range(4):
            response = self.http.request(method, url, **kwargs)
            if response.status_code == 401 and attempt == 0:
                headers["Authorization"] = "Bearer " + self.access_token(force=True)
                kwargs["headers"] = headers
                continue
            if response.status_code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(min(8, 1.5 * (attempt + 1)))
                continue
            return response
        return response

    def _raise(self, response):
        if response.status_code < 400:
            return
        message = ""
        try:
            payload = response.json()
            err = payload.get("error")
            if isinstance(err, dict):
                message = err.get("message") or ""
            elif isinstance(err, str):
                message = payload.get("error_description") or err
        except Exception:
            message = ""
        message = public_message(message)
        if message:
            raise SyncError("sync.failed", {"message": message})
        raise SyncError("sync.failed_plain")

    def _raise_raw(self, status, raw):
        message = ""
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
            err = payload.get("error")
            if isinstance(err, dict):
                message = err.get("message") or ""
        except Exception:
            message = ""
        message = public_message(message)
        if status == 401:
            raise SyncError("sync.oauth_expired")
        if message:
            raise SyncError("sync.failed", {"message": message})
        raise SyncError("sync.failed_plain")

    def user_email(self):
        response = self._send("GET", USERINFO)
        self._raise(response)
        email = response.json().get("email") or ""
        return email if isinstance(email, str) else ""

    def _query(self, query):
        files = []
        token = None
        for _page in range(100):
            params = {
                "spaces": "appDataFolder",
                "pageSize": 1000,
                "fields": "nextPageToken,files(id,name,mimeType,parents,trashed)",
                "q": query,
            }
            if token:
                params["pageToken"] = token
            response = self._send("GET", DRIVE + "/files", params=params)
            self._raise(response)
            payload = response.json()
            files.extend(payload.get("files") or [])
            token = payload.get("nextPageToken")
            if not token:
                return files
        raise SyncError("sync.failed_plain")

    def _exists(self, file_id):
        url = DRIVE + "/files/{}?fields=id,trashed".format(urllib.parse.quote(file_id, safe=""))
        response = self._send("GET", url)
        if response.status_code == 404:
            return False
        self._raise(response)
        return not response.json().get("trashed")

    def _create_folder(self, name, parent):
        response = self._send("POST", DRIVE + "/files?fields=id", json={
            "name": name,
            "mimeType": FOLDER,
            "parents": [parent],
        })
        self._raise(response)
        return response.json()["id"]

    def ensure_root(self):
        folder_id = self.creds.get("folder_id") or ""
        if folder_id and self._exists(folder_id):
            self._folders[""] = folder_id
            return folder_id
        found = self._query(
            "name = 'ArxivReader' and mimeType = '{}' and 'appDataFolder' in parents and trashed = false".format(FOLDER)
        )
        if found:
            folder_id = found[0]["id"]
        else:
            folder_id = self._create_folder("ArxivReader", "appDataFolder")
        self.creds["folder_id"] = folder_id
        self._save()
        self._folders[""] = folder_id
        return folder_id

    def ensure_dir(self, rel_dir):
        rel_dir = (rel_dir or "").strip("/")
        if rel_dir in self._folders:
            return self._folders[rel_dir]
        if not rel_dir:
            return self.ensure_root()
        parent_rel, name = rel_dir.rsplit("/", 1) if "/" in rel_dir else ("", rel_dir)
        parent_id = self.ensure_dir(parent_rel)
        found = self._query(
            "name = '{}' and mimeType = '{}' and '{}' in parents and trashed = false".format(
                _q(name), FOLDER, parent_id)
        )
        folder_id = found[0]["id"] if found else self._create_folder(name, parent_id)
        self._folders[rel_dir] = folder_id
        return folder_id

    def put_file(self, relpath, local_path, sha256, cancel=None):
        if cancel is not None and cancel.is_set():
            raise SyncError("sync.cancelled")
        relpath = relpath.replace("\\", "/")
        parent_rel, name = relpath.rsplit("/", 1) if "/" in relpath else ("", relpath)
        parent_id = self.ensure_dir(parent_rel)
        metadata = {"name": name, "parents": [parent_id], "appProperties": {"sha256": sha256}}
        size = os.path.getsize(local_path)
        if size == 0 or size <= self.upload_limit:
            file_id = self._upload_multipart(metadata, local_path)
        else:
            file_id = self._upload_resumable(metadata, local_path, size, cancel)
        return file_id

    def _upload_multipart(self, metadata, local_path):
        boundary = "arxivreader" + secrets.token_hex(12)
        with open(local_path, "rb") as fh:
            content = fh.read()
        body = b"".join([
            b"--" + boundary.encode() + b"\r\n",
            b"Content-Type: application/json; charset=UTF-8\r\n\r\n",
            json.dumps(metadata).encode("utf-8") + b"\r\n",
            b"--" + boundary.encode() + b"\r\n",
            b"Content-Type: application/octet-stream\r\n\r\n",
            content + b"\r\n",
            b"--" + boundary.encode() + b"--\r\n",
        ])
        response = self._send(
            "POST",
            UPLOAD + "?uploadType=multipart&fields=id",
            content=body,
            headers={"Content-Type": "multipart/related; boundary=" + boundary},
        )
        self._raise(response)
        return response.json()["id"]

    def _upload_resumable(self, metadata, local_path, size, cancel):
        response = self._send(
            "POST",
            UPLOAD + "?uploadType=resumable&fields=id",
            json=metadata,
            headers={
                "X-Upload-Content-Type": "application/octet-stream",
                "X-Upload-Content-Length": str(size),
            },
        )
        self._raise(response)
        session = response.headers.get("Location")
        if not session:
            raise SyncError("sync.failed_plain")
        sent = 0
        with open(local_path, "rb") as fh:
            while sent < size:
                if cancel is not None and cancel.is_set():
                    raise SyncError("sync.cancelled")
                chunk = fh.read(CHUNK)
                end = sent + len(chunk) - 1
                put = self.http.put(session, content=chunk, headers={
                    "Authorization": "Bearer " + self.access_token(),
                    "Content-Length": str(len(chunk)),
                    "Content-Range": "bytes {}-{}/{}".format(sent, end, size),
                }, timeout=300)
                if put.status_code in (200, 201):
                    return put.json()["id"]
                if put.status_code == 308:
                    rng = put.headers.get("Range") or ""
                    sent = int(rng.split("-")[-1]) + 1 if "-" in rng else end + 1
                    continue
                self._raise(put)
        raise SyncError("sync.failed_plain")

    def get_file(self, file_id, dest, cancel=None):
        url = DRIVE + "/files/{}?alt=media".format(urllib.parse.quote(file_id, safe=""))
        tmp = dest + ".partial"
        try:
            for attempt in range(2):
                if cancel is not None and cancel.is_set():
                    raise SyncError("sync.cancelled")
                token = self.access_token(force=attempt == 1)
                with self.http.stream("GET", url, headers={"Authorization": "Bearer " + token}, timeout=httpx.Timeout(30.0, read=300.0)) as response:
                    if response.status_code == 401 and attempt == 0:
                        response.read()
                        continue
                    if response.status_code >= 400:
                        self._raise_raw(response.status_code, response.read())
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with open(tmp, "wb") as fh:
                        for chunk in response.iter_bytes(1024 * 1024):
                            if cancel is not None and cancel.is_set():
                                raise SyncError("sync.cancelled")
                            fh.write(chunk)
                    os.replace(tmp, dest)
                    return
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        raise SyncError("sync.oauth_expired")

    def delete_file(self, file_id):
        url = DRIVE + "/files/{}".format(urllib.parse.quote(file_id, safe=""))
        response = self._send("DELETE", url)
        if response.status_code in (204, 404):
            return
        self._raise(response)

    def load_manifest(self):
        file_id = self.creds.get("manifest_id") or ""
        if file_id and not self._exists(file_id):
            file_id = ""
        if not file_id:
            root = self.ensure_root()
            found = self._query("name = 'manifest.json' and '{}' in parents and trashed = false".format(root))
            found = [item for item in found if item.get("mimeType") != FOLDER]
            if not found:
                return None
            file_id = found[0]["id"]
            self.creds["manifest_id"] = file_id
            self.manifest_id = file_id
            self._save()
        handle, tmp = tempfile.mkstemp(prefix="arxiv-manifest-")
        os.close(handle)
        try:
            self.get_file(file_id, tmp)
            with open(tmp, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError:
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
            file_id = self.put_file("manifest.json", tmp, hashlib.sha256(raw).hexdigest())
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        self.creds["manifest_id"] = file_id
        self.manifest_id = file_id
        self._save()

    def list_file_ids(self):
        root = self.ensure_root()
        files = self._query("trashed = false")
        by_id = {item.get("id"): item for item in files if item.get("id")}

        def under_root(item):
            seen = set()
            current = item
            while current:
                if current.get("id") == root:
                    return True
                parents = current.get("parents") or []
                if not parents:
                    return False
                parent_id = parents[0]
                if parent_id == root:
                    return True
                if parent_id in seen or parent_id == "appDataFolder":
                    return False
                seen.add(parent_id)
                current = by_id.get(parent_id)
            return False

        return [
            item["id"] for item in files
            if item.get("mimeType") != FOLDER and item.get("id") and under_root(item)
        ]
