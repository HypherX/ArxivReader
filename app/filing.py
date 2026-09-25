"""自动归档规则引擎。

策略：按 priority 升序遍历所有启用的规则，首条命中即返回其 folder_id（first-match）；
全部未命中返回 None，由调用方落入默认 Inbox 文件夹。

本模块不依赖 ORM/Pydantic：规则只要是带 match_type/pattern/folder_id/priority/enabled
属性的对象即可（数据库的 FilingRule 与测试用的 RuleSpec 都满足），便于单元测试。

match_type 语义：
  category  规则 pattern 命中论文任一 arXiv 分类；支持精确(cs.CL)或前缀(cs -> cs.*)
  keyword   pattern（不区分大小写）出现在 标题+摘要 中
  author    pattern 是任一作者的子串（不区分大小写）
  title     pattern 是标题的子串（不区分大小写）
"""

from dataclasses import dataclass, field
from typing import List, Optional, Sequence


@dataclass
class FilingTarget:
    """待归档论文的匹配视图。"""

    title: str = ""
    abstract: str = ""
    authors: List[str] = field(default_factory=list)
    categories: List[str] = field(default_factory=list)


@dataclass
class RuleSpec:
    """规则的最小结构（测试与非 ORM 场景用）。"""

    match_type: str
    pattern: str
    folder_id: Optional[int] = None
    priority: int = 100
    enabled: bool = True


def target_from_meta(meta) -> FilingTarget:
    """从 arxiv_service.PaperMeta 构造匹配视图。"""
    return FilingTarget(
        title=getattr(meta, "title", "") or "",
        abstract=getattr(meta, "abstract", "") or "",
        authors=list(getattr(meta, "authors", []) or []),
        categories=list(getattr(meta, "categories", []) or []),
    )


def _match_category(pattern: str, categories: Sequence[str]) -> bool:
    p = pattern.lower()
    for c in categories:
        cl = (c or "").lower()
        if cl == p or cl.startswith(p + "."):
            return True
    return False


def _matches(rule, target: FilingTarget) -> bool:
    pattern = (getattr(rule, "pattern", "") or "").strip()
    if not pattern:
        return False
    match_type = getattr(rule, "match_type", "")
    hay_title = (target.title or "").lower()
    hay_abstract = (target.abstract or "").lower()

    if match_type == "category":
        return _match_category(pattern, target.categories)
    if match_type == "keyword":
        p = pattern.lower()
        return p in hay_title or p in hay_abstract
    if match_type == "author":
        p = pattern.lower()
        return any(p in (a or "").lower() for a in target.authors)
    if match_type == "title":
        return pattern.lower() in hay_title
    return False


def apply_rules(target: FilingTarget, rules: Sequence) -> Optional[int]:
    """返回首个命中规则的目标 folder_id；无命中返回 None。"""
    enabled = [r for r in rules if getattr(r, "enabled", True)]
    ordered = sorted(enabled, key=lambda r: getattr(r, "priority", 100))
    for rule in ordered:
        if _matches(rule, target):
            return getattr(rule, "folder_id", None)
    return None
