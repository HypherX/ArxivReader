"""对话窗口（chat scope）的 agent 工具集。

工具是确定性、零 LLM 的能力，注册进 harness 工具注册表，由 harness.chat 按名装配：

  search_arxiv      arXiv 关键词检索（找相关工作 / 最新进展），OpenAlex 兜底
  get_arxiv_paper   按 arXiv ID 或标题查证一篇论文（复用关联检索的防幻觉验收链）
  search_library    检索本机文库（标题/作者/摘要），回答"我库里有没有…"类问题
  related_papers    当前论文在知识网络里的方向归属与论文间关系边
  find_in_paper     在当前论文全文里定位关键词，返回带页码的原文片段（核对引用用）

fetch_web_page 在 harness.tools.web 注册（不依赖 app 层）。
所有数据库访问都在工具调用时新建会话（工具运行在请求线程/后台线程，不能借请求级会话）。
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

import arxiv
import requests
from sqlalchemy import or_

from harness.tools import Tool, ToolContext, register_tool
from harness import arxiv_page
from knowledge import arxiv_search as axs

from . import database
from .models import Folder, Paper, PaperDirection, PaperRelation

logger = logging.getLogger(__name__)

_TIMEOUT = 20.0
_UA = "ArxivReader/1.1 (+https://github.com/HypherX/ArxivReader)"
_OPENALEX_SEARCH = "https://api.openalex.org/works"


def _fmt_hit(hit: Dict[str, Any]) -> str:
    authors = ", ".join(hit.get("authors") or [])[:120]
    line = "- 《{}》".format(hit.get("title") or "?")
    if authors:
        line += " — {}".format(authors)
    meta = [part for part in (("arXiv:" + hit["arxiv_id"]) if hit.get("arxiv_id") else "",
                              (hit.get("published") or "")[:10]) if part]
    if meta:
        line += "（{}）".format(" · ".join(meta))
    abstract = re.sub(r"\s+", " ", (hit.get("abstract") or "")).strip()
    if abstract:
        line += "\n  " + abstract[:420] + ("…" if len(abstract) > 420 else "")
    return line


# ---------------------------------------------------------------- arXiv 检索

@register_tool("search_arxiv")
def _search_arxiv(ctx: ToolContext) -> Tool:
    def run(query: str, max_results: int = 8, sort: str = "relevance") -> str:
        terms = [t for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]+", query or "")][:8]
        if not terms:
            return ctx.text("查询里没有可用的英文检索词。", "No usable English search terms in the query.")
        limit = max(1, min(int(max_results or 8), 15))
        expression = " AND ".join("all:{}".format(t) for t in terms)
        sort_by = arxiv.SortCriterion.SubmittedDate if sort == "date" else arxiv.SortCriterion.Relevance
        try:
            results = arxiv.Client().results(
                arxiv.Search(query=expression, max_results=limit, sort_by=sort_by))
            hits = [axs._to_hit(item) for item in results]
        except Exception as exc:  # noqa: BLE001  限流/超时一律转 OpenAlex 兜底
            logger.info("search_arxiv 主源失败，转 OpenAlex：%s", str(exc)[:160])
            hits = _openalex_general(" ".join(terms), limit)
        if not hits:
            return ctx.text("没有找到相关论文。可以换一组更宽泛的检索词再试。",
                            "No matching papers. Try broader search terms.")
        return ctx.text("arXiv 检索命中 {} 篇：", "{} arXiv hits:").format(len(hits)) + "\n" + \
            "\n".join(_fmt_hit(h.as_dict() if hasattr(h, "as_dict") else h) for h in hits)

    return Tool(
        name="search_arxiv",
        description=ctx.text(
            "按关键词检索 arXiv 论文（标题/作者/摘要/日期）。找相关工作、最新进展、某方法的后续研究时用。"
            "检索词用英文空格分隔即可；sort=date 取最新，默认按相关度。",
            "Search arXiv papers by keywords (title/authors/abstract/date). Use for related work, "
            "recent progress, or follow-up studies. Provide English keywords; sort='date' for newest first, "
            "default is relevance."),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": ctx.text("英文检索词，空格分隔", "English keywords, space separated")},
                "max_results": {"type": "integer", "description": ctx.text("返回条数（1-15，默认 8）", "Hits to return (1-15, default 8)")},
                "sort": {"type": "string", "enum": ["relevance", "date"],
                         "description": ctx.text("relevance 相关度 / date 最新优先", "relevance or date (newest first)")},
            },
            "required": ["query"],
        },
        handler=run,
        needs_document=False,
    )


def _openalex_general(query: str, limit: int) -> List[Any]:
    """OpenAlex 全文检索兜底（arXiv API 故障时）；只保留有 arXiv 版本的命中。"""
    resp = requests.get(_OPENALEX_SEARCH, timeout=_TIMEOUT, headers={"User-Agent": _UA},
                        params={"search": query, "per-page": max(limit * 2, 10),
                                "select": axs._OPENALEX_FIELDS})
    if resp.status_code >= 400:
        raise ValueError("OpenAlex 暂不可用（HTTP {}）".format(resp.status_code))
    out = []
    for work in (resp.json() or {}).get("results") or []:
        hit = axs._oa_to_hit(work)
        if hit is not None:
            out.append(hit)
        if len(out) >= limit:
            break
    return out


@register_tool("get_arxiv_paper")
def _get_arxiv_paper(ctx: ToolContext) -> Tool:
    def run(arxiv_id: str = "", title: str = "") -> str:
        seed = {"arxiv_id": (arxiv_id or "").strip(), "title": (title or "").strip()}
        if not seed["arxiv_id"] and not seed["title"]:
            return ctx.text("需要给出 arXiv ID 或论文标题。", "Provide an arXiv ID or a paper title.")
        try:
            hits = axs.search_reference(seed, max_results=3)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("arXiv 查询失败：{}".format(str(exc)[:160]))
        if not hits:
            return ctx.text("没有查到这篇论文（ID 不存在或标题对不上）。",
                            "Paper not found (unknown ID or title mismatch).")
        return "\n".join(_fmt_hit(h.as_dict()) for h in hits)

    return Tool(
        name="get_arxiv_paper",
        description=ctx.text(
            "按 arXiv ID（如 2401.12345）或论文标题查证一篇论文的元信息与摘要。"
            "引用其他论文前先查证，不要凭记忆编造 ID。",
            "Look up one paper's metadata and abstract by arXiv ID (e.g. 2401.12345) or title. "
            "Always verify before citing another paper; never invent IDs from memory."),
        parameters={
            "type": "object",
            "properties": {
                "arxiv_id": {"type": "string", "description": ctx.text("arXiv ID（可带版本号）", "arXiv ID (version suffix allowed)")},
                "title": {"type": "string", "description": ctx.text("论文标题（没有 ID 时用）", "Paper title when no ID is known")},
            },
        },
        handler=run,
        needs_document=False,
    )


# ---------------------------------------------------------------- 本地文库

@register_tool("search_library")
def _search_library(ctx: ToolContext) -> Tool:
    def run(query: str, max_results: int = 10) -> str:
        text = (query or "").strip()
        if not text:
            return ctx.text("检索词不能为空。", "Empty query.")
        limit = max(1, min(int(max_results or 10), 20))
        like = "%{}%".format(text.replace("%", "").replace("_", ""))
        db = database.SessionLocal()
        try:
            rows = (db.query(Paper)
                    .filter(or_(Paper.title.like(like), Paper.abstract.like(like),
                                Paper.authors_json.like(like)))
                    .order_by(Paper.added_at.desc()).limit(limit).all())
            folders = {f.id: f.name for f in db.query(Folder).all()}
        finally:
            db.close()
        if not rows:
            return ctx.text("文库里没有匹配的论文。", "No matching papers in the library.")
        lines = []
        for p in rows:
            authors = ", ".join((json.loads(p.authors_json or "[]"))[:3])
            folder = folders.get(p.folder_id, "")
            lines.append("- [{}] 《{}》{}{}{}".format(
                p.id, p.title or p.arxiv_id,
                " — " + authors if authors else "",
                "（{}）".format(" · ".join(x for x in (p.arxiv_id, (p.published or "")[:10],
                                                        folder) if x)),
                "\n  " + re.sub(r"\s+", " ", p.abstract)[:240] + "…" if p.abstract else ""))
        return ctx.text("文库检索命中 {} 篇（[数字] 是库内论文 id）：",
                        "{} library hits ([number] is the library paper id):").format(len(rows)) + \
            "\n" + "\n".join(lines)

    return Tool(
        name="search_library",
        description=ctx.text(
            "检索用户本机文库里的论文（按标题/作者/摘要匹配）。用户问「我库里有没有关于…的论文」、"
            "想把当前论文和库内某篇对比时使用。",
            "Search the user's local library by title/author/abstract. Use when the user asks whether "
            "their library contains papers on a topic, or wants to compare with a library paper."),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": ctx.text("检索词（中英文均可）", "Search text (Chinese or English)")},
                "max_results": {"type": "integer", "description": ctx.text("返回条数（1-20，默认 10）", "Hits to return (1-20, default 10)")},
            },
            "required": ["query"],
        },
        handler=run,
        needs_document=False,
    )


@register_tool("related_papers")
def _related_papers(ctx: ToolContext) -> Tool:
    def run() -> str:
        paper_id = getattr(ctx.paper, "id", None)
        arxiv_id = getattr(ctx.paper, "arxiv_id", "") or ""
        db = database.SessionLocal()
        try:
            current = None
            if isinstance(paper_id, int):
                current = db.get(Paper, paper_id)
            if current is None and arxiv_id:
                current = db.query(Paper).filter(Paper.arxiv_id == arxiv_id).first()
            if current is None:
                return ctx.text("当前论文不在文库中，没有知识网络信息。",
                                "The current paper is not in the library; no knowledge-graph data.")
            lines = []
            memberships = (db.query(PaperDirection).filter(PaperDirection.paper_id == current.id).all())
            for m in memberships:
                node_name = m.node.name if m.node else ""
                lines.append(ctx.text("方向归属：{}", "Topic membership: {}").format(node_name))
            edges = (db.query(PaperRelation)
                     .filter(or_(PaperRelation.src_paper_id == current.id,
                                 PaperRelation.dst_paper_id == current.id)).all())
            id_title = {}
            for e in edges:
                for pid in (e.src_paper_id, e.dst_paper_id):
                    if pid != current.id and pid not in id_title:
                        other = db.get(Paper, pid)
                        id_title[pid] = (other.title if other else str(pid),)
            for e in edges:
                other_id = e.dst_paper_id if e.src_paper_id == current.id else e.src_paper_id
                other = id_title.get(other_id, (str(other_id),))[0]
                if e.src_paper_id == current.id:
                    line = ctx.text("- 本文 {} 《{}》", "- This paper {} 《{}》").format(e.relation, other)
                else:
                    line = ctx.text("- 《{}》 {} 本文", "- 《{}》 {} this paper").format(other, e.relation)
                if e.rationale:
                    line += "：" + e.rationale[:160]
                lines.append(line)
        finally:
            db.close()
        if not lines:
            return ctx.text("知识网络里还没有这篇论文的关联记录（可先生成深度精读或跑智能关联检索）。",
                            "No relations recorded yet (generate a deep reading or run related-work search first).")
        return "\n".join(lines[:25])

    return Tool(
        name="related_papers",
        description=ctx.text(
            "查看当前论文在知识网络里的关联：方向归属、与其他库内论文的关系边（引用/改进/对比等，含理由）。"
            "回答「这篇和哪些工作相关」「谁改进了谁」类问题时用。",
            "Show the current paper's knowledge-graph relations: topic membership and edges to other "
            "library papers (cites/extends/improves/contradicts, with rationale). Use for questions about "
            "how this paper relates to other work."),
        parameters={"type": "object", "properties": {}},
        handler=run,
        needs_document=False,
    )


# ---------------------------------------------------------------- 本文内定位

# 原始抽取文本的页码标记是 [Page N]；渲染进上下文的版本是 [p.N]，两种都认
_PAGE_MARK = re.compile(r"\[(?:p\.|Page\s+)(\d+)\]", re.IGNORECASE)


@register_tool("find_in_paper")
def _find_in_paper(ctx: ToolContext) -> Tool:
    def run(query: str, max_hits: int = 5) -> str:
        text = getattr(ctx.paper, "full_text", "") or ""
        needle = (query or "").strip()
        if not needle:
            return ctx.text("检索词不能为空。", "Empty query.")
        limit = max(1, min(int(max_hits or 5), 10))
        lowers = text.lower()
        terms = [needle.lower()] if needle.lower() in lowers else \
            [t.lower() for t in re.findall(r"[\w\-]+", needle) if len(t) >= 3][:4]
        if not terms:
            return ctx.text("全文里没有找到「{}」。", "“{}” does not appear in the full text.").format(needle)
        hits: List[str] = []
        seen_pages = set()
        for term in terms:
            start = 0
            while len(hits) < limit:
                at = lowers.find(term, start)
                if at < 0:
                    break
                start = at + len(term)
                page = None
                for mark in _PAGE_MARK.finditer(lowers[max(0, at - 200000):at + 1]):
                    page = mark.group(1)
                snippet = re.sub(r"\s+", " ", text[max(0, at - 160):at + len(term) + 160]).strip()
                key = (page, snippet[:40])
                if key in seen_pages:
                    continue
                seen_pages.add(key)
                hits.append(ctx.text("- [p.{}] …{}…", "- [p.{}] …{}…").format(page or "?", snippet))
            if len(hits) >= limit:
                break
        if not hits:
            return ctx.text("全文里没有找到「{}」。", "“{}” does not appear in the full text.").format(needle)
        return ctx.text("在全文命中 {} 处：", "{} hits in the full text:").format(len(hits)) + \
            "\n" + "\n".join(hits)

    return Tool(
        name="find_in_paper",
        description=ctx.text(
            "在当前论文全文里检索关键词，返回带 [p.N] 页码的原文片段。需要精确核对某个术语、数字、"
            "公式或引用在原文中的位置时用。",
            "Search the current paper's full text for a keyword and return snippets with [p.N] page "
            "markers. Use to verify exactly where a term, number, formula or citation appears."),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": ctx.text("要定位的词或短语", "Word or phrase to locate")},
                "max_hits": {"type": "integer", "description": ctx.text("返回片段数（1-10，默认 5）", "Snippets to return (1-10, default 5)")},
            },
            "required": ["query"],
        },
        handler=run,
        needs_document=True,
    )
