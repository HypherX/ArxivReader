"""只覆盖 Zotero 导入：映射、分页、增量、重试，以及密钥不回传。"""

import json
import os
import tempfile
import time

from harness.config import set_data_dir

DATA_DIR = tempfile.mkdtemp(prefix="arxivreader-zotero-")
set_data_dir(DATA_DIR)

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import database, zotero_sync
from app.models import Folder, Paper
from app.routers import zotero as zotero_router

API_KEY = "Abcd1234Abcd1234Abcd"
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"

api = FastAPI()
api.include_router(zotero_router.router)


@pytest.fixture(scope="module", autouse=True)
def _database():
    database.reopen_db()
    database.init_db()


@pytest.fixture(autouse=True)
def _clean():
    _reset()
    yield
    _reset()


def _reset():
    db = database.SessionLocal()
    try:
        db.query(Paper).delete(synchronize_session=False)
        db.query(Folder).delete(synchronize_session=False)
        from app.models import Setting
        db.query(Setting).filter(Setting.key.like("zotero_%")).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    path = zotero_sync._creds_path()
    if os.path.isfile(path):
        os.remove(path)
    with zotero_sync._LOCK:
        zotero_sync._JOB.update({
            "id": 0, "running": False, "finished": False, "ok": False, "phase": "",
            "done": 0, "total": 0, "imported": 0, "skipped": 0, "failed": 0, "with_pdf": 0,
            "error": "", "error_params": {},
        })


def _item(key, title, collections=None, **data):
    body = {"key": key, "itemType": "preprint", "title": title, "collections": collections or []}
    body.update(data)
    return {"key": key, "data": body}


def _collection(key, name, parent=""):
    return {"key": key, "data": {"key": key, "name": name, "parentCollection": parent or False}}


class FakeZotero:
    def __init__(self, items, collections, extra_items=None):
        self.items = items
        self.collections = collections
        self.extra_items = extra_items or []
        self.calls = []
        self.fail_children = {}

    def handler(self, request):
        self.calls.append(request)
        assert request.headers["Zotero-API-Key"] == API_KEY
        assert request.headers["Zotero-API-Version"] == "3"
        parts = [part for part in request.url.path.split("/") if part]
        since = request.url.params.get("since")
        start = int(request.url.params.get("start") or 0)
        limit = int(request.url.params.get("limit") or 100)
        version = "4" if since else "3"
        if parts[:2] == ["keys", "CURRENT"]:
            return httpx.Response(200, json={
                "userID": 42, "username": "ada",
                "access": {"user": {"library": True, "files": True}},
            })
        if parts[2:] == ["collections"]:
            rows = [] if since else self.collections
            return _page(rows, start, limit, version)
        if parts[2:] == ["items"]:
            rows = self.extra_items if since else self.items
            return _page(rows, start, limit, version)
        if len(parts) == 5 and parts[4] == "children":
            key = parts[3]
            left = self.fail_children.get(key, 0)
            if left:
                self.fail_children[key] = left - 1
                return httpx.Response(500, json={"error": "down"})
            if key == "ITEMARXIV":
                rows = [{"key": "FILEARXIV", "data": {
                    "key": "FILEARXIV", "itemType": "attachment", "linkMode": "imported_file",
                    "contentType": "application/pdf", "filename": "attention.pdf",
                }}]
            else:
                rows = []
            return _page(rows, start, limit, version)
        if len(parts) == 4 and parts[2] == "items":
            key = parts[3]
            for row in self.items + self.extra_items:
                if row["key"] == key:
                    return httpx.Response(200, json=row, headers={"Last-Modified-Version": version})
            return httpx.Response(404, json={})
        if len(parts) == 5 and parts[4] == "file":
            return httpx.Response(200, content=PDF, headers={"Last-Modified-Version": version})
        return httpx.Response(404, json={"path": request.url.path})


def _page(rows, start, limit, version):
    batch = rows[start:start + limit]
    return httpx.Response(200, json=batch, headers={
        "Last-Modified-Version": version,
        "Total-Results": str(len(rows)),
    })


def _client(fake, user_id="42", page_size=1):
    return zotero_sync.ZoteroClient(
        API_KEY, user_id, transport=httpx.MockTransport(fake.handler), page_size=page_size)


def _library():
    note = _item("ITEMNOTE1", "just a note")
    note["data"]["itemType"] = "note"
    items = [
        _item(
            "ITEMARXIV", "Attention Is All You Need", collections=["COLLLLM1"],
            creators=[{"creatorType": "author", "firstName": "Ashish", "lastName": "Vaswani"}],
            abstractNote="tokens", date="2017-06-12",
            url="https://arxiv.org/abs/1706.03762v3", extra="arXiv: 1706.03762",
        ),
        _item("ITEMPLAIN", "A note I wrote", abstractNote="local thought"),
        note,
    ]
    # 子分类排在父分类前面，用来确认导入时会先建父文件夹。
    collections = [
        _collection("COLLLLM1", "LLM", parent="COLLREAD"),
        _collection("COLLREAD", "Reading"),
    ]
    return FakeZotero(items, collections, extra_items=[
        _item("NEWITEM01", "Imported later"),
    ])


def test_extract_arxiv_id_from_common_zotero_fields():
    assert zotero_sync.extract_arxiv_id(
        {"archive": "arXiv", "archiveLocation": "1706.03762v2"}) == "1706.03762"
    assert zotero_sync.extract_arxiv_id(
        {"url": "https://arxiv.org/pdf/2401.12345.pdf"}) == "2401.12345"
    assert zotero_sync.extract_arxiv_id({"DOI": "10.48550/arXiv.2401.12345"}) == "2401.12345"
    assert zotero_sync.extract_arxiv_id({"extra": "arXiv: math/0211159"}) == "math/0211159"
    assert zotero_sync.extract_arxiv_id({"title": "no id here", "url": "https://example.com"}) == ""


def test_import_pages_collections_and_pdf(monkeypatch):
    monkeypatch.setattr(zotero_sync.pdf_service, "extract_text", lambda path: "hello from pdf")
    def _no_arxiv(*_args, **_kwargs):
        raise AssertionError("已有 Zotero PDF 时不应再请求 arXiv")
    monkeypatch.setattr(zotero_sync, "fetch_arxiv_pdf", _no_arxiv)

    fake = _library()
    db = database.SessionLocal()
    try:
        stats = zotero_sync.import_library(db, _client(fake))
        assert stats["imported"] == 2
        assert stats["skipped"] == 1
        assert stats["failed"] == 0
        assert stats["with_pdf"] == 1

        arxiv_paper = db.query(Paper).filter_by(arxiv_id="1706.03762").one()
        assert arxiv_paper.title == "Attention Is All You Need"
        assert json.loads(arxiv_paper.authors_json) == ["Ashish Vaswani"]
        assert arxiv_paper.abstract == "tokens"
        assert arxiv_paper.full_text == "hello from pdf"
        assert arxiv_paper.source == "arxiv"
        assert arxiv_paper.pdf_path and open(arxiv_paper.pdf_path, "rb").read().startswith(b"%PDF-")
        llm = db.get(Folder, arxiv_paper.folder_id)
        reading = db.get(Folder, llm.parent_id)
        root = db.get(Folder, reading.parent_id)
        assert (llm.name, reading.name, root.name) == ("LLM", "Reading", "Zotero")

        plain = db.query(Paper).filter_by(arxiv_id="zotero-ITEMPLAIN").one()
        assert plain.source == "zotero"
        assert plain.folder_id == root.id
        assert plain.pdf_path is None
        assert db.query(Paper).filter(Paper.title == "just a note").count() == 0
        assert zotero_sync._version(db) == 3
        starts = [int(call.url.params.get("start") or 0) for call in fake.calls
                  if call.url.path.endswith("/items") and "children" not in call.url.path]
        assert starts[:2] == [0, 1]
    finally:
        db.close()


def test_existing_arxiv_paper_is_not_overwritten(monkeypatch):
    monkeypatch.setattr(zotero_sync.pdf_service, "extract_text", lambda path: "x")
    monkeypatch.setattr(zotero_sync, "fetch_arxiv_pdf", lambda *_a, **_k: "")
    db = database.SessionLocal()
    try:
        db.add(Paper(
            arxiv_id="1706.03762", title="already here", authors_json="[]",
            abstract="keep me", categories_json="[]",
        ))
        db.commit()
        stats = zotero_sync.import_library(db, _client(_library()))
        assert stats["imported"] == 1
        assert stats["skipped"] == 2
        kept = db.query(Paper).filter_by(arxiv_id="1706.03762").one()
        assert kept.title == "already here"
        assert kept.abstract == "keep me"
    finally:
        db.close()


def test_second_sync_uses_library_version(monkeypatch):
    monkeypatch.setattr(zotero_sync.pdf_service, "extract_text", lambda path: "x")
    monkeypatch.setattr(zotero_sync, "fetch_arxiv_pdf", lambda *_a, **_k: "")
    fake = _library()
    db = database.SessionLocal()
    try:
        zotero_sync.import_library(db, _client(fake))
        fake.calls.clear()
        stats = zotero_sync.import_library(db, _client(fake))
        assert stats == {"imported": 1, "skipped": 0, "failed": 0, "with_pdf": 0}
        assert db.query(Paper).filter_by(arxiv_id="zotero-NEWITEM01").one().title == "Imported later"
        since_values = {call.url.params.get("since") for call in fake.calls}
        assert "3" in since_values
        assert zotero_sync._version(db) == 4
        assert not any(call.url.path.endswith("/file") for call in fake.calls)
    finally:
        db.close()


def test_failed_attachment_is_retried_next_sync(monkeypatch):
    monkeypatch.setattr(zotero_sync.pdf_service, "extract_text", lambda path: "retried")
    monkeypatch.setattr(zotero_sync, "fetch_arxiv_pdf", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("失败的附件应留待重试，而不是改去 arXiv")))
    fake = _library()
    fake.extra_items = []
    fake.fail_children["ITEMARXIV"] = 1
    db = database.SessionLocal()
    try:
        first = zotero_sync.import_library(db, _client(fake))
        assert first["failed"] == 1
        assert db.query(Paper).filter_by(arxiv_id="1706.03762").count() == 0
        assert json.loads(zotero_sync._get_setting(db, "zotero_retry_keys")) == ["ITEMARXIV"]

        second = zotero_sync.import_library(db, _client(fake))
        assert second["imported"] == 1
        assert second["failed"] == 0
        paper = db.query(Paper).filter_by(arxiv_id="1706.03762").one()
        assert paper.full_text == "retried"
        assert json.loads(zotero_sync._get_setting(db, "zotero_retry_keys")) == []
    finally:
        db.close()


def test_switching_user_does_not_keep_the_old_cursor():
    db = database.SessionLocal()
    try:
        zotero_sync._set_setting(db, "zotero_user_id", "1")
        zotero_sync._set_setting(db, "zotero_library_version", "99")
        db.commit()
        fake = _library()
        zotero_sync.import_library(db, _client(fake))
        assert all(call.url.params.get("since") != "99" for call in fake.calls)
        assert db.query(Paper).filter_by(arxiv_id="1706.03762").count() == 1
        assert zotero_sync._get_setting(db, "zotero_user_id") == "42"
    finally:
        db.close()


def test_missing_zotero_pdf_falls_back_to_arxiv(monkeypatch):
    def _fetch(arxiv_id, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        path = os.path.join(dest_dir, arxiv_id.replace("/", "_") + ".pdf")
        with open(path, "wb") as fh:
            fh.write(PDF)
        return path
    monkeypatch.setattr(zotero_sync, "fetch_arxiv_pdf", _fetch)
    monkeypatch.setattr(zotero_sync.pdf_service, "extract_text", lambda path: "from arxiv")
    item = _item("ITEMLINK1", "Only a link", url="https://arxiv.org/abs/2401.00001")
    item["data"]["collections"] = []
    # 只有本机链接、没有存进 Zotero 的 PDF。
    fake = FakeZotero([item], [])
    db = database.SessionLocal()
    try:
        stats = zotero_sync.import_library(db, _client(fake, page_size=100))
        assert stats["with_pdf"] == 1
        paper = db.query(Paper).filter_by(arxiv_id="2401.00001").one()
        assert paper.full_text == "from arxiv"
        assert not any(call.url.path.endswith("/file") for call in fake.calls)
    finally:
        db.close()


def test_verify_key_requires_library_access():
    def handler(request):
        return httpx.Response(200, json={"userID": 7, "username": "ada", "access": {"user": {"library": False}}})
    with pytest.raises(zotero_sync.ZoteroError) as caught:
        zotero_sync.verify_key(API_KEY, transport=httpx.MockTransport(handler))
    assert caught.value.code == "zotero.no_library"

    def denied(_request):
        return httpx.Response(401, json={})
    with pytest.raises(zotero_sync.ZoteroError) as caught:
        zotero_sync.verify_key(API_KEY, transport=httpx.MockTransport(denied))
    assert caught.value.code == "zotero.unauthorized"


def test_status_and_routes_do_not_return_the_api_key(monkeypatch):
    monkeypatch.setattr(
        zotero_sync, "verify_key",
        lambda key, transport=None: {"user_id": "42", "username": "ada"},
    )
    monkeypatch.setattr(
        zotero_sync, "import_library",
        lambda db, client, on_progress=None: {"imported": 2, "skipped": 1, "failed": 0, "with_pdf": 2},
    )
    client = TestClient(api)
    missing = client.post("/api/zotero/sync")
    assert missing.status_code == 400
    assert missing.json()["detail"]["code"] == "zotero.not_configured"

    bad = client.put("/api/zotero/key", json={"api_key": "not a key"})
    assert bad.status_code == 422
    assert bad.json()["detail"]["code"] == "zotero.invalid_key"

    saved = client.put("/api/zotero/key", json={"api_key": API_KEY})
    assert saved.status_code == 200
    assert API_KEY not in saved.text
    assert saved.json()["configured"] is True
    assert saved.json()["username"] == "ada"

    started = client.post("/api/zotero/sync")
    assert started.status_code == 200
    view = started.json()
    for _ in range(50):
        if not view["job"]["running"]:
            break
        time.sleep(0.05)
        view = client.get("/api/zotero").json()
    assert view["job"]["ok"] is True
    assert view["job"]["imported"] == 2
    assert API_KEY not in json.dumps(view)

    forgotten = client.delete("/api/zotero/key")
    assert forgotten.status_code == 200
    assert forgotten.json()["configured"] is False
    assert API_KEY not in forgotten.text
