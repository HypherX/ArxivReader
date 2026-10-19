"""Google Drive 同步：路径可搬到新电脑，上传跳过未改文件，恢复会替换本地资料。"""

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

import httpx

from app import database
from app.models import DirectionNode, Paper, PdfTranslationTask, PipelineRun
from app.sync import credentials, google_drive, inventory, service
from app.sync.errors import SyncError
from harness import config

CLIENT = "123456789012-abcdefghi.apps.googleusercontent.com"
OTHER = "123456789012-zzzzzzzzz.apps.googleusercontent.com"
SECRET = "secret-value-1"
PDF = b"%PDF-1.4 from-a"


class MemoryStore:
    def __init__(self):
        self.blobs = {}
        self.manifest = None
        self.manifest_id = None
        self.puts = 0
        self.n = 0

    def put_file(self, relpath, local_path, sha256, cancel=None):
        self.puts += 1
        self.n += 1
        file_id = "f{}".format(self.n)
        self.blobs[file_id] = {"path": relpath, "sha256": sha256, "data": Path(local_path).read_bytes()}
        return file_id

    def get_file(self, file_id, dest, cancel=None):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        Path(dest).write_bytes(self.blobs[file_id]["data"])

    def delete_file(self, file_id):
        self.blobs.pop(file_id, None)

    def load_manifest(self):
        return self.manifest

    def save_manifest(self, manifest):
        self.n += 1
        self.manifest_id = "m{}".format(self.n)
        self.manifest = manifest
        self.blobs[self.manifest_id] = {"path": "manifest.json", "data": b""}

    def list_file_ids(self):
        return list(self.blobs)


def _noop(*_args):
    return None


class PathTests(unittest.TestCase):
    def test_portable_roundtrip_and_outside_path(self):
        data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, data, True)
        full = os.path.join(data, "pdfs", "Unfiled", "a.pdf")
        portable = inventory.to_portable(full, data)
        self.assertEqual(portable, "arxivreader-data:pdfs/Unfiled/a.pdf")
        self.assertEqual(inventory.to_absolute(portable, data + "/moved"), os.path.abspath(os.path.join(data, "moved", "pdfs", "Unfiled", "a.pdf")))
        self.assertEqual(inventory.to_portable("/tmp/outside.pdf", data), "/tmp/outside.pdf")
        self.assertEqual(inventory.to_absolute("arxivreader-data:../google.json", data), "arxivreader-data:../google.json")

    def test_rewrite_known_columns_only(self):
        data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, data, True)
        db_path = os.path.join(data, "library.db")
        pdf = os.path.join(data, "pdfs", "a.pdf")
        with sqlite3_connect(db_path) as conn:
            conn.execute("CREATE TABLE papers (pdf_path TEXT)")
            conn.execute("INSERT INTO papers (pdf_path) VALUES (?)", (pdf,))
            conn.commit()
        inventory.rewrite_database(db_path, data, "portable")
        with sqlite3_connect(db_path) as conn:
            stored = conn.execute("SELECT pdf_path FROM papers").fetchone()[0]
        self.assertEqual(stored, "arxivreader-data:pdfs/a.pdf")


def sqlite3_connect(path):
    import sqlite3
    return sqlite3.connect(path)


class SyncFlowTests(unittest.TestCase):
    def setUp(self):
        self.original = config.get_base_dir()
        self.dirs = []

    def tearDown(self):
        config.set_data_dir(self.original)
        database.reopen_db()
        for path in self.dirs:
            shutil.rmtree(path, ignore_errors=True)

    def make(self):
        path = tempfile.mkdtemp(prefix="arxiv-sync-")
        self.dirs.append(path)
        config.set_data_dir(path)
        database.reopen_db()
        database.init_db()
        return path

    def fill(self, data_dir):
        pdf = os.path.join(data_dir, "pdfs", "Unfiled", "a.pdf")
        os.makedirs(os.path.dirname(pdf), exist_ok=True)
        Path(pdf).write_bytes(PDF)
        md = os.path.join(data_dir, "knowledge", "RL", "graph.md")
        os.makedirs(os.path.dirname(md), exist_ok=True)
        Path(md).write_text("# graph", encoding="utf-8")
        mono = os.path.join(data_dir, "pdftranslate", "output", "task-1", "a.mono.pdf")
        os.makedirs(os.path.dirname(mono), exist_ok=True)
        Path(mono).write_bytes(b"mono")
        Path(os.path.join(os.path.dirname(mono), "config.toml")).write_text("api_key = secret", encoding="utf-8")
        venv = os.path.join(data_dir, "pdftranslate", "venv", "bin", "tool")
        os.makedirs(os.path.dirname(venv), exist_ok=True)
        Path(venv).write_text("engine", encoding="utf-8")
        Path(os.path.join(data_dir, "sync", "google.json")).parent.mkdir(exist_ok=True)
        Path(os.path.join(data_dir, "sync", "google.json")).write_text("{}", encoding="utf-8")
        db = database.SessionLocal()
        try:
            paper = Paper(arxiv_id="2401.00001", title="Synced", pdf_path=pdf)
            db.add(paper)
            db.flush()
            db.add(DirectionNode(name="RL", path="RL", graph_md_path=md))
            db.add(PdfTranslationTask(paper_id=paper.id, status="done", mono_path=mono))
            db.commit()
        finally:
            db.close()
        return pdf

    def test_push_skips_unchanged_and_hides_engine_and_secrets(self):
        data_dir = self.make()
        pdf = self.fill(data_dir)
        store = MemoryStore()
        manifest = inventory.push_library(data_dir, store, _noop, threading.Event())
        names = set(manifest["files"])
        self.assertIn("library.db", names)
        self.assertIn("pdfs/Unfiled/a.pdf", names)
        self.assertIn("knowledge/RL/graph.md", names)
        self.assertIn("pdftranslate/output/task-1/a.mono.pdf", names)
        self.assertFalse(any(name.endswith("config.toml") or "venv" in name or name.startswith("sync/") for name in names))
        blob = store.blobs[manifest["files"]["library.db"]["id"]]["data"]
        snapped = os.path.join(data_dir, "snap-check.db")
        Path(snapped).write_bytes(blob)
        import sqlite3
        with sqlite3.connect(snapped) as conn:
            stored = conn.execute("SELECT pdf_path FROM papers").fetchone()[0]
        self.assertTrue(stored.startswith("arxivreader-data:"))
        with sqlite3.connect(os.path.join(data_dir, "library.db")) as conn:
            live = conn.execute("SELECT pdf_path FROM papers").fetchone()[0]
        self.assertEqual(live, pdf)
        pdf_id = manifest["files"]["pdfs/Unfiled/a.pdf"]["id"]
        again = inventory.push_library(data_dir, store, _noop, threading.Event())
        self.assertEqual(again["files"]["pdfs/Unfiled/a.pdf"]["id"], pdf_id)
        Path(pdf).write_bytes(PDF + b"!")
        changed = inventory.push_library(data_dir, store, _noop, threading.Event())
        self.assertNotEqual(changed["files"]["pdfs/Unfiled/a.pdf"]["id"], pdf_id)
        self.assertNotIn(pdf_id, store.blobs)
        os.remove(pdf)
        removed = inventory.push_library(data_dir, store, _noop, threading.Event())
        self.assertNotIn("pdfs/Unfiled/a.pdf", removed["files"])

    def test_pull_replaces_local_library_and_keeps_previous(self):
        source = self.make()
        self.fill(source)
        store = MemoryStore()
        inventory.push_library(source, store, _noop, threading.Event())
        target = self.make()
        local_only = os.path.join(target, "pdfs", "local-only.pdf")
        os.makedirs(os.path.dirname(local_only), exist_ok=True)
        Path(local_only).write_bytes(b"local")
        inventory.pull_library(target, store, _noop, threading.Event())
        import sqlite3
        with sqlite3.connect(os.path.join(target, "library.db")) as conn:
            pdf_path, title = conn.execute("SELECT pdf_path, title FROM papers").fetchone()
            graph = conn.execute("SELECT graph_md_path FROM direction_nodes").fetchone()[0]
            mono = conn.execute("SELECT mono_path FROM pdf_translation_tasks").fetchone()[0]
        self.assertEqual(title, "Synced")
        self.assertTrue(pdf_path.startswith(os.path.abspath(target)))
        self.assertEqual(Path(pdf_path).read_bytes(), PDF)
        self.assertTrue(graph.startswith(os.path.abspath(target)))
        self.assertEqual(Path(graph).read_text(encoding="utf-8"), "# graph")
        self.assertEqual(Path(mono).read_bytes(), b"mono")
        self.assertFalse(os.path.exists(local_only))
        self.assertTrue(os.path.isfile(os.path.join(target, "sync", "previous", "pdfs", "local-only.pdf")))
        self.assertFalse(os.path.isdir(os.path.join(target, "sync", "incoming")))

    def test_pull_rejects_bad_manifest_without_touching_local_files(self):
        target = self.make()
        marker = os.path.join(target, "pdfs", "keep.pdf")
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        Path(marker).write_bytes(b"keep")
        store = MemoryStore()
        store.manifest = {"format": 1, "app": "ArxivReader", "files": {"../x": {"id": "f1", "sha256": "aa", "size": 1}}}
        with self.assertRaises(SyncError) as caught:
            inventory.pull_library(target, store, _noop, threading.Event())
        self.assertEqual(caught.exception.code, "sync.cloud_invalid")
        self.assertEqual(Path(marker).read_bytes(), b"keep")

    def test_hash_mismatch_leaves_local_library(self):
        source = self.make()
        self.fill(source)
        store = MemoryStore()
        manifest = inventory.push_library(source, store, _noop, threading.Event())
        file_id = manifest["files"]["library.db"]["id"]
        store.blobs[file_id]["data"] = b"not-the-database"
        target = self.make()
        marker = os.path.join(target, "library.db")
        before = Path(marker).read_bytes()
        with self.assertRaises(SyncError) as caught:
            inventory.pull_library(target, store, _noop, threading.Event())
        self.assertEqual(caught.exception.code, "sync.hash_mismatch")
        self.assertEqual(Path(marker).read_bytes(), before)

    def test_cancel_does_not_publish_manifest(self):
        data_dir = self.make()
        self.fill(data_dir)
        cancel = threading.Event()

        class CancelStore(MemoryStore):
            def put_file(self, relpath, local_path, sha256, cancel_event=None):
                cancel.set()
                return super().put_file(relpath, local_path, sha256, cancel_event)

        store = CancelStore()
        with self.assertRaises(SyncError) as caught:
            inventory.push_library(data_dir, store, _noop, cancel)
        self.assertEqual(caught.exception.code, "sync.cancelled")
        self.assertIsNone(store.manifest)

    def test_credentials_stay_local_and_client_change_drops_token(self):
        self.make()
        service.save_client(CLIENT, SECRET)
        creds = credentials.load()
        creds["refresh_token"] = "refresh-token-value"
        creds["email"] = "reader@example.com"
        credentials.save(creds)
        body = service.status()
        self.assertNotIn("client_secret", body)
        self.assertNotIn("refresh_token", body)
        self.assertTrue(body["has_client_secret"])
        self.assertTrue(body["connected"])
        self.assertNotIn(SECRET, json.dumps(body))
        mode = os.stat(credentials.credential_path()).st_mode & 0o777
        self.assertEqual(mode, 0o600)
        service.save_client(OTHER, "another-secret")
        self.assertEqual(credentials.load()["refresh_token"], "")
        with self.assertRaises(SyncError):
            service.save_client("not-a-google-client", SECRET)
        service.save_client(OTHER, None)
        self.assertEqual(credentials.load()["client_secret"], "another-secret")

    def test_busy_library_refuses_push(self):
        self.make()
        service.save_client(CLIENT, SECRET)
        creds = credentials.load()
        creds["refresh_token"] = "refresh-token-value"
        credentials.save(creds)
        db = database.SessionLocal()
        try:
            paper = Paper(arxiv_id="2401.9", title="Busy")
            db.add(paper)
            db.flush()
            db.add(PipelineRun(paper_id=paper.id, status="running"))
            db.commit()
        finally:
            db.close()
        with self.assertRaises(SyncError) as caught:
            service.start_push()
        self.assertEqual(caught.exception.code, "sync.busy")
        job = service.public_job()
        self.assertTrue(job is None or not job["running"])

    def test_start_job_returns_without_waiting_for_the_worker(self):
        self.make()
        started = threading.Event()

        def work(job, cancel):
            started.set()

        caller = threading.Thread(target=lambda: service._start("push", work))
        caller.start()
        caller.join(2)
        self.assertFalse(caller.is_alive())
        self.assertTrue(started.wait(2))
        for _ in range(50):
            job = service.public_job()
            if job and job["finished"]:
                return
            time.sleep(0.02)
        self.fail("job did not finish")

    def test_routes_hide_secret_and_require_confirm(self):
        self.make()
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as client:
            saved = client.put("/api/sync/client", json={"client_id": CLIENT, "client_secret": SECRET})
            self.assertEqual(saved.status_code, 200)
            self.assertNotIn(SECRET, saved.text)
            self.assertNotIn("refresh_token", saved.json())
            denied = client.post("/api/sync/pull", json={})
            self.assertEqual(denied.status_code, 400)
            self.assertEqual(denied.json()["detail"]["code"], "sync.confirm_required")
            invalid = client.put("/api/sync/client", json={"client_id": "bad", "client_secret": SECRET})
            self.assertEqual(invalid.status_code, 422)


class GoogleClientTests(unittest.TestCase):
    def test_authorize_url_uses_pkce_and_appdata(self):
        verifier, challenge = google_drive.pkce_pair()
        url = google_drive.build_authorize_url("client", "http://127.0.0.1:9/oauth2callback", "state", challenge)
        self.assertIn("code_challenge=" + challenge, url)
        self.assertIn("code_challenge_method=S256", url)
        self.assertIn("drive.appdata", url)
        self.assertNotIn(verifier, url)
        self.assertIn("prompt=consent", url)

    def test_callback_receives_code(self):
        httpd, redirect = google_drive.bind_oauth_server()
        box = {}

        def serve():
            try:
                box["result"] = google_drive.serve_oauth(httpd, threading.Event(), timeout=5)
            except Exception as exc:  # noqa: BLE001  测试线程把异常带回来
                box["error"] = exc

        thread = threading.Thread(target=serve)
        thread.start()
        httpx.get(redirect + "?code=4/0AbC-def_token&state=state1", timeout=5)
        thread.join(5)
        self.assertNotIn("error", box)
        self.assertEqual(box["result"]["code"], "4/0AbC-def_token")
        self.assertEqual(box["result"]["state"], "state1")

    def test_upload_and_download_use_app_folder(self):
        seen = []

        def handler(request):
            seen.append(request)
            if request.url.host == "oauth2.googleapis.com":
                return httpx.Response(200, json={"access_token": "ya29.test", "expires_in": 3600})
            if request.url.host == "upload.example":
                return httpx.Response(200, json={"id": "big1"})
            if "uploadType=multipart" in str(request.url):
                return httpx.Response(200, json={"id": "file1"})
            if "uploadType=resumable" in str(request.url):
                return httpx.Response(200, headers={"Location": "https://upload.example/session"}, json={})
            if request.url.params.get("alt") == "media":
                return httpx.Response(200, content=b"%PDF")
            if request.method == "GET":
                return httpx.Response(200, json={"files": []})
            if request.method == "POST":
                body = json.loads(request.content.decode())
                return httpx.Response(200, json={"id": "root" if body.get("parents") == ["appDataFolder"] else "pdfs"})
            return httpx.Response(500, text="unexpected")

        transport = httpx.MockTransport(handler)
        http = httpx.Client(transport=transport)
        path = os.path.join(tempfile.mkdtemp(), "google.json")
        creds = credentials.blank()
        creds.update(client_id=CLIENT, client_secret=SECRET, refresh_token="refresh")
        store = google_drive.GoogleStore(creds, path, http=http)
        folder = store.ensure_root()
        self.assertEqual(folder, "root")
        self.assertEqual(creds["folder_id"], "root")
        small = os.path.join(os.path.dirname(path), "a.pdf")
        Path(small).write_bytes(b"%PDF-small")
        file_id = store.put_file("pdfs/a.pdf", small, hashlib.sha256(b"%PDF-small").hexdigest())
        self.assertEqual(file_id, "file1")
        upload = [req for req in seen if "uploadType=multipart" in str(req.url)][0]
        body = upload.content.decode()
        self.assertIn('"parents": ["pdfs"]', body)
        created = [json.loads(req.content.decode()) for req in seen if req.method == "POST" and req.url.path == "/drive/v3/files"]
        self.assertEqual(created[0]["parents"], ["appDataFolder"])
        self.assertIn("sha256", body)
        dest = os.path.join(os.path.dirname(path), "down.pdf")
        store.get_file("abc", dest)
        self.assertEqual(Path(dest).read_bytes(), b"%PDF")
        big = os.path.join(os.path.dirname(path), "big.pdf")
        Path(big).write_bytes(b"abc")
        store.upload_limit = 0
        self.assertEqual(store.put_file("big.pdf", big, "abc"), "big1")
        put = [req for req in seen if req.url.host == "upload.example"][0]
        self.assertEqual(put.headers["Content-Range"], "bytes 0-2/3")
        store.close()


if __name__ == "__main__":
    unittest.main()
