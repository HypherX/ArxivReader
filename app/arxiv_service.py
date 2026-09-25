"""ArXiv 链接解析、元数据获取与 PDF 下载（基于官方 arxiv 包，2.x API）。

对外：
  normalize_arxiv_id(raw)   把各种形式的链接/ID 规范成 arxiv id
  fetch_metadata(arxiv_id)  取标题/作者/摘要/分类/发表时间/pdf_url -> PaperMeta
  download_pdf(meta, dir)   下载 PDF 到指定目录，返回本地路径
"""

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import arxiv

logger = logging.getLogger(__name__)

# 新式 id：2401.12345 / 2401.12345v2（4 位年份月份 + 4~5 位序号）
_NEW_ID_RE = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")
# 旧式 id：math/0211159 / cs.CL/0112017（可带版本）
_OLD_ID_RE = re.compile(r"([a-z\-]+(?:\.[A-Z]{2})?)/(\d{7})(v\d+)?")
_VERSION_SUFFIX_RE = re.compile(r"v\d+$")

_CLIENT: Optional["arxiv.Client"] = None


def _get_client() -> "arxiv.Client":
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = arxiv.Client(page_size=10, delay_seconds=3.0, num_retries=3)
    return _CLIENT


@dataclass
class PaperMeta:
    arxiv_id: str                       # 规范 id（不含版本号）
    title: str
    authors: List[str]
    abstract: str
    categories: List[str]
    published: Optional[str]
    pdf_url: Optional[str]
    result: object = field(default=None, repr=False)   # 原始 arxiv.Result，供下载用


def normalize_arxiv_id(raw: str) -> str:
    """把裸 id / abs 链接 / pdf 链接 / 旧式 id 规范成 arxiv id（保留版本号）。

    支持：
      2401.12345 | 2401.12345v2
      http(s)://arxiv.org/abs/2401.12345(v1)
      http(s)://arxiv.org/pdf/2401.12345(.pdf)
      math/0211159 | http://arxiv.org/abs/math/0211159
    """
    if not raw or not raw.strip():
        raise ValueError("空的 ArXiv 链接/ID")

    text = raw.strip()
    text = text.split("?", 1)[0].split("#", 1)[0]      # 去查询串/锚点
    if text.lower().endswith(".pdf"):
        text = text[:-4]
    text = text.rstrip("/")

    old = _OLD_ID_RE.search(text)
    if old:
        archive, num, ver = old.group(1), old.group(2), old.group(3) or ""
        return "{}/{}{}".format(archive, num, ver)

    new = _NEW_ID_RE.search(text)
    if new:
        return new.group(1) + (new.group(2) or "")

    raise ValueError("无法解析的 ArXiv 链接/ID：{}".format(raw))


def strip_version(arxiv_id: str) -> str:
    """去掉尾部版本号，用作数据库去重键（v1/v2 视为同一篇）。"""
    return _VERSION_SUFFIX_RE.sub("", arxiv_id)


def fetch_metadata(arxiv_id: str) -> PaperMeta:
    """按 id 取元数据；arxiv_id 可含版本号。找不到时抛 ValueError。"""
    search = arxiv.Search(id_list=[arxiv_id])
    result = next(_get_client().results(search), None)
    if result is None:
        raise ValueError("ArXiv 未找到该论文：{}".format(arxiv_id))

    short_id = result.get_short_id()
    published = result.published.isoformat() if getattr(result, "published", None) else None
    return PaperMeta(
        arxiv_id=strip_version(short_id),
        title=(result.title or "").strip(),
        authors=[a.name for a in (result.authors or [])],
        abstract=(result.summary or "").strip(),
        categories=list(result.categories or []),
        published=published,
        pdf_url=getattr(result, "pdf_url", None),
        result=result,
    )


def download_pdf(meta: PaperMeta, dest_dir: str, filename: Optional[str] = None) -> Path:
    """下载 PDF 到 dest_dir，返回本地文件路径。"""
    if meta.result is None:
        raise ValueError("缺少 arxiv 结果对象，无法下载 PDF")
    os.makedirs(dest_dir, exist_ok=True)
    fname = filename or (meta.arxiv_id.replace("/", "_") + ".pdf")
    path = meta.result.download_pdf(dirpath=dest_dir, filename=fname)
    return Path(path)
