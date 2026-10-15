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

限流兜底（export.arxiv.org 被掐时的降级链，互不拖累）：
  - ID 种子：API id_list 失败 -> arXiv 主站 abs 页 meta 标签（与 export API 两套限流，
    实测 API 被掐时 abs 页 0.4s 内返回，字段齐全：citation_title/author/abstract）。
  - 标题检索：API search_query 整轮失败 -> OpenAlex title.search（免费、配额宽裕，
    带摘要与 arXiv 链接），命中仍过同一套标题/作者验收门槛，只保留有 arXiv ID 的。
"""

import html as html_lib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import arxiv
import requests

from harness import arxiv_client
from harness.cancel import Cancelled

logger = logging.getLogger(__name__)

_API_PAGE_SIZE = 100
_ID_BATCH_SIZE = 50          # id_list 批量查询的单次 ID 数（URL 长度与限流的双重考虑）

# export API 熔断：撞一次限流后 3 分钟内直接走兜底源，不再每个种子花几十秒
# 重新发现服务器还在掐流量（实测限流期 83% 请求被晾到超时或 429）。
_CIRCUIT_SECONDS = 180.0
_circuit_until = 0.0


def _circuit_open() -> bool:
    return time.time() < _circuit_until


def _trip_circuit() -> None:
    global _circuit_until
    _circuit_until = max(_circuit_until, time.time() + _CIRCUIT_SECONDS)


def _reset_circuit() -> None:
    global _circuit_until
    _circuit_until = 0.0


class SearchTransient(Exception):
    """arXiv 暂时不可用（429/503 限流、超时、连接错误）。

    继续打同一个服务器只会延长限流，调用方应停止当前级联、记下来，
    等冷却后整轮重试，而不是原地硬扛。
    """


def _run_query(request: Any, *, client: Any = None, should_stop: Any = None) -> List[Any]:
    """包一层 arxiv_client.results：把限流/超时翻译成 SearchTransient 快速失败并熔断。"""
    try:
        out = arxiv_client.results(request, client=client, should_stop=should_stop)
        _reset_circuit()
        return out
    except Cancelled:
        raise
    except arxiv.HTTPError as exc:
        if getattr(exc, "status", None) in (429, 503):
            _trip_circuit()
            raise SearchTransient("arXiv 限流（HTTP {}）".format(exc.status))
        raise
    except requests.exceptions.RequestException as exc:
        _trip_circuit()
        raise SearchTransient("arXiv 请求失败：{}".format(type(exc).__name__))


# ---------------------------------------------------------------- 限流兜底源（非 export API）
_FALLBACK_TIMEOUT = 15.0
_FALLBACK_UA = "ArxivReader/1.0 (+https://github.com/HypherX/ArxivReader)"
_ABS_PAGE = "https://arxiv.org/abs/{}"
_OPENALEX_SEARCH = "https://api.openalex.org/works"
_OPENALEX_FIELDS = "id,title,publication_date,authorships,locations,abstract_inverted_index"
# 新式 1706.03762 与老式 hep-th/9901001 两类 ID，容许版本号后缀。
_ARXIV_URL_RE = re.compile(
    r"arxiv\.org/(?:abs|pdf)/([A-Za-z\-]+(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?", re.I)
_CITATION_RE = re.compile(r'<meta\s+name="(citation_[a-z_]+)"\s+content="([^"]*)"\s*/?>', re.I)


def _stop_check(should_stop: Any) -> None:
    if should_stop and should_stop():
        raise Cancelled("已被用户终止")


def _fallback_get(url: str, *, source: str, params: Any = None, should_stop: Any = None,
                  allow_not_found: bool = False) -> Optional[requests.Response]:
    """兜底源统一入口：超时/429/5xx 翻译成 SearchTransient 快速失败，交给冷却后整轮重试。"""
    _stop_check(should_stop)
    try:
        resp = requests.get(url, params=params, timeout=_FALLBACK_TIMEOUT,
                            headers={"User-Agent": _FALLBACK_UA})
    except requests.exceptions.RequestException as exc:
        raise SearchTransient("{} 请求失败：{}".format(source, type(exc).__name__))
    if allow_not_found and resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise SearchTransient("{} 暂不可用（HTTP {}）".format(source, resp.status_code))
    _stop_check(should_stop)
    return resp


def _oa_abstract(inverted: Optional[Dict[str, List[int]]]) -> str:
    """OpenAlex 摘要是倒排索引 {词: [位置]}，还原成正常文本。"""
    if not inverted:
        return ""
    positions: Dict[int, str] = {}
    for word, places in inverted.items():
        for place in places:
            positions[place] = word
    return " ".join(positions[i] for i in sorted(positions))


def _oa_to_hit(work: Dict[str, Any]) -> Optional["SearchHit"]:
    """OpenAlex work -> SearchHit；没有 arXiv 版本的不要（下游以 arXiv ID 为主键）。"""
    arxiv_id = ""
    for loc in work.get("locations") or []:
        for key in ("landing_page_url", "pdf_url"):
            match = _ARXIV_URL_RE.search(loc.get(key) or "")
            if match:
                arxiv_id = match.group(1)
                break
        if arxiv_id:
            break
    if not arxiv_id:
        return None
    authors = [(a.get("author") or {}).get("display_name", "").strip()
               for a in work.get("authorships") or []]
    return SearchHit(
        arxiv_id=arxiv_id,
        title=(work.get("title") or "").strip(),
        abstract=_oa_abstract(work.get("abstract_inverted_index")),
        authors=[a for a in authors if a],
        published=work.get("publication_date") or None,
    )


def fetch_by_id_via_page(arxiv_id: str, *, should_stop: Any = None) -> Optional["SearchHit"]:
    """arXiv 主站 abs 页拿元信息（export API 限流时的同源兜底）。

    404 说明 ID 不存在，返回 None（不是暂时性故障，不占用重试名额）。"""
    resp = _fallback_get(_ABS_PAGE.format(arxiv_id), source="arXiv abs 页",
                         should_stop=should_stop, allow_not_found=True)
    if resp is None:
        return None
    metas: Dict[str, List[str]] = {}
    for name, content in _CITATION_RE.findall(resp.text):
        metas.setdefault(name, []).append(html_lib.unescape(content).strip())
    title = (metas.get("citation_title") or [""])[0]
    if not title:
        raise SearchTransient("arXiv abs 页缺少 citation_title（页面结构变化？）")
    date = (metas.get("citation_date") or [""])[0].replace("/", "-")
    return SearchHit(
        arxiv_id=re.sub(r"v\d+$", "", arxiv_id),
        title=title,
        abstract=(metas.get("citation_abstract") or [""])[0],
        authors=[a for a in metas.get("citation_author") or [] if a],
        published=date or None,
    )

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

def _grade_title_hit(hit: SearchHit, title: str, authors: str) -> Optional[Tuple[float, float, bool]]:
    """对一条命中过标题/作者验收门槛；通过则填 match_score 并返回 (Jaccard, 包含度, 作者命中)。"""
    similarity, containment, min_terms = _term_stats(title, hit.title)
    author_match = bool(_author_terms(authors) & _author_terms(" ".join(hit.authors)))
    if not _title_accept(similarity, containment, min_terms):
        return None
    if not _author_gate_ok(authors, hit.authors, similarity):
        return None
    hit.match_score = round(min(1.0, max(similarity, containment * 0.92)
                                      + (_AUTHOR_BONUS if author_match else 0)), 4)
    return similarity, containment, author_match


def _collect(collected: Dict[str, SearchHit], hit: SearchHit, source: Dict[str, Any]) -> None:
    """按 arXiv ID 去重收录；重复命中保留最高分并追加来源。"""
    if hit.arxiv_id in collected:
        old = collected[hit.arxiv_id]
        old.query_sources.append(source)
        old.match_score = max(old.match_score, hit.match_score)
    else:
        hit.query_sources = [source]
        collected[hit.arxiv_id] = hit


def search_by_title(title: str, *, max_results: int = 3,
                    client: Optional[arxiv.Client] = None, authors: str = "",
                    should_stop: Any = None) -> List[SearchHit]:
    """宽松回退并按标题/作者证据重排；近似标题不作为确定引用。

    arXiv API 整轮限流（一无所获）时降级 OpenAlex title.search 兜底；
    熔断期直接跳过 API，不为每个种子重复支付超时。"""
    limit = max(1, min(int(max_results or 3), 20))
    collected: Dict[str, SearchHit] = {}
    transient: Optional[SearchTransient] = None
    if _circuit_open():
        transient = SearchTransient("arXiv API 熔断中")
    else:
        try:
            for index, query in enumerate(title_queries(title, authors)):
                request = arxiv.Search(query=query, max_results=max(limit * 3, 10),
                                       sort_by=arxiv.SortCriterion.Relevance)
                for item in _run_query(request, client=client, should_stop=should_stop):
                    hit = _to_hit(item)
                    graded = _grade_title_hit(hit, title, authors)
                    if graded is None:
                        continue
                    similarity, containment, author_match = graded
                    _collect(collected, hit, {"kind": "title", "query": query, "fallback": index,
                                              "title_similarity": similarity,
                                              "title_containment": containment,
                                              "author_match": author_match,
                                              "exact_reference": False})
                if len(collected) >= limit or any(h.match_score >= 0.95 for h in collected.values()):
                    break
        except SearchTransient as exc:
            transient = exc          # 继续打同一服务器只会延长限流，转兜底源
    if not collected and transient is not None:
        # 兜底源健康返回（哪怕是空）就算数：空 = 该文找不到（很多老论文不在 arXiv），
        # 不是暂时性故障，不值得为它冷却重试一整轮。
        return openalex_search_by_title(title, max_results=limit,
                                        authors=authors, should_stop=should_stop)
    return sorted(collected.values(), key=lambda h: -h.match_score)[:limit]


# OpenAlex filter 值的语法字符：? * 被当通配符、逗号被当 OR 分隔符，都会直接 400
# （实测 "Can active memory replace attention?" 与 "...compact, and interpretable..."）。
_OA_FILTER_BAD_RE = re.compile(r"[?*,\"():;\\]+")


def openalex_search_by_title(title: str, *, max_results: int = 3, authors: str = "",
                             should_stop: Any = None) -> List[SearchHit]:
    """OpenAlex title.search 兜底（arXiv API 限流时用）；验收门槛与 arXiv 路径完全一致。"""
    limit = max(1, min(int(max_results or 3), 20))
    filter_title = re.sub(r"\s+", " ", _OA_FILTER_BAD_RE.sub(" ", title or "")).strip()
    if not filter_title:
        return []
    resp = _fallback_get(_OPENALEX_SEARCH, source="OpenAlex", should_stop=should_stop,
                         params={"filter": "title.search:{}".format(filter_title),
                                 "per-page": max(limit * 3, 10), "select": _OPENALEX_FIELDS})
    try:
        works = (resp.json() or {}).get("results") or []
    except ValueError:
        raise SearchTransient("OpenAlex 返回无法解析")
    collected: Dict[str, SearchHit] = {}
    for work in works:
        hit = _oa_to_hit(work)
        if hit is None:
            continue
        graded = _grade_title_hit(hit, title, authors)
        if graded is None:
            continue
        similarity, containment, author_match = graded
        _collect(collected, hit, {"kind": "openalex", "query": filter_title,
                                  "title_similarity": similarity,
                                  "title_containment": containment,
                                  "author_match": author_match, "exact_reference": False})
    return sorted(collected.values(), key=lambda h: -h.match_score)[:limit]

def batch_fetch_by_ids(arxiv_ids: List[str], *, should_stop: Any = None,
                       client: Optional[arxiv.Client] = None) -> Dict[str, SearchHit]:
    """一次 id_list 批量拉取多个 ID 的元信息（1 次请求替代 N 次单查）。

    限流期每个请求都可能挂到超时，批量是把请求数压到最低的关键手段。
    返回 {去掉版本号的 arxiv_id: SearchHit}；查不到的 ID 不在结果里。
    熔断期直接抛 SearchTransient，调用方退化为逐条走 abs 页兜底。"""
    if _circuit_open():
        raise SearchTransient("arXiv API 熔断中，跳过批量查询")
    found: Dict[str, SearchHit] = {}
    seen: List[str] = []
    for raw in arxiv_ids:
        clean = re.sub(r"v\d+$", "", (raw or "").strip())
        if clean and clean not in seen:
            seen.append(clean)
    for offset in range(0, len(seen), _ID_BATCH_SIZE):
        chunk = seen[offset:offset + _ID_BATCH_SIZE]
        request = arxiv.Search(id_list=chunk, max_results=len(chunk))
        for item in _run_query(request, client=client, should_stop=should_stop):
            hit = _to_hit(item)
            found[hit.arxiv_id] = hit
    return found


def _accept_id_hit(hit: SearchHit, seed_title: str, query_label: str,
                   source_kind: str) -> Optional[SearchHit]:
    """ID 命中的防伪造校验：ID 与提取标题对不上（模型可能配错）时不当作确定引用，
    退回标题检索。参考文献常写截断标题，门槛用宽松 Jaccard / 包含度双通道。"""
    if not seed_title.strip():
        # 引用只给了 ID 没给标题：ID 本身已在原文中核实过，直接算确定引用。
        hit.match_score = 1.0
        hit.query_sources = [{"kind": source_kind, "query": query_label,
                              "exact_reference": True}]
        return hit
    similarity, containment, min_terms = _term_stats(seed_title, hit.title)
    if similarity >= _ID_JACCARD_FLOOR or (
            min_terms >= _MIN_PARTIAL_TERMS and containment >= _ID_CONTAINMENT_FLOOR):
        hit.match_score = 1.0
        hit.query_sources = [{"kind": source_kind, "query": query_label,
                              "title_similarity": similarity,
                              "title_containment": containment,
                              "exact_reference": True}]
        return hit
    return None


def search_reference(seed: Dict[str, str], *, max_results: int = 3,
                     should_stop: Any = None,
                     id_cache: Optional[Dict[str, SearchHit]] = None) -> List[SearchHit]:
    """优先真实 arXiv ID；未定位时按标题逐级回退。

    id_cache：batch_fetch_by_ids 的批量结果；命中就不再发单 ID 请求。
    arXiv API 限流时自动降级：ID 种子走主站 abs 页，标题检索走 OpenAlex。"""
    arxiv_id = seed.get("arxiv_id")
    seed_title = seed.get("title", "")
    if arxiv_id:
        clean_id = re.sub(r"v\d+$", "", arxiv_id.strip())
        transient: Optional[SearchTransient] = None
        if id_cache is None and _circuit_open():
            # 熔断期跳过 API 直达 abs 页兜底（省一次必然超时的请求）。
            transient = SearchTransient("arXiv API 熔断中")
        else:
            try:
                if id_cache is not None:
                    items = [id_cache[clean_id]] if clean_id in id_cache else []
                else:
                    items = _run_query(arxiv.Search(id_list=[arxiv_id], max_results=1),
                                       should_stop=should_stop)
                if items:
                    hit = _to_hit(items[0]) if id_cache is None else items[0]
                    accepted = _accept_id_hit(hit, seed_title, arxiv_id, "arxiv_id")
                    if accepted:
                        return [accepted]
            except Cancelled:
                raise
            except SearchTransient as exc:
                transient = exc
            except Exception:
                if not seed_title:
                    raise
        if transient is not None and id_cache is None:
            # export API 与主站是两套限流：API 被掐时 abs 页通常仍可用。
            page_hit = fetch_by_id_via_page(clean_id, should_stop=should_stop)
            if page_hit is not None:
                accepted = _accept_id_hit(page_hit, seed_title, arxiv_id, "abs_page")
                if accepted:
                    return [accepted]
    return search_by_title(seed_title, max_results=max_results,
                           authors=seed.get("authors", ""), should_stop=should_stop)

