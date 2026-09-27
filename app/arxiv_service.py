"""ArXiv 链接解析、元数据获取与 PDF 下载（基于官方 arxiv 包，2.x API）。

对外：
  normalize_arxiv_id(raw)   把各种形式的链接/ID 规范成 arxiv id
  fetch_metadata(arxiv_id)  取标题/作者/摘要/分类/发表时间/pdf_url -> PaperMeta
  download_pdf(meta, dir)   下载 PDF 到指定目录，返回本地路径
  safe_dirname(name)        文件夹名清洗（入库落盘路径共用同一口径）

下载策略：先试**多连接分块并行**（arXiv 支持 Range，本机单连接常被限速到几十 KB/s），
任一步骤不满足条件（不支持 Range / 分块失败 / 成品校验不通过）就回落官方客户端的
单连接下载，保证行为与以前一致。
"""

import logging
import os
import re
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
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
_UNSAFE_NAME_RE = re.compile(r"[^\w\-. ]")

_UA = "ArxivReader/1.0"
_PARALLEL_CHUNK = 262144
_PARALLEL_WORKERS = 8
_PARALLEL_TIMEOUT = 60.0

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


def safe_dirname(name: str) -> str:
    """文件夹名清洗：去路径分隔符与控制字符，空则回退 Inbox。"""
    cleaned = _UNSAFE_NAME_RE.sub("_", (name or "").strip()).strip(".")
    return cleaned or "Inbox"


def _head(url: str, timeout: float):
    """返回 (内容长度, 是否支持 Range)；取不到长度则 (0, False)。"""
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            size = int(resp.headers.get("Content-Length") or 0)
            return size, "bytes" in (resp.headers.get("Accept-Ranges") or "")
    except Exception as exc:  # noqa: BLE001
        logger.info("HEAD 探测失败（将回落单连接下载）：%s", str(exc)[:120])
        return 0, False


def _fetch_chunk(url: str, start: int, end: int, timeout: float, tries: int = 3) -> bytes:
    last: Optional[Exception] = None
    for _ in range(max(1, tries)):
        try:
            request = urllib.request.Request(url, headers={
                "User-Agent": _UA, "Range": "bytes={}-{}".format(start, end)})
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
    raise RuntimeError("分块下载失败 {}-{}：{}".format(start, end, str(last)[:100]))


def _pdf_ok(path: Path) -> bool:
    """成品校验：能被 PyMuPDF 打开且有页（防止半截文件）；无 fitz 时只看非空。"""
    if not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        import fitz

        with fitz.open(str(path)) as doc:
            return doc.page_count > 0
    except ImportError:
        return True
    except Exception:  # noqa: BLE001
        return False


def download_pdf(meta: PaperMeta, dest_dir: str, filename: Optional[str] = None) -> Path:
    """下载 PDF 到 dest_dir，返回本地文件路径。

    先试多连接分块并行（快），不满足条件则回落官方客户端的单连接下载（稳）。
    """
    os.makedirs(dest_dir, exist_ok=True)
    fname = filename or (meta.arxiv_id.replace("/", "_") + ".pdf")
    dest = Path(dest_dir) / fname
    url = meta.pdf_url or "https://arxiv.org/pdf/{}".format(meta.arxiv_id)

    if _download_parallel(url, dest):
        logger.info("并行分块下载完成：%s", dest)
        return dest

    if meta.result is None:
        raise ValueError("缺少 arxiv 结果对象，无法下载 PDF")
    logger.info("回落单连接下载：%s", url)
    return Path(meta.result.download_pdf(dirpath=dest_dir, filename=fname))


def _download_parallel(url: str, dest: Path, *,
                       workers: int = _PARALLEL_WORKERS,
                       chunk_bytes: int = _PARALLEL_CHUNK,
                       timeout: float = _PARALLEL_TIMEOUT) -> bool:
    """多连接分块下载；任何一步不满足条件都返回 False（由调用方回落单连接）。"""
    size, ranges = _head(url, timeout)
    if not (size and ranges):
        return False
    spans = [(i, start, min(start + chunk_bytes - 1, size - 1))
             for i, start in enumerate(range(0, size, chunk_bytes))]
    results: List[Optional[bytes]] = [None] * len(spans)
    lock = threading.Lock()

    def job(index: int, start: int, end: int) -> None:
        data = _fetch_chunk(url, start, end, timeout)
        with lock:
            results[index] = data

    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            list(pool.map(lambda span: job(*span), spans))
        tmp = dest.with_suffix(dest.suffix + ".part")
        with open(tmp, "wb") as fh:
            for index, data in enumerate(results):
                if data is None:
                    raise RuntimeError("分块缺失：第 {} 块".format(index))
                fh.write(data)
        if tmp.stat().st_size != size:
            raise RuntimeError("下载大小不符：{} != {}".format(tmp.stat().st_size, size))
        if not _pdf_ok(tmp):
            tmp.unlink(missing_ok=True)
            raise RuntimeError("成品 PDF 校验未通过")
        tmp.replace(dest)
        return True
    except Exception as exc:  # noqa: BLE001  回落到单连接下载
        logger.warning("并行分块下载失败（%s），回落单连接：%s", url, str(exc)[:160])
        return False
