"""论文路由：ArXiv 入库、列表检索、移动/编辑、PDF 与全文获取、删除。

入库流程（POST /api/papers/from-arxiv）：
  规范化 id -> 去重 -> 取元数据 -> 下载 PDF -> 抽取全文(超长截断) ->
  规则归档(未指定文件夹时) -> 落库
下载在请求内同步完成（单用户本地场景可接受），失败返回明确的 HTTP 错误。
"""

import json
import logging
import os
import re
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .. import arxiv_service, config_loader, database, filing, pdf_service, schemas
from ..models import FilingRule, Folder, Paper

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/papers", tags=["papers"])

_UNSAFE_RE = re.compile(r"[^\w\-. ]")


def _safe_dirname(name: str) -> str:
    cleaned = _UNSAFE_RE.sub("_", (name or "").strip()).strip(".")
    return cleaned or "Inbox"


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
    except Exception as exc:  # noqa: BLE001  网络异常等
        logger.warning("获取 ArXiv 元数据失败：%s", str(exc)[:300])
        raise HTTPException(status_code=502, detail="获取 ArXiv 元数据失败：%s" % str(exc)[:200])

    # 4) 决定目标文件夹：显式指定 > 规则命中 > Inbox
    folder_id = body.folder_id
    if folder_id is not None:
        if db.get(Folder, folder_id) is None:
            raise HTTPException(status_code=400, detail="目标文件夹不存在")
    else:
        rules = db.query(FilingRule).all()
        folder_id = filing.apply_rules(filing.target_from_meta(meta), rules)
    if folder_id is None:
        folder_id = database.ensure_inbox(db).id

    # 5) 下载 PDF
    folder = db.get(Folder, folder_id)
    dest_dir = os.path.join(config_loader.get_pdf_dir(), _safe_dirname(folder.name if folder else "Inbox"))
    try:
        pdf_path = arxiv_service.download_pdf(meta, dest_dir)
    except Exception as exc:  # noqa: BLE001
        logger.warning("下载 PDF 失败：%s", str(exc)[:300])
        raise HTTPException(status_code=502, detail="下载 PDF 失败：%s" % str(exc)[:200])

    # 6) 抽取全文（超长截断），失败不阻断入库
    try:
        text, truncated = pdf_service.extract_text_for_context(pdf_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("PDF 文本抽取失败（%s）：%s", pdf_path, str(exc)[:300])
        text, truncated = "", False

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
        text_truncated=truncated,
    )
    db.add(paper)
    db.commit()
    db.refresh(paper)
    return paper


@router.get("", response_model=List[schemas.PaperOut])
def list_papers(folder_id: Optional[int] = None, q: Optional[str] = None,
                db: Session = Depends(database.get_db)):
    query = db.query(Paper)
    if folder_id is not None:
        query = query.filter(Paper.folder_id == folder_id)
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
        paper.folder_id = body.folder_id
    if "title" in fields and body.title is not None:
        title = body.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="标题不能为空")
        paper.title = title
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
    db.delete(paper)   # 级联删除其会话与消息
    db.commit()
    return Response(status_code=204)


@router.get("/{paper_id}/pdf")
def get_pdf(paper_id: int, db: Session = Depends(database.get_db)):
    paper = _get_paper_or_404(db, paper_id)
    if not paper.pdf_path or not os.path.isfile(paper.pdf_path):
        raise HTTPException(status_code=404, detail="PDF 文件不存在")
    # inline 让浏览器直接在 iframe 内渲染，而非触发下载
    return FileResponse(
        paper.pdf_path,
        media_type="application/pdf",
        filename=os.path.basename(paper.pdf_path),
        content_disposition_type="inline",
    )


@router.get("/{paper_id}/text")
def get_text(paper_id: int, db: Session = Depends(database.get_db)):
    paper = _get_paper_or_404(db, paper_id)
    return {
        "arxiv_id": paper.arxiv_id,
        "truncated": paper.text_truncated,
        "char_count": len(paper.full_text or ""),
        "text": paper.full_text or "",
    }


@router.post("/{paper_id}/reextract", response_model=schemas.PaperOut)
def reextract_text(paper_id: int, db: Session = Depends(database.get_db)):
    """重新抽取论文全文（用于入库时抽取失败 / 正文为空的论文恢复）。

    若本地 PDF 存在则直接重抽；否则根据 arxiv_id 重新下载后再抽取。
    抽取仍失败时返回 502，不覆盖已有内容。
    """
    paper = _get_paper_or_404(db, paper_id)

    pdf_path = paper.pdf_path if (paper.pdf_path and os.path.isfile(paper.pdf_path)) else None
    if pdf_path is None:
        # 本地无 PDF，尝试重新下载
        folder = db.get(Folder, paper.folder_id) if paper.folder_id else None
        dest_dir = os.path.join(
            config_loader.get_pdf_dir(), _safe_dirname(folder.name if folder else "Inbox")
        )
        try:
            meta = arxiv_service.fetch_metadata(paper.arxiv_id)
            downloaded = arxiv_service.download_pdf(meta, dest_dir)
        except Exception as exc:  # noqa: BLE001
            logger.warning("重抽时重新下载 PDF 失败（%s）：%s", paper.arxiv_id, str(exc)[:300])
            raise HTTPException(status_code=502, detail="重新下载 PDF 失败：%s" % str(exc)[:200])
        pdf_path = str(downloaded)
        paper.pdf_path = pdf_path

    try:
        text, truncated = pdf_service.extract_text_for_context(pdf_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("重新抽取全文失败（%s）：%s", pdf_path, str(exc)[:300])
        raise HTTPException(status_code=502, detail="抽取全文失败：%s" % str(exc)[:200])

    if not text.strip():
        raise HTTPException(status_code=502, detail="未能从 PDF 抽取到文本（可能是扫描版/图片型 PDF）")

    paper.full_text = text
    paper.text_truncated = truncated
    db.commit()
    db.refresh(paper)
    return paper
