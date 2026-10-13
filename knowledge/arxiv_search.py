"""ArXiv 检索（只取元信息，不下载全文）。

两种入口：
1) 批量检索（检索 Pipeline 的 Step 1）：分类/关键词/日期范围拼表达式，省带宽与时间；
2) 标题检索 search_by_title（智能关联检索用）：把 Related Work / 参考文献里的
   论文标题直接转成查询，命中后按标题相似度重排取“匹配度最高”的论文。

检索条件（对齐前端表单）：
  categories  分类列表，多个分类取并集（cs.CL / cs.AI / cs.LG …）
  keywords    关键词列表，多个关键词取交集（都需在标题或摘要中出现，避免宽泛检索淹没）
  date_from / date_to  提交/更新日期范围（YYYY-MM-DD）
  max_results 上限（检索窗口内最多取多少条）

对外：
  build_query(...)      拼 arXiv 检索表达式（便于单测与调试）
  build_title_query(..) 按标题拼 ti: 表达式
  title_similarity(..)  标题相似度（词集合 Jaccard）
  search(...)           执行批量检索，返回 PaperMeta 列表（含 arxiv_id/title/abstract/…）
  search_by_title(...)  按标题检索，返回按相似度降序的命中（match_score 已填）
"""

import datetime
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import arxiv

from harness import arxiv_client
from harness.cancel import Cancelled

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
    match_score: float = 0.0        # 标题检索时的相似度（批量检索为 0）
    query_sources: List[Dict[str, Any]] = field(default_factory=list)

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
            "match_score": self.match_score,
            "query_sources": self.query_sources,
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
    return "(" + " AND ".join("(ti:{0} OR abs:{0})".format(t) for t in terms) + ")"


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
           sort_by: str = "submitted", keyword_mode: str = "and",
           should_stop: Any = None) -> List[SearchHit]:
    """执行检索；网络/解析异常向上抛出，由调用方记录到 run 日志。"""
    query = build_query(categories, keywords, date_from, date_to, keyword_mode=keyword_mode)
    if not query:
        raise ValueError("检索条件为空：至少给一个分类、关键词或日期范围")
    limit = max(1, min(int(max_results or DEFAULT_MAX_RESULTS), 500))
    sort = arxiv.SortCriterion.SubmittedDate if sort_by == "submitted" \
        else arxiv.SortCriterion.Relevance
    logger.info("arXiv 检索：query=%s limit=%d", query, limit)

    search_req = arxiv.Search(query=query, max_results=limit, sort_by=sort)
    hits = [_to_hit(item) for item in arxiv_client.results(search_req, should_stop=should_stop)]
    logger.info("arXiv 检索完成：%d 条", len(hits))
    return hits


# ---------------------------------------------------------------- 标题检索（智能关联检索用）
_TITLE_STOPWORDS = frozenset({
    "a", "an", "the", "of", "for", "and", "or", "on", "in", "with", "to", "via",
    "using", "towards", "toward", "from", "by", "at", "is", "are", "be", "as",
    "into", "over", "under", "about", "all", "can", "do", "does", "how", "what",
    "when", "new", "novel",
})
_TITLE_TERM_LIMIT = 12            # 拼查询用的最多检索词数（控制查询长度）
_TITLE_SIMILARITY_FLOOR = 0.15    # 仅剔除无词汇证据的噪声；是否相关由摘要筛选决定


def title_terms(title: str, limit: int = _TITLE_TERM_LIMIT) -> List[str]:
    """标题 -> 检索词：去标点、去停用词、去重（保序），最多 limit 个。"""
    cleaned = re.sub(r"[^A-Za-z0-9\-\s]", " ", str(title or ""))
    terms: List[str] = []
    for word in cleaned.split():
        low = word.strip("-").lower()
        if len(low) < 2 or low in _TITLE_STOPWORDS:
            continue
        if low not in terms:
            terms.append(low)
        if len(terms) >= max(1, int(limit)):
            break
    return terms


def build_title_query(title: str, limit: int = _TITLE_TERM_LIMIT) -> str:
    """按标题拼 ti: 查询（多词 AND）。

    实测要点（与 abs: 同类）：带引号的短语 `ti:"..."` 会返回 0 条，
    因此拆词后用 AND 组合，再配合本地标题相似度重排取“匹配度最高”的论文。
    """
    terms = title_terms(title, limit=limit)
    if not terms:
        return ""
    return " AND ".join("ti:{}".format(t) for t in terms)


def title_similarity(title_a: str, title_b: str) -> float:
    """标题相似度（检索词集合的 Jaccard，0~1）：用于排序与过滤低质量命中。"""
    set_a = set(title_terms(title_a, limit=40))
    set_b = set(title_terms(title_b, limit=40))
    if not set_a or not set_b:
        return 0.0
    union = len(set_a | set_b)
    return len(set_a & set_b) / union if union else 0.0


def title_queries(title: str) -> List[str]:
    """严格标题、少量核心词、标题/摘要宽松检索逐级回退。"""
    terms = title_terms(title)
    if not terms:
        return []
    core = sorted(terms, key=lambda term: -len(term))[:3]
    queries = [build_title_query(title), " AND ".join("ti:" + t for t in core),
               " AND ".join("(ti:{0} OR abs:{0})".format(t) for t in core[:2])]
    return list(dict.fromkeys(queries))


def search_by_title(title: str, *, max_results: int = 3,
                    client: Optional[arxiv.Client] = None, authors: str = "",
                    should_stop: Any = None) -> List[SearchHit]:
    """宽松回退并按标题/作者证据重排；近似标题不作为确定引用。"""
    limit = max(1, min(int(max_results or 3), 20))
    collected: Dict[str, SearchHit] = {}
    for index, query in enumerate(title_queries(title)):
        request = arxiv.Search(query=query, max_results=max(limit * 3, 10),
                               sort_by=arxiv.SortCriterion.Relevance)
        for item in arxiv_client.results(request, client=client, should_stop=should_stop):
            hit = _to_hit(item)
            similarity = title_similarity(title, hit.title)
            author_terms = set(title_terms(authors, 40))
            matched_authors = bool(author_terms & set(title_terms(" ".join(hit.authors), 40)))
            hit.match_score = round(min(1.0, similarity + (0.1 if matched_authors else 0)), 4)
            if similarity < _TITLE_SIMILARITY_FLOOR:
                continue
            source = {"kind": "title", "query": query, "fallback": index,
                      "title_similarity": similarity, "author_match": matched_authors,
                      "exact_reference": False}
            if hit.arxiv_id in collected:
                collected[hit.arxiv_id].query_sources.append(source)
            else:
                hit.query_sources = [source]
                collected[hit.arxiv_id] = hit
        if len(collected) >= limit or any(h.match_score >= 0.95 for h in collected.values()):
            break
    return sorted(collected.values(), key=lambda h: -h.match_score)[:limit]


def search_reference(seed: Dict[str, str], *, max_results: int = 3,
                     should_stop: Any = None) -> List[SearchHit]:
    """优先真实 arXiv ID；未定位时按标题逐级回退。"""
    arxiv_id = seed.get("arxiv_id")
    if arxiv_id:
        try:
            items = arxiv_client.results(arxiv.Search(id_list=[arxiv_id], max_results=1),
                                         should_stop=should_stop)
            if items:
                hit = _to_hit(items[0])
                hit.match_score = 1.0
                hit.query_sources = [{"kind": "arxiv_id", "query": arxiv_id,
                                      "exact_reference": True}]
                return [hit]
        except Cancelled:
            raise
        except Exception:
            if not seed.get("title"):
                raise
    return search_by_title(seed.get("title", ""), max_results=max_results,
                           authors=seed.get("authors", ""), should_stop=should_stop)


def search_topic(terms: Sequence[str], *, max_results: int = 20,
                 should_stop: Any = None) -> List[SearchHit]:
    query = build_query([], terms)
    hits = search([], terms, max_results=max_results, sort_by="relevance", should_stop=should_stop)
    for hit in hits:
        hit.query_sources = [{"kind": "topic", "query": query, "exact_reference": False}]
    return hits
