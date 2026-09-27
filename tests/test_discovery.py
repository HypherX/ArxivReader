"""检索 Pipeline / 待读缓冲区 / 图谱 md 测试（全部离线：arXiv 与 LLM 一律 monkeypatch）。"""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import arxiv_service, database
from app.database import Base
from app.main import app
from app.models import BufferPaper, Folder, Paper, PaperRelation
from harness import llm
from harness.runner import SkillResult
from knowledge import arxiv_search, discovery, graphdoc, store
from knowledge.llm_steps import StepOutput

_PAPER_TEXT = "[Page 1]\nAbstract\nWe study credit assignment.\n1 Method\nGroup-relative advantage.\n"


def _mk_paper(db, arxiv_id="2401.00001", title="Credit Assignment for Agentic RL"):
    paper = Paper(arxiv_id=arxiv_id, title=title, abstract="group-relative credit assignment",
                  authors_json=json.dumps(["A. Author"], ensure_ascii=False),
                  categories_json=json.dumps(["cs.LG"], ensure_ascii=False),
                  published="2024-01-01T00:00:00", full_text=_PAPER_TEXT)
    db.add(paper)
    db.commit()
    db.refresh(paper)
    return paper


def _hit(arxiv_id, title, score_hint=""):
    return arxiv_search.SearchHit(arxiv_id=arxiv_id, title=title,
                                  abstract="A group-relative estimator for credit assignment " + score_hint,
                                  authors=["A. Author"], categories=["cs.LG"],
                                  published="2026-09-10T00:00:00")


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "discovery.db"),
                           connect_args={"check_same_thread": False}, future=True)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    session = Session()
    # 图谱 md 落到临时目录，避免污染 data/
    monkeypatch.setattr("app.config_loader.get_base_dir", lambda: str(tmp_path))
    try:
        yield session
    finally:
        session.close()


# ---------------------------------------------------------------- 检索表达式
def test_build_query_variants():
    assert arxiv_search.build_query(["cs.CL", "cs.LG"], []) == "(cat:cs.CL OR cat:cs.LG)"
    query = arxiv_search.build_query([], ["credit assignment"], "2026-09-01", "2026-09-20")
    # 多词关键词拆成 abs: 词与词的 AND（实测带引号的短语形式会返回 0 条）
    assert "(abs:credit AND abs:assignment)" in query
    assert "submittedDate:[202609010000 TO 202609202359]" in query      # 必须带字段名
    assert arxiv_search.build_query([], []) == ""
    # 关键词之间默认取交集（AND），可切为并集（OR）
    both = arxiv_search.build_query(["cs.AI"], ["rl", "agent"])
    assert "(abs:rl)" in both and "(abs:agent)" in both and " AND " in both
    either = arxiv_search.build_query(["cs.AI"], ["rl", "agent"], keyword_mode="or")
    assert "(abs:rl)" in either and " OR " in either
    # 语法保留字符被剔除，避免用户输入变成查询表达式
    assert "abs:rl" in arxiv_search.build_query([], ["rl) OR cat:*"])
    assert "cat:*" not in arxiv_search.build_query([], ["rl) OR cat:*"])


def test_search_requires_condition(monkeypatch):
    with pytest.raises(ValueError):
        arxiv_search.search([], [])


# ---------------------------------------------------------------- 缓冲区与检索 run 存储
def test_buffer_upsert_dedupe_and_stats(db):
    row = store.upsert_buffer_paper(db, _hit("2609.00001", "Paper A"), match_score=0.8,
                                    suggested_path="LLM/RL", screen_reason="r1")
    again = store.upsert_buffer_paper(db, _hit("2609.00001", "Paper A v2"), match_score=0.9)
    assert row.id == again.id and again.title == "Paper A v2" and again.match_score == 0.9
    store.upsert_buffer_paper(db, _hit("2609.00002", "Paper B"), match_score=0.2)
    assert len(store.list_buffer(db)) == 2
    assert store.list_buffer(db, q="Paper B")[0].arxiv_id == "2609.00002"
    store.set_buffer_status(db, again, store.BUFFER_REJECTED)
    counts = store.buffer_stats(db)
    assert counts[store.BUFFER_PENDING] == 1 and counts[store.BUFFER_REJECTED] == 1
    assert counts["total"] == 2
    assert store.list_buffer(db, status="all")[0].match_score == 0.9      # 按分数倒序


def test_search_run_logs_and_payload(db):
    run = store.create_search_run(db, {"categories": ["cs.LG"]})
    for index in range(5):
        store.log_search(db, run, "第 {} 条日志".format(index), arxiv_id="2609.0000{}".format(index))
    store.update_search_run(db, run, stage="screen", counters={"found": 5, "screened": 3})
    payload = store.search_run_payload(run, log_tail=3)
    assert payload["stage"] == "screen"
    assert payload["counters"]["found"] == 5 and payload["counters"]["screened"] == 3
    assert len(payload["logs"]) == 3 and payload["log_total"] == 5
    assert payload["logs"][-1]["message"] == "第 4 条日志"


# ---------------------------------------------------------------- 检索 pipeline 编排
_PREFILTER = {"items": [
    {"id": 1, "score": 0.9, "path": "LLM/RL/Credit Assignment", "reason": "group-relative 估计"},
    {"id": 2, "score": 0.7, "path": "LLM/RL", "reason": "RL 相关但子问题不同"},
    {"id": 3, "score": 0.1, "path": "", "reason": "无关"},
]}


def _patch_discovery(monkeypatch, hits, *, verify_fit=True, cancel_after=None):
    calls = {"prefilter": 0, "verify": 0, "summary": 0}

    monkeypatch.setattr(discovery.arxiv_search, "search", lambda *a, **k: list(hits))

    def fake_call(prompt_name, user_text, settings, *, as_json=True, cfg=None, json_retries=2,
                  should_stop=None):
        if prompt_name == "discovery_prefilter.md":
            calls["prefilter"] += 1
            return StepOutput(ok=True, data=_PREFILTER, usage={"total_tokens": 100})
        calls["verify"] += 1
        return StepOutput(ok=True, data={"fit": verify_fit, "path": "LLM/RL/Credit Assignment",
                                         "confidence": 0.88, "reason": "核心贡献属于该子问题"},
                          usage={"total_tokens": 50})

    def fake_skill(skill, paper, settings, **kw):
        calls["summary"] += 1
        return SkillResult(skill="quick_summary",
                           output="TL;DR: x\n- 解决了什么: 用 group-relative 估计降方差\n"
                                  "- 未来展望: 扩展到多智能体",
                           reasoning_content="思考", stats={"usage": {"total_tokens": 30},
                                                          "model": "m", "reasoning_effort": "max"})

    monkeypatch.setattr(discovery.llm_steps, "call_structured", fake_call)
    monkeypatch.setattr(discovery.runner, "run_skill", fake_skill)
    return calls


def test_search_pipeline_buffers_only_verified(db, monkeypatch):
    hits = [_hit("2609.10001", "Credit assignment A"), _hit("2609.10002", "RL paper B"),
            _hit("2609.10003", "Unrelated C")]
    calls = _patch_discovery(monkeypatch, hits)
    request = {"categories": ["cs.LG"], "max_results": 3, "min_score": 0.5, "max_summarize": 5}

    run = store.create_search_run(db, request)
    outcome = discovery.run_search_pipeline(db, request, run=run,
                                           settings=llm.LLMSettings(model="m"),
                                           kcfg={"discovery_batch_size": 8, "discovery_min_score": 0.5,
                                                 "discovery_max_summarize": 5, "max_tree_paths": 50},
                                           hcfg={})
    assert outcome.status == "done"
    assert outcome.counters["found"] == 3 and outcome.counters["passed"] == 2
    assert outcome.counters["dropped"] == 1 and outcome.counters["buffered"] == 2
    assert calls["summary"] == 2                      # 只对过线的两篇跑总结
    assert calls["prefilter"] == 1                    # 批式：一次调用覆盖全部候选

    pending = {row.arxiv_id for row in store.list_buffer(db, status=store.BUFFER_PENDING)}
    rejected = {row.arxiv_id for row in store.list_buffer(db, status=store.BUFFER_REJECTED)}
    assert pending == {"2609.10001", "2609.10002"}
    assert rejected == {"2609.10003"}
    kept = [row for row in store.list_buffer(db) if row.arxiv_id == "2609.10001"][0]
    assert kept.verified_path == "LLM/RL/Credit Assignment"
    assert kept.summary_text.startswith("TL;DR")
    assert kept.status == store.BUFFER_PENDING
    # 实时日志：包含预筛结果与保留/丢弃决策
    messages = " | ".join(log["message"] for log in run.logs)
    assert "预筛 2609.10001" in messages and "保留：2609.10001" in messages
    assert "丢弃（预筛 0.10 低于阈值）：2609.10003" in messages


def test_search_pipeline_drops_when_verify_says_no(db, monkeypatch):
    hits = [_hit("2609.20001", "Weakly related")]
    _patch_discovery(monkeypatch, hits, verify_fit=False)
    request = {"categories": ["cs.LG"], "min_score": 0.5, "max_summarize": 5}
    outcome = discovery.run_search_pipeline(db, request, settings=llm.LLMSettings(model="m"),
                                           kcfg={"discovery_min_score": 0.5}, hcfg={})
    assert outcome.counters["buffered"] == 0
    assert store.list_buffer(db, status=store.BUFFER_PENDING) == []
    assert len(store.list_buffer(db, status=store.BUFFER_REJECTED)) == 1


def test_search_pipeline_skips_known_and_can_cancel(db, monkeypatch):
    _mk_paper(db, "2609.30001", "Already in library")
    store.upsert_buffer_paper(db, _hit("2609.30002", "Already buffered"))
    hits = [_hit("2609.30001", "Already in library"), _hit("2609.30002", "Already buffered"),
            _hit("2609.30003", "Fresh one")]
    _patch_discovery(monkeypatch, hits)
    request = {"categories": ["cs.LG"], "min_score": 0.5, "max_summarize": 5}
    run = store.create_search_run(db, request)
    run.cancel_requested = True                        # 提交后立刻请求中断
    db.commit()

    outcome = discovery.run_search_pipeline(db, request, run=run,
                                           settings=llm.LLMSettings(model="m"),
                                           kcfg={"discovery_min_score": 0.5}, hcfg={})
    assert outcome.status == "cancelled"
    assert outcome.counters["found"] == 3 and outcome.counters["duplicated"] == 2
    assert run.status == "cancelled" and run.finished_at is not None


def test_search_pipeline_zero_hits_stops_and_finishes(db, monkeypatch):
    """回归：0 命中时 run 必须终结。

    历史 bug：收尾只写 stage="done"，status 一直停在 "running"，
    于是前端无限轮询、按钮永久禁用（用户看到的就是“卡住不停止”）。
    """
    calls = _patch_discovery(monkeypatch, [])
    request = {"keywords": ["totally-unrelated-topic"], "min_score": 0.5}
    run = store.create_search_run(db, request)

    outcome = discovery.run_search_pipeline(db, request, run=run,
                                           settings=llm.LLMSettings(model="m"), hcfg={})
    assert outcome.status == "done" and outcome.counters["found"] == 0
    assert run.status == "done" and run.finished_at is not None
    assert calls["prefilter"] == 0 and calls["summary"] == 0
    assert store.list_buffer(db, status="all") == []


def test_search_pipeline_all_dropped_still_finishes(db, monkeypatch):
    """命中但全部低于阈值：同样要终结为 done（并留下明确日志）。"""
    hits = [_hit("2609.50001", "Off topic")]
    _patch_discovery(monkeypatch, hits)

    def all_low(prompt_name, user_text, settings, *, as_json=True, cfg=None, json_retries=2):
        return StepOutput(ok=True, usage={"total_tokens": 10},
                          data={"items": [{"id": 1, "score": 0.05, "path": "", "reason": "无关"}]})

    monkeypatch.setattr(discovery.llm_steps, "call_structured", all_low)
    request = {"categories": ["cs.LG"], "min_score": 0.5}
    run = store.create_search_run(db, request)

    outcome = discovery.run_search_pipeline(db, request, run=run,
                                           settings=llm.LLMSettings(model="m"), hcfg={})
    assert outcome.status == "done" and outcome.counters["passed"] == 0
    assert run.status == "done" and run.finished_at is not None
    assert "无过线论文" in " | ".join(log["message"] for log in run.logs)


def test_search_pipeline_marks_failed_when_stream_ends_without_final(db, monkeypatch):
    """事件流异常结束（没有 final 帧）也必须终结为 failed，不能悬在 running。"""
    monkeypatch.setattr(discovery, "iter_search_events", lambda *a, **k: iter(()))
    request = {"categories": ["cs.LG"]}
    run = store.create_search_run(db, request)

    outcome = discovery.run_search_pipeline(db, request, run=run)
    assert outcome.status == "failed" and "异常结束" in outcome.error
    assert run.status == "failed" and run.finished_at is not None


def test_search_pipeline_keeps_failure_reason(db, monkeypatch):
    """失败时 error 不能被后续收尾覆盖（前端要展示失败原因）。"""
    hits = [_hit("2609.60001", "Any paper")]
    monkeypatch.setattr(discovery.arxiv_search, "search", lambda *a, **k: list(hits))

    def boom(prompt_name, user_text, settings, *, as_json=True, cfg=None, json_retries=2):
        raise RuntimeError("上游模型不可用")

    monkeypatch.setattr(discovery.llm_steps, "call_structured", boom)
    request = {"categories": ["cs.LG"], "min_score": 0.5}
    run = store.create_search_run(db, request)

    outcome = discovery.run_search_pipeline(db, request, run=run,
                                           settings=llm.LLMSettings(model="m"), hcfg={})
    assert outcome.status == "failed"
    assert "上游模型不可用" in outcome.error
    assert run.status == "failed" and run.finished_at is not None
    assert "上游模型不可用" in (run.error or "")


# ---------------------------------------------------------------- 缓冲区 -> 正式库
def test_promote_buffer_creates_paper_and_mirrors_folder(db, monkeypatch, tmp_path):
    node = store.get_or_create_node(db, ["LLM", "RL", "Credit Assignment"])
    row = store.upsert_buffer_paper(db, _hit("2609.40001", "Fresh paper"), match_score=0.9,
                                    suggested_path=node.path, matched_node_id=node.id)
    meta = arxiv_service.PaperMeta(arxiv_id="2609.40001", title="Fresh paper (arxiv)",
                                   authors=["A. Author"], abstract="abs", categories=["cs.LG"],
                                   published="2026-09-10", pdf_url="http://example.com/x.pdf")
    pdf_path = tmp_path / "2609.40001.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(discovery.arxiv_service, "fetch_metadata", lambda arxiv_id: meta)
    monkeypatch.setattr(discovery.arxiv_service, "download_pdf", lambda meta, dest: pdf_path)
    monkeypatch.setattr(discovery.pdf_service, "extract_text_for_context",
                        lambda path, max_chars=120000: ("Body text.", False))
    called = {}

    class _FakeOutcome:
        run_id = 7
        status = "done"
        steps = [{"step": "summary", "status": "done"}]

    def fake_run_pipeline(db_, paper, **kw):
        called["paper_id"] = paper.id
        return _FakeOutcome()

    monkeypatch.setattr("knowledge.pipeline.run_pipeline", fake_run_pipeline)

    result = discovery.promote_buffer(db, row, trigger_pipeline=True)
    assert result["paper_id"] and called["paper_id"] == result["paper_id"]
    assert result["pipeline"]["run_id"] == 7 and result["pipeline"]["status"] == "done"
    paper = db.get(Paper, result["paper_id"])
    assert paper.arxiv_id == "2609.40001" and paper.full_text == "Body text."
    # 论文被挂到方向文件夹（文件夹树 = 方向树）
    folder = db.get(Folder, paper.folder_id)
    assert folder.name == "Credit Assignment" and folder.direction_node_id == node.id
    assert folder.parent.name == "RL" and folder.parent.parent.name == "LLM"
    # 缓冲区状态与提升状态
    db.refresh(row)
    assert row.status == store.BUFFER_READ and row.promote_state == "done"
    assert row.promoted_paper_id == paper.id


# ---------------------------------------------------------------- 图谱 md
def test_graph_md_contains_mermaid_tables_and_synthesis(db):
    node = store.get_or_create_node(db, ["LLM", "RL", "Credit Assignment"])
    p1 = _mk_paper(db, "2401.1", "LoRA for RL")
    p2 = _mk_paper(db, "2401.2", "Prefix for RL")
    store.set_paper_memberships(db, p1, [{"path": ["LLM", "RL", "Credit Assignment"], "role": "primary"}])
    store.set_paper_memberships(db, p2, [{"path": ["LLM", "RL", "Credit Assignment"]}])
    store.upsert_edges(db, node.id, p1.id, [{"other_paper_id": p2.id, "relation": "improves",
                                             "strength": 0.8, "rationale": "换成低秩增量",
                                             "evidence": "Tab.3"}])
    store.save_synthesis(db, node, "### 1. 这个方向在解决什么\n大家都在做减法。", [p1.id, p2.id], "papers+2")
    store.refresh_nodes(db, [node])

    md = graphdoc.render_graph_md(db, node)
    assert md.startswith("# 论文图谱 · LLM/RL/Credit Assignment")
    assert "```mermaid" in md and "graph LR" in md and "improves 0.8" in md
    assert "## 3. 论文清单" in md and "LoRA for RL" in md
    assert "## 4. 关系明细" in md and "换成低秩增量" in md
    assert "### 1. 这个方向在解决什么" in md
    # 落盘（data 根被 monkeypatch 到 tmp_path）
    db.refresh(node)
    assert node.graph_md_path.endswith("GRAPH.md")
    written = store.knowledge_md_root().joinpath("LLM", "RL", "Credit Assignment", "GRAPH.md")
    assert written.is_file() and "论文图谱" in written.read_text(encoding="utf-8")


# ---------------------------------------------------------------- API
@pytest.fixture()
def client(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "api.db"),
                           connect_args={"check_same_thread": False}, future=True)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", TestingSession)
    monkeypatch.setattr("app.config_loader.get_base_dir", lambda: str(tmp_path))
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
    finally:
        db.close()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _seed_run_and_buffer(client):
    db = database.SessionLocal()
    try:
        run = store.create_search_run(db, {"categories": ["cs.LG"]})
        store.log_search(db, run, "预筛 2609.50001 → 0.90 LLM/RL", arxiv_id="2609.50001")
        store.update_search_run(db, run, stage="screen", counters={"found": 1, "buffered": 1})
        row = store.upsert_buffer_paper(db, _hit("2609.50001", "Buffered paper"), match_score=0.9,
                                        suggested_path="LLM/RL", search_run_id=run.id)
        return run.id, row.id
    finally:
        db.close()


def test_api_search_status_and_cancel(client):
    assert client.get("/api/search/status").status_code == 404
    run_id, _ = _seed_run_and_buffer(client)
    status = client.get("/api/search/status", params={"run_id": run_id}).json()
    assert status["stage"] == "screen" and status["counters"]["buffered"] == 1
    assert any("预筛 2609.50001" in log["message"] for log in status["logs"])
    assert client.post("/api/search/cancel", params={"run_id": run_id}).json()["cancel_requested"] is True
    runs = client.get("/api/search/runs").json()
    assert runs and runs[0]["id"] == run_id


def test_api_search_run_requires_conditions(client):
    body = {"categories": [], "keywords": [], "date_from": None, "date_to": None}
    r = client.post("/api/search/run", json=body)
    assert r.status_code == 400 and "至少填写一个检索条件" in r.json()["detail"]


def test_api_search_run_starts_background(client, monkeypatch):
    started = {}

    def fake_start(request, run_id=None):
        db = database.SessionLocal()
        try:
            run = store.create_search_run(db, request)
            started["request"] = request
            return run.id
        finally:
            db.close()

    monkeypatch.setattr("app.routers.search.discovery.start_search_background", fake_start)
    r = client.post("/api/search/run", json={"categories": ["cs.CL"], "keywords": ["rl"],
                                             "max_results": 5})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "running" and r.json()["id"]
    assert started["request"]["categories"] == ["cs.CL"]
    assert started["request"]["keywords"] == ["rl"]


def test_api_buffer_crud_and_reject(client):
    _, buffer_id = _seed_run_and_buffer(client)
    listing = client.get("/api/buffer").json()
    assert listing["counts"]["pending"] == 1 and listing["items"][0]["arxiv_id"] == "2609.50001"
    detail = client.get("/api/buffer/{}".format(buffer_id)).json()
    assert detail["suggested_path"] == "LLM/RL" and detail["status"] == "pending"
    assert client.post("/api/buffer/{}/reject".format(buffer_id)).json()["status"] == "rejected"
    assert client.get("/api/buffer").json()["items"] == []
    assert client.delete("/api/buffer/{}".format(buffer_id)).status_code == 204
    assert client.get("/api/buffer/{}".format(buffer_id)).status_code == 404


def test_api_buffer_promote_and_summarize(client, monkeypatch):
    _, buffer_id = _seed_run_and_buffer(client)

    monkeypatch.setattr("app.routers.search.discovery.start_promote_background",
                        lambda buffer_id_, trigger_pipeline=True: "queued")
    promoted = client.post("/api/buffer/{}/promote".format(buffer_id)).json()
    assert promoted["promote_state"] == "queued"

    def fake_summarize(db, row, paths, settings, hcfg):
        store.set_buffer_summary(db, row, summary_text="TL;DR: y", stats={},
                                 verified_path="LLM/RL", verify_reason="ok", fit=True)
        return {"fit": True, "path": "LLM/RL", "reason": "ok", "usage": {}}

    monkeypatch.setattr("app.routers.search.discovery._summarize_and_verify", fake_summarize)
    monkeypatch.setattr("app.routers.search.discovery._tree_paths", lambda db, limit: ["LLM/RL"])
    summarized = client.post("/api/buffer/{}/summarize".format(buffer_id)).json()
    assert summarized["summary_text"] == "TL;DR: y" and summarized["verified_path"] == "LLM/RL"


def test_api_knowledge_graph_md_endpoint(client):
    db = database.SessionLocal()
    try:
        node = store.get_or_create_node(db, ["LLM", "RL"])
        paper = _mk_paper(db, "2402.1", "Graph md paper")
        store.set_paper_memberships(db, paper, [{"path": ["LLM", "RL"]}])
        node_id = node.id
    finally:
        db.close()
    r = client.get("/api/knowledge/nodes/{}/graph.md".format(node_id))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert "论文图谱" in r.text and "```mermaid" in r.text
    exported = client.post("/api/knowledge/export").json()
    assert exported["count"] >= 1 and exported["paths"][0].endswith("GRAPH.md")
