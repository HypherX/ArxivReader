"""ArXiv 批量检索（只取元信息，不下载全文）。

用途：检索 Pipeline 的 Step 1。为了省带宽与时间，这里**只用 arXiv 官方 API 拉标题/摘要/作者/分类/日期**，
绝不触发 PDF 下载；全文下载只发生在用户点“加入详细阅读”（迁入正式库）之后。

检索条件（对齐前端表单）：
  categories  分类列表，多个分类取并集（cs.CL / cs.AI / cs.LG …）
  keywords    关键词列表，多个关键词取交集（都需在标题或摘要中出现，避免宽泛检索淹没）
  date_from / date_to  提交/更新日期范围（YYYY-MM-DD）
  max_results 上限（检索窗口内最多取多少条）

对外：
  build_query(...)      拼 arXiv 检索表达式（便于单测与调试）
  search(...)           执行检索，返回 PaperMeta 列表（含 arxiv_id/title/abstract/…）
"""

import datetime
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import arxiv

logger = logging.getLogger(__name__)

DEFAULT_MAX_RESULTS = 40
_API_PAGE_SIZE = 100
_MAX_KEYWORDS = 6


@dataclass
class SearchHit:
    """一条检索结果（仅元信息，无正文）。"""

    arxiv_id: str
    title: str
    abstract: str
    authors: List[str] = field(default_factory=list)
    categories: List[str] = field(default_factory=list)
    published: Optional[str] = None
    updated: Optional[str] = None
    comment: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "arxiv_id": self.arxiv_id,
            "title": self.title,
            "abstract": self.abstract,
            "authors": self.authors,
            "categories": self.categories,
            "published": self.published,
            "updated": self.updated,
            "comment": self.comment,
        }


def _keyword_group(keyword: str) -> str:
    """把一个关键词（可能多词）变成一个 abs: 条件组。

    实测要点：arXiv 的 `abs:"短语"` 引号形式会返回 0 条（服务端不支持），
    因此拆成多个词用 AND 组合；`abs:fine-tuning` 这类带连字符的单词是有效的。
    同时剔除查询语法保留字符，避免用户输入被当成表达式注入。
    """
    terms = [re.sub(r"[^\w\-.+]", "", t) for t in re.split(r"\s+", (keyword or "").strip())]
    terms = [t for t in terms if t]
    if not terms:
        return ""
    return "(" + " AND ".join("abs:{}".format(t) for t in terms) + ")"


def build_query(categories: Sequence[str], keywords: Sequence[str],
                date_from: Optional[str] = None, date_to: Optional[str] = None,
                keyword_mode: str = "and") -> str:
    """拼检索表达式：分类取并集，关键词取交/并集，日期用 submittedDate 字段。

    注意：日期范围必须写成 `submittedDate:[...]`（实测无字段名的裸范围会被服务端忽略）。
    """
    groups: List[str] = []
    cats = [c.strip() for c in (categories or []) if c and c.strip()]
    if cats:
        groups.append("(" + " OR ".join("cat:{}".format(c) for c in cats) + ")")

    keyword_groups = [g for g in (_keyword_group(k) for k in
                                  [k for k in (keywords or []) if k and k.strip()][:_MAX_KEYWORDS]) if g]
    if keyword_groups:
        joiner = " OR " if str(keyword_mode).lower() == "or" else " AND "
        groups.append("(" + joiner.join(keyword_groups) + ")")

    span = _date_range(date_from, date_to)
    if span:
        groups.append(span)
    return " AND ".join(groups)


def _date_range(date_from: Optional[str], date_to: Optional[str]) -> str:
    """arXiv 提交日期范围：submittedDate:[YYYYMMDDHHMM TO YYYYMMDDHHMM]。"""
    start = _parse_date(date_from, end=False)
    end = _parse_date(date_to, end=True)
    if not start and not end:
        return ""
    start = start or datetime.datetime(1991, 1, 1)
    end = end or datetime.datetime.utcnow()
    return "submittedDate:[{} TO {}]".format(start.strftime("%Y%m%d%H%M"),
                                             end.strftime("%Y%m%d%H%M"))


def _parse_date(value: Optional[str], *, end: bool) -> Optional[datetime.datetime]:
    if not value:
        return None
    text = str(value).strip()[:10]
    try:
        day = datetime.datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        logger.warning("无法解析日期：%r（已忽略）", value)
        return None
    return day + (datetime.timedelta(days=1) - datetime.timedelta(seconds=1) if end else
                  datetime.timedelta(0))


def _to_hit(result: Any) -> SearchHit:
    short_id = result.get_short_id()
    return SearchHit(
        arxiv_id=re.sub(r"v\d+$", "", short_id),
        title=(result.title or "").strip(),
        abstract=(result.summary or "").strip(),
        authors=[a.name for a in (result.authors or [])],
        categories=list(result.categories or []),
        published=result.published.isoformat() if getattr(result, "published", None) else None,
        updated=result.updated.isoformat() if getattr(result, "updated", None) else None,
        comment=(getattr(result, "comment", "") or "").strip(),
    )


def search(categories: Sequence[str], keywords: Sequence[str], *,
           date_from: Optional[str] = None, date_to: Optional[str] = None,
           max_results: int = DEFAULT_MAX_RESULTS,
           sort_by: str = "submitted", keyword_mode: str = "and") -> List[SearchHit]:
    """执行检索；网络/解析异常向上抛出，由调用方记录到 run 日志。"""
    query = build_query(categories, keywords, date_from, date_to, keyword_mode=keyword_mode)
    if not query:
        raise ValueError("检索条件为空：至少给一个分类、关键词或日期范围")
    limit = max(1, min(int(max_results or DEFAULT_MAX_RESULTS), 500))
    sort = arxiv.SortCriterion.SubmittedDate if sort_by == "submitted" \
        else arxiv.SortCriterion.Relevance
    logger.info("arXiv 检索：query=%s limit=%d", query, limit)

    client = arxiv.Client(page_size=_API_PAGE_SIZE, delay_seconds=3.0, num_retries=3)
    search_req = arxiv.Search(query=query, max_results=limit, sort_by=sort)
    hits = [_to_hit(item) for item in client.results(search_req)]
    logger.info("arXiv 检索完成：%d 条", len(hits))
    return hits
