"""节点图谱 Markdown 渲染：每个方向节点一份「论文图谱」文档。

为什么用 Markdown：方向树用文件夹体现，而论文图谱用**每个节点文件夹里的一份 .md** 体现——
既能直接阅读（人类可读的论文清单 + 关系明细 + 阶段综述），又能被任何 Markdown 工具渲染；
其中关系部分额外输出一段 Mermaid 图，在 GitHub / VS Code / Typora 里直接可视化，
而同一份 nodes/edges JSON 也可喂给 D3 / ECharts（前后端共用同一数据源）。

对外：
  render_graph_md(db, node) -> str     渲染（不落盘，便于接口直接返回与单测）
"""

import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.models import DirectionNode, Paper, PaperDirection, PaperRelation

GRAPH_MD_FILENAME = "GRAPH.md"
_TITLE_LIMIT = 28


def _short(text: str, limit: int) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _mermaid_label(text: str) -> str:
    """Mermaid 标签里不能出现未转义的引号/方括号。"""
    return _short(text, _TITLE_LIMIT).replace('"', "'").replace("[", "(").replace("]", ")")


def _node_id(paper_id: int) -> str:
    return "P{}".format(paper_id)


def render_mermaid(papers: List[Paper], edges: List[PaperRelation]) -> str:
    lines = ["```mermaid", "graph LR"]
    for paper in papers:
        lines.append('  {}["{}"]'.format(_node_id(paper.id), _mermaid_label(paper.title or paper.arxiv_id)))
    for edge in edges:
        arrow = "<-->" if edge.direction == "<->" else "-->"
        lines.append('  {} {}|"{} {}"| {}'.format(
            _node_id(edge.src_paper_id), arrow, edge.relation, round(edge.strength, 2),
            _node_id(edge.dst_paper_id)))
    lines.append("```")
    return "\n".join(lines)


def render_graph_md(db: Session, node: DirectionNode) -> str:
    """渲染节点图谱文档（不落盘）。数据来源：归属表 + 关系边表 + 节点上的最新综述。"""
    directions = (db.query(PaperDirection).filter(PaperDirection.node_id == node.id)
                  .order_by(PaperDirection.id).all())
    papers: List[Paper] = [d.paper for d in directions if d.paper is not None]
    paper_ids = {p.id for p in papers}
    edges = [e for e in (db.query(PaperRelation).filter(PaperRelation.node_id == node.id)
                         .order_by(PaperRelation.id).all())
             if e.src_paper_id in paper_ids and e.dst_paper_id in paper_ids]
    by_id = {p.id: p for p in papers}
    children = (db.query(DirectionNode).filter(DirectionNode.parent_id == node.id)
                .order_by(DirectionNode.name).all())
    subtree = node.paper_count + sum(c.paper_count for c in children)

    updated = node.graph_md_at or node.updated_at or datetime.datetime.utcnow()
    out: List[str] = []
    out.append("# 论文图谱 · {}".format(node.path))
    out.append("")
    if node.description:
        out.append("> {}".format(node.description))
    out.append("> 论文 {} 篇（子树 {}）｜关系边 {} 条｜最后更新 {}｜节点类型：{}".format(
        node.paper_count, subtree, len(edges),
        updated.strftime("%Y-%m-%d %H:%M"),
        "叶子节点" if node.is_leaf else "含子方向（{}）".format(len(children))))
    out.append("")

    # 1. 概览
    out.append("## 1. 概览")
    if papers:
        latest = max(papers, key=lambda p: p.added_at or datetime.datetime.min)
        out.append("- 最新入库：{}（{}）".format(_short(latest.title or latest.arxiv_id, 60),
                                          (latest.published or "")[:10] or "日期未知"))
    else:
        out.append("- 该节点下暂无论文（可在检索页把匹配论文「加入详细阅读」后自动挂载）")
    if node.synthesis_md:
        out.append("- 已有阶段综述：{} 篇时生成于 {}（见第 5 节）".format(
            node.synthesis_paper_count,
            (node.synthesis_at or node.updated_at or datetime.datetime.utcnow()).strftime("%Y-%m-%d %H:%M")))
    else:
        threshold_hint = "论文数每增加若干篇会自动生成" if papers else "暂无"
        out.append("- 阶段综述：尚未生成（{}）".format(threshold_hint))
    if children:
        out.append("- 子方向：{}".format("、".join(c.name for c in children)))
    out.append("")

    # 2. Mermaid 图
    out.append("## 2. 论文关系图（Mermaid，可直接渲染）")
    if edges:
        out.append("")
        out.append(render_mermaid(papers, edges))
    else:
        out.append("")
        out.append("```mermaid")
        out.append("graph LR")
        for paper in papers:
            out.append('  {}["{}"]'.format(_node_id(paper.id),
                                           _mermaid_label(paper.title or paper.arxiv_id)))
        out.append("```")
        out.append("")
        out.append("> 暂未提炼出关系边：同节点下需要至少两篇完成深读的论文，"
                   "关系由 `paper_relation` 步骤在深读之后自动抽取。")
    out.append("")

    # 3. 论文清单
    out.append("## 3. 论文清单")
    if papers:
        out.append("")
        out.append("| # | 论文 | arXiv | 发表日期 | 归属 | 加入时间 |")
        out.append("|---|---|---|---|---|---|")
        for paper in papers:
            role = next((d.role for d in directions if d.paper_id == paper.id), "secondary")
            out.append("| {} | {} | {} | {} | {} | {} |".format(
                paper.id, _short(paper.title or "", 70), paper.arxiv_id,
                (paper.published or "")[:10] or "-",
                "primary" if role == "primary" else "secondary",
                (paper.added_at or datetime.datetime.utcnow()).strftime("%Y-%m-%d")))
    else:
        out.append("（空）")
    out.append("")

    # 4. 关系明细
    out.append("## 4. 关系明细（人类可读）")
    if edges:
        out.append("")
        out.append("| 源 | 关系 | 目标 | 强度 | 理由 | 证据 |")
        out.append("|---|---|---|---|---|---|")
        for edge in edges:
            out.append("| {} | {} | {} | {} | {} | {} |".format(
                _short((by_id.get(edge.src_paper_id).title if by_id.get(edge.src_paper_id) else str(edge.src_paper_id)), 34),
                edge.relation,
                _short((by_id.get(edge.dst_paper_id).title if by_id.get(edge.dst_paper_id) else str(edge.dst_paper_id)), 34),
                round(edge.strength, 2),
                _short(edge.rationale, 120) or "-",
                _short(edge.evidence, 60) or "-"))
    else:
        out.append("（暂无：见第 2 节说明）")
    out.append("")

    # 5. 阶段综述
    out.append("## 5. 阶段综述（最新一版）")
    out.append("")
    if node.synthesis_md:
        out.append(node.synthesis_md.strip())
    else:
        out.append("（尚未生成。触发条件：该节点论文数每增加 N 篇，N 见 `config.py` 的 "
                   "`KNOWLEDGE.synthesis_every`）")
    out.append("")
    return "\n".join(out)
