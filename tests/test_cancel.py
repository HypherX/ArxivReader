"""「终止」按钮的取消链路测试（全部离线）。

覆盖：登记表语义 / llm 逐块中止并关流 / agent 不把取消当错误 / 结构化调用不重试 /
pipeline 把当前步与整轮记成 cancelled / API 端点 / 对话落库已生成部分。
"""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import database
from app.database import Base
from app.main import app
from app.models import ChatMessage, ChatSession, Paper
from harness import agent, cancel, llm
from knowledge import llm_steps, pipeline, store
from knowledge.llm_steps import StepOutput

_PAPER_TEXT = "[Page 1]\nAbstract\nWe study credit assignment.\n1 Method\nGroup-relative.\n"


def _mk_paper(db, arxiv_id="2401.70001"):
    paper = Paper(arxiv_id=arxiv_id, title="Cancel target",
                  authors_json=json.dumps(["A. Author"], ensure_ascii=False),
                  abstract="credit assignment", categories_json=json.dumps(["cs.LG"]),
                  published="2024-01-01T00:00:00", full_text=_PAPER_TEXT)
    db.add(paper)
    db.commit()
    db.refresh(paper)
    return paper


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "cancel.db"),
                           connect_args={"check_same_thread": False}, future=True)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    session = Session()
    monkeypatch.setattr("app.config_loader.get_base_dir", lambda: str(tmp_path))
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "api_cancel.db"),
                           connect_args={"check_same_thread": False}, future=True)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", TestingSession)
    monkeypatch.setattr("app.config_loader.get_base_dir", lambda: str(tmp_path))
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[database.get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------- 登记表
def test_registry_lifecycle():
    token = cancel.new_token()
    assert cancel.is_stopping(token) is False
    assert cancel.request_stop(token) is True
    assert cancel.is_stopping(token) is True
    with pytest.raises(cancel.Cancelled):
        cancel.check(token)
    cancel.release(token)
    assert cancel.is_stopping(token) is False
    cancel.release(token)                      # 幂等
    # 空 token：不报错也不拦
    cancel.check(None)
    assert cancel.request_stop(None) is False
    assert cancel.watcher(None) is None
    # 懒登记：没登记过的 token 也能置位（前端终止请求可能先到）
    late = cancel.new_token()
    assert cancel.request_stop(late) is True
    assert cancel.is_stopping(late) is True
    cancel.release(late)


# ---------------------------------------------------------------- 底层流式循环
class _Delta:
    def __init__(self, text):
        self.content = text
        self.tool_calls = None
        self.reasoning = None


class _Choice:
    def __init__(self, text):
        self.delta = _Delta(text)


class _Chunk:
    def __init__(self, text):
        self.choices = [_Choice(text)]
        self.usage = None


def test_llm_stream_stops_and_closes(monkeypatch):
    token = cancel.new_token()

    class _Stream:
        def __init__(self):
            self.closed = False

        def __iter__(self):
            for i in range(20):
                yield _Chunk("块{}".format(i))

        def close(self):
            self.closed = True

    stream = _Stream()

    class _Completions:
        @staticmethod
        def create(**kwargs):
            return stream

    class _Client:
        chat = type("C", (), {"completions": _Completions})()

    monkeypatch.setattr(llm, "get_client", lambda settings: _Client())
    settings = llm.LLMSettings(model="m", base_url="http://x", api_key="k")
    seen = []
    with pytest.raises(cancel.Cancelled):
        for event in llm.stream_events(settings, [{"role": "user", "content": "hi"}],
                                       should_stop=cancel.watcher(token)):
            seen.append(event)
            cancel.request_stop(token)      # 收到第一块之后，用户点了「终止」
    assert len(seen) == 1, "终止后不能再把后续 19 块读完"
    assert stream.closed is True, "提前退出必须关掉底层流，否则会一直读到模型结束"
    cancel.release(token)


def test_agent_does_not_report_cancel_as_error(monkeypatch):
    token = cancel.new_token()
    cancel.request_stop(token)

    def fake_stream(settings, messages, tools=None, tool_choice=None, include_usage=None,
                    should_stop=None):
        raise cancel.Cancelled("已被用户终止")
        yield  # pragma: no cover

    monkeypatch.setattr(llm, "stream_events", fake_stream)
    settings = llm.LLMSettings(model="m", base_url="http://x", api_key="k")
    with pytest.raises(cancel.Cancelled):
        for _ in agent.iter_agent_events(settings, [{"role": "user", "content": "hi"}],
                                        should_stop=cancel.watcher(token)):
            pass
    cancel.release(token)


def test_call_structured_does_not_retry_after_cancel(monkeypatch):
    token = cancel.new_token()
    calls = {"n": 0}

    def fake_complete(settings, messages):
        calls["n"] += 1
        cancel.request_stop(token)
        return llm.Completion(text="不是 JSON")

    monkeypatch.setattr(llm, "complete", fake_complete)
    settings = llm.LLMSettings(model="m")
    with pytest.raises(cancel.Cancelled):
        llm_steps.call_structured("paper_relation.md", "输入", settings,
                                  json_retries=3, should_stop=cancel.watcher(token))
    assert calls["n"] == 1, "已终止就不该再重试（每次重试都在白烧 token）"
    cancel.release(token)


# ---------------------------------------------------------------- pipeline
def test_pipeline_cancel_marks_step_and_run(db, monkeypatch):
    paper = _mk_paper(db)
    token = cancel.new_token()
    calls = {"deep": 0}

    def fake_run_skill(skill, paper_arg, settings, **kw):
        if skill == "quick_summary":
            from harness.runner import SkillResult
            return SkillResult(skill=skill, output="TL;DR: x\n- 解决了什么: y\n- 未来展望: z",
                               stats={"usage": {"total_tokens": 5}})
        calls["deep"] += 1
        cancel.request_stop(token)                    # 深读途中用户点了终止
        raise cancel.Cancelled("已被用户终止")

    monkeypatch.setattr(pipeline.runner, "run_skill", fake_run_skill)
    monkeypatch.setattr(pipeline.llm_steps, "call_structured",
                        lambda *a, **k: StepOutput(ok=True, data={"memberships": [], "new_nodes": []}))

    events = []
    for event in pipeline.iter_pipeline_events(db, paper, settings=llm.LLMSettings(model="m"),
                                               kcfg={"reuse_artifacts": True}, hcfg={},
                                               should_stop=cancel.watcher(token)):
        events.append(event)
    final = [e for e in events if e["type"] == "final"][0]
    assert final["status"] == "cancelled"
    statuses = {s["step"]: s["status"] for s in final["steps"]}
    assert statuses["summary"] == "done"
    assert statuses["deep"] == "failed"
    assert "终止" in final["steps"][-1]["detail"]
    assert "synthesis" not in statuses          # 终止后续步骤不再跑
    assert calls["deep"] == 1
    # 账本落库：整轮 cancelled，且已完成的产物不回滚
    from app.models import PipelineRun
    run = db.query(PipelineRun).order_by(PipelineRun.id.desc()).first()
    assert run.status == "cancelled" and "终止" in run.error
    assert store.get_artifact(db, paper.id, store.ARTIFACT_SUMMARY) is not None
    assert store.get_artifact(db, paper.id, store.ARTIFACT_DEEP) is None
    cancel.release(token)


# ---------------------------------------------------------------- API
def test_api_cancel_endpoint(client):
    token = cancel.new_token()
    assert client.post("/api/cancel/{}".format(token)).json()["stopped"] is True
    assert cancel.is_stopping(token) is True
    cancel.release(token)
    assert client.post("/api/cancel/unknown-token").json()["stopped"] is True   # 懒登记


def test_chat_cancel_saves_partial_reply(client, monkeypatch):
    """对话被终止时：已生成的部分要落库，并带终止标记。"""
    db_session = database.SessionLocal()
    try:
        paper = _mk_paper(db_session, "2401.70002")
        session = ChatSession(paper_id=paper.id, title="t")
        db_session.add(session)
        db_session.commit()
        db_session.refresh(session)
        sid, pid = session.id, paper.id
    finally:
        db_session.close()

    token = cancel.new_token()

    def fake_events(settings, messages, tools=None, max_rounds=0, should_stop=None):
        yield {"type": "delta", "content": "前半段", "reasoning_content": "想"}
        cancel.request_stop(token)                  # 收到第一段后用户终止
        yield {"type": "delta", "content": "后半段", "reasoning_content": ""}
        yield {"type": "delta", "content": "再也不会到达", "reasoning_content": ""}

    def guarded(settings, messages, tools=None, max_rounds=0, should_stop=None):
        for event in fake_events(settings, messages, tools, max_rounds, should_stop):
            if should_stop is not None and should_stop():
                raise cancel.Cancelled("已被用户终止")
            yield event

    monkeypatch.setattr("app.routers.chat.agent.iter_agent_events", guarded)

    with client.stream("POST", "/api/sessions/{}/messages".format(sid),
                       json={"content": "讲讲这篇", "cancel_token": token}) as resp:
        body = "".join(resp.iter_text())
    assert "cancelled" in body
    assert "再也不会到达" not in body

    db_session = database.SessionLocal()
    try:
        msg = (db_session.query(ChatMessage)
               .filter(ChatMessage.session_id == sid, ChatMessage.role == "assistant").first())
        assert msg is not None
        assert "前半段" in msg.content and "已被终止" in msg.content
        assert "后半段" not in msg.content          # 终止后不再生成
    finally:
        db_session.close()
    assert cancel.is_stopping(token) is False       # 流结束后令牌已释放
