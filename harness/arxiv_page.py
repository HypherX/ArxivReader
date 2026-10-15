"""arXiv 主站 abs 页元信息抓取：export API 限流时的同源兜底。

主站与 export API 是两套限流：实测 API 被掐（429/晾连接）时 abs 页 0.4s 内返回。
解析 citation_* meta 标签（标题/作者/摘要/日期/PDF 链接），不依赖 arxiv 包。

对外：
  fetch_abs_page_meta(arxiv_id)  抓 abs 页元信息，404 返回 None，暂时故障抛 PageTransient
"""

import html as html_lib
import re
from typing import Any, Callable, Dict, List, Optional

import requests

from .cancel import Cancelled

_ABS_PAGE = "https://arxiv.org/abs/{}"
_CITATION_RE = re.compile(r'<meta\s+name="(citation_[a-z_]+)"\s+content="([^"]*)"\s*/?>', re.I)
_DEFAULT_TIMEOUT = 15.0
_UA = "ArxivReader/1.0 (+https://github.com/HypherX/ArxivReader)"
_VERSION_SUFFIX_RE = re.compile(r"v\d+$")


class PageTransient(Exception):
    """abs 页暂时不可用（超时/429/5xx/页面结构变化）：调用方决定重试或翻译成本层异常。"""


def fetch_abs_page_meta(arxiv_id: str, *, should_stop: Optional[Callable[[], bool]] = None,
                        timeout: float = _DEFAULT_TIMEOUT) -> Optional[Dict[str, Any]]:
    """抓 abs 页元信息；404 返回 None（ID 不存在，非暂时性故障）。

    返回 dict：arxiv_id（去版本号）/title/abstract/authors/published(YYYY-MM-DD)/pdf_url。
    """
    if should_stop and should_stop():
        raise Cancelled("已被用户终止")
    try:
        resp = requests.get(_ABS_PAGE.format(arxiv_id), timeout=timeout,
                            headers={"User-Agent": _UA})
    except requests.exceptions.RequestException as exc:
        raise PageTransient(type(exc).__name__)
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise PageTransient("HTTP {}".format(resp.status_code))
    if should_stop and should_stop():
        raise Cancelled("已被用户终止")
    metas: Dict[str, List[str]] = {}
    for name, content in _CITATION_RE.findall(resp.text):
        metas.setdefault(name, []).append(html_lib.unescape(content).strip())
    title = (metas.get("citation_title") or [""])[0]
    if not title:
        raise PageTransient("页面缺少 citation_title（结构变化？）")
    date = (metas.get("citation_date") or [""])[0].replace("/", "-")
    return {
        "arxiv_id": _VERSION_SUFFIX_RE.sub("", arxiv_id),
        "title": title,
        "abstract": (metas.get("citation_abstract") or [""])[0],
        "authors": [a for a in metas.get("citation_author") or [] if a],
        "published": date or None,
        "pdf_url": (metas.get("citation_pdf_url") or [""])[0] or None,
    }
