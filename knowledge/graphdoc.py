"""节点图谱 Markdown 渲染：每个方向节点一份「论文图谱」文档。

为什么用 Markdown：方向树用文件夹体现，而论文图谱用**每个节点文件夹里的一份 .md** 体现——
既能直接阅读（人类可读的论文清单 + 关系明细 + 阶段综述），又能被任何 Markdown 工具渲染；
其中关系部分额外输出一段 Mermaid 图，在 GitHub / VS Code / Typora 里直接可视化，
而同一份 nodes/edges JSON 也可喂给 D3 / ECharts（前后端共用同一数据源）。

对外：
  render_graph_md(db, node) -> str     渲染（不落盘，便于接口直接返回与单测）
"""

import datetime
import re
from typing import List, Optional

from sqlalchemy.orm import Session

from app import settings_store
from app.models import DirectionNode, Paper, PaperDirection, PaperRelation
from harness.language import choose

GRAPH_MD_FILENAME = "GRAPH.md"
_TITLE_LIMIT = 28
_RELATIONS = {
    "cites": ("引用", "Cites"), "extends": ("扩展", "Extends"),
    "improves": ("改进", "Improves"), "contradicts": ("结论冲突", "Contradicts"),
    "complements": ("互补", "Complements"), "applies": ("应用", "Applies"),
    "baseline_of": ("对比基线", "Baseline comparison"), "uses": ("使用", "Uses"),
}


def _escape(text: str) -> str:
    return re.sub(r"([\\`*_{}\[\]<>|])", r"\\\1", str(text or "")).replace("\n", " ")


def _relation_label(relation: str, language: str) -> str:
    return choose({"output_language": language}, *_RELATIONS.get(relation, ("关系待核实", "Unverified relationship")))


def _short(text: str, limit: int) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _mermaid_label(text: str) -> str:
    """Mermaid 标签里不能出现未转义的引号/方括号。"""
    return _short(text, _TITLE_LIMIT).replace('"', "'").replace("[", "(").replace("]", ")")


def _node_id(paper_id: int) -> str:
    return "P{}".format(paper_id)


def render_mermaid(papers: List[Paper], edges: List[PaperRelation], language: str = "zh") -> str:
    lines = ["```mermaid", "graph LR"]
    for paper in papers:
        lines.append('  {}["{}"]'.format(_node_id(paper.id), _mermaid_label(paper.title or paper.arxiv_id)))
    for edge in edges:
        arrow = "<-->" if edge.direction == "<->" else "-->"
        lines.append('  {} {}|"{} {}"| {}'.format(
            _node_id(edge.src_paper_id), arrow, _relation_label(edge.relation, language), round(edge.strength, 2),
            _node_id(edge.dst_paper_id)))
    lines.append("```")
    return "\n".join(lines)


def render_graph_md(db: Session, node: DirectionNode, language: Optional[str] = None) -> str:
    """渲染唯一分类下的可读关系；保留历史原文，不隐式调用翻译模型。"""
    language = language or settings_store.get_output_language(db)
    def text(zh: str, en: str) -> str:
        return choose({"output_language": language}, zh, en)

    directions = (db.query(PaperDirection).filter(PaperDirection.node_id == node.id)
                  .order_by(PaperDirection.id).all())
    papers = [d.paper for d in directions if d.paper is not None]
    by_id = {p.id: p for p in papers}
    edges = [e for e in db.query(PaperRelation).filter_by(node_id=node.id).order_by(PaperRelation.id)
             if e.src_paper_id in by_id and e.dst_paper_id in by_id]
    nodes = db.query(DirectionNode).all()
    children = sorted((n for n in nodes if n.parent_id == node.id), key=lambda n: n.name)
    descendants, pending = {node.id}, [node.id]
    while pending:
        parent = pending.pop()
        for item in nodes:
            if item.parent_id == parent and item.id not in descendants:
                descendants.add(item.id)
                pending.append(item.id)
    subtree = db.query(PaperDirection.paper_id).filter(PaperDirection.node_id.in_(descendants)).distinct().count()
    updated = node.graph_md_at or node.updated_at or datetime.datetime.utcnow()
    out = ["# " + text("论文图谱", "Paper relationships") + " · " + _escape(node.path), ""]
    if node.description:
        out.extend(["> " + text("目录原始说明（未自动翻译）", "Original folder description (not automatically translated)"),
                    "", node.description, ""])
    out.extend([text("> 当前层 {} 篇（子树 {}）｜关系 {} 条｜最后更新 {}",
                     "> Direct papers: {} (subtree: {}) | Relationships: {} | Updated: {}").format(
                         len(papers), subtree, len(edges), updated.strftime("%Y-%m-%d %H:%M")), "",
                "## 1. " + text("概览", "Overview"),
                text("- 本文清单与关系仅涵盖当前层；祖先目录不是额外分类。",
                     "- This list and its relationships cover direct papers only; ancestor folders are not additional classifications."),
                text("- 新综述请从文件夹菜单手动生成，默认包含全部后代论文。",
                     "- Generate a review manually from the folder menu; it includes all descendant papers by default.")])
    if children:
        out.append(text("- 子方向：", "- Subfolders: ") + ", ".join(_escape(c.name) for c in children))
    out.extend(["", "## 2. " + text("论文清单", "Paper list"), ""])
    if papers:
        out.extend([text("| # | 论文 | arXiv | 发表日期 | 加入时间 |", "| # | Paper | arXiv | Published | Added |"),
                    "|---|---|---|---|---|"])
        for paper in papers:
            out.append("| {} | [{}](/?paper={}) | {} | {} | {} |".format(
                paper.id, _escape(paper.title or paper.arxiv_id), paper.id, _escape(paper.arxiv_id),
                _escape((paper.published or "")[:10]) or "-",
                paper.added_at.strftime("%Y-%m-%d") if paper.added_at else "-"))
    else:
        out.append(text("当前层暂无论文；可选择子目录查看。", "No direct papers; select a subfolder to see its papers."))
    out.extend(["", "## 3. " + text("关系明细", "Relationship details"), ""])
    for edge in edges:
        src, dst = by_id[edge.src_paper_id], by_id[edge.dst_paper_id]
        out.extend(["### [{}](/?paper={}) {} [{}](/?paper={})".format(
            _escape(src.title or src.arxiv_id), src.id, "↔" if edge.direction == "<->" else "→",
            _escape(dst.title or dst.arxiv_id), dst.id), "",
            text("关系：{} · 强度：{}", "Relationship: {} · Strength: {}").format(
                _relation_label(edge.relation, language), round(edge.strength, 2)), ""])
        saved_language = edge.output_language if edge.output_language in ("zh", "en") else "unknown"
        labels = {"zh": text("中文", "Chinese"), "en": text("英文", "English"), "unknown": text("未知", "Unknown")}
        out.extend(["> " + text("原说明语言：{}；原文保留，可在关系面板按需翻译。",
                                  "Original language: {}; source preserved, translate on demand in the relationship panel.").format(labels[saved_language]),
                    "", text("**理由**", "**Rationale**"), "", edge.rationale or "-", "",
                    text("**证据**", "**Evidence**"), "", edge.evidence or "-", ""])
    if not edges:
        out.append(text("暂无证据支持的关系；需要同目录其他论文及足够材料。",
                        "No evidence-supported relationships yet; comparison papers and sufficient material are required."))
    out.extend(["", "## 4. " + text("历史阶段综述", "Historical review"), ""])
    if node.synthesis_md:
        out.extend(["> " + text("历史原文，语言与材料覆盖未知；不会自动更新。",
                                 "Historical original; language and material coverage are unknown. It is not updated automatically."),
                    "", node.synthesis_md.strip()])
    else:
        out.append(text("无历史阶段综述。新报告从文件夹菜单手动生成。",
                        "No historical review. Generate new reports manually from the folder menu."))
    out.extend(["", "## 5. " + text("辅助图源码", "Optional diagram source"), "",
                text("下列 Mermaid 源码供兼容编辑器使用；本阅读页显示源码，关系说明见上文。",
                     "Mermaid source for compatible editors; this reader shows source code. Read relationship details above."),
                "", render_mermaid(papers, edges, language), ""])
    return "\n".join(out)
