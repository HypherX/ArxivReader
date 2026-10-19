"""本地资料清单、路径改写，以及用云端副本替换本机资料。

云端保存的是一份完整副本，不是两边修改的合并结果。
library.db 里的绝对路径在上传前改成相对标记，恢复时再写成新电脑上的路径。
"""

import datetime
import hashlib
import logging
import os
import shutil
import socket
import sqlite3
import tempfile
from pathlib import Path

from harness import config

from .errors import SyncError

logger = logging.getLogger(__name__)

PREFIX = "arxivreader-data:"
PAYLOAD_ROOTS = ("library.db", "pdfs", "knowledge", os.path.join("pdftranslate", "output"))
PATH_COLUMNS = (
    ("papers", "pdf_path"),
    ("direction_nodes", "graph_md_path"),
    ("pdf_translation_tasks", "mono_path"),
    ("pdf_translation_tasks", "dual_path"),
)
_DATA_ROOTS = ("pdfs", "knowledge", os.path.join("pdftranslate", "output"))


def safe_rel(rel):
    if not isinstance(rel, str) or not rel or len(rel) > 1024:
        return False
    if rel.startswith(("/", "\\")) or "\\" in rel or "\x00" in rel:
        return False
    parts = rel.split("/")
    return all(part not in ("", ".", "..") for part in parts)


def allowed_rel(rel):
    if rel == "library.db":
        return True
    return rel.startswith(("pdfs/", "knowledge/", "pdftranslate/output/"))


def to_portable(value, data_dir):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.startswith(PREFIX):
        return text
    full = os.path.abspath(text)
    try:
        rel = os.path.relpath(full, os.path.abspath(data_dir))
    except ValueError:
        return text
    rel = rel.replace("\\", "/")
    if not safe_rel(rel):
        return text
    return PREFIX + rel


def to_absolute(value, data_dir):
    if value is None:
        return None
    text = str(value).strip()
    if not text.startswith(PREFIX):
        return text
    rel = text[len(PREFIX):]
    if not safe_rel(rel):
        return text
    return os.path.abspath(os.path.join(data_dir, *rel.split("/")))


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def device_name():
    name = socket.gethostname() or "computer"
    cleaned = "".join(ch for ch in name if ch >= " " and ch != "\x7f").strip()
    return cleaned[:80] or "computer"


def _table_names(conn):
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def rewrite_database(db_path, data_dir, mode):
    converter = to_portable if mode == "portable" else to_absolute
    conn = sqlite3.connect(db_path)
    try:
        tables = _table_names(conn)
        for table, column in PATH_COLUMNS:
            if table not in tables:
                continue
            rows = list(conn.execute("SELECT rowid, {} FROM {}".format(column, table)))
            for rowid, value in rows:
                if not value:
                    continue
                updated = converter(value, data_dir)
                if updated != value:
                    conn.execute(
                        "UPDATE {} SET {}=? WHERE rowid=?".format(table, column),
                        (updated, rowid),
                    )
        conn.commit()
    finally:
        conn.close()


def snapshot_database(src, dest):
    if not os.path.isfile(src):
        raise SyncError("sync.no_library")
    with sqlite3.connect(src) as source, sqlite3.connect(dest) as destination:
        source.backup(destination)
        if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SyncError("sync.failed_plain")


def iter_data_files(data_dir):
    for folder in _DATA_ROOTS:
        root = os.path.join(data_dir, folder)
        if not os.path.isdir(root) or os.path.islink(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(
                name for name in dirnames
                if not name.startswith(".") and not os.path.islink(os.path.join(dirpath, name))
            )
            for name in sorted(filenames):
                if name.startswith(".") or name == "config.toml" or name.endswith(".partial"):
                    continue
                full = os.path.join(dirpath, name)
                if os.path.islink(full) or not os.path.isfile(full):
                    continue
                rel = os.path.relpath(full, data_dir).replace(os.sep, "/")
                if safe_rel(rel) and allowed_rel(rel):
                    yield rel, full


def collect_files(data_dir, library_snapshot):
    items = [{
        "relpath": "library.db",
        "path": library_snapshot,
        "sha256": sha256_file(library_snapshot),
        "size": os.path.getsize(library_snapshot),
    }]
    for rel, path in iter_data_files(data_dir):
        items.append({
            "relpath": rel,
            "path": path,
            "sha256": sha256_file(path),
            "size": os.path.getsize(path),
        })
    items.sort(key=lambda item: item["relpath"])
    return items


def paper_count(db_path):
    if not os.path.isfile(db_path):
        return 0
    try:
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            row = conn.execute("SELECT count(*) FROM papers").fetchone()
            return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0


def summarize_local(data_dir):
    files = 0
    total = 0
    db_path = os.path.join(data_dir, "library.db")
    if os.path.isfile(db_path):
        files += 1
        total += os.path.getsize(db_path)
    for _rel, path in iter_data_files(data_dir):
        files += 1
        try:
            total += os.path.getsize(path)
        except OSError:
            continue
    return {"files": files, "bytes": total, "papers": paper_count(db_path)}


def parse_manifest(data):
    if not isinstance(data, dict) or data.get("format") != 1 or data.get("app") != "ArxivReader":
        raise SyncError("sync.cloud_invalid")
    files = data.get("files")
    if not isinstance(files, dict) or len(files) > 100000 or "library.db" not in files:
        raise SyncError("sync.cloud_invalid")
    for rel, meta in files.items():
        if not safe_rel(rel) or not allowed_rel(rel) or not isinstance(meta, dict):
            raise SyncError("sync.cloud_invalid")
        file_id = meta.get("id")
        sha = meta.get("sha256")
        size = meta.get("size")
        if (not isinstance(file_id, str) or not 1 <= len(file_id) <= 256
                or "/" in file_id or "\\" in file_id or ".." in file_id):
            raise SyncError("sync.cloud_invalid")
        if not isinstance(sha, str) or len(sha) != 64 or any(ch not in "0123456789abcdef" for ch in sha):
            raise SyncError("sync.cloud_invalid")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise SyncError("sync.cloud_invalid")
    return data


def summarize_manifest(manifest):
    if not manifest:
        return None
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, dict):
        return None
    total = 0
    for meta in files.values():
        if isinstance(meta, dict) and isinstance(meta.get("size"), int) and not isinstance(meta.get("size"), bool):
            total += meta["size"]
    return {
        "uploaded_at": manifest.get("uploaded_at") if isinstance(manifest.get("uploaded_at"), str) else "",
        "device": manifest.get("device") if isinstance(manifest.get("device"), str) else "",
        "files": len(files),
        "bytes": total,
    }


def _verify(path, sha256, size, rel):
    if not os.path.isfile(path) or os.path.getsize(path) != int(size) or sha256_file(path) != sha256:
        raise SyncError("sync.hash_mismatch", {"path": rel})


def _integrity(path):
    with sqlite3.connect(path) as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SyncError("sync.cloud_invalid")


def release_database():
    from app import database
    path = config.get_db_path()
    if os.path.isfile(path):
        try:
            with database.engine.connect() as conn:
                conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            logger.warning("同步前无法完成数据库检查点", exc_info=True)
    database.engine.dispose()
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


def swap_payload(data_dir, staging):
    """用 staging 里的副本替换资料。失败时把已经挪走的文件放回去。"""
    previous = os.path.join(data_dir, "sync", "previous")
    if os.path.isdir(previous):
        shutil.rmtree(previous)
    os.makedirs(previous, exist_ok=True)
    backed = []
    placed = []
    try:
        for rel in PAYLOAD_ROOTS:
            live = os.path.join(data_dir, rel)
            if not os.path.lexists(live):
                continue
            dest = os.path.join(previous, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            os.rename(live, dest)
            backed.append(rel)
        for rel in PAYLOAD_ROOTS:
            staged = os.path.join(staging, rel)
            live = os.path.join(data_dir, rel)
            if not os.path.lexists(staged):
                continue
            os.makedirs(os.path.dirname(live), exist_ok=True)
            os.rename(staged, live)
            placed.append(rel)
    except OSError:
        for rel in reversed(placed):
            live = os.path.join(data_dir, rel)
            staged = os.path.join(staging, rel)
            if os.path.lexists(live) and not os.path.lexists(staged):
                os.makedirs(os.path.dirname(staged), exist_ok=True)
                os.rename(live, staged)
        for rel in reversed(backed):
            live = os.path.join(data_dir, rel)
            dest = os.path.join(previous, rel)
            if os.path.lexists(dest) and not os.path.lexists(live):
                os.makedirs(os.path.dirname(live), exist_ok=True)
                os.rename(dest, live)
        raise


def _cleanup(store, files):
    if not hasattr(store, "list_file_ids"):
        return
    keep = {meta["id"] for meta in files.values()}
    manifest_id = getattr(store, "manifest_id", None)
    if manifest_id:
        keep.add(manifest_id)
    try:
        stale = [file_id for file_id in store.list_file_ids() if file_id not in keep]
    except SyncError:
        logger.warning("云端旧文件清理失败，下次上传会再试")
        return
    for file_id in stale:
        try:
            store.delete_file(file_id)
        except SyncError:
            logger.warning("云端旧文件清理失败，下次上传会再试")
            return


def push_library(data_dir, store, on_progress, cancel):
    on_progress("scan", 0, 0, "")
    snap_dir = tempfile.mkdtemp(prefix="arxiv-sync-")
    try:
        snapshot = os.path.join(snap_dir, "library.db")
        snapshot_database(os.path.join(data_dir, "library.db"), snapshot)
        rewrite_database(snapshot, data_dir, "portable")
        local = collect_files(data_dir, snapshot)
        try:
            remote = store.load_manifest()
        except SyncError as exc:
            if exc.code != "sync.cloud_invalid":
                raise
            remote = None
        remote_files = remote.get("files") if isinstance(remote, dict) else {}
        if not isinstance(remote_files, dict):
            remote_files = {}
        total = len(local)
        new_files = {}
        for index, item in enumerate(local):
            if cancel.is_set():
                raise SyncError("sync.cancelled")
            on_progress("upload", index, total, item["relpath"])
            old = remote_files.get(item["relpath"])
            if (isinstance(old, dict) and old.get("sha256") == item["sha256"]
                    and old.get("id") and old.get("size") == item["size"]):
                new_files[item["relpath"]] = {
                    "id": old["id"], "sha256": item["sha256"], "size": item["size"],
                }
                continue
            file_id = store.put_file(item["relpath"], item["path"], item["sha256"], cancel)
            new_files[item["relpath"]] = {
                "id": file_id, "sha256": item["sha256"], "size": item["size"],
            }
        if cancel.is_set():
            raise SyncError("sync.cancelled")
        manifest = {
            "format": 1,
            "app": "ArxivReader",
            "uploaded_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "device": device_name(),
            "files": new_files,
        }
        on_progress("cleanup", total, total, "")
        store.save_manifest(manifest)
        _cleanup(store, new_files)
        on_progress("upload", total, total, "")
        logger.info("已上传资料到 Google Drive：%d 个文件", len(new_files))
        return manifest
    finally:
        shutil.rmtree(snap_dir, ignore_errors=True)


def pull_library(data_dir, store, on_progress, cancel):
    from app import database

    remote = store.load_manifest()
    if not remote:
        raise SyncError("sync.cloud_empty", status=404)
    manifest = parse_manifest(remote)
    staging = os.path.join(data_dir, "sync", "incoming")
    if os.path.isdir(staging):
        shutil.rmtree(staging)
    os.makedirs(staging, exist_ok=True)
    try:
        files = list(manifest["files"].items())
        total = len(files)
        for index, (rel, meta) in enumerate(files):
            if cancel.is_set():
                raise SyncError("sync.cancelled")
            on_progress("download", index, total, rel)
            dest = os.path.join(staging, *rel.split("/"))
            store.get_file(meta["id"], dest, cancel)
            _verify(dest, meta["sha256"], meta["size"], rel)
        db_path = os.path.join(staging, "library.db")
        rewrite_database(db_path, data_dir, "absolute")
        _integrity(db_path)
        if _library_busy():
            raise SyncError("sync.busy", status=409)
        on_progress("replace", total, total, "")
        release_database()
        try:
            swap_payload(data_dir, staging)
        except OSError as exc:
            database.reopen_db()
            from .errors import public_message
            raise SyncError("sync.files_busy", {"message": public_message(exc)})
        database.reopen_db()
        database.init_db()
        on_progress("download", total, total, "")
        logger.info("已从 Google Drive 恢复资料：%d 个文件", total)
        return manifest
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _library_busy():
    from app.database import SessionLocal
    from app.models import FolderReview, PdfTranslationTask, PipelineRun, RelatedRun
    db = SessionLocal()
    try:
        for model in (PipelineRun, RelatedRun, FolderReview, PdfTranslationTask):
            if db.query(model).filter(model.status.in_(("queued", "running"))).first():
                return True
        return False
    finally:
        db.close()
