"""对话轮次：编辑会丢掉后续回复，重新生成只重做当前轮，删除会话会级联清消息。"""

import shutil
import tempfile
import unittest

from app import database
from app.models import ChatMessage, ChatSession, Paper
from harness import config


class ChatTurnTests(unittest.TestCase):
    def setUp(self):
        self.original = config.get_base_dir()
        self.dirs = []

    def tearDown(self):
        config.set_data_dir(self.original)
        database.reopen_db()
        for path in self.dirs:
            shutil.rmtree(path, ignore_errors=True)

    def make(self):
        path = tempfile.mkdtemp(prefix="arxiv-chat-")
        self.dirs.append(path)
        config.set_data_dir(path)
        database.reopen_db()
        database.init_db()
        return path

    def seed(self, rounds=2):
        self.make()
        db = database.SessionLocal()
        paper = Paper(arxiv_id="local-chat-turns", title="Chat paper", full_text="paper body")
        db.add(paper)
        db.flush()
        session = ChatSession(paper_id=paper.id, title="old title")
        db.add(session)
        db.flush()
        messages = []
        for index in range(rounds):
            user = ChatMessage(session_id=session.id, role="user", content="question %s" % (index + 1))
            assistant = ChatMessage(session_id=session.id, role="assistant", content="answer %s" % (index + 1))
            db.add(user)
            db.add(assistant)
            messages.extend([user, assistant])
        db.commit()
        ids = {
            "paper": paper.id,
            "session": session.id,
            "messages": [row.id for row in messages],
        }
        db.close()
        return ids

    def rows(self, session_id):
        db = database.SessionLocal()
        try:
            return [(row.id, row.role, row.content) for row in
                    db.query(ChatMessage).filter(ChatMessage.session_id == session_id).order_by(ChatMessage.id).all()]
        finally:
            db.close()

    def title(self, session_id):
        db = database.SessionLocal()
        try:
            return db.get(ChatSession, session_id).title
        finally:
            db.close()

    def test_edit_drops_later_turns_and_retitles(self):
        ids = self.seed()
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as client:
            response = client.post(
                "/api/sessions/%s/messages/%s/edit" % (ids["session"], ids["messages"][0]),
                json={"content": "rewritten opening"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("尚未配置", response.text)
        self.assertEqual(self.rows(ids["session"]), [
            (ids["messages"][0], "user", "rewritten opening"),
        ])
        self.assertEqual(self.title(ids["session"]), "rewritten opening")

    def test_edit_rejects_assistant_message(self):
        ids = self.seed()
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as client:
            response = client.post(
                "/api/sessions/%s/messages/%s/edit" % (ids["session"], ids["messages"][1]),
                json={"content": "nope"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.rows(ids["session"])[1][2], "answer 1")

    def test_regenerate_keeps_last_question(self):
        ids = self.seed()
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as client:
            response = client.post("/api/sessions/%s/regenerate" % ids["session"], json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.rows(ids["session"]), [
            (ids["messages"][0], "user", "question 1"),
            (ids["messages"][1], "assistant", "answer 1"),
            (ids["messages"][2], "user", "question 2"),
        ])

    def test_regenerate_without_question(self):
        self.make()
        db = database.SessionLocal()
        paper = Paper(arxiv_id="local-empty-chat", title="Empty")
        db.add(paper)
        db.flush()
        session = ChatSession(paper_id=paper.id, title="empty")
        db.add(session)
        db.commit()
        session_id = session.id
        db.close()
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as client:
            response = client.post("/api/sessions/%s/regenerate" % session_id, json={})
        self.assertEqual(response.status_code, 400)

    def test_delete_session_removes_messages(self):
        ids = self.seed(rounds=1)
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as client:
            response = client.delete("/api/sessions/%s" % ids["session"])
            missing = client.get("/api/sessions/%s/messages" % ids["session"])
        self.assertEqual(response.status_code, 204)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(self.rows(ids["session"]), [])
