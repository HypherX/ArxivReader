"""论文输入适配层：把 Web 层对象（ORM 对象或普通 dict）转成 harness 内部结构。

harness 不依赖 SQLAlchemy：只按鸭子类型读取字段，因此可离线单测、可被 CLI 复用。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from . import tokens
from .document import PaperDocument

_MAX_AUTHORS_IN_META = 8


def _pick(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


@dataclass
class PaperInput:
    """一篇论文的最小输入集合（与存储层解耦）。"""

    arxiv_id: str = ""
    title: str = ""
    authors: List[str] = field(default_factory=list)
    abstract: str = ""
    categories: List[str] = field(default_factory=list)
    published: Optional[str] = None
    full_text: str = ""
    text_truncated: bool = False

    _doc: Optional[PaperDocument] = field(default=None, repr=False, compare=False)

    @classmethod
    def from_any(cls, obj: Any) -> "PaperInput":
        return cls(
            arxiv_id=str(_pick(obj, "arxiv_id", "") or ""),
            title=str(_pick(obj, "title", "") or ""),
            authors=_as_list(_pick(obj, "authors")),
            abstract=str(_pick(obj, "abstract", "") or ""),
            categories=_as_list(_pick(obj, "categories")),
            published=_pick(obj, "published"),
            full_text=str(_pick(obj, "full_text", "") or ""),
            text_truncated=bool(_pick(obj, "text_truncated", False)),
        )

    @property
    def has_body(self) -> bool:
        return bool(self.full_text.strip())

    def document(self) -> PaperDocument:
        """结构化正文视图（按需构造并缓存）。"""
        if self._doc is None:
            self._doc = PaperDocument(self.full_text)
        return self._doc

    def meta_block(self) -> str:
        """紧凑元信息块（标题/作者/出处/分类/摘要），不含正文。"""
        authors: Sequence[str] = self.authors
        if len(authors) > _MAX_AUTHORS_IN_META:
            author_text = "{}, 等 {} 人".format(", ".join(authors[:_MAX_AUTHORS_IN_META]), len(authors))
        else:
            author_text = ", ".join(authors) if authors else "未知"
        line2 = " · ".join(part for part in (
            (self.published or "")[:10],
            "arXiv:{}".format(self.arxiv_id) if self.arxiv_id else "",
            "分类: {}".format(", ".join(self.categories)) if self.categories else "",
        ) if part)
        lines = [
            "## 论文元信息",
            "- 标题: {}".format(self.title or "(未取到标题)"),
            "- 作者: {}".format(author_text),
        ]
        if line2:
            lines.append("- 出处: {}".format(line2))
        lines.append("- 摘要: {}".format((self.abstract or "(未取到摘要)").strip()))
        if not self.has_body:
            lines.append("- 正文: 未抽取到正文（可能是扫描版/图片型 PDF），只能依据标题与摘要作答。")
        elif self.text_truncated:
            lines.append("- 正文: 抽取时已按上限截断，缺少论文末尾部分。")
        return "\n".join(lines)
