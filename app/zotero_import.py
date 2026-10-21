"""从本机 Zotero 文库导入论文到 ArxivReader（数据迁移入口）。

Zotero 的本地数据目录（默认 ~/Zotero）里：
  zotero.sqlite  元数据库（条目、作者、DOI、收藏夹）
  storage/<附件key>/<文件名>.pdf  每篇 PDF 的实际存放位置

Zotero 运行时数据库处于 WAL 锁定状态，直接读有风险，因此先整份复制到
临时目录再打开（只读，绝不写原库）。PDF 只复制不移动，导入后 Zotero 文库原样保留。

身份与去重（与 from-arxiv / upload 同一套口径）：
  条目的 url / DOI / extra 里能解析出 arXiv id → 用 arXiv id 入库；
  否则按 PDF 内容 SHA-256 生成 local- 前缀 id。已存在的跳过、不重复导入。

收藏夹映射：勾选时按 Zotero 收藏夹层级镜像建 Folder 树；一篇论文在多个
收藏夹时取路径最深的那个（ArxivReader 里一篇论文只属一个文件夹）。
"""

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile

from . import arxiv_service, pdf_service
from .models import LOCAL_ID_PREFIX, Folder, Paper

logger = logging.getLogger(__name__)

DEFAULT_DIR = os.path.expanduser(os.path.join("~", "Zotero"))
_ARXIV_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5}|[a-z\-]+/[0-9]{7})", re.I)
_DOI_RE = re.compile(r"10\.48550/arXiv\.([0-9]{4}\.[0-9]{4,5})", re.I)
_EXTRA_RE = re.compile(r"arXiv:\s*([0-9]{4}\.[0-9]{4,5}|[a-z\-]+/[0-9]{7})", re.I)


class ZoteroError(Exception):
    def __init__(self, code, params=None):
        super().__init__(code)
        self.code = code
        self.params = params or {}


def default_dir():
    return DEFAULT_DIR


def validate_dir(path):
    path = os.path.abspath(os.path.expanduser((path or "").strip() or DEFAULT_DIR))
    if not os.path.isdir(path) or not os.path.isfile(os.path.join(path, "zotero.sqlite")):
        raise ZoteroError("zotero.not_found")
    return path


# ---------------------------------------------------------------- 读取 Zotero 库
def _open_copy(zotero_dir):
    """复制 sqlite（含 WAL/SHM）到临时目录后打开，避免与正在运行的 Zotero 抢锁。"""
    tmp = tempfile.mkdtemp(prefix="arxiv-zotero-")
    for suffix in ("", "-wal", "-shm"):
        src = os.path.join(zotero_dir, "zotero.sqlite" + suffix)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(tmp, "zotero.sqlite" + suffix))
    conn = sqlite3.connect(os.path.join(tmp, "zotero.sqlite"))
    conn.row_factory = sqlite3.Row
    return conn, tmp


def _field_map(conn):
    """fieldName -> fieldID（title/date/url/DOI/abstractNote/extra 等）。"""
    return {r["fieldName"]: r["fieldID"]
            for r in conn.execute("SELECT fieldID, fieldName FROM fields")}


def _item_fields(conn, fmap, item_ids):
    """父条目的字段值：{itemID: {fieldName: value}}。"""
    wanted = {k: v for k, v in fmap.items()
              if k in ("title", "date", "url", "DOI", "abstractNote", "extra")}
    if not wanted or not item_ids:
        return {}
    marks = ",".join(str(i) for i in item_ids)
    out = {}
    rows = conn.execute(
        "SELECT d.itemID, d.fieldID, v.value FROM itemData d "
        "JOIN itemDataValues v ON d.valueID = v.valueID "
        "WHERE d.itemID IN (%s)" % marks)
    for row in rows:
        name = next((n for n, fid in wanted.items() if fid == row["fieldID"]), None)
        if name:
            out.setdefault(row["itemID"], {})[name] = row["value"]
    return out


def _item_creators(conn, item_ids):
    """兼容两代 Zotero schema：新版 creators 表直接带 firstName/lastName（fieldMode=1 时
    名字整体在 lastName），旧版（≤4）要再联 creatorData。"""
    if not item_ids:
        return {}
    creator_cols = {row[1] for row in conn.execute("PRAGMA table_info(creators)")}
    marks = ",".join(str(i) for i in item_ids)
    out = {}
    if "firstName" in creator_cols:                     # Zotero 5+
        rows = conn.execute(
            "SELECT ic.itemID, c.firstName, c.lastName, c.fieldMode FROM itemCreators ic "
            "JOIN creators c ON ic.creatorID = c.creatorID "
            "WHERE ic.itemID IN (%s) ORDER BY ic.itemID, ic.orderIndex" % marks)
        for row in rows:
            name = row["lastName"] if row["fieldMode"] == 1 else \
                " ".join(p for p in (row["firstName"], row["lastName"]) if p).strip()
            if name:
                out.setdefault(row["itemID"], []).append(name)
        return out
    rows = conn.execute(
        "SELECT ic.itemID, cd.firstName, cd.lastName FROM itemCreators ic "
        "JOIN creators c ON ic.creatorID = c.creatorID "
        "JOIN creatorData cd ON c.creatorDataID = cd.creatorDataID "
        "WHERE ic.itemID IN (%s) ORDER BY ic.itemID, ic.orderIndex" % marks)
    for row in rows:
        name = " ".join(p for p in (row["firstName"], row["lastName"]) if p).strip()
        if name:
            out.setdefault(row["itemID"], []).append(name)
    return out


def _collections(conn):
    """收藏夹层级：{collectionID: {"name", "parent", "path"}} + 条目归属。"""
    cols = {}
    for row in conn.execute(
            "SELECT collectionID, collectionName, parentCollectionID FROM collections"):
        cols[row["collectionID"]] = {"name": row["collectionName"],
                                     "parent": row["parentCollectionID"]}
    for cid, col in cols.items():
        parts, cur, seen = [], cid, set()
        while cur and cur in cols and cur not in seen:
            seen.add(cur)
            parts.append(cols[cur]["name"])
            cur = cols[cur]["parent"]
        col["path"] = "/".join(reversed(parts))
    membership = {}
    for row in conn.execute("SELECT collectionID, itemID FROM collectionItems"):
        path = cols.get(row["collectionID"], {}).get("path")
        if path:
            membership.setdefault(row["itemID"], []).append(path)
    return cols, membership


def _arxiv_id_of(fields):
    for pattern, key in ((_ARXIV_RE, "url"), (_DOI_RE, "DOI"), (_EXTRA_RE, "extra")):
        match = pattern.search(fields.get(key) or "")
        if match:
            try:
                return arxiv_service.strip_version(
                    arxiv_service.normalize_arxiv_id(match.group(1)))
            except ValueError:
                continue
    return None


def _translation_kind(filename):
    """判断附件是不是「已翻译好的 PDF」：pdf2zh 系产物命名 xxx.mono.pdf /
    xxx.dual.pdf / xxx.compare.pdf，或文件名带 对照/双语/翻译。返回 "mono"/"dual"/None。"""
    lower = (filename or "").lower()
    if re.search(r"[.\-_]mono\.pdf$", lower):
        return "mono"
    if re.search(r"[.\-_](dual|compare)\.pdf$", lower):
        return "dual"
    if re.search(r"(对照|双语)", filename or ""):
        return "dual"
    if re.search(r"[.\-_]mono[.\-_]", lower):
        return "mono"
    return None


def scan(zotero_dir):
    """扫描 Zotero 库：返回可导入条目（含 PDF 的）与统计。

    同一 parent 条目可能挂多个 PDF：第一个非翻译附件当正文；mono/dual/compare
    这类「已翻译好的 PDF」收进 translations，导入时登记为已完成的全文翻译任务；
    其余多余附件（附录等）跳过并计数。"""
    zotero_dir = validate_dir(zotero_dir)
    conn, tmp = _open_copy(zotero_dir)
    try:
        attach_type = conn.execute(
            "SELECT itemTypeID FROM itemTypes WHERE typeName='attachment'").fetchone()
        if attach_type is None:
            raise ZoteroError("zotero.invalid")
        rows = conn.execute(
            "SELECT a.itemID, a.parentItemID, a.path, i.key FROM itemAttachments a "
            "JOIN items i ON a.itemID = i.itemID "
            "WHERE a.contentType='application/pdf' AND a.parentItemID IS NOT NULL "
            "ORDER BY a.parentItemID, a.itemID").fetchall()
        by_parent = {}
        for row in rows:
            by_parent.setdefault(row["parentItemID"], []).append(row)
        parent_ids = sorted(by_parent)
        fields = _item_fields(conn, _field_map(conn), parent_ids)
        creators = _item_creators(conn, parent_ids)
        cols, membership = _collections(conn)
        notes = conn.execute("SELECT COUNT(*) FROM itemNotes").fetchone()[0]

        papers = []
        missing_file = 0
        extra_pdfs = 0
        for pid in parent_ids:
            primaries = []
            translations = []
            for row in by_parent[pid]:
                stored = row["path"] or ""
                if not stored.startswith("storage:"):
                    continue                  # 链接式附件/快照不在本机，跳过
                filename = stored.split(":", 1)[1].strip()
                pdf = os.path.join(zotero_dir, "storage", row["key"], filename)
                if not os.path.isfile(pdf):
                    continue                  # 文件没同步到本机：不计入
                kind = _translation_kind(filename)
                entry = {"kind": kind, "pdf_path": pdf, "name": filename}
                (translations if kind else primaries).append(entry)
            if not primaries:
                missing_file += 1             # 条目在本机没有正文 PDF
                continue
            extra_pdfs += len(primaries) - 1
            # 译文每种版式（mono/dual）只留最早一个
            seen_kinds = set()
            kept_translations = []
            for t in translations:
                if t["kind"] in seen_kinds:
                    continue
                seen_kinds.add(t["kind"])
                kept_translations.append(t)
            primary = primaries[0]
            meta = fields.get(pid, {})
            filename = primary["name"]
            title = (meta.get("title") or "").strip() or os.path.splitext(filename)[0]
            papers.append({
                "title": title,
                "authors": creators.get(pid, []),
                "published": (meta.get("date") or "").strip() or None,
                "abstract": (meta.get("abstractNote") or "").strip(),
                "arxiv_id": _arxiv_id_of(meta),
                "pdf_path": primary["pdf_path"],
                "translations": kept_translations,
                "collections": sorted(membership.get(pid, [])),
            })
        papers.sort(key=lambda p: p["title"].lower())
        return {
            "path": zotero_dir,
            "papers": papers,
            "stats": {
                "with_pdf": len(papers),
                "missing_file": missing_file,
                "collections": len(cols),
                "arxiv": sum(1 for p in papers if p["arxiv_id"]),
                "translations": sum(len(p["translations"]) for p in papers),
                "extra_pdfs": extra_pdfs,
                "notes": notes,
            },
        }
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


def count_existing(db, papers):
    """扫描后标注已在库中的条目（按 arXiv id；内容哈希重复的导入时再判）。"""
    existing = {row[0] for row in
                db.query(Paper.arxiv_id).filter(Paper.arxiv_id.isnot(None)).all()}
    known = 0
    for paper in papers:
        paper["exists"] = bool(paper["arxiv_id"] and paper["arxiv_id"] in existing)
        known += 1 if paper["exists"] else 0
    return known


# ---------------------------------------------------------------- 导入
def _ensure_folder(db, cache, path):
    """按 a/b/c 路径建（或取）Folder 树，返回叶子 Folder。"""
    if path in cache:
        return cache[path]
    parent = None
    parts = [p for p in path.split("/") if p]
    for depth, _part in enumerate(parts):
        prefix = "/".join(parts[:depth + 1])
        if prefix in cache:
            parent = cache[prefix]
            continue
        name = parts[depth][:255]
        folder = (db.query(Folder)
                  .filter(Folder.name == name,
                          Folder.parent_id == (parent.id if parent else None))
                  .first())
        if folder is None:
            folder = Folder(name=name, parent_id=parent.id if parent else None)
            db.add(folder)
            db.flush()
        cache[prefix] = folder
        parent = folder
    return cache[path]


def _pick_collection(collections):
    """多收藏夹取路径最深的（同深度按字典序，保证可复现）。"""
    if not collections:
        return None
    return sorted(collections, key=lambda p: (-p.count("/"), p))[0]


def _import_translations(db, paper, translations):
    """把 Zotero 里已翻译好的附件登记为「已完成」的全文翻译任务：
    文件复制进翻译输出目录（pdftranslate/output/task-<id>/），mono/dual 路径入任务，
    阅读器里的 原文/对照版 切换立即可用。已有同版式译文时不重复登记。
    返回是否实际新增了译文。"""
    import datetime as _dt

    from .models import PdfTranslationTask
    from .pdftranslate import env as pdf_env

    done_tasks = (db.query(PdfTranslationTask)
                  .filter_by(paper_id=paper.id, status="done").all())
    have_mono = any(t.mono_path and os.path.isfile(t.mono_path) for t in done_tasks)
    have_dual = any(t.dual_path and os.path.isfile(t.dual_path) for t in done_tasks)
    want = {}
    for t in translations or []:
        want.setdefault(t["kind"], t)
    if (have_mono or "mono" not in want) and (have_dual or "dual" not in want):
        return False

    task = PdfTranslationTask(
        paper_id=paper.id, status="done", progress=100,
        message="来自 Zotero 的已有译文",
        options_json=json.dumps({"source": "zotero"}, ensure_ascii=False),
        finished_at=_dt.datetime.utcnow())
    db.add(task)
    db.flush()                                   # 拿到 task.id 组织输出目录
    dest_dir = os.path.join(pdf_env.output_dir(), "task-{}".format(task.id))
    os.makedirs(dest_dir, exist_ok=True)
    copied = False
    for kind, has in (("mono", have_mono), ("dual", have_dual)):
        entry = want.get(kind)
        if not entry or has:
            continue
        try:
            stem = arxiv_service.safe_dirname(os.path.splitext(entry["name"])[0])[:80] or "translation"
            dest = os.path.join(dest_dir, "{}.{}.pdf".format(stem, kind))
            shutil.copy2(entry["pdf_path"], dest)
        except OSError as exc:
            logger.info("Zotero 译文复制失败（%s）：%s", entry["name"], str(exc)[:200])
            continue
        if kind == "mono":
            task.mono_path = dest
        else:
            task.dual_path = dest
        copied = True
    if not copied:
        db.delete(task)
        db.flush()
        return False
    return True


def import_library(db, papers, zotero_dir, use_collections, on_progress, cancel):
    """执行导入；返回统计。每个条目独立容错（坏 PDF 记 failed 继续）。

    已在库中的论文不重复导入，但若 Zotero 条目带有「已翻译好的 PDF」而该论文
    还没有对应版式的译文任务，会把译文补进来（重复执行即可补齐历史译文）。"""
    from harness.config import get_pdf_dir

    existing = {row[0] for row in
                db.query(Paper.arxiv_id).filter(Paper.arxiv_id.isnot(None)).all()}
    folder_cache = {}
    stats = {"imported": 0, "skipped_existing": 0, "failed": 0, "folders": 0,
             "translations_added": 0}
    total = len(papers)
    on_progress("copy", 0, total, "")

    for index, item in enumerate(papers):
        if cancel.is_set():
            raise ZoteroError("zotero.cancelled")
        title = item["title"][:80]
        on_progress("copy", index, total, title)
        try:
            with open(item["pdf_path"], "rb") as fh:
                content = fh.read()
            digest = hashlib.sha256(content).hexdigest()
            paper_id = item["arxiv_id"] or (LOCAL_ID_PREFIX + digest[:16])
            if paper_id in existing:
                paper = db.query(Paper).filter(Paper.arxiv_id == paper_id).first()
                if (paper is not None and item.get("translations")
                        and _import_translations(db, paper, item["translations"])):
                    db.commit()
                    stats["translations_added"] += 1
                else:
                    stats["skipped_existing"] += 1
                continue

            folder = None
            if use_collections:
                chosen = _pick_collection(item["collections"])
                if chosen:
                    folder = _ensure_folder(db, folder_cache, chosen)
            dest_dir = os.path.join(
                get_pdf_dir(),
                arxiv_service.safe_dirname(folder.name if folder else arxiv_service.UNFILED_DIR))
            os.makedirs(dest_dir, exist_ok=True)
            stem = arxiv_service.safe_dirname(item["title"])[:80] or "paper"
            dest = os.path.join(dest_dir, "{}-{}.pdf".format(stem, digest[:8]))
            with open(dest, "wb") as fh:
                fh.write(content)

            on_progress("extract", index, total, title)
            try:
                text = pdf_service.extract_text(dest)
            except Exception as exc:  # noqa: BLE001  扫描版/损坏：入库但全文为空
                logger.info("Zotero 导入：文本抽取失败（%s）：%s", title, str(exc)[:200])
                text = ""

            paper = Paper(
                arxiv_id=paper_id,
                title=item["title"][:4000] or stem,
                authors_json=json.dumps(item["authors"][:50], ensure_ascii=False),
                abstract=item["abstract"][:8000],
                categories_json="[]",
                published=item["published"],
                pdf_url=("https://arxiv.org/pdf/{}".format(item["arxiv_id"])
                         if item["arxiv_id"] else None),
                pdf_path=dest,
                folder_id=folder.id if folder else None,
                full_text=text,
            )
            db.add(paper)
            db.flush()                           # 先拿 paper.id 再登记译文任务
            if item.get("translations"):
                if _import_translations(db, paper, item["translations"]):
                    stats["translations_added"] += 1
            db.commit()
            existing.add(paper_id)
            stats["imported"] += 1
        except ZoteroError:
            raise
        except Exception as exc:  # noqa: BLE001  单条失败不阻断整体导入
            logger.warning("Zotero 导入条目失败（%s）：%s", title, str(exc)[:300])
            db.rollback()
            stats["failed"] += 1

    stats["folders"] = len(folder_cache)
    on_progress("done", total, total, "")
    return stats
