"""Zotero 导入：假 Zotero 库（最小 sqlite schema + storage 目录）端到端。

覆盖：扫描统计、arXiv 识别（DOI/url/extra）、收藏夹→文件夹树、
去重跳过、缺文件计数、use_collections 关闭、坏目录报错。
"""

import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from app import database, zotero_import
from app.models import Folder, Paper
from harness import config

PDF_A = b"%PDF-1.4 zotero-a"
PDF_B = b"%PDF-1.4 zotero-b"

SCHEMA = """
CREATE TABLE itemTypes (itemTypeID INTEGER PRIMARY KEY, typeName TEXT);
CREATE TABLE items (itemID INTEGER PRIMARY KEY, itemTypeID INTEGER, key TEXT);
CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
CREATE TABLE creatorData (creatorDataID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT);
CREATE TABLE creators (creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT, fieldMode INTEGER DEFAULT 0);
CREATE TABLE itemCreators (itemID INTEGER, creatorID INTEGER, orderIndex INTEGER);
CREATE TABLE itemAttachments (itemID INTEGER PRIMARY KEY, parentItemID INTEGER, path TEXT, contentType TEXT);
CREATE TABLE collections (collectionID INTEGER PRIMARY KEY, collectionName TEXT, parentCollectionID INTEGER);
CREATE TABLE collectionItems (collectionID INTEGER, itemID INTEGER);
"""


def make_zotero_dir():
    """造一个假 Zotero 数据目录：两篇有 PDF（一篇带 arXiv DOI），一篇缺文件。"""
    root = tempfile.mkdtemp(prefix="arxiv-fake-zotero-")
    conn = sqlite3.connect(os.path.join(root, "zotero.sqlite"))
    conn.executescript(SCHEMA)
    conn.executemany("INSERT INTO itemTypes VALUES (?,?)",
                     [(1, "attachment"), (2, "journalArticle")])
    conn.executemany("INSERT INTO fields VALUES (?,?)",
                     [(1, "title"), (2, "date"), (3, "url"), (4, "DOI"),
                      (5, "abstractNote"), (6, "extra")])
    # 父条目 A（带 arXiv DOI）/ B（纯本地）/ C（PDF 缺失）
    conn.executemany("INSERT INTO items VALUES (?,?,?)",
                     [(100, 2, "PARENTA"), (101, 2, "PARENTB"), (102, 2, "PARENTC"),
                      (200, 1, "ATTKEY1"), (201, 1, "ATTKEY2"), (202, 1, "ATTKEY3")])
    values = [(1, "Attention Paper"), (2, "2023-05-01"), (3, "10.48550/arXiv.2305.12345"),
              (4, "An abstract."), (5, "Local Only"), (6, "Missing File")]
    conn.executemany("INSERT INTO itemDataValues VALUES (?,?)", values)
    conn.executemany("INSERT INTO itemData VALUES (?,?,?)",
                     [(100, 1, 1), (100, 2, 2), (100, 4, 3), (100, 5, 4),
                      (101, 1, 5), (102, 1, 6)])
    conn.execute("INSERT INTO creators VALUES (10,'Jane','Doe',0)")
    conn.execute("INSERT INTO itemCreators VALUES (100,10,0)")
    conn.executemany("INSERT INTO itemAttachments VALUES (?,?,?,?)",
                     [(200, 100, "storage:paper a.pdf", "application/pdf"),
                      (201, 101, "storage:paper b.pdf", "application/pdf"),
                      (202, 102, "storage:missing.pdf", "application/pdf")])
    conn.executemany("INSERT INTO collections VALUES (?,?,?)",
                     [(1, "ML", None), (2, "RL", 1)])
    conn.executemany("INSERT INTO collectionItems VALUES (?,?)", [(1, 100), (2, 100)])
    conn.commit()
    conn.close()
    for key, name, data in (("ATTKEY1", "paper a.pdf", PDF_A), ("ATTKEY2", "paper b.pdf", PDF_B)):
        folder = os.path.join(root, "storage", key)
        os.makedirs(folder)
        Path(os.path.join(folder, name)).write_bytes(data)
    return root


class ZoteroUnitTests(unittest.TestCase):
    def test_validate_dir_rejects_bad_path(self):
        with self.assertRaises(zotero_import.ZoteroError) as caught:
            zotero_import.validate_dir(tempfile.mkdtemp())
        self.assertEqual(caught.exception.code, "zotero.not_found")

    def test_arxiv_id_from_url_doi_extra(self):
        f = zotero_import._arxiv_id_of
        self.assertEqual(f({"url": "https://arxiv.org/abs/2401.12345v2"}), "2401.12345")
        self.assertEqual(f({"DOI": "10.48550/arXiv.2305.12345"}), "2305.12345")
        self.assertEqual(f({"extra": "PMID: 1\narXiv: hep-th/9901001"}), "hep-th/9901001")
        self.assertIsNone(f({"url": "https://example.com", "DOI": "10.1000/xyz"}))
        self.assertIsNone(f({}))

    def test_creators_legacy_schema(self):
        """旧版 Zotero（≤4）：creators 只有 id，名字在 creatorData 里。"""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            "CREATE TABLE creators (creatorID INTEGER PRIMARY KEY, creatorDataID INTEGER);"
            "CREATE TABLE creatorData (creatorDataID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT);"
            "CREATE TABLE itemCreators (itemID INTEGER, creatorID INTEGER, orderIndex INTEGER);"
            "INSERT INTO creatorData VALUES (1,'Ada','Lovelace');"
            "INSERT INTO creators VALUES (5,1);"
            "INSERT INTO itemCreators VALUES (42,5,0);")
        self.assertEqual(zotero_import._item_creators(conn, [42]), {42: ["Ada Lovelace"]})
        conn.close()

    def test_pick_deepest_collection(self):
        pick = zotero_import._pick_collection
        self.assertEqual(pick(["ML", "ML/RL"]), "ML/RL")
        self.assertEqual(pick(["B", "A"]), "A")          # 同深度按字典序，可复现
        self.assertIsNone(pick([]))


class ZoteroFlowTests(unittest.TestCase):
    def setUp(self):
        self.original = config.get_base_dir()
        self.dirs = []
        self.zotero = make_zotero_dir()
        self.dirs.append(self.zotero)
        self.data = tempfile.mkdtemp(prefix="arxiv-zotero-data-")
        self.dirs.append(self.data)
        config.set_data_dir(self.data)
        database.reopen_db()
        database.init_db()

    def tearDown(self):
        config.set_data_dir(self.original)
        database.reopen_db()
        for path in self.dirs:
            shutil.rmtree(path, ignore_errors=True)

    def test_scan_counts_and_metadata(self):
        result = zotero_import.scan(self.zotero)
        stats = result["stats"]
        self.assertEqual(stats["with_pdf"], 2)
        self.assertEqual(stats["missing_file"], 1)
        self.assertEqual(stats["collections"], 2)
        self.assertEqual(stats["arxiv"], 1)
        paper_a = next(p for p in result["papers"] if p["arxiv_id"])
        self.assertEqual(paper_a["arxiv_id"], "2305.12345")
        self.assertEqual(paper_a["title"], "Attention Paper")
        self.assertEqual(paper_a["authors"], ["Jane Doe"])
        self.assertEqual(paper_a["published"], "2023-05-01")
        self.assertEqual(sorted(paper_a["collections"]), ["ML", "ML/RL"])

    def test_import_then_reimport_skips_existing(self):
        result = zotero_import.scan(self.zotero)
        db = database.SessionLocal()
        try:
            stats = zotero_import.import_library(
                db, result["papers"], self.zotero, True,
                lambda *_a: None, threading.Event())
            self.assertEqual(stats["imported"], 2)
            self.assertEqual(stats["failed"], 0)

            paper_a = db.query(Paper).filter(Paper.arxiv_id == "2305.12345").one()
            self.assertEqual(paper_a.title, "Attention Paper")
            self.assertEqual(paper_a.pdf_url, "https://arxiv.org/pdf/2305.12345")
            self.assertTrue(os.path.isfile(paper_a.pdf_path))
            self.assertEqual(Path(paper_a.pdf_path).read_bytes(), PDF_A)
            folder = db.get(Folder, paper_a.folder_id)
            self.assertEqual(folder.name, "RL")                       # 最深的收藏夹
            self.assertEqual(db.get(Folder, folder.parent_id).name, "ML")

            paper_b = db.query(Paper).filter(Paper.title == "Local Only").one()
            self.assertTrue(paper_b.arxiv_id.startswith("local-"))
            self.assertIsNone(paper_b.folder_id)                      # 无收藏夹 → 全部论文

            # 再导一次：全部跳过，不重复建文件夹
            again = zotero_import.scan(self.zotero)
            stats2 = zotero_import.import_library(
                db, again["papers"], self.zotero, True,
                lambda *_a: None, threading.Event())
            self.assertEqual(stats2["imported"], 0)
            self.assertEqual(stats2["skipped_existing"], 2)
            self.assertEqual(db.query(Folder).count(), 2)
            self.assertEqual(db.query(Paper).count(), 2)
        finally:
            db.close()

    def test_import_without_collections_goes_unfiled(self):
        result = zotero_import.scan(self.zotero)
        db = database.SessionLocal()
        try:
            zotero_import.import_library(db, result["papers"], self.zotero, False,
                                         lambda *_a: None, threading.Event())
            self.assertEqual(db.query(Folder).count(), 0)
            self.assertEqual(db.query(Paper).filter(Paper.folder_id.isnot(None)).count(), 0)
        finally:
            db.close()

    def test_cancel_stops_import(self):
        result = zotero_import.scan(self.zotero)
        db = database.SessionLocal()
        cancel = threading.Event()
        cancel.set()
        try:
            with self.assertRaises(zotero_import.ZoteroError) as caught:
                zotero_import.import_library(db, result["papers"], self.zotero, True,
                                             lambda *_a: None, cancel)
            self.assertEqual(caught.exception.code, "zotero.cancelled")
            self.assertEqual(db.query(Paper).count(), 0)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
