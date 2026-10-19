"""重跑覆盖旧结果；分类延伸到具体主题并清掉空类别；精读和对话带上附录全文且不调用工具。"""

import shutil
import tempfile
import unittest

from app import database
from app.models import Folder, Paper, PaperArtifactVersion
from harness import config
from harness.chat import build_chat_messages, build_chat_tools
from harness.document import PaperDocument
from harness.paper import PaperInput
from harness.runner import build_skill_messages
from harness.skills import get_skill
from knowledge import store


PAPER = """
[Page 1]

Abstract

Agents can keep improving after deployment.

[Page 2]

References

[1] Someone, 2024.

[Page 3]

Appendix

The RSI proof contains APPENDIX_SECRET.
"""


class ReadingFlowTests(unittest.TestCase):
    def setUp(self):
        self.original = config.get_base_dir()
        self.dirs = []

    def tearDown(self):
        config.set_data_dir(self.original)
        database.reopen_db()
        for path in self.dirs:
            shutil.rmtree(path, ignore_errors=True)

    def make(self):
        path = tempfile.mkdtemp(prefix="arxiv-reading-")
        self.dirs.append(path)
        config.set_data_dir(path)
        database.reopen_db()
        database.init_db()
        return path

    def test_full_pdf_context_includes_appendix_without_tools(self):
        rendered = PaperDocument(PAPER).render_complete()
        self.assertIn("APPENDIX_SECRET", rendered.text)
        self.assertIn("appendix", rendered.sections)
        paper = PaperInput.from_any({"title": "RSI agents", "full_text": PAPER})
        messages, _info = build_chat_messages(paper, [], "附录里的证明是什么？")
        self.assertIn("APPENDIX_SECRET", messages[0]["content"])
        self.assertEqual(build_chat_tools(paper), [])
        deep = get_skill("deep_reading")
        self.assertEqual(deep.tools, ())
        self.assertEqual(deep.max_tool_rounds, 0)
        skill_messages, info = build_skill_messages(deep, paper)
        self.assertIn("APPENDIX_SECRET", skill_messages[1]["content"])
        self.assertFalse(info["context_truncated"])

    def test_rerun_replaces_the_previous_artifact(self):
        self.make()
        db = database.SessionLocal()
        paper = Paper(arxiv_id="local-reading", title="Reading")
        db.add(paper)
        db.commit()
        store.upsert_artifact(db, paper.id, store.ARTIFACT_SUMMARY, "第一版")
        db.add(PaperArtifactVersion(paper_id=paper.id, kind=store.ARTIFACT_SUMMARY, content_md="旧历史"))
        db.commit()
        store.upsert_artifact(db, paper.id, store.ARTIFACT_SUMMARY, "第二版")
        current = store.get_artifact(db, paper.id, store.ARTIFACT_SUMMARY)
        versions = db.query(PaperArtifactVersion).filter_by(paper_id=paper.id).count()
        db.close()
        self.assertEqual(current.content_md, "第二版")
        self.assertEqual(versions, 0)

    def test_finer_category_moves_paper_and_drops_empty_category(self):
        self.make()
        db = database.SessionLocal()
        inbox = Folder(name="待整理")
        db.add(inbox)
        db.flush()
        paper = Paper(arxiv_id="local-rsi", title="RSI", folder_id=inbox.id)
        db.add(paper)
        db.commit()
        parent = store.get_or_create_node(db, ["智能体后训练"])
        store.sync_node_folders(db, parent)
        empty = store.get_or_create_node(db, ["无人使用"])
        store.sync_node_folders(db, empty)
        store.set_paper_memberships(db, paper, [{
            "node_id": parent.id,
            "path": ["智能体后训练", "RSI"],
            "role": "primary",
            "reason": "核心工作是 RSI",
            "confidence": 0.9,
        }])
        db.refresh(paper)
        self.assertEqual(paper.folder.name, "RSI")
        self.assertEqual(db.get(Folder, paper.folder.parent_id).name, "智能体后训练")
        removed = store.prune_empty_directions(db)
        self.assertIn("无人使用", removed)
        self.assertIsNotNone(store.get_node_by_path(db, "智能体后训练"))
        self.assertIsNotNone(store.get_node_by_path(db, "智能体后训练/RSI"))
        self.assertIsNone(store.get_node_by_path(db, "无人使用"))
        db.close()
