"""知识网络测试：方向树 / 归属 / 关系边 / 综述 / pipeline 编排 / API。

全部离线：LLM 调用一律 monkeypatch（技能用 runner.run_skill、结构化步骤用 llm_steps.call_structured）。
"""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import database
from app.database import Base
from app.main import app
from app.models import DirectionNode, Folder, Paper, PaperDirection, PaperRelation
from harness import llm
from harness.runner import SkillResult
from knowledge import STEP_TITLES, STEPS, llm_steps, pipeline, run_pipeline, store
from knowledge.llm_steps import StepOutput, robust_json_load

_PAPER_TEXT = ("[Page 1]\nAbstract\nWe study credit assignment in agentic RL.\n"
               "1 Introduction\nGroup-relative advantage helps.\n"
               "2 Method\nOur estimator reduces variance.\n"
               "3 Conclusion\nWorks on GSM8K.\n")


def _mk_paper(db, arxiv_id="2401.00001", title="Credit Assignment for Agentic RL"):
    paper = Paper(arxiv_id=arxiv_id, title=title,
                  authors_json=json.dumps(["A. Author"], ensure_ascii=False),
                  abstract="We propose a group-relative credit assignment estimator.",
                  categories_json=json.dumps(["cs.LG"], ensure_ascii=False),
                  published="2024-01-01T00:00:00", full_text=_PAPER_TEXT)
    db.add(paper)
    db.commit()
    db.refresh(paper)
    return paper


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """独立临时库；knowledge 层直接吃 Session。

    同时把图谱 md 的落盘根目录也指向临时目录（否则测试会写真实 data/knowledge/）。
    """
    engine = create_engine("sqlite:///" + str(tmp_path / "knowledge.db"),
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
def settings():
    return llm.LLMSettings(model="test-model", reasoning_effort="max")


# ---------------------------------------------------------------- 存储层
def test_get_or_create_node_builds_ancestors_idempotently(db):
    node = store.get_or_create_node(db, ["LLM", "RL", "Agentic RL"], description="智能体强化学习")
    assert node.path == "LLM/RL/Agentic RL" and node.depth == 2
    assert node.parent.path == "LLM/RL" and node.parent.parent.path == "LLM"
    assert node.description == "智能体强化学习"
    again = store.get_or_create_node(db, ["LLM", "RL", "Agentic RL"])
    assert again.id == node.id
    assert db.query(DirectionNode).count() == 3


def test_normalize_path_guards_dirty_input():
    assert store.normalize_path(" LLM / RL /  ") == ["LLM", "RL"]
    assert store.normalize_path(["LLM", "", "RL"]) == ["LLM", "RL"]
    assert store.normalize_path(None) == []
    assert len(store.normalize_path(["a", "b", "c", "d", "e", "f", "g"])) == 6


def test_set_memberships_is_replacing_and_recounts(db):
    p1, p2 = _mk_paper(db, "2401.1"), _mk_paper(db, "2401.2", "Second paper")
    members = [{"path": ["LLM", "RL"], "role": "primary", "reason": "主线", "confidence": 0.9},
               {"path": ["LLM", "RL", "Entropy Collapse"], "role": "secondary",
                "reason": "副线", "confidence": 0.5}]
    rows = store.set_paper_memberships(db, p1, members)
    assert len(rows) == 2
    assert store.get_or_create_node(db, ["LLM", "RL"]).paper_count == 1

    store.set_paper_memberships(db, p2, [{"path": ["LLM", "RL"], "role": "primary"}])
    assert store.get_or_create_node(db, ["LLM", "RL"]).paper_count == 2

    # 覆盖式：p1 改挂到别的路径后，原路径计数应回落
    store.set_paper_memberships(db, p1, [{"path": ["LLM", "Training"]}])
    assert store.get_or_create_node(db, ["LLM", "RL"]).paper_count == 1
    assert db.query(PaperDirection).filter_by(paper_id=p1.id).count() == 1
    # 只有一条 primary：第二条 primary 会被降级
    rows = store.set_paper_memberships(db, p2, [{"path": ["A"], "role": "primary"},
                                                {"path": ["B"], "role": "primary"}])
    assert [r.role for r in rows] == ["primary", "secondary"]


def test_upsert_edges_dedupe_and_update(db):
    p1, p2 = _mk_paper(db, "2401.1"), _mk_paper(db, "2401.2", "Second")
    node = store.get_or_create_node(db, ["LLM", "RL"])
    assert store.upsert_edges(db, node.id, p1.id, [
        {"other_paper_id": p2.id, "relation": "improves", "strength": 0.4, "rationale": "r1"}]) == 1
    assert store.upsert_edges(db, node.id, p1.id, [
        {"other_paper_id": p2.id, "relation": "improves", "strength": 0.9, "rationale": "r2"}]) == 1
    rows = db.query(PaperRelation).all()
    assert len(rows) == 1 and rows[0].strength == 0.9 and rows[0].rationale == "r2"
    # 自环与非法输入被忽略
    assert store.upsert_edges(db, node.id, p1.id, [
        {"other_paper_id": p1.id, "relation": "cites"},
        {"other_paper_id": p2.id, "relation": ""},
        {"other_paper_id": "abc", "relation": "cites"}]) == 0
    assert store.has_edges(db, node.id, p1.id) is True


def test_tree_payload_aggregates_subtree(db):
    p1, p2 = _mk_paper(db, "2401.1"), _mk_paper(db, "2401.2", "Second")
    store.set_paper_memberships(db, p1, [{"path": ["LLM", "RL", "Credit Assignment"]}])
    store.set_paper_memberships(db, p2, [{"path": ["LLM", "RL"]}])
    payload = store.tree_payload(db)
    by_path = {n["path"]: n for n in payload["nodes"]}
    assert by_path["LLM"]["subtree_paper_count"] == 2
    assert by_path["LLM/RL"]["subtree_paper_count"] == 2 and by_path["LLM/RL"]["paper_count"] == 1
    assert len(payload["edges"]) == 2 and payload["edges"][0]["type"] == "parent"


def test_node_payload_and_paper_payload(db):
    p1, p2 = _mk_paper(db, "2401.1"), _mk_paper(db, "2401.2", "Second")
    store.set_paper_memberships(db, p1, [{"path": ["LLM", "RL"], "role": "primary", "reason": "主线"}])
    store.set_paper_memberships(db, p2, [{"path": ["LLM", "RL"]}])
    node = store.get_or_create_node(db, ["LLM", "RL"])
    store.upsert_edges(db, node.id, p1.id, [{"other_paper_id": p2.id, "relation": "improves"}])
    store.upsert_artifact(db, p1.id, store.ARTIFACT_SUMMARY, "- 解决了什么: x", stats={"usage": {}})

    node_view = store.node_payload(db, node)
    assert node_view["node"]["path"] == "LLM/RL" and node_view["node"]["subtree_paper_count"] == 2
    assert {n["id"] for n in node_view["graph"]["nodes"]} == {p1.id, p2.id}
    assert node_view["graph"]["edges"][0]["relation"] == "improves"
    assert node_view["synthesis"]["latest"] == ""

    paper_view = store.paper_knowledge_payload(db, p1)
    assert paper_view["memberships"][0]["path"] == "LLM/RL"
    assert paper_view["artifacts"][0]["kind"] == "summary"
    assert paper_view["edges"][0]["relation"] == "improves"


def test_save_synthesis_updates_node_and_keeps_history(db):
    p1 = _mk_paper(db)
    store.set_paper_memberships(db, p1, [{"path": ["LLM", "RL"]}])
    node = store.get_or_create_node(db, ["LLM", "RL"])
    store.save_synthesis(db, node, "# 综述 v1", [p1.id], "papers+5", {"usage": {}})
    assert node.synthesis_md == "# 综述 v1" and node.synthesis_paper_count == 1
    store.save_synthesis(db, node, "# 综述 v2", [p1.id], "papers+5")
    history = store.synthesis_history(db, node.id)
    assert [h.content_md for h in history] == ["# 综述 v2", "# 综述 v1"]
    assert node.synthesis_md == "# 综述 v2"


# ---------------------------------------------------------------- 手工整备（改名 / 删除）
def test_rename_node_moves_subtree_folder_and_md(db):
    p1 = _mk_paper(db, "2401.1")
    store.set_paper_memberships(db, p1, [{"path": ["LLM", "RL", "Credit Assignment"]}])
    mid = store.get_or_create_node(db, ["LLM", "RL"])
    leaf = store.get_or_create_node(db, ["LLM", "RL", "Credit Assignment"])
    store.refresh_nodes(db, [mid, leaf])           # 建镜像文件夹 + 写 md
    old_dir = Path(mid.graph_md_path).parent

    store.rename_node(db, mid, "强化学习")
    db.refresh(mid)
    db.refresh(leaf)
    assert mid.path == "LLM/强化学习"
    assert leaf.path == "LLM/强化学习/Credit Assignment" and leaf.depth == 2
    assert leaf.parent_id == mid.id
    assert store.get_node_by_path(db, "LLM/RL") is None
    folder = store.folder_of_node(db, mid.id)
    assert folder is not None and folder.name == "强化学习"
    assert not old_dir.exists()                    # 旧目录已搬迁
    assert Path(leaf.graph_md_path).exists()       # 子树 md 跟着新路径重写
    assert Path(leaf.graph_md_path).parent.name == "Credit Assignment"


def test_rename_node_rejects_duplicate_sibling(db):
    store.get_or_create_node(db, ["LLM", "RL"])
    other = store.get_or_create_node(db, ["LLM", "Training"])
    with pytest.raises(ValueError):
        store.rename_node(db, other, "RL")
    assert other.path == "LLM/Training"


def test_delete_node_lifts_descendants_and_cleans_up(db):
    p1, p2 = _mk_paper(db, "2401.1"), _mk_paper(db, "2401.2", "Second")
    store.set_paper_memberships(db, p1, [{"path": ["LLM", "RL", "Credit Assignment"]}])
    store.set_paper_memberships(db, p2, [{"path": ["LLM", "RL"]}])
    mid = store.get_or_create_node(db, ["LLM", "RL"])
    leaf = store.get_or_create_node(db, ["LLM", "RL", "Credit Assignment"])
    root = store.get_or_create_node(db, ["LLM"])
    store.refresh_nodes(db, [mid, leaf])
    store.upsert_edges(db, mid.id, p2.id, [{"other_paper_id": p1.id, "relation": "improves"}])
    md_file = Path(mid.graph_md_path)

    assert store.delete_node(db, mid) == 1         # 上移一个子节点
    assert db.get(DirectionNode, mid.id) is None
    db.refresh(leaf)
    assert leaf.path == "LLM/Credit Assignment" and leaf.depth == 1
    assert leaf.parent_id == root.id
    assert db.query(PaperRelation).filter_by(node_id=mid.id).count() == 0
    assert not md_file.exists()                    # 该节点的 md 清理掉
    assert Path(leaf.graph_md_path).exists()       # 子节点 md 在新位置重写
    # 论文不受影响：leaf 上的归属还在，mid 上的归属随节点删除
    assert [d.node.path for d in store.paper_memberships(db, p1.id)] == ["LLM/Credit Assignment"]
    assert store.paper_memberships(db, p2.id) == []
    # 镜像文件夹保留但已解绑（用户仍可在文件夹栏里找到它）
    assert store.folder_of_node(db, mid.id) is None
    assert db.query(Folder).filter_by(name="RL").count() == 1


# ---------------------------------------------------------------- 结构化解析
def test_robust_json_load_variants():
    assert robust_json_load('{"a": 1}') == {"a": 1}
    assert robust_json_load('```json\n{"a": 1}\n```') == {"a": 1}
    assert robust_json_load('好的：\n{"a": 1,}\n以上。') == {"a": 1}
    assert robust_json_load('[{"a": 1}]') == [{"a": 1}]
    assert robust_json_load("这不是 JSON") is None
    assert robust_json_load("") is None


def test_call_structured_repairs_invalid_json(monkeypatch, settings):
    replies = [llm.Completion(text="抱歉，我忘了格式"), llm.Completion(text='{"ok": true}')]
    monkeypatch.setattr(llm, "complete", lambda *a, **k: replies.pop(0))
    out = llm_steps.call_structured("direction_assign.md", "输入", settings)
    assert out.ok and out.data == {"ok": True}
    assert not replies


def test_call_structured_gives_up_after_retries(monkeypatch, settings):
    monkeypatch.setattr(llm, "complete", lambda *a, **k: llm.Completion(text="still not json"))
    out = llm_steps.call_structured("paper_relation.md", "输入", settings, json_retries=1)
    assert not out.ok and "JSON" in out.error


# ---------------------------------------------------------------- pipeline 编排
_ASSIGN = {"memberships": [
    {"path": ["LLM", "RL", "Credit Assignment"], "role": "primary",
     "reason": "解决 credit assignment", "confidence": 0.9},
    {"path": ["LLM", "RL", "Entropy Collapse"], "role": "secondary",
     "reason": "顺带讨论熵崩塌", "confidence": 0.4},
], "new_nodes": [{"path": ["LLM", "RL"], "description": "强化学习方向"}]}


def _patch_llm(monkeypatch, *, fail_step=None):
    calls = {"skill": [], "structured": []}

    def fake_run_skill(skill, paper, settings, **kw):
        calls["skill"].append(skill)
        if fail_step == skill:
            return SkillResult(skill=skill, output="", error="上游失败")
        if skill == "quick_summary":
            return SkillResult(skill=skill, output="TL;DR: x\n- 解决了什么: y\n- 未来展望: z",
                               reasoning_content="思考", stats={"usage": {"total_tokens": 5},
                                                              "model": "m", "reasoning_effort": "max"})
        return SkillResult(skill=skill, output="# 深读笔记\n方法细节…", stats={"usage": {"total_tokens": 9}})

    def fake_call(prompt_name, user_text, settings, *, as_json=True, cfg=None, json_retries=2,
                  should_stop=None):
        calls["structured"].append(prompt_name)
        if prompt_name == "direction_assign.md":
            return StepOutput(ok=True, data=_ASSIGN, usage={"total_tokens": 21})
        if prompt_name == "paper_relation.md":
            # 从输入的"已有论文"段里取真实 id，模拟模型的引用行为
            tail = user_text.split("## 该方向节点下的已有论文")[-1]
            other_id = int(re.findall(r"- id=(\d+) \|", tail)[0])
            return StepOutput(ok=True, data={"edges": [
                {"other_paper_id": other_id, "relation": "improves", "strength": 0.8,
                 "rationale": "把基线换成 group-relative", "evidence": "§2"}]},
                usage={"total_tokens": 13})
        return StepOutput(ok=True, text="# 子领域综述\n大家都在做 X…", usage={"total_tokens": 30})

    monkeypatch.setattr(pipeline.runner, "run_skill", fake_run_skill)
    monkeypatch.setattr(pipeline.llm_steps, "call_structured", fake_call)
    return calls


def test_pipeline_full_run_writes_tree_graph_and_synthesis(db, settings, monkeypatch):
    calls = _patch_llm(monkeypatch)
    p1 = _mk_paper(db, "2401.1")

    result = run_pipeline(db, p1, settings=settings, kcfg={"synthesis_every": 1,
                                                          "reuse_artifacts": True})
    assert result.status == "done" and [s["step"] for s in result.steps] == list(STEPS)
    statuses = {s["step"]: s["status"] for s in result.steps}
    assert statuses["summary"] == "done" and statuses["tree"] == "done"
    assert statuses["deep"] == "done" and statuses["synthesis"] == "done"
    assert statuses["graph"] == "skipped"      # 单篇论文：该节点下暂无可比对论文

    # 产物
    assert store.get_artifact(db, p1.id, store.ARTIFACT_SUMMARY).content_md.startswith("TL;DR")
    assert "深读笔记" in store.get_artifact(db, p1.id, store.ARTIFACT_DEEP).content_md
    # 方向树（多归属）
    paths = sorted(d.node.path for d in store.paper_memberships(db, p1.id))
    assert paths == ["LLM/RL/Credit Assignment", "LLM/RL/Entropy Collapse"]
    assert store.get_or_create_node(db, ["LLM", "RL"]).description == "强化学习方向"
    # 综述（阈值=1，单论文即触发）
    node = store.get_or_create_node(db, ["LLM", "RL", "Credit Assignment"])
    assert node.synthesis_md.startswith("# 子领域综述")
    assert result.syntheses and result.syntheses[0]["path"].endswith("Credit Assignment")
    assert calls["skill"] == ["quick_summary", "deep_reading"]
    assert calls["structured"][-1] == "field_synthesis.md"


def test_pipeline_writes_relation_edges_between_papers(db, settings, monkeypatch):
    _patch_llm(monkeypatch)
    p1 = _mk_paper(db, "2401.1")
    p2 = _mk_paper(db, "2401.2", "Older baseline")
    # 让 p2 先落在同一节点下，p1 再读时才有可比对对象
    store.set_paper_memberships(db, p2, [{"path": ["LLM", "RL", "Credit Assignment"]}])

    result = run_pipeline(db, p1, steps=["summary", "tree", "deep", "graph"],
                          settings=settings, kcfg={"reuse_artifacts": True})
    assert result.status == "done"
    graph = [s for s in result.steps if s["step"] == "graph"][0]
    assert graph["status"] == "done" and graph["edges"] == 1
    node = store.get_or_create_node(db, ["LLM", "RL", "Credit Assignment"])
    edges = store.list_edges(db, node.id)
    assert len(edges) == 1 and edges[0].src_paper_id == p1.id and edges[0].dst_paper_id == p2.id
    assert edges[0].rationale.startswith("把基线换成")


def test_pipeline_second_run_skips_everything(db, settings, monkeypatch):
    calls = _patch_llm(monkeypatch)
    p1 = _mk_paper(db, "2401.1")
    run_pipeline(db, p1, settings=settings, kcfg={"synthesis_every": 1, "reuse_artifacts": True})
    first = list(calls["skill"])

    result = run_pipeline(db, p1, settings=settings, kcfg={"synthesis_every": 1, "reuse_artifacts": True})
    assert result.status == "done"
    statuses = {s["step"]: s["status"] for s in result.steps}
    assert statuses["summary"] == "skipped" and statuses["tree"] == "skipped"
    assert statuses["deep"] == "skipped" and statuses["graph"] == "skipped"
    assert statuses["synthesis"] == "skipped"
    assert calls["skill"] == first          # 没有重复调用 LLM


def test_pipeline_failure_marks_step_and_skips_dependents(db, settings, monkeypatch):
    _patch_llm(monkeypatch, fail_step="deep_reading")
    p1 = _mk_paper(db)
    result = run_pipeline(db, p1, settings=settings, kcfg={"synthesis_every": 1,
                                                          "reuse_artifacts": True})
    assert result.status == "failed"
    statuses = {s["step"]: s["status"] for s in result.steps}
    assert statuses["summary"] == "done"          # 已成功的步骤不回滚
    assert statuses["tree"] == "done"
    assert statuses["deep"] == "failed"
    assert statuses["graph"] == "skipped" and statuses["synthesis"] == "skipped"
    assert "上游失败" in result.error
    # 第二轮：summary/tree 仍可复用，deep 会被重试
    result2 = run_pipeline(db, p1, settings=settings, kcfg={"reuse_artifacts": True})
    assert {s["step"]: s["status"] for s in result2.steps}["summary"] == "skipped"


def test_pipeline_reports_missing_dependency(db, settings):
    p1 = _mk_paper(db)
    result = run_pipeline(db, p1, steps=["tree"], settings=settings)
    assert result.status == "failed" and "缺少摘要产物" in result.error


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


def _add_paper(client, arxiv_id="2405.11111"):
    db = database.SessionLocal()
    try:
        paper = _mk_paper(db, arxiv_id)
        return paper.id
    finally:
        db.close()


def test_api_steps_and_empty_tree(client):
    steps = client.get("/api/knowledge/steps").json()
    assert [s["name"] for s in steps["steps"]] == list(STEPS)
    assert steps["steps"][0]["title"] == STEP_TITLES["summary"]
    tree = client.get("/api/knowledge/tree").json()
    assert tree == {"nodes": [], "edges": [], "total_papers": 0}


def test_api_run_pipeline_and_read_back(client, monkeypatch):
    _patch_llm(monkeypatch)          # 复用同一套 fake（pipeline 与 API 走同一实现）
    pid = _add_paper(client)
    r = client.post("/api/papers/{}/knowledge/run".format(pid),
                    json={"steps": ["summary", "tree"], "force": False})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "done" and data["run_id"]
    assert [s["step"] for s in data["steps"]] == ["summary", "tree"]

    # 方向树 / 节点详情 / 论文视角 都可读回
    tree = client.get("/api/knowledge/tree").json()
    assert any(n["path"] == "LLM/RL/Credit Assignment" for n in tree["nodes"])
    node_id = next(n["id"] for n in tree["nodes"] if n["path"] == "LLM/RL/Credit Assignment")
    node = client.get("/api/knowledge/nodes/{}".format(node_id)).json()
    assert node["papers"][0]["id"] == pid and node["papers"][0]["role"] == "primary"
    paper = client.get("/api/papers/{}/knowledge".format(pid)).json()
    assert paper["memberships"][0]["path"] == "LLM/RL/Credit Assignment"
    assert paper["artifacts"][0]["kind"] == "summary"
    assert paper["runs"][0]["status"] == "done"

    # 删除误建节点：论文不受影响（还留着另一个归属），被删路径消失
    assert client.delete("/api/knowledge/nodes/{}".format(node_id)).status_code == 204
    tree2 = client.get("/api/knowledge/tree").json()
    assert all(n["path"] != "LLM/RL/Credit Assignment" for n in tree2["nodes"])
    left = client.get("/api/papers/{}/knowledge".format(pid)).json()["memberships"]
    assert left and all(m["path"] != "LLM/RL/Credit Assignment" for m in left)


def test_api_artifact_endpoints(client, monkeypatch):
    """阅读产物接口：没生成时 404，生成后 JSON 给正文，raw=true 直接给 Markdown。"""
    _patch_llm(monkeypatch)
    pid = _add_paper(client)

    # 未生成：两个 kind 都是 404，且提示可操作
    r = client.get("/api/papers/{}/artifacts/summary".format(pid))
    assert r.status_code == 404 and "知识网络" in r.json()["detail"]
    # 未知 kind 也 404（不能拿任意字符串当文件名用）
    assert client.get("/api/papers/{}/artifacts/whatever".format(pid)).status_code == 404

    client.post("/api/papers/{}/knowledge/run".format(pid),
                json={"steps": ["summary"], "force": False})

    got = client.get("/api/papers/{}/artifacts/summary".format(pid))
    assert got.status_code == 200, got.text
    data = got.json()
    assert data["kind"] == "summary" and data["title"] == "快速总结"
    assert data["content_md"].startswith("TL;DR") and data["chars"] == len(data["content_md"])
    assert data["model"] == "m" and data["updated_at"]

    raw = client.get("/api/papers/{}/artifacts/summary?raw=true".format(pid))
    assert raw.status_code == 200 and raw.text.startswith("TL;DR")
    assert "markdown" in raw.headers["content-type"]

    # 深度精读与快速总结是两个独立产物
    assert client.get("/api/papers/{}/artifacts/deep_reading".format(pid)).status_code == 404
    assert client.get("/api/papers/999999/artifacts/summary").status_code == 404


def _walk_folders(nodes):
    out = []
    for n in nodes:
        out.append(n)
        out.extend(_walk_folders(n.get("children") or []))
    return out


def test_api_rename_node_keeps_folder_mirror_in_sync(client, monkeypatch):
    _patch_llm(monkeypatch)
    pid = _add_paper(client)
    run = client.post("/api/papers/{}/knowledge/run".format(pid),
                      json={"steps": ["summary", "tree"], "force": False}).json()
    assert run["status"] == "done", run          # 先保证镜像文件夹已随 tree 步骤建成

    tree = client.get("/api/knowledge/tree").json()
    node_id = next(n["id"] for n in tree["nodes"] if n["path"] == "LLM/RL")
    r = client.patch("/api/knowledge/nodes/{}".format(node_id), json={"name": "强化学习"})
    assert r.status_code == 200, r.text
    assert r.json()["node"]["path"] == "LLM/强化学习"
    paths = [n["path"] for n in client.get("/api/knowledge/tree").json()["nodes"]]
    assert "LLM/强化学习/Credit Assignment" in paths and "LLM/RL" not in paths

    folders = _walk_folders(client.get("/api/folders").json())
    folder = next((f for f in folders if f.get("direction_node_id") == node_id), None)
    assert folder, folders                         # tree 步骤应已建好 🧭 镜像文件夹
    assert folder["name"] == "强化学习"              # 镜像文件夹跟着改

    # 反向：通过文件夹 API 改名，方向节点也要同步（单一数据源）
    assert client.patch("/api/folders/{}".format(folder["id"]),
                        json={"name": "Reinforcement Learning"}).status_code == 200
    paths = [n["path"] for n in client.get("/api/knowledge/tree").json()["nodes"]]
    assert "LLM/Reinforcement Learning/Credit Assignment" in paths

    # 镜像文件夹不能直接删（否则下次同步又冒出来）；非法名字被 schema 拦下
    assert client.delete("/api/folders/{}".format(folder["id"])).status_code == 400
    assert client.patch("/api/knowledge/nodes/{}".format(node_id),
                        json={"name": ""}).status_code == 422
