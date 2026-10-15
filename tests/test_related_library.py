"""关联候选只加入文献库：不跑阅读流程，已丢弃候选不出现在列表里。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import config

config.set_data_dir(tempfile.mkdtemp(prefix="arxivreader-related-"))

from app.arxiv_service import PaperMeta  # noqa: E402
from app.database import SessionLocal, init_db  # noqa: E402
from app.models import Paper, PaperArtifact, PipelineRun, RelatedCandidate  # noqa: E402
from knowledge import related, store  # noqa: E402


def _paper(db, arxiv_id, title="Source"):
    row = Paper(arxiv_id=arxiv_id, title=title, authors_json="[]", abstract="abs",
                categories_json="[]", full_text="body")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _candidate(db, source, arxiv_id, status="pending"):
    row = RelatedCandidate(source_paper_id=source.id, arxiv_id=arxiv_id, title="Candidate " + arxiv_id,
                           status=status, relevance="strong", relevance_score=0.9,
                           relevance_reason="same method")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _meta(arxiv_id):
    return PaperMeta(arxiv_id=arxiv_id, title="Downloaded " + arxiv_id, authors=["Ada"],
                     abstract="abstract", categories=["cs.CL"], published="2024-01-02",
                     pdf_url="https://arxiv.org/pdf/" + arxiv_id)


def _download(meta, dest_dir, filename=None):
    path = Path(dest_dir) / (meta.arxiv_id.replace("/", "_") + ".pdf")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\n")
    return path


class RelatedLibraryTests(unittest.TestCase):
    def test_promote_adds_to_library_without_reading_pipeline(self):
        init_db()
        db = SessionLocal()
        try:
            source = _paper(db, "1111.11111")
            row = _candidate(db, source, "2222.22222")
            with patch("knowledge.related.arxiv_service.fetch_metadata", return_value=_meta("2222.22222")), \
                 patch("knowledge.related.arxiv_service.download_pdf", side_effect=_download), \
                 patch("knowledge.related.pdf_service.extract_text", return_value="full text"), \
                 patch("knowledge.pipeline.run_pipeline") as run_pipeline:
                result = related.promote_related_candidate(db, row)
                run_pipeline.assert_not_called()
            paper = db.get(Paper, result["paper_id"])
            self.assertEqual(paper.arxiv_id, "2222.22222")
            self.assertIsNone(paper.folder_id)
            self.assertEqual(paper.full_text, "full text")
            self.assertEqual(db.query(PipelineRun).count(), 0)
            self.assertEqual(db.query(PaperArtifact).filter_by(paper_id=paper.id).count(), 0)
            db.refresh(row)
            self.assertEqual(row.status, "promoted")
            self.assertEqual(row.promote_state, "done")
            self.assertEqual(row.promoted_paper_id, paper.id)

            with patch("knowledge.related.arxiv_service.download_pdf") as download:
                again = related.promote_related_candidate(db, row)
                download.assert_not_called()
            self.assertEqual(again["paper_id"], paper.id)
            self.assertEqual(db.query(Paper).filter_by(arxiv_id="2222.22222").count(), 1)
        finally:
            db.close()

    def test_existing_library_paper_is_linked_without_moving_it(self):
        init_db()
        db = SessionLocal()
        try:
            source = _paper(db, "3333.33333")
            placed = _paper(db, "4444.44444", title="Already here")
            row = _candidate(db, source, "4444.44444")
            with patch("knowledge.related.arxiv_service.download_pdf") as download:
                result = related.promote_related_candidate(db, row)
                download.assert_not_called()
            self.assertEqual(result["paper_id"], placed.id)
            db.refresh(placed)
            self.assertEqual(placed.title, "Already here")
            self.assertIsNone(placed.folder_id)
        finally:
            db.close()

    def test_discarded_candidates_stay_hidden(self):
        init_db()
        db = SessionLocal()
        try:
            source = _paper(db, "5555.55555")
            kept = _candidate(db, source, "6666.66666")
            dropped = _candidate(db, source, "7777.77777", status="discarded")
            payload = store.related_candidates_payload(db, source.id)
            ids = {item["arxiv_id"] for item in payload}
            self.assertIn(kept.arxiv_id, ids)
            self.assertNotIn(dropped.arxiv_id, ids)
            self.assertTrue(all("library_paper_id" in item for item in payload))
        finally:
            db.close()

    def test_promote_endpoint_ignores_pipeline_flag_and_hidden_discards(self):
        init_db()
        from fastapi.testclient import TestClient
        from app.main import app

        db = SessionLocal()
        try:
            source = _paper(db, "8888.88888")
            row = _candidate(db, source, "9999.99999")
            dropped = _candidate(db, source, "1010.10101", status="discarded")
            source_id, row_id, dropped_id = source.id, row.id, dropped.arxiv_id
        finally:
            db.close()

        with TestClient(app) as client, \
             patch("knowledge.related.arxiv_service.fetch_metadata", return_value=_meta("9999.99999")), \
             patch("knowledge.related.arxiv_service.download_pdf", side_effect=_download), \
             patch("knowledge.related.pdf_service.extract_text", return_value="text"), \
             patch("knowledge.pipeline.run_pipeline") as run_pipeline:
            view = client.get("/api/papers/{}/related?include_discarded=true".format(source_id))
            self.assertEqual(view.status_code, 200)
            listed = {item["arxiv_id"] for item in view.json()["candidates"]}
            self.assertIn("9999.99999", listed)
            self.assertNotIn(dropped_id, listed)
            promoted = client.post(
                "/api/related/candidates/{}/promote?background=false&trigger_pipeline=true".format(row_id))
            self.assertEqual(promoted.status_code, 200, promoted.text)
            body = promoted.json()
            self.assertEqual(body["promote_state"], "done")
            self.assertIsNone(body["pipeline"])
            run_pipeline.assert_not_called()

        db = SessionLocal()
        try:
            paper = db.get(Paper, body["paper_id"])
            self.assertIsNone(paper.folder_id)
            self.assertEqual(db.query(PipelineRun).filter_by(paper_id=paper.id).count(), 0)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
