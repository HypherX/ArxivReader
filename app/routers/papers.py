"""论文路由：ArXiv 入库、本地 PDF 上传、列表检索、移动/编辑、PDF 与全文获取、删除。

入库流程（POST /api/papers/from-arxiv）：
  规范化 id -> 去重 -> 取元数据 -> 下载 PDF -> 抽取全文 ->
  规则归档(未指定文件夹时) -> 落库
本地上传（POST /api/papers/upload）：
  校验 PDF -> 内容哈希生成 local- 前缀 id -> 去重 -> 落盘 -> 抽取全文 -> 落库
  本地论文没有 arXiv 元数据；标题取 表单 > PDF 元数据 > 文件名。
下载在请求内同步完成（单用户本地场景可接受），失败返回明确的 HTTP 错误。
"""

import hashlib
import datetime
import json
import logging
import os
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .. import arxiv_service, database, pdf_service, schemas
from harness.config import get_pdf_dir
from harness.paper import readable_text
from ..models import LOCAL_ID_PREFIX, Folder, Paper
from knowledge import store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/papers", tags=["papers"])

# 上传文件大小上限（100 MB）：本地单用户场景，整文件读入内存校验后落盘
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
_PDF_MAGIC = b"%PDF-"


def _safe_dirname(name: str) -> str:
    """保留旧名作为薄封装（与 arxiv_service.safe_dirname 同一口径）。"""
    return arxiv_service.safe_dirname(name)


def _get_paper_or_404(db: Session, paper_id: int) -> Paper:
    paper = db.get(Paper, paper_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="论文不存在")
    return paper


@router.post("/from-arxiv", response_model=schemas.PaperOut, status_code=201)
def add_from_arxiv(body: schemas.PaperFromArxiv, db: Session = Depends(database.get_db)):
    # 1) 解析链接
    try:
        norm_id = arxiv_service.normalize_arxiv_id(body.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    base_id = arxiv_service.strip_version(norm_id)

    # 2) 去重
    existing = db.query(Paper).filter(Paper.arxiv_id == base_id).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="该论文已在库中（%s）" % base_id)

    # 3) 取元数据
    try:
        meta = arxiv_service.fetch_metadata(norm_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except arxiv_service.ArxivUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:  # noqa: BLE001  网络异常等
        logger.warning("获取 ArXiv 元数据失败：%s", str(exc)[:300])
        raise HTTPException(status_code=502, detail="获取 ArXiv 元数据失败：%s" % str(exc)[:200])

    # 4) 目标文件夹：只认用户显式选择；没选就只属于「全部论文」
    # 4) 归属：选了文件夹就落进去，没选则只属于"全部论文"（folder_id 为空）
    folder_id = body.folder_id
    if folder_id is not None and db.get(Folder, folder_id) is None:
        raise HTTPException(status_code=400, detail="目标文件夹不存在")

    # 5) 下载 PDF
    folder = db.get(Folder, folder_id) if folder_id is not None else None
    dest_dir = os.path.join(get_pdf_dir(), _safe_dirname(folder.name if folder else arxiv_service.UNFILED_DIR))
    try:
        pdf_path = arxiv_service.download_pdf(meta, dest_dir)
    except Exception as exc:  # noqa: BLE001
        logger.warning("下载 PDF 失败：%s", str(exc)[:300])
        raise HTTPException(status_code=502, detail="下载 PDF 失败：%s" % str(exc)[:200])

    # 6) 抽取全文（整篇，不截断），失败不阻断入库
    try:
        text = pdf_service.extract_text(pdf_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("PDF 文本抽取失败（%s）：%s", pdf_path, str(exc)[:300])
        text = ""

    # 7) 落库
    paper = Paper(
        arxiv_id=meta.arxiv_id,
        title=meta.title,
        authors_json=json.dumps(meta.authors, ensure_ascii=False),
        abstract=meta.abstract,
        categories_json=json.dumps(meta.categories, ensure_ascii=False),
        published=meta.published,
        pdf_url=meta.pdf_url,
        pdf_path=str(pdf_path),
        folder_id=folder_id,
        full_text=text,
    )
    db.add(paper)
    db.flush()
    store.move_paper_to_folder(db, paper, folder, commit=False)
    db.commit()
    db.refresh(paper)
    return paper


@router.post("/upload", response_model=schemas.PaperOut, status_code=201)
async def upload_pdf(file: UploadFile = File(...),
                     folder_id: Optional[int] = Form(None),
                     title: Optional[str] = Form(None),
                     db: Session = Depends(database.get_db)):
    """上传本地 PDF 入库。

    以文件内容的 SHA-256 生成 `local-` 前缀 id 去重；同一文件重复上传返回 409。
    没有 arXiv 元数据可取，未指定文件夹时只属于「全部论文」。
    损坏/加密到无法打开的 PDF 不入库；扫描版（无文字层）允许入库但全文为空。
    """
    filename = file.filename or ""
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail={"code": "papers.not_pdf", "params": {}})

    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if not content:
        raise HTTPException(status_code=400, detail={"code": "papers.empty_file", "params": {}})
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail={
            "code": "papers.too_large",
            "params": {"max": MAX_UPLOAD_BYTES // (1024 * 1024)}})
    if not content.startswith(_PDF_MAGIC):
        raise HTTPException(status_code=400, detail={"code": "papers.not_pdf", "params": {}})

    digest = hashlib.sha256(content).hexdigest()
    local_id = LOCAL_ID_PREFIX + digest[:16]
    if db.query(Paper).filter(Paper.arxiv_id == local_id).first() is not None:
        raise HTTPException(status_code=409, detail={"code": "papers.duplicate", "params": {}})

    if folder_id is not None and db.get(Folder, folder_id) is None:
        raise HTTPException(status_code=400, detail={"code": "papers.folder_missing", "params": {}})
    folder = db.get(Folder, folder_id) if folder_id is not None else None

    # 落盘：<清洗后的文件名>-<hash8>.pdf，同名不同内容不冲突，同内容已被去重拦截
    stem = os.path.splitext(os.path.basename(filename))[0]
    dest_dir = os.path.join(get_pdf_dir(), _safe_dirname(folder.name if folder else arxiv_service.UNFILED_DIR))
    os.makedirs(dest_dir, exist_ok=True)
    dest_name = "{}-{}.pdf".format(_safe_dirname(stem)[:80] or "paper", digest[:8])
    pdf_path = os.path.join(dest_dir, dest_name)
    with open(pdf_path, "wb") as fh:
        fh.write(content)

    # 抽取全文（同时验证文件可读）；打不开的损坏文件删除落盘副本并拒绝入库
    try:
        text = pdf_service.extract_text(pdf_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("上传的 PDF 无法读取（%s）：%s", filename, str(exc)[:300])
        try:
            os.remove(pdf_path)
        except OSError:
            pass
        raise HTTPException(status_code=400, detail={"code": "papers.bad_pdf", "params": {}})

    final_title = (title or "").strip() or pdf_service.document_title(pdf_path) or stem
    paper = Paper(
        arxiv_id=local_id,
        title=final_title,
        authors_json="[]",
        abstract="",
        categories_json="[]",
        published=None,
        pdf_url=None,
        pdf_path=pdf_path,
        folder_id=folder_id,
        full_text=text,
    )
    db.add(paper)
    db.flush()
    store.move_paper_to_folder(db, paper, folder, commit=False)
    db.commit()
    db.refresh(paper)
    return paper


@router.get("", response_model=List[schemas.PaperOut])
def list_papers(folder_id: Optional[int] = None, q: Optional[str] = None,
                recursive: bool = True, db: Session = Depends(database.get_db)):
    query = db.query(Paper)
    if folder_id is not None:
        try:
            ids = store.folder_subtree_ids(db, folder_id) if recursive else [folder_id]
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        query = query.filter(Paper.folder_id.in_(ids))
    keyword = (q or "").strip()
    if keyword:
        like = "%" + keyword + "%"
        query = query.filter(or_(
            Paper.title.ilike(like),
            Paper.abstract.ilike(like),
            Paper.authors_json.ilike(like),
            Paper.arxiv_id.ilike(like),
        ))
    return query.order_by(Paper.added_at.desc(), Paper.id.desc()).all()


@router.get("/{paper_id}", response_model=schemas.PaperOut)
def get_paper(paper_id: int, db: Session = Depends(database.get_db)):
    return _get_paper_or_404(db, paper_id)


@router.patch("/{paper_id}", response_model=schemas.PaperOut)
def update_paper(paper_id: int, body: schemas.PaperUpdate,
                 db: Session = Depends(database.get_db)):
    paper = _get_paper_or_404(db, paper_id)
    fields = body.model_fields_set
    if "folder_id" in fields:
        if body.folder_id is not None and db.get(Folder, body.folder_id) is None:
            raise HTTPException(status_code=404, detail="目标文件夹不存在")
        store.move_paper_to_folder(db, paper, db.get(Folder, body.folder_id) if body.folder_id else None,
                                   commit=False)
    if "title" in fields and body.title is not None:
        title = body.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="标题不能为空")
        paper.title = title
    db.commit()
    db.refresh(paper)
    return paper


@router.post("/{paper_id}/reading-progress", response_model=schemas.PaperOut)
def report_reading_progress(paper_id: int, body: schemas.ReadingProgressIn,
                            db: Session = Depends(database.get_db)):
    """阅读进度上报（前端节流调用）：最远页只增不减，时长累加，单次上报上限 1 小时防呆。"""
    paper = _get_paper_or_404(db, paper_id)
    if body.pages > 0:
        paper.read_pages = body.pages
    if body.page > 0:
        paper.read_page = max(paper.read_page, min(body.page, paper.read_pages or body.page))
    if body.seconds > 0:
        paper.read_seconds += min(int(body.seconds), 3600)
    if body.page > 0 or body.seconds > 0:
        paper.last_read_at = datetime.datetime.utcnow()
    db.commit()
    db.refresh(paper)
    return paper


@router.post("/{paper_id}/reading-mark", response_model=schemas.PaperOut)
def mark_reading(paper_id: int, body: schemas.ReadingMarkIn,
                 db: Session = Depends(database.get_db)):
    """手动标记已读完 / 未读。标已读时若还不知道总页数，用已知最远页兜底成 100%。"""
    paper = _get_paper_or_404(db, paper_id)
    if body.read:
        total = max(paper.read_pages, paper.read_page, 1)
        paper.read_pages = total
        paper.read_page = total
        paper.last_read_at = datetime.datetime.utcnow()
    else:
        paper.read_page = 0
        paper.read_seconds = 0
        paper.last_read_at = None
    db.commit()
    db.refresh(paper)
    return paper


@router.delete("/{paper_id}", status_code=204)
def delete_paper(paper_id: int, db: Session = Depends(database.get_db)):
    paper = _get_paper_or_404(db, paper_id)
    if paper.pdf_path and os.path.isfile(paper.pdf_path):
        try:
            os.remove(paper.pdf_path)
        except OSError as exc:
            logger.warning("删除 PDF 文件失败（%s）：%s", paper.pdf_path, exc)
    old_nodes = [d.node_id for d in store.paper_memberships(db, paper.id)]
    db.delete(paper)   # 级联删除其会话与消息
    db.flush()
    store._recount_ids(db, old_nodes)
    db.commit()
    return Response(status_code=204)


@router.get("/{paper_id}/pdf")
def get_pdf(paper_id: int, db: Session = Depends(database.get_db)):
    paper = _get_paper_or_404(db, paper_id)
    if not paper.pdf_path or not os.path.isfile(paper.pdf_path):
        raise HTTPException(status_code=404, detail="PDF 文件不存在")
    # inline：前端 PDF.js 直接取这个地址渲染，"↗ 原始 PDF" 也用它在新标签页打开
    return FileResponse(
        paper.pdf_path,
        media_type="application/pdf",
        filename=os.path.basename(paper.pdf_path),
        content_disposition_type="inline",
    )


def _pdf_file_or_404(paper: Paper) -> str:
    if not paper.pdf_path or not os.path.isfile(paper.pdf_path):
        raise HTTPException(status_code=404, detail="PDF 文件不存在")
    return paper.pdf_path


@router.get("/{paper_id}/text")
def get_text(paper_id: int, db: Session = Depends(database.get_db)):
    paper = _get_paper_or_404(db, paper_id)
    text = readable_text(paper.full_text or "")
    return {
        "arxiv_id": paper.arxiv_id,
        "char_count": len(text),
        "text": text,
    }
