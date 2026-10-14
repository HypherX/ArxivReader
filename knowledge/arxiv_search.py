"""ArXiv 标题检索（只取元信息，不下载全文）。

智能关联检索专用：把 Related Work / 参考文献里提取到的论文标题转成查询，命中后按标题
相似度重排，取"最像这一篇"的结果。

召回策略（按真实引用文本的三种形态设计）：
  - 完整标题：整题 AND 查询 + Jaccard 0.7 即收。
  - 截断/缩写标题（参考文献常只写标题主干）：包含度 ≥0.9 也收，避免整题 Jaccard
    因尾部缺失被一刀切（实测 "A decomposable attention model" vs 完整标题只差后缀）。
  - 标题措辞与 arXiv 不同（改名/会议版）：追加 all: 全字段查询和 au: 作者查询兜底。
误配防线：种子带作者时，标题匹配弱（Jaccard <0.9）且作者零重叠的命中直接拒收
（实测 "Long short-term memory" 会误配到 "Lipreading with Long Short-Term Memory"）。

对外：
  build_title_query(..) 按标题拼 ti: 表达式
  title_similarity(..)  标题相似度（词集合 Jaccard）
  title_containment(..) 标题包含度（交集 / 较小集合，衡量截断标题）
  title_queries(..)     一个标题的降级查询序列（整题 -> 前缀 -> 关键短语 -> 全字段）
  search_by_title(...)  按标题检索，返回按相似度降序的命中（match_score 已填）
  search_reference(...)  一条引用种子的检索入口（有 arXiv ID 时直接精确命中）
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
_JACCARD_FLOOR = 0.7              # 完整标题匹配：Jaccard 低于此值视为没匹配上
_CONTAINMENT_FLOOR = 0.9          # 截断标题匹配：种子词几乎全出现在命中标题里也收
_MIN_PARTIAL_TERMS = 3            # 走包含度通道时较小词集合至少这么多词（防单词标题滥配）
_ID_JACCARD_FLOOR = 0.5           # 真实 arXiv ID 命中的标题防伪造门槛（Jaccard）
_ID_CONTAINMENT_FLOOR = 0.7       # 同上（包含度通道，允许参考文献写截断标题）
_AUTHOR_BONUS = 0.05              # 作者有重叠时的加分
_AUTHOR_GATE_JACCARD = 0.9        # 种子带作者但零重叠时，Jaccard 达到此值仍可豁免（标题几乎一致）

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

def _term_stats(title_a: str, title_b: str) -> Tuple[float, float, int]:
    """(Jaccard, 包含度=交集/较小集合, 较小集合词数)；任一为空集时返回全零。"""
    set_a = set(title_terms(title_a, limit=40))
    set_b = set(title_terms(title_b, limit=40))
    if not set_a or not set_b:
        return 0.0, 0.0, 0
    inter = len(set_a & set_b)
    return inter / len(set_a | set_b), inter / min(len(set_a), len(set_b)), min(len(set_a), len(set_b))

def title_similarity(title_a: str, title_b: str) -> float:
    """标题相似度（检索词集合的 Jaccard，0~1）：用于排序与过滤低质量命中。"""
    return _term_stats(title_a, title_b)[0]

def title_containment(title_a: str, title_b: str) -> float:
    """标题包含度（交集 / 较小集合，0~1）：截断标题对完整标题时接近 1。"""
    return _term_stats(title_a, title_b)[1]

def _title_accept(jaccard: float, containment: float, min_terms: int) -> bool:
    """标题是否算匹配上：完整匹配走 Jaccard；截断标题走包含度（要求词数下限）。"""
    if jaccard >= _JACCARD_FLOOR:
        return True
    return min_terms >= _MIN_PARTIAL_TERMS and containment >= _CONTAINMENT_FLOOR

def _author_terms(authors: str) -> set:
    """作者字符串 -> 名字词集合（复用标题分词；连字符/大小写已归一）。"""
    return set(title_terms(authors, limit=40))

def _distinctive_term(terms: Sequence[str]) -> str:
    """取最有区分度的词（最长者优先），无则空串。"""
    return max(terms, key=len) if terms else ""

def _author_gate_ok(seed_authors: str, hit_authors: List[str], jaccard: float) -> bool:
    """种子给了作者时的误配闸门：零重叠且标题并非几乎一致 -> 拒收。"""
    seed_terms = _author_terms(seed_authors)
    if not seed_terms:
        return True
    if seed_terms & _author_terms(" ".join(hit_authors)):
        return True
    return jaccard >= _AUTHOR_GATE_JACCARD

def title_queries(title: str, authors: str = "") -> List[str]:
    """严格标题、少量核心词、标题/摘要宽松检索逐级回退；再给全字段与作者兜底。

    all: 兜底覆盖"引用标题与 arXiv 标题措辞不同"（改名 / 会议版）；作者查询覆盖
    标题完全改写但作者稳定的情形（取最有区分度的作者词 + 标题核心词）。
    """
    terms = title_terms(title)
    if not terms:
        return []
    core = sorted(terms, key=lambda term: -len(term))[:3]
    queries = [build_title_query(title), " AND ".join("ti:" + t for t in core),
               " AND ".join("(ti:{0} OR abs:{0})".format(t) for t in core[:2]),
               " AND ".join("all:" + t for t in core)]
    author_terms = [t for t in _author_terms(authors) if len(t) >= 4]
    distinctive_author = _distinctive_term(author_terms)
    if distinctive_author and core:
        queries.append("au:{} AND all:{}".format(distinctive_author, core[0]))
    return list(dict.fromkeys(queries))

def search_by_title(title: str, *, max_results: int = 3,
                    client: Optional[arxiv.Client] = None, authors: str = "",
                    should_stop: Any = None) -> List[SearchHit]:
    """宽松回退并按标题/作者证据重排；近似标题不作为确定引用。"""
    limit = max(1, min(int(max_results or 3), 20))
    collected: Dict[str, SearchHit] = {}
    for index, query in enumerate(title_queries(title, authors)):
        request = arxiv.Search(query=query, max_results=max(limit * 3, 10),
                               sort_by=arxiv.SortCriterion.Relevance)
        for item in arxiv_client.results(request, client=client, should_stop=should_stop):
            hit = _to_hit(item)
            similarity, containment, min_terms = _term_stats(title, hit.title)
            author_match = bool(_author_terms(authors) & _author_terms(" ".join(hit.authors)))
            if not _title_accept(similarity, containment, min_terms):
                continue
            if not _author_gate_ok(authors, hit.authors, similarity):
                continue
            hit.match_score = round(min(1.0, max(similarity, containment * 0.92)
                                          + (_AUTHOR_BONUS if author_match else 0)), 4)
            source = {"kind": "title", "query": query, "fallback": index,
                      "title_similarity": similarity, "title_containment": containment,
                      "author_match": author_match, "exact_reference": False}
            if hit.arxiv_id in collected:
                old = collected[hit.arxiv_id]
                old.query_sources.append(source)
                old.match_score = max(old.match_score, hit.match_score)
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
    seed_title = seed.get("title", "")
    if arxiv_id:
        try:
            items = arxiv_client.results(arxiv.Search(id_list=[arxiv_id], max_results=1),
                                         should_stop=should_stop)
            if items:
                hit = _to_hit(items[0])
                if not seed_title.strip():
                    # 引用只给了 ID 没给标题：ID 本身已在原文中核实过，直接算确定引用。
                    hit.match_score = 1.0
                    hit.query_sources = [{"kind": "arxiv_id", "query": arxiv_id,
                                          "exact_reference": True}]
                    return [hit]
                # ID 与提取标题对不上（模型可能配错）时不当作确定引用，退回标题检索。
                # 参考文献常写截断标题，门槛用宽松 Jaccard / 包含度双通道。
                similarity, containment, min_terms = _term_stats(seed_title, hit.title)
                if similarity >= _ID_JACCARD_FLOOR or (
                        min_terms >= _MIN_PARTIAL_TERMS and containment >= _ID_CONTAINMENT_FLOOR):
                    hit.match_score = 1.0
                    hit.query_sources = [{"kind": "arxiv_id", "query": arxiv_id,
                                          "title_similarity": similarity,
                                          "title_containment": containment,
                                          "exact_reference": True}]
                    return [hit]
        except Cancelled:
            raise
        except Exception:
            if not seed_title:
                raise
    return search_by_title(seed_title, max_results=max_results,
                           authors=seed.get("authors", ""), should_stop=should_stop)

