"""API 集成测试：用临时 SQLite + mock 的 arxiv/pdf/llm，覆盖文件夹、论文入库、
规则、设置、对话流式（SSE）等关键路径。不触网、不依赖真实 LLM。
"""

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import arxiv_service, config_loader, database, llm_client, pdf_service, settings_store
from app.database import Base
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个用例独立的临时数据库；重绑 database.engine / SessionLocal 供 chat 持久化使用。"""
    engine = create_engine(
        "sqlite:///" + str(tmp_path / "test.db"),
        connect_args={"check_same_thread": False}, future=True,
    )
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", TestingSession)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[database.get_db] = override_get_db
    db = TestingSession()
    try:
        database.ensure_inbox(db)
        settings_store.seed_from_config(db)
    finally:
        db.close()

    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def mock_arxiv(monkeypatch, tmp_path):
    """mock 掉网络相关的元数据获取、PDF 下载与文本抽取。"""
    monkeypatch.setattr(config_loader, "get_pdf_dir", lambda: str(tmp_path / "pdfs"))

    def fake_fetch(arxiv_id):
        return arxiv_service.PaperMeta(
            arxiv_id=arxiv_service.strip_version(arxiv_id),
            title="Test Paper on Diffusion Models",
            authors=["Alice Smith", "Bob Lee"],
            abstract="We study diffusion models for generation.",
            categories=["cs.LG", "cs.CV"],
            published="2024-01-01T00:00:00",
            pdf_url="http://example.com/x.pdf",
            result=object(),
        )

    def fake_download(meta, dest_dir, filename=None):
        os.makedirs(dest_dir, exist_ok=True)
        p = Path(dest_dir) / (filename or (meta.arxiv_id.replace("/", "_") + ".pdf"))
        p.write_bytes(b"%PDF-1.4 fake content")
        return p

    monkeypatch.setattr(arxiv_service, "fetch_metadata", fake_fetch)
    monkeypatch.setattr(arxiv_service, "download_pdf", fake_download)
    monkeypatch.setattr(pdf_service, "extract_text_for_context",
                        lambda path, max_chars=120000: ("Full text of the paper.", False))


# ---------------- 基础 ----------------
def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_inbox_seeded(client):
    names = [n["name"] for n in client.get("/api/folders").json()]
    assert "Inbox" in names


# ---------------- 文件夹 ----------------
def test_folder_crud(client):
    fid = client.post("/api/folders", json={"name": "NLP"}).json()["id"]
    cid = client.post("/api/folders", json={"name": "Sub", "parent_id": fid}).json()["id"]

    r = client.patch("/api/folders/{}".format(fid), json={"name": "NLP2"})
    assert r.json()["name"] == "NLP2"

    tree = client.get("/api/folders").json()
    node = [n for n in tree if n["id"] == fid][0]
    assert node["name"] == "NLP2"
    assert len(node["children"]) == 1 and node["children"][0]["id"] == cid

    assert client.delete("/api/folders/{}".format(cid)).status_code == 204

    inbox = [n for n in client.get("/api/folders").json() if n["name"] == "Inbox"][0]
    assert client.delete("/api/folders/{}".format(inbox["id"])).status_code == 400


def test_folder_cannot_move_into_own_descendant(client):
    a = client.post("/api/folders", json={"name": "A"}).json()["id"]
    b = client.post("/api/folders", json={"name": "B", "parent_id": a}).json()["id"]
    r = client.patch("/api/folders/{}".format(a), json={"parent_id": b})
    assert r.status_code == 400


# ---------------- 论文入库 / 归档 ----------------
def test_add_paper_to_inbox(client, mock_arxiv):
    r = client.post("/api/papers/from-arxiv", json={"url": "https://arxiv.org/abs/2401.99999"})
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["arxiv_id"] == "2401.99999"
    assert data["folder_name"] == "Inbox"
    assert data["has_text"] is True
    assert data["authors"] == ["Alice Smith", "Bob Lee"]
    # 去重
    dup = client.post("/api/papers/from-arxiv", json={"url": "2401.99999"})
    assert dup.status_code == 409


def test_add_paper_by_rule(client, mock_arxiv):
    fid = client.post("/api/folders", json={"name": "Vision"}).json()["id"]
    client.post("/api/rules", json={"name": "lg", "match_type": "category",
                                    "pattern": "cs.LG", "folder_id": fid, "priority": 10})
    r = client.post("/api/papers/from-arxiv", json={"url": "2402.11111"})
    assert r.status_code == 201
    assert r.json()["folder_id"] == fid
    assert r.json()["folder_name"] == "Vision"


def test_add_paper_explicit_folder(client, mock_arxiv):
    fid = client.post("/api/folders", json={"name": "Manual"}).json()["id"]
    r = client.post("/api/papers/from-arxiv", json={"url": "2407.77777", "folder_id": fid})
    assert r.json()["folder_id"] == fid


def test_add_paper_invalid_url(client):
    r = client.post("/api/papers/from-arxiv", json={"url": "https://example.com/nope"})
    assert r.status_code == 400


def test_list_filter_and_search(client, mock_arxiv):
    client.post("/api/papers/from-arxiv", json={"url": "2401.10001"})
    client.post("/api/papers/from-arxiv", json={"url": "2401.10002"})
    assert len(client.get("/api/papers").json()) == 2
    assert len(client.get("/api/papers?q=Diffusion").json()) == 2
    assert len(client.get("/api/papers?q=zzz-no-match").json()) == 0


def test_pdf_and_text_endpoints(client, mock_arxiv):
    pid = client.post("/api/papers/from-arxiv", json={"url": "2403.22222"}).json()["id"]
    r = client.get("/api/papers/{}/pdf".format(pid))
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    t = client.get("/api/papers/{}/text".format(pid)).json()
    assert "Full text" in t["text"] and t["truncated"] is False


def test_move_and_delete_paper(client, mock_arxiv):
    pid = client.post("/api/papers/from-arxiv", json={"url": "2406.55555"}).json()["id"]
    fid = client.post("/api/folders", json={"name": "Target"}).json()["id"]
    assert client.patch("/api/papers/{}".format(pid), json={"folder_id": fid}).json()["folder_id"] == fid
    assert client.delete("/api/papers/{}".format(pid)).status_code == 204
    assert client.get("/api/papers/{}".format(pid)).status_code == 404


def test_text_chars_reported(client, mock_arxiv):
    p = client.post("/api/papers/from-arxiv", json={"url": "2409.70001"}).json()
    assert p["has_text"] is True
    assert p["text_chars"] == len("Full text of the paper.")


def test_reextract_restores_full_text(client, mock_arxiv, monkeypatch):
    # 入库时抽取失败 -> full_text 为空
    monkeypatch.setattr(pdf_service, "extract_text_for_context",
                        lambda path, max_chars=120000: ("", False))
    pid = client.post("/api/papers/from-arxiv", json={"url": "2409.70002"}).json()["id"]
    assert client.get("/api/papers/{}".format(pid)).json()["has_text"] is False

    # 修复抽取后重抽 -> 全文恢复
    monkeypatch.setattr(pdf_service, "extract_text_for_context",
                        lambda path, max_chars=120000: ("Recovered body text.", False))
    r = client.post("/api/papers/{}/reextract".format(pid))
    assert r.status_code == 200, r.text
    assert r.json()["has_text"] is True
    assert r.json()["text_chars"] == len("Recovered body text.")


def test_reextract_fails_when_still_empty(client, mock_arxiv, monkeypatch):
    monkeypatch.setattr(pdf_service, "extract_text_for_context",
                        lambda path, max_chars=120000: ("", False))
    pid = client.post("/api/papers/from-arxiv", json={"url": "2409.70003"}).json()["id"]
    r = client.post("/api/papers/{}/reextract".format(pid))
    assert r.status_code == 502


# ---------------- 规则 ----------------
def test_rules_crud(client):
    fid = client.post("/api/folders", json={"name": "R"}).json()["id"]
    r = client.post("/api/rules", json={"match_type": "keyword", "pattern": "gan",
                                        "folder_id": fid, "priority": 5})
    assert r.status_code == 201
    rid = r.json()["id"]
    assert r.json()["folder_name"] == "R"
    assert client.patch("/api/rules/{}".format(rid), json={"enabled": False}).json()["enabled"] is False
    assert any(x["id"] == rid for x in client.get("/api/rules").json())
    assert client.delete("/api/rules/{}".format(rid)).status_code == 204


# ---------------- 设置 ----------------
def test_settings_get_and_update(client):
    s = client.get("/api/settings").json()
    assert "base_url" in s and "model" in s and "has_api_key" in s and "api_key_preview" in s
    # 完整 key 不应出现在响应里
    assert "sk-xxxxxxxxxxxxxxxxxxxxxxxx" not in json.dumps(s)

    r = client.put("/api/settings", json={"model": "test-model", "temperature": 0.9})
    assert r.json()["model"] == "test-model"
    assert r.json()["temperature"] == 0.9
    # 空 api_key 不应清除已有 key
    r2 = client.put("/api/settings", json={"api_key": "", "model": "m2"})
    assert r2.json()["has_api_key"] is True


# ---------------- 对话（SSE 流式） ----------------
def test_chat_stream(client, mock_arxiv, monkeypatch):
    def fake_stream(settings, messages):
        for piece in ["Hello", ", ", "world"]:
            yield piece

    monkeypatch.setattr(llm_client, "chat_stream", fake_stream)

    pid = client.post("/api/papers/from-arxiv", json={"url": "2404.33333"}).json()["id"]
    sid = client.post("/api/papers/{}/sessions".format(pid)).json()["id"]

    r = client.post("/api/sessions/{}/messages".format(sid), json={"content": "What is this about?"})
    assert r.status_code == 200
    body = r.text
    assert "delta" in body and "Hello" in body and "world" in body
    assert "done" in body

    msgs = client.get("/api/sessions/{}/messages".format(sid)).json()
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["content"] == "Hello, world"

    # 会话标题应更新为首条用户消息
    sessions = client.get("/api/papers/{}/sessions".format(pid)).json()
    assert sessions[0]["title"].startswith("What is this")


def test_chat_persists_history_across_turns(client, mock_arxiv, monkeypatch):
    calls = {"n": 0}

    def fake_stream(settings, messages):
        calls["n"] += 1
        # messages 应含 system + 之前所有轮次
        assert messages[0]["role"] == "system"
        yield "reply{}".format(calls["n"])

    monkeypatch.setattr(llm_client, "chat_stream", fake_stream)
    pid = client.post("/api/papers/from-arxiv", json={"url": "2408.12121"}).json()["id"]
    sid = client.post("/api/papers/{}/sessions".format(pid)).json()["id"]
    client.post("/api/sessions/{}/messages".format(sid), json={"content": "q1"})
    client.post("/api/sessions/{}/messages".format(sid), json={"content": "q2"})
    msgs = client.get("/api/sessions/{}/messages".format(sid)).json()
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert msgs[3]["content"] == "reply2"
