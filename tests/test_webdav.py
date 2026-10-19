"""WebDAV 云端同步：假服务器端到端（坚果云协议面），配置校验与错误映射。"""

import base64
import os
import shutil
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app.sync import inventory, webdav
from app.sync.errors import SyncError

USER = "reader@example.com"
PASSWORD = "app-password-1"
PDF = b"%PDF-1.4 webdav"


class _FakeWebDavHandler(BaseHTTPRequestHandler):
    """把 MKCOL/PUT/GET/DELETE/PROPFIND 映射到一个临时目录的最小 WebDAV。"""

    root = None          # 临时目录（class 级，由测试设置）
    token = b""          # 期望的 Basic 凭证

    def log_message(self, *_args):
        return None

    # ---------------------------------------------------------- 工具
    def _local(self):
        rel = urllib.parse.unquote(self.path.split("?", 1)[0]).strip("/")
        parts = [p for p in rel.split("/") if p and p not in (".", "..")]
        return os.path.join(self.root, *parts), "/".join(parts)

    def _authorized(self):
        if self.headers.get("Authorization", "").encode() != b"Basic " + self.token:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="fake-dav"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
        return True

    def _empty(self, status):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---------------------------------------------------------- 方法
    def do_MKCOL(self):
        if not self._authorized():
            return
        local, _rel = self._local()
        if os.path.exists(local):
            self._empty(405)
            return
        os.makedirs(local, exist_ok=True)
        self._empty(201)

    def do_PUT(self):
        if not self._authorized():
            return
        local, _rel = self._local()
        length = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(length)
        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local, "wb") as fh:
            fh.write(data)
        self._empty(201)

    def do_GET(self):
        if not self._authorized():
            return
        local, _rel = self._local()
        if not os.path.isfile(local):
            self._empty(404)
            return
        data = Path(local).read_bytes()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_DELETE(self):
        if not self._authorized():
            return
        local, _rel = self._local()
        if os.path.isfile(local):
            os.remove(local)
            self._empty(204)
        else:
            self._empty(404)

    def do_PROPFIND(self):
        if not self._authorized():
            return
        local, rel = self._local()
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if not os.path.isdir(local):
            self._empty(404)
            return
        depth = self.headers.get("Depth", "1")
        entries = [(local, rel)]
        if depth != "0":
            for name in sorted(os.listdir(local)):
                entries.append((os.path.join(local, name), "/".join(p for p in (rel, name) if p)))
        body = ['<?xml version="1.0" encoding="utf-8"?>',
                '<d:multistatus xmlns:d="DAV:">']
        for path, entry_rel in entries:
            is_dir = os.path.isdir(path)
            body.append("<d:response><d:href>/{}</d:href>".format(entry_rel))
            if is_dir:
                body.append("<d:propstat><d:prop><d:resourcetype><d:collection/>"
                            "</d:resourcetype></d:prop></d:propstat>")
            body.append("</d:response>")
        body.append("</d:multistatus>")
        raw = "".join(body).encode("utf-8")
        self.send_response(207)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class WebDavConfigTests(unittest.TestCase):
    def test_validate_config_rules(self):
        with self.assertRaises(SyncError):
            webdav.validate_config("", USER, "")                       # 空地址
        with self.assertRaises(SyncError):
            webdav.validate_config("http://dav.jianguoyun.com/dav/", USER, "")  # 非回环 http
        with self.assertRaises(SyncError):
            webdav.validate_config("https://dav.example.com/dav/", "", "")      # 空账号
        with self.assertRaises(SyncError):
            webdav.validate_config("https://dav.example.com/", USER, "../etc")  # 路径穿越
        url, user, base = webdav.validate_config("https://dav.jianguoyun.com/dav/", USER, "/Zotero style/")
        self.assertEqual(url, "https://dav.jianguoyun.com/dav")
        self.assertEqual(base, "Zotero style")
        _url, _user, default_base = webdav.validate_config("http://127.0.0.1:8765/", USER, "")
        self.assertEqual(default_base, webdav.DEFAULT_BASE_PATH)        # 回环 http 放行


class WebDavFlowTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="arxiv-webdav-server-")
        self.dirs = [self.root]
        _FakeWebDavHandler.root = self.root
        _FakeWebDavHandler.token = base64.b64encode("{}:{}".format(USER, PASSWORD).encode())
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeWebDavHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:{}/dav".format(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for path in self.dirs:
            shutil.rmtree(path, ignore_errors=True)

    def make_store(self, password=PASSWORD, base_path="ArxivReader"):
        return webdav.WebDavStore(self.url, USER, password, base_path)

    def _local_file(self, name="a.pdf", data=PDF):
        path = os.path.join(tempfile.mkdtemp(prefix="arxiv-webdav-local-"), name)
        self.dirs.append(os.path.dirname(path))
        Path(path).write_bytes(data)
        return path

    def test_put_get_delete_roundtrip_and_list(self):
        store = self.make_store()
        self.addCleanup(store.close)
        local = self._local_file()
        file_id = store.put_file("pdfs/Unfiled/a.pdf", local, "sha")
        self.assertEqual(file_id, "pdfs/Unfiled/a.pdf")          # id 即相对路径
        store.put_file("knowledge/RL/graph.md", local, "sha")

        dest = os.path.join(self.dirs[1] if len(self.dirs) > 1 else tempfile.mkdtemp(), "out.pdf")
        self.dirs.append(os.path.dirname(dest))
        store.get_file(file_id, dest)
        self.assertEqual(Path(dest).read_bytes(), PDF)

        ids = sorted(store.list_file_ids())
        self.assertEqual(ids, ["knowledge/RL/graph.md", "pdfs/Unfiled/a.pdf"])

        store.delete_file(file_id)
        self.assertEqual(store.list_file_ids(), ["knowledge/RL/graph.md"])
        with self.assertRaises(SyncError) as caught:
            store.get_file(file_id, dest)
        self.assertEqual(caught.exception.code, "sync.webdav_not_found")

    def test_wrong_password_maps_to_auth_error(self):
        store = self.make_store(password="wrong-password")
        self.addCleanup(store.close)
        with self.assertRaises(SyncError) as caught:
            store.put_file("pdfs/a.pdf", self._local_file(), "sha")
        self.assertEqual(caught.exception.code, "sync.webdav_auth")
        with self.assertRaises(SyncError) as caught:
            store.check_connection()
        self.assertEqual(caught.exception.code, "sync.webdav_auth")

    def test_manifest_roundtrip_and_empty_cloud(self):
        store = self.make_store()
        self.addCleanup(store.close)
        self.assertIsNone(store.load_manifest())                  # 云端还没备份
        store.save_manifest({"format": 1, "app": "ArxivReader", "files": {"a": {"id": "a"}}})
        self.assertEqual(store.load_manifest()["files"]["a"]["id"], "a")
        self.assertEqual(store.manifest_id, "manifest.json")

    def test_check_connection_creates_base_dir(self):
        store = self.make_store(base_path="My/Custom Path")
        self.addCleanup(store.close)
        self.assertTrue(store.check_connection())
        self.assertTrue(os.path.isdir(os.path.join(self.root, "dav", "My", "Custom Path")))

    def test_push_then_pull_restores_library(self):
        from harness import config
        from app import database

        original = config.get_base_dir()
        dirs = []
        try:
            source = tempfile.mkdtemp(prefix="arxiv-webdav-src-")
            dirs.append(source)
            config.set_data_dir(source)
            database.reopen_db()
            database.init_db()
            pdf = os.path.join(source, "pdfs", "Unfiled", "a.pdf")
            os.makedirs(os.path.dirname(pdf), exist_ok=True)
            Path(pdf).write_bytes(PDF)
            db = database.SessionLocal()
            try:
                from app.models import Paper
                db.add(Paper(arxiv_id="2401.00002", title="WebDAV Synced", pdf_path=pdf))
                db.commit()
            finally:
                db.close()

            store = self.make_store()
            manifest = inventory.push_library(source, store, lambda *_a: None, threading.Event())
            self.assertEqual(manifest["files"]["pdfs/Unfiled/a.pdf"]["id"], "pdfs/Unfiled/a.pdf")
            self.assertTrue(os.path.isfile(os.path.join(self.root, "dav", "ArxivReader", "manifest.json")))

            target = tempfile.mkdtemp(prefix="arxiv-webdav-dst-")
            dirs.append(target)
            config.set_data_dir(target)
            database.reopen_db()
            database.init_db()
            inventory.pull_library(target, store, lambda *_a: None, threading.Event())
            restored = os.path.join(target, "pdfs", "Unfiled", "a.pdf")
            self.assertEqual(Path(restored).read_bytes(), PDF)
            import sqlite3
            with sqlite3.connect(os.path.join(target, "library.db")) as conn:
                title = conn.execute("SELECT title FROM papers").fetchone()[0]
            self.assertEqual(title, "WebDAV Synced")
            store.close()
        finally:
            config.set_data_dir(original)
            database.reopen_db()
            for path in dirs:
                shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
