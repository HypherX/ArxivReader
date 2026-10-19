"""总结和精读的追问留在自己的窗口里，普通对话页之间也不共用历史。"""

import shutil
import tempfile
import unittest

from app import database
from app.models import ChatMessage, ChatSession, Paper
from harness import config
from harness.chat import build_followup_messages
from harness.paper import PaperInput
from knowledge import store


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.original = config.get_base_dir()
        self.dirs = []

    def tearDown(self):
        config.set_data_dir(self.original)
        database.reopen_db()
        for path in self.dirs:
            shutil.rmtree(path, ignore_errors=True)

    def make(self):
        path = tempfile.mkdtemp(prefix="arxiv-follow-")
        self.dirs.append(path)
        config.set_data_dir(path)
        database.reopen_db()
        database.init_db()
        return path

    def test_windows_do_not_share_messages(self):
        self.make()
        db = database.SessionLocal()
        row = Paper(arxiv_id="local-follow", title="Follow", full_text="全文 APPENDIX_SECRET")
        db.add(row)
        db.commit()
        paper_id = row.id
        db.close()
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as client:
            first = client.post("/api/papers/%s/sessions" % paper_id, json={}).json()
            second = client.post("/api/papers/%s/sessions" % paper_id, json={"scope": "chat"}).json()
            summary = client.post("/api/papers/%s/sessions" % paper_id, json={"scope": "summary"}).json()
            again = client.post("/api/papers/%s/sessions" % paper_id, json={"scope": "summary"}).json()
            self.assertNotEqual(first["id"], second["id"])
            self.assertEqual(summary["id"], again["id"])
            self.assertEqual(summary["scope"], "summary")
            db = database.SessionLocal()
            db.add(ChatMessage(session_id=first["id"], role="user", content="只属于第一页"))
            db.add(ChatMessage(session_id=summary["id"], role="user", content="只属于总结追问"))
            db.commit()
            db.close()
            chats = client.get("/api/papers/%s/sessions" % paper_id).json()
            summary_page = client.get("/api/papers/%s/sessions?scope=summary" % paper_id).json()
            deep_page = client.get("/api/papers/%s/sessions?scope=deep_reading" % paper_id).json()
            self.assertEqual(sorted(item["id"] for item in chats), sorted([first["id"], second["id"]]))
            self.assertEqual([item["id"] for item in summary_page], [summary["id"]])
            self.assertEqual(deep_page, [])
            self.assertEqual(client.get("/api/sessions/%s/messages" % first["id"]).json()[0]["content"], "只属于第一页")
            self.assertEqual(client.get("/api/sessions/%s/messages" % second["id"]).json(), [])
            self.assertEqual(client.get("/api/sessions/%s/messages" % summary["id"]).json()[0]["content"], "只属于总结追问")

    def test_followup_context_contains_only_this_window(self):
        paper = PaperInput.from_any({"title": "RSI", "full_text": "正文\n\nAppendix\n\nAPPENDIX_SECRET"})
        messages, _info = build_followup_messages(
            paper,
            [{"role": "user", "content": "总结里的第一问"}, {"role": "assistant", "content": "总结里的第一答"}],
            "附录里写了什么？",
            "这份总结只谈到训练目标。",
            scope="summary",
        )
        system = messages[0]["content"]
        self.assertIn("APPENDIX_SECRET", system)
        self.assertIn("这份总结只谈到训练目标。", system)
        self.assertIn("当前窗口：快速总结", system)
        self.assertIn("不要引用其他窗口", system)
        self.assertEqual([item["content"] for item in messages[1:]], ["总结里的第一问", "总结里的第一答", "附录里写了什么？"])

    def test_replacing_artifact_clears_its_followup(self):
        self.make()
        db = database.SessionLocal()
        paper = Paper(arxiv_id="local-follow-reset", title="Reset")
        db.add(paper)
        db.commit()
        store.upsert_artifact(db, paper.id, store.ARTIFACT_SUMMARY, "第一版总结")
        session = ChatSession(paper_id=paper.id, scope="summary", title="快速总结")
        db.add(session)
        db.flush()
        db.add(ChatMessage(session_id=session.id, role="user", content="旧追问"))
        chat = ChatSession(paper_id=paper.id, scope="chat", title="新对话")
        db.add(chat)
        db.commit()
        chat_id = chat.id
        store.upsert_artifact(db, paper.id, store.ARTIFACT_SUMMARY, "第二版总结")
        scopes = [row.scope for row in db.query(ChatSession).filter_by(paper_id=paper.id).all()]
        kept = db.get(ChatSession, chat_id)
        db.close()
        self.assertEqual(scopes, ["chat"])
        self.assertIsNotNone(kept)
