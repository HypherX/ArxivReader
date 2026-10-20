"""从 Zotero 个人文库只读导入论文。

用户在 zotero.org/settings/keys 创建 API Key（个人文库只读，并允许读取文件）。
密钥留在本机 sync/zotero.json，不进文献库，因此不会跟着云同步上传。

增量：记下 Zotero 文库版本，下次用 since 只拉变更。已经在库里的论文（同一 arXiv
编号，或同一条 Zotero 条目）跳过，不覆盖本地修改。分类镜像到顶层「Zotero」文件夹下。
PDF 优先用 Zotero 里已保存的附件；没有附件但能认出 arXiv 编号时，再从 arXiv 下载。
"""

import json
import logging
import os
import re
import threading
import time

import httpx

from harness import config

from . import arxiv_service, database, pdf_service
from .models import ZOTERO_ID_PREFIX, Folder, Paper, Setting

logger = logging.getLogger(__name__)

API_ROOT = "https://api.zotero.org"
ROOT_NAME = "Zotero"
_KEY_RE = re.compile(r"^[A-Za-z0-9]{20,64}$")
_MAX_PDF = 100 * 1024 * 1024
_CRED_FIELDS = ("api_key", "user_id", "username")
_LOCK = threading.Lock()
_JOB_SEQ = 0
_JOB = {
    "id": 0,
    "running": False,
    "finished": False,
    "ok": False,
    "phase": "",
    "done": 0,
    "total": 0,
    "imported": 0,
    "skipped": 0,
    "failed": 0,
    "with_pdf": 0,
    "error": "",
    "error_params": {},
}


class ZoteroError(Exception):
    def __init__(self, code, params=None, status=400):
        super().__init__(code)
        self.code = code
        self.params = params or {}
        self.status = status


class _ItemFailed(Exception):
    """单条导入失败：整次同步继续，并把这条放进下次重试。"""


def _creds_path():
    return os.path.join(config.get_base_dir(), "sync", "zotero.json")


def load_creds():
    path = _creds_path()
    blank = {key: "" for key in _CRED_FIELDS}
    if not os.path.isfile(path):
        return blank
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return blank
    if not isinstance(raw, dict):
        return blank
    data = dict(blank)
    for key in _CRED_FIELDS:
        value = raw.get(key)
        if isinstance(value, str):
            data[key] = value
    return data


def _save_creds(data):
    path = _creds_path()
    clean = {key: "" for key in _CRED_FIELDS}
    for key in _CRED_FIELDS:
        value = data.get(key) if isinstance(data, dict) else ""
        clean[key] = value if isinstance(value, str) else ""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    if os.name != "nt":
        os.chmod(directory, 0o700)
    temporary = path + ".partial"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(clean, fh, ensure_ascii=False)
            fh.write("\n")
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except Exception:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise
    return clean


def _clear_creds():
    path = _creds_path()
    try:
        os.remove(path)
    except OSError:
        pass


def _public(text):
    cleaned = str(text or "").replace("\n", " ").strip()
    return cleaned[:180]


def _check_key_format(api_key):
    if not isinstance(api_key, str) or not _KEY_RE.fullmatch(api_key.strip()):
        raise ZoteroError("zotero.invalid_key", status=422)


def _job_snapshot():
    with _LOCK:
        return dict(_JOB)


def _job_update(**fields):
    with _LOCK:
        _JOB.update(fields)


class ZoteroClient:
    """Zotero Web API v3。transport 只给测试替换，正常走 HTTPS。"""

    def __init__(self, api_key, user_id="", transport=None, page_size=100):
        self.api_key = api_key
        self.user_id = str(user_id or "")
        self.page_size = max(1, min(100, int(page_size)))
        self._http = httpx.Client(
            base_url=API_ROOT,
            headers={
                "Zotero-API-Key": api_key,
                "Zotero-API-Version": "3",
                "User-Agent": "ArxivReader/1.0 (Zotero import)",
            },
            timeout=httpx.Timeout(30.0, read=120.0),
            follow_redirects=True,
            transport=transport,
        )

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def _send(self, path, params=None):
        last = None
        for attempt in range(3):
            try:
                resp = self._http.get(path, params=params)
            except httpx.HTTPError as exc:
                raise ZoteroError("zotero.unreachable", status=502) from exc
            if resp.status_code == 429 and attempt < 2:
                last = resp
                wait = resp.headers.get("Retry-After") or resp.headers.get("Backoff") or "1"
                try:
                    delay = min(8, max(0, int(float(wait))))
                except ValueError:
                    delay = 1
                time.sleep(delay)
                continue
            return resp
        return last

    def identity(self):
        resp = self._send("/keys/CURRENT")
        if resp.status_code in (401, 403):
            raise ZoteroError("zotero.unauthorized", status=401)
        if resp.status_code >= 400:
            raise ZoteroError("zotero.failed", {"message": "HTTP {}".format(resp.status_code)}, status=502)
        try:
            data = resp.json()
        except ValueError as exc:
            raise ZoteroError("zotero.failed", {"message": "bad key response"}, status=502) from exc
        if not isinstance(data, dict):
            raise ZoteroError("zotero.failed", {"message": "bad key response"}, status=502)
        access = data.get("access") if isinstance(data.get("access"), dict) else {}
        user = access.get("user") if isinstance(access.get("user"), dict) else {}
        if not user.get("library"):
            raise ZoteroError("zotero.no_library", status=403)
        user_id = str(data.get("userID") or "")
        if not user_id.isdigit():
            raise ZoteroError("zotero.failed", {"message": "missing user id"}, status=502)
        username = data.get("username") or data.get("displayName") or ""
        if not isinstance(username, str):
            username = ""
        return {"user_id": user_id, "username": username[:120]}

    def _library(self, kind, since):
        if not self.user_id.isdigit():
            raise ZoteroError("zotero.not_configured")
        params = [("format", "json")]
        if since and int(since) > 0:
            params.append(("since", str(int(since))))
        if kind == "items":
            params.extend([
                ("itemType", "-attachment"),
                ("itemType", "-note"),
                ("itemType", "-annotation"),
            ])
        version, rows = self._paginate("/users/{}/{}".format(self.user_id, kind), params)
        return version, rows

    def collections(self, since=0):
        return self._library("collections", since)

    def items(self, since=0):
        return self._library("items", since)

    def item(self, item_key):
        resp = self._send("/users/{}/items/{}".format(self.user_id, item_key), params=[("format", "json")])
        if resp.status_code == 404:
            return None
        if resp.status_code in (401, 403):
            raise ZoteroError("zotero.unauthorized", status=401)
        if resp.status_code >= 400:
            raise _ItemFailed("HTTP {}".format(resp.status_code))
        try:
            data = resp.json()
        except ValueError as exc:
            raise _ItemFailed("bad item") from exc
        return data if isinstance(data, dict) else None

    def pdf_attachment(self, item_key):
        """返回 (attachment_key, filename) 或 (None, None)。没有文件权限时当没有附件。"""
        try:
            _version, rows = self._paginate(
                "/users/{}/items/{}/children".format(self.user_id, item_key),
                [("format", "json"), ("itemType", "attachment")],
            )
        except ZoteroError:
            raise
        except _ItemFailed:
            raise
        chosen = _pick_pdf(rows)
        if not chosen:
            return None, None
        return chosen.get("key") or None, chosen.get("filename") or chosen.get("title") or "paper.pdf"

    def download(self, item_key):
        resp = self._send("/users/{}/items/{}/file".format(self.user_id, item_key))
        if resp.status_code in (401,):
            raise ZoteroError("zotero.unauthorized", status=401)
        if resp.status_code in (403, 404):
            return b""
        if resp.status_code >= 400:
            raise _ItemFailed("HTTP {}".format(resp.status_code))
        length = resp.headers.get("Content-Length") or ""
        try:
            if length and int(length) > _MAX_PDF:
                return b""
        except ValueError:
            pass
        content = resp.content
        if len(content) > _MAX_PDF or not content.startswith(b"%PDF-"):
            return b""
        return content

    def _paginate(self, path, params):
        start = 0
        version = 0
        rows = []
        pages = 0
        while pages < 1000:
            pages += 1
            page_params = list(params) + [("limit", str(self.page_size)), ("start", str(start))]
            resp = self._send(path, params=page_params)
            if resp.status_code in (401, 403):
                # 子附件没有文件权限：调用方把整条当「没有 PDF」，而不是失败。
                if path.endswith("/children") and resp.status_code == 403:
                    return version, []
                raise ZoteroError("zotero.unauthorized", status=401)
            if resp.status_code >= 400:
                if path.endswith("/children"):
                    raise _ItemFailed("HTTP {}".format(resp.status_code))
                raise ZoteroError("zotero.failed", {"message": "HTTP {}".format(resp.status_code)}, status=502)
            header = resp.headers.get("Last-Modified-Version") or ""
            if header.isdigit():
                version = max(version, int(header))
            try:
                batch = resp.json()
            except ValueError as exc:
                raise ZoteroError("zotero.failed", {"message": "bad list"}, status=502) from exc
            if not isinstance(batch, list):
                raise ZoteroError("zotero.failed", {"message": "bad list"}, status=502)
            if not batch:
                break
            rows.extend(batch)
            start += len(batch)
            if len(batch) < self.page_size:
                break
            total = resp.headers.get("Total-Results")
            if total and total.isdigit() and start >= int(total):
                break
        return version, rows


def verify_key(api_key, transport=None):
    _check_key_format(api_key)
    with ZoteroClient(api_key.strip(), transport=transport) as client:
        return client.identity()


def save_key(api_key, transport=None):
    _ensure_idle()
    _check_key_format(api_key)
    identity = verify_key(api_key, transport=transport)
    _save_creds({
        "api_key": api_key.strip(),
        "user_id": identity["user_id"],
        "username": identity["username"],
    })
    return identity


def forget_key():
    _ensure_idle()
    _clear_creds()


def _ensure_idle():
    with _LOCK:
        if _JOB.get("running"):
            raise ZoteroError("zotero.running", status=409)


def status():
    creds = load_creds()
    version = "0"
    try:
        with database.SessionLocal() as db:
            version = _get_setting(db, "zotero_library_version") or "0"
    except Exception:  # noqa: BLE001  状态查询不应因为库打不开而把界面打挂
        logger.warning("读取 Zotero 同步版本失败", exc_info=True)
    view = {
        "configured": bool(creds.get("api_key") and creds.get("user_id")),
        "username": creds.get("username") or "",
        "user_id": creds.get("user_id") or "",
        "library_version": int(version) if str(version).isdigit() else 0,
        "job": _job_snapshot(),
    }
    return view


def start_sync():
    global _JOB_SEQ
    creds = load_creds()
    if not creds.get("api_key") or not str(creds.get("user_id") or "").isdigit():
        raise ZoteroError("zotero.not_configured")
    with _LOCK:
        if _JOB.get("running"):
            raise ZoteroError("zotero.running", status=409)
        _JOB_SEQ += 1
        job_id = _JOB_SEQ
        _JOB.update({
            "id": job_id,
            "running": True,
            "finished": False,
            "ok": False,
            "phase": "collections",
            "done": 0,
            "total": 0,
            "imported": 0,
            "skipped": 0,
            "failed": 0,
            "with_pdf": 0,
            "error": "",
            "error_params": {},
        })
    threading.Thread(target=_worker, args=(creds, job_id), name="zotero-sync", daemon=True).start()


def _worker(creds, job_id):
    db = database.SessionLocal()
    try:
        client = ZoteroClient(creds["api_key"], creds["user_id"])
        try:
            stats = import_library(db, client, on_progress=_progress)
        finally:
            client.close()
        _job_update(running=False, finished=True, ok=True, phase="done", error="", error_params={}, **stats)
    except ZoteroError as exc:
        logger.info("Zotero 同步未完成：%s", exc.code)
        _job_update(running=False, finished=True, ok=False, error=exc.code, error_params=exc.params or {})
    except Exception as exc:  # noqa: BLE001
        logger.exception("Zotero 同步失败")
        _job_update(running=False, finished=True, ok=False, error="zotero.failed",
                    error_params={"message": _public(exc)})
    finally:
        db.close()
        with _LOCK:
            if _JOB.get("id") == job_id:
                _JOB["running"] = False
                _JOB["finished"] = True


def _progress(phase, done, total):
    _job_update(phase=phase, done=done, total=total)


def extract_arxiv_id(data):
    """从 Zotero 条目的 archive / DOI / url / extra 里认出 arXiv 编号。认不出返回空串。"""
    if not isinstance(data, dict):
        return ""
    candidates = []
    archive = str(data.get("archive") or "")
    location = str(data.get("archiveLocation") or "").strip()
    if location and ("arxiv" in archive.lower() or re.match(r"^\d{4}\.\d{4,5}", location)):
        candidates.append(location)
    doi = data.get("DOI") or data.get("doi") or ""
    if isinstance(doi, str) and doi.strip():
        candidates.append(doi)
    url = str(data.get("url") or "")
    if "arxiv.org" in url.lower():
        candidates.append(url)
    extra = str(data.get("extra") or "")
    for line in extra.splitlines():
        if "arxiv" in line.lower():
            candidates.append(line)
    for raw in candidates:
        try:
            return arxiv_service.strip_version(arxiv_service.normalize_arxiv_id(raw))
        except ValueError:
            continue
    return ""


def fetch_arxiv_pdf(arxiv_id, dest_dir):
    """Zotero 没存 PDF 时，按编号向 arXiv 要一份。失败返回空串，不挡住条目入库。"""
    try:
        meta = arxiv_service.fetch_metadata(arxiv_id)
        return str(arxiv_service.download_pdf(meta, dest_dir))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Zotero 条目回落 arXiv 下载失败（%s）：%s", arxiv_id, str(exc)[:200])
        return ""


def import_library(db, client, on_progress=None):
    """拉取 since 上次版本之后的变更并入库。返回计数，不抛出单条失败。"""
    _bind_account(db, client.user_id)
    since = _version(db)
    on_progress = on_progress or (lambda *_args: None)
    on_progress("collections", 0, 0)
    col_version, collections = client.collections(since)
    _apply_collections(db, collections)
    db.commit()

    on_progress("items", 0, 0)
    item_version, changed = client.items(since)
    retry_keys = _load_json(db, "zotero_retry_keys", [])
    if not isinstance(retry_keys, list):
        retry_keys = []

    pending = []
    seen = set()
    still_retry = []
    for key in retry_keys:
        if not isinstance(key, str) or not key or key in seen:
            continue
        seen.add(key)
        try:
            row = client.item(key)
        except _ItemFailed as exc:
            logger.warning("重试 Zotero 条目 %s 失败：%s", key, str(exc)[:200])
            still_retry.append(key)
            continue
        if row:
            pending.append(row)
    for row in changed:
        key = _item_key(row)
        if not key or key in seen:
            continue
        seen.add(key)
        pending.append(row)

    stats = {"imported": 0, "skipped": 0, "failed": len(still_retry), "with_pdf": 0}
    total = len(pending) + stats["failed"]
    on_progress("items", stats["failed"], total)
    for index, row in enumerate(pending, start=stats["failed"] + 1):
        key = _item_key(row)
        try:
            outcome = _import_item(db, client, row)
        except ZoteroError:
            db.rollback()
            raise
        except Exception as exc:  # noqa: BLE001  单条失败留到下次
            db.rollback()
            logger.warning("Zotero 条目 %s 导入失败：%s", key, str(exc)[:200])
            stats["failed"] += 1
            if key:
                still_retry.append(key)
        else:
            stats[outcome["status"]] += 1
            if outcome.get("pdf"):
                stats["with_pdf"] += 1
        on_progress("items", index, total)

    library_version = max(since, col_version, item_version)
    _set_setting(db, "zotero_library_version", str(library_version))
    _set_setting(db, "zotero_retry_keys", json.dumps(still_retry, ensure_ascii=False))
    db.commit()
    logger.info("Zotero 导入完成：新增 %s，跳过 %s，失败 %s", stats["imported"], stats["skipped"], stats["failed"])
    return stats


def _bind_account(db, user_id):
    bound = _get_setting(db, "zotero_user_id")
    if bound and bound != user_id:
        # 换了 Zotero 账号：游标和分类映射属于上一个文库，不能接着用。
        _set_setting(db, "zotero_library_version", "0")
        _set_setting(db, "zotero_collection_map", "{}")
        _set_setting(db, "zotero_retry_keys", "[]")
        _set_setting(db, "zotero_root_folder_id", "")
    _set_setting(db, "zotero_user_id", user_id)


def _import_item(db, client, row):
    data = row.get("data") if isinstance(row, dict) else None
    if not isinstance(data, dict):
        data = {}
    key = _item_key(row)
    if not key or data.get("itemType") in ("attachment", "note", "annotation"):
        return {"status": "skipped", "pdf": False}
    arxiv_id = extract_arxiv_id(data)
    storage_id = arxiv_id if arxiv_id else ZOTERO_ID_PREFIX + re.sub(r"[^A-Za-z0-9]", "", key)[:40]
    if not storage_id or storage_id == ZOTERO_ID_PREFIX:
        raise _ItemFailed("bad key")
    existing = db.query(Paper).filter(Paper.arxiv_id == storage_id).first()
    legacy = None
    if arxiv_id:
        legacy = db.query(Paper).filter(Paper.arxiv_id == ZOTERO_ID_PREFIX + re.sub(r"[^A-Za-z0-9]", "", key)[:40]).first()
    if existing is not None or legacy is not None:
        return {"status": "skipped", "pdf": False}

    folder = _folder_for_item(db, data)
    pdf_path, filename = _pull_pdf(client, key, folder)
    if not pdf_path and arxiv_id:
        dest_dir = os.path.join(config.get_pdf_dir(), arxiv_service.safe_dirname(folder.name if folder else ROOT_NAME))
        os.makedirs(dest_dir, exist_ok=True)
        pdf_path = fetch_arxiv_pdf(arxiv_id, dest_dir)
        filename = filename or (arxiv_id.replace("/", "_") + ".pdf")
    text = ""
    if pdf_path:
        try:
            text = pdf_service.extract_text(pdf_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Zotero PDF 抽文本失败（%s）：%s", key, str(exc)[:200])
            text = ""

    authors = _authors(data.get("creators"))
    title = str(data.get("title") or "").strip() or storage_id
    published = str(data.get("date") or "").strip()[:64] or None
    url = str(data.get("url") or "").strip() or None
    paper = Paper(
        arxiv_id=storage_id,
        title=title,
        authors_json=json.dumps(authors, ensure_ascii=False),
        abstract=str(data.get("abstractNote") or ""),
        categories_json="[]",
        published=published,
        pdf_url=url,
        pdf_path=pdf_path or None,
        folder_id=folder.id if folder else None,
        full_text=text,
    )
    from knowledge import store
    db.add(paper)
    db.flush()
    store.move_paper_to_folder(db, paper, folder, commit=False)
    db.commit()
    return {"status": "imported", "pdf": bool(pdf_path)}


def _pull_pdf(client, item_key, folder):
    attachment_key, filename = client.pdf_attachment(item_key)
    if not attachment_key:
        return "", filename or ""
    content = client.download(attachment_key)
    if not content:
        return "", filename or ""
    return _write_pdf(folder, item_key, filename, content), filename or ""


def _write_pdf(folder, item_key, filename, content):
    dest_dir = os.path.join(config.get_pdf_dir(), arxiv_service.safe_dirname(folder.name if folder else ROOT_NAME))
    os.makedirs(dest_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(filename or "paper"))[0]
    stem = arxiv_service.safe_dirname(stem)[:80]
    safe_key = re.sub(r"[^A-Za-z0-9]", "", item_key)[:16] or "item"
    path = os.path.join(dest_dir, "{}-{}.pdf".format(stem, safe_key))
    with open(path, "wb") as fh:
        fh.write(content)
    return path


def _folder_for_item(db, data):
    root = _root_folder(db)
    cmap = _collection_map(db)
    for key in data.get("collections") or []:
        if not isinstance(key, str):
            continue
        folder_id = cmap.get(key)
        folder = db.get(Folder, int(folder_id)) if str(folder_id).isdigit() else None
        if folder is not None:
            return folder
    return root


def _apply_collections(db, rows):
    root = _root_folder(db)
    cmap = _collection_map(db)
    for row in _order_collections(rows):
        data = row.get("data") if isinstance(row.get("data"), dict) else {}
        key = _item_key(row)
        if not key:
            continue
        name = str(data.get("name") or "").strip() or key
        name = name[:255]
        parent_key = data.get("parentCollection") or ""
        if not isinstance(parent_key, str):
            parent_key = ""
        parent = root
        if parent_key and parent_key in cmap and str(cmap[parent_key]).isdigit():
            found = db.get(Folder, int(cmap[parent_key]))
            if found is not None:
                parent = found
        folder = db.get(Folder, int(cmap[key])) if str(cmap.get(key, "")).isdigit() else None
        if folder is None:
            folder = Folder(name=name, parent_id=parent.id)
            db.add(folder)
            db.flush()
        else:
            folder.name = name
            folder.parent_id = parent.id
        cmap[key] = folder.id
    _set_setting(db, "zotero_collection_map", json.dumps({k: int(v) for k, v in cmap.items()}))


def _root_folder(db):
    raw = _get_setting(db, "zotero_root_folder_id")
    if str(raw).isdigit():
        folder = db.get(Folder, int(raw))
        if folder is not None:
            return folder
    folder = (db.query(Folder)
              .filter(Folder.name == ROOT_NAME, Folder.parent_id.is_(None), Folder.direction_node_id.is_(None))
              .first())
    if folder is None:
        folder = Folder(name=ROOT_NAME, parent_id=None)
        db.add(folder)
        db.flush()
    _set_setting(db, "zotero_root_folder_id", str(folder.id))
    return folder


def _order_collections(rows):
    pending = [row for row in rows if isinstance(row, dict)]
    ordered = []
    seen = set()
    guard = 0
    limit = len(pending) + 1
    while pending and guard < limit:
        guard += 1
        nxt = []
        for row in pending:
            data = row.get("data") if isinstance(row.get("data"), dict) else {}
            parent = data.get("parentCollection") or ""
            if not isinstance(parent, str):
                parent = ""
            if not parent or parent in seen:
                ordered.append(row)
                key = _item_key(row)
                if key:
                    seen.add(key)
            else:
                nxt.append(row)
        if len(nxt) == len(pending):
            ordered.extend(pending)
            break
        pending = nxt
    return ordered


def _pick_pdf(rows):
    for row in rows:
        data = row.get("data") if isinstance(row, dict) else None
        if not isinstance(data, dict) or data.get("itemType") != "attachment":
            continue
        if data.get("linkMode") not in ("imported_file", "imported_url"):
            continue
        ctype = str(data.get("contentType") or "").lower()
        name = str(data.get("filename") or data.get("title") or "")
        if "pdf" in ctype or name.lower().endswith(".pdf"):
            return data
    return None


def _authors(creators):
    names = []
    if not isinstance(creators, list):
        return names
    preferred = []
    others = []
    for creator in creators:
        if not isinstance(creator, dict):
            continue
        name = str(creator.get("name") or "").strip()
        if not name:
            name = " ".join(part for part in (
                str(creator.get("firstName") or "").strip(),
                str(creator.get("lastName") or "").strip(),
            ) if part).strip()
        if not name:
            continue
        if creator.get("creatorType") in (None, "", "author"):
            preferred.append(name)
        else:
            others.append(name)
    return preferred or others


def _item_key(row):
    if not isinstance(row, dict):
        return ""
    key = row.get("key") or (row.get("data") or {}).get("key") or ""
    return key if isinstance(key, str) else ""


def _version(db):
    raw = _get_setting(db, "zotero_library_version") or "0"
    return int(raw) if str(raw).isdigit() else 0


def _collection_map(db):
    raw = _load_json(db, "zotero_collection_map", {})
    if not isinstance(raw, dict):
        return {}
    clean = {}
    for key, value in raw.items():
        if isinstance(key, str) and str(value).isdigit():
            clean[key] = int(value)
    return clean


def _load_json(db, key, default):
    raw = _get_setting(db, key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


def _get_setting(db, key):
    row = db.get(Setting, key)
    return row.value if row is not None else ""


def _set_setting(db, key, value):
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value
