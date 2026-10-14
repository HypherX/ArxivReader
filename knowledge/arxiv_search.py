"""ArXiv 标题检索（只取元信息，不下载全文）。

智能关联检索专用：把 Related Work / 参考文献里提取到的论文标题转成查询，命中后按标题
相似度重排，取"最像这一篇"的结果。

对外：
  build_title_query(..) 按标题拼 ti: 表达式
  title_similarity(..)  标题相似度（词集合 Jaccard）
  title_queries(..)     一个标题的降级查询序列（整题 -> 前缀 -> 关键短语）
  search_by_title(...)  按标题检索，返回按相似度降序的命中（match_score 已填）
  search_reference(...)  一条引用种子的检索入口（有 arXiv ID 时直接精确命中）
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import arxiv

from harness import arxiv_client
from harness.cancel import Cancelled

logger = logging.getLogger(__name__)

_API_PAGE_SIZE = 100

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

# ---------------------------------------------------------------- 标题检索（智能关联检索用）
_TITLE_STOPWORDS = frozenset({
    "a", "an", "the", "of", "for", "and", "or", "on", "in", "with", "to", "via",
    "using", "towards", "toward", "from", "by", "at", "is", "are", "be", "as",
    "into", "over", "under", "about", "all", "can", "do", "does", "how", "what",
    "when", "new", "novel",
})
_TITLE_TERM_LIMIT = 12            # 拼查询用的最多检索词数（控制查询长度）
_TITLE_SIMILARITY_FLOOR = 0.7     # 标题匹配低于此值直接视为没匹配上（不再进入后续判定）

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
                # ID 与提取标题对不上（模型可能配错）时不当作确定引用，退回标题检索。
                if title_similarity(seed.get("title", ""), hit.title) >= _TITLE_SIMILARITY_FLOOR:
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

