"""论文正文的结构化视图（PaperDocument）：把 PDF 抽取的纯文本变成"可检索、可预算"的对象。

输入是 pdf_service 抽出的文本（页间以 `[Page N]` 标记），输出三种消费方式，全部零 LLM：

  outline()      章节清单（规范名 / 页范围 / 规模），供模型决定读哪一节
  section_text() 按节取原文，供工具调用
  search()       关键词检索片段（带页码），供工具调用
  render()       按 token 预算渲染成模型上下文

压缩手段（全部确定性、不丢正文信息）：
  1. 行级噪声清除：arXiv 侧栏/水印、期刊页脚、"Preprint/Under review"、孤立页码
  2. 跨页重复行清除：在两页以上重复出现的短行（running head / 页脚）判为版式噪声
  3. 断词修复：`trans-\nformer` -> `transformer`
  4. 空白归并：段内换行归一为空格、连续空格压缩（不删任何词）
  5. 参考文献/致谢：默认整节丢弃（引用列表对精读无信息增量）
  6. 预算裁剪：超预算时按信息密度优先级取节，超出部分按段截断并显式声明"已省略"，
     模型可用工具把省略内容取回（token 只花在真正要读的地方）
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import tokens

# ---------------------------------------------------------------- 结构
@dataclass
class Block:
    """一段正文（或一行标题），带页码锚点。"""

    page: int
    text: str
    kind: str = "para"      # para | heading
    key: str = ""           # heading 的规范名（识别不出则空）

    @property
    def tokens(self) -> int:
        return tokens.estimate_tokens(self.text)


@dataclass
class Section:
    """一个规范章节（同名的多个片段按出现顺序合并）。"""

    key: str
    title: str
    blocks: List[Block] = field(default_factory=list)

    def merge(self, other: "Section") -> None:
        self.blocks.extend(other.blocks)
        if not self.title:
            self.title = other.title

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.blocks)

    @property
    def tokens(self) -> int:
        return sum(b.tokens for b in self.blocks)

    @property
    def chars(self) -> int:
        return sum(len(b.text) for b in self.blocks)

    @property
    def pages(self) -> Tuple[int, int]:
        if not self.blocks:
            return (0, 0)
        return (self.blocks[0].page, self.blocks[-1].page)


@dataclass
class RenderedContext:
    """render() 的产物：可直接拼进 messages 的正文 + 本次预算的账。"""

    text: str
    tokens: int
    sections: List[str] = field(default_factory=list)        # 已纳入的章节
    omitted: List[Tuple[str, int]] = field(default_factory=list)  # [(章节, 省略 token)]
    truncated: bool = False                                  # 是否有节被按段截断
    note: str = ""                                           # 给模型看的省略说明


# ---------------------------------------------------------------- 文本清洗
_PAGE_RE = re.compile(r"^\s*\[Page\s+(\d+)\]\s*$", re.IGNORECASE)
_HYPHEN_BREAK_RE = re.compile(r"([A-Za-z])-\s*\n\s*([a-z])")
_SPACE_RE = re.compile(r"[ \t\u00a0\u3000]{2,}")
_NOISE_RES = (
    re.compile(r"^\s*arXiv:\s*\S+", re.IGNORECASE),
    re.compile(r"^\s*(?:preprint|under review|under submission|draft)\b", re.IGNORECASE),
    re.compile(r"^\s*(?:https?://)?(?:www\.)?arxiv\.org\S*$", re.IGNORECASE),
    re.compile(r"^\s*page\s+\d+(?:\s+of\s+\d+)?\s*$", re.IGNORECASE),
    re.compile(r"^\s*\d{1,4}\s*$"),
    re.compile(r"^\s*[•·|]\s*$"),
)

# 章节关键词 -> 规范名（顺序敏感：先匹配到者胜）
_KEYWORD_MAP: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("abstract", ("abstract", "摘要")),
    ("introduction", ("introduction", "intro")),
    ("related_work", ("related work", "related works", "prior work", "literature review")),
    ("preliminaries", ("preliminaries", "background", "notation", "problem setting",
                       "problem formulation", "problem definition", "preliminary")),
    ("method", ("method", "methods", "methodology", "approach", "approaches", "our method",
                "our approach", "proposed method", "proposed approach", "model", "models",
                "framework", "architecture", "system overview", "training objective")),
    ("experiments", ("experiment", "experiments", "experimental setup", "experimental settings",
                     "experimental results", "evaluation setup", "evaluation", "implementation details",
                     "training details", "setup", "datasets")),
    ("results", ("results", "main results", "quantitative results")),
    ("analysis", ("analysis", "ablation", "ablation study", "ablations", "case study",
                  "analysis and discussion", "discussion", "why")),
    ("conclusion", ("conclusion", "conclusions", "concluding remarks")),
    ("limitations", ("limitation", "limitations")),
    ("future_work", ("future work", "future directions")),
    ("references", ("references", "bibliography")),
    ("appendix", ("appendix", "appendices", "supplementary", "supplemental")),
    ("acknowledgements", ("acknowledg", "acknowledgement", "acknowledgment")),
)

_HEADING_NUMBER_RE = re.compile(r"^(?:\d+(?:\.\d+)*|[IVXLC]+)[.)]?\s+", re.IGNORECASE)
# 小节标题：编号不超过两位（避免把 "2017 was the year..." 这类年份开头的句子当标题）
_SUBSECTION_RE = re.compile(r"^\d{1,2}(?:\.\d{1,2}){0,3}[.)]?\s+\S")
# 强标题关键词：即使上一行不是句末（如引用列表结尾），也直接当标题
_STRONG_KEYS = ("abstract", "references", "appendix", "acknowledgements")

# 渲染优先级：预算不足时按此顺序保留（信息密度高的在前）
_KEEP_ORDER = (
    "abstract", "front", "introduction", "method", "experiments", "results", "analysis",
    "discussion", "conclusion", "limitations", "other", "related_work", "preliminaries",
    "future_work", "appendix", "acknowledgements", "references",
)
# 默认丢弃（可用 render(drop=...) 覆盖）
_DROP_DEFAULT = ("references", "acknowledgements", "future_work")
# 需要限额保底的"全局性"章节（超预算时才启用）
_RESERVE_RATIO = {"abstract": 0.06, "introduction": 0.25, "conclusion": 0.10, "limitations": 0.05}


def _norm_line(line: str) -> str:
    return _SPACE_RE.sub(" ", line.strip()).lower()


def _is_noise(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    return any(rx.match(stripped) for rx in _NOISE_RES)


def _dehyphenate(text: str) -> str:
    prev = None
    while prev != text:
        prev = text
        text = _HYPHEN_BREAK_RE.sub(r"\1\2", text)
    return text


def _clean_paragraph(raw: str) -> str:
    text = _dehyphenate(raw)
    text = re.sub(r"\s*\n\s*", " ", text)          # 段内换行 -> 空格
    text = _SPACE_RE.sub(" ", text)
    return text.strip()


def _prev_ends_sentence(prev_line: str) -> bool:
    """上一行是否结束了一个句子：只有这种情况下才允许在段中出现标题。"""
    text = (prev_line or "").strip()
    return (not text) or text[-1] in ".!?:;】》"


def _strip_numbering(text: str) -> str:
    return _HEADING_NUMBER_RE.sub("", text.strip()).strip()


def classify_heading(text: str) -> Tuple[str, bool]:
    """判定一行是否为标题，返回 (规范名, 是否标题)。

    标题特征：短（≤ 90 字符）、不以句号结尾（带编号的小节标题除外）、
    去掉章节编号后以已知关键词开头，或形如 "3.2 Tokenizer" 的编号小标题。
    """
    stripped = _SPACE_RE.sub(" ", text.strip())
    if not stripped or len(stripped) > 90:
        return "", False
    if stripped.endswith(".") and not _SUBSECTION_RE.match(stripped):
        return "", False
    core = _strip_numbering(stripped)
    if not core:
        return "", False
    probe = core.strip(" :：-–—").lower()
    if len(probe.split()) > 8:
        return "", False
    for key, keywords in _KEYWORD_MAP:
        for kw in keywords:
            if probe == kw or probe.startswith(kw + " ") or probe.startswith(kw + ":") \
                    or probe.startswith(kw + " -") or probe.startswith(kw + "\u2014"):
                return key, True
    # 纯编号小标题（如 "3.2 Tokenizer"）也算结构信息，但规范名未知：
    # 要求不含逗号（排除句子），且词数不多（排除正文行）。
    if _SUBSECTION_RE.match(stripped) and len(stripped) <= 60 \
            and "," not in stripped and len(stripped.split()) <= 8:
        return "", True
    return "", False


def is_strong_heading(text: str) -> bool:
    key, is_heading = classify_heading(text)
    return is_heading and key in _STRONG_KEYS


# ---------------------------------------------------------------- 文档
class PaperDocument:
    """论文正文的结构化视图。构造后即可反复检索/渲染，无副作用。"""

    def __init__(self, text: str):
        self.raw_text = text or ""
        self.raw_chars = len(self.raw_text)
        self.dropped_lines: List[str] = []
        self.blocks: List[Block] = []
        self.sections: Dict[str, Section] = {}
        self._order: List[str] = []
        self._front_blocks: List[Block] = []
        self._build()

    # ---------------- 构造 ----------------
    def _build(self) -> None:
        pages = self._split_pages(self.raw_text)
        cleaned_pages = self._strip_noise(pages)
        current: Optional[Section] = None
        for page_no, lines in cleaned_pages:
            for kind, key, raw in self._to_blocks(lines):
                text = _clean_paragraph(raw)
                if not text:
                    continue
                block = Block(page=page_no, text=text, kind=kind, key=key)
                self.blocks.append(block)
                if kind == "heading" and key:
                    current = self._open_section(key, text, block)
                elif current is None:
                    # 第一个规范章节标题之前的内容（题目/作者/脚注）
                    self._front_blocks.append(block)
                else:
                    # 无规范名的小标题（如 "3.1 Encoder Stacks"）不另开章节，只作结构标记
                    current.blocks.append(block)

    @staticmethod
    def _split_pages(text: str) -> List[Tuple[int, List[str]]]:
        pages: List[Tuple[int, List[str]]] = []
        current_no, current_lines = 1, []
        found_marker = False
        for line in (text or "").splitlines():
            m = _PAGE_RE.match(line)
            if m:
                found_marker = True
                if current_lines or pages:
                    pages.append((current_no, current_lines))
                current_no, current_lines = int(m.group(1)), []
                continue
            current_lines.append(line)
        pages.append((current_no, current_lines))
        if not found_marker:
            return [(1, (text or "").splitlines())]
        return [(no, lines) for no, lines in pages if any(l.strip() for l in lines)] or [(1, [])]

    def _strip_noise(self, pages: List[Tuple[int, List[str]]]) -> List[Tuple[int, List[str]]]:
        """行级噪声 + 跨页重复行清除；被删的行会记录在 dropped_lines 里备查。"""
        stage1: List[Tuple[int, List[str]]] = []
        for page_no, lines in pages:
            kept = []
            for line in lines:
                if _is_noise(line):
                    self.dropped_lines.append(line.strip())
                else:
                    kept.append(line)
            stage1.append((page_no, kept))

        if len(stage1) >= 3:
            seen: Dict[str, int] = {}
            for _, lines in stage1:
                for key in {_norm_line(l) for l in lines if l.strip()}:
                    if 6 <= len(key) <= 100:
                        seen[key] = seen.get(key, 0) + 1
            threshold = max(3, int(len(stage1) * 0.5))
            repeated = {k for k, n in seen.items() if n >= threshold}
        else:
            repeated = set()

        cleaned: List[Tuple[int, List[str]]] = []
        for page_no, lines in stage1:
            kept = []
            for line in lines:
                if line.strip() and _norm_line(line) in repeated:
                    self.dropped_lines.append(line.strip())
                else:
                    kept.append(line)
            cleaned.append((page_no, kept))
        return cleaned

    @staticmethod
    def _to_blocks(lines: Sequence[str]) -> List[Tuple[str, str, str]]:
        """把页内行切成 block：标题行单独成块，其余按空行合并成段。

        标题判定不只依赖空行（PDF 抽取常缺空行），而是“逐行判定 + 上下文约束”：
          - 上一行以句末标点结束（或处于页首）才允许把当前行当标题，避免句中误切；
          - 带编号的小节标题（"3.1 xxx"）不受该约束；
          - 强关键词标题（References/Appendix 等）不受该约束（前面常是引用或公式行）。
        """
        out: List[Tuple[str, str, str]] = []
        buffer: List[str] = []
        prev = ""

        def flush() -> None:
            if buffer:
                out.append(("para", "", "\n".join(buffer)))
                del buffer[:]

        for line in list(lines) + [""]:
            stripped = line.strip()
            if not stripped:
                flush()
                prev = ""
                continue
            if _prev_ends_sentence(prev) or _SUBSECTION_RE.match(stripped) or is_strong_heading(stripped):
                key, is_heading = classify_heading(stripped)
                if is_heading:
                    flush()
                    out.append(("heading", key, stripped))
                    prev = stripped
                    continue
            buffer.append(line)
            prev = line
        flush()
        return out

    def _open_section(self, key: str, title: str, block: Block) -> Section:
        name = key or "other"
        section = self.sections.get(name)
        if section is None:
            section = Section(key=name, title=title.strip())
            self.sections[name] = section
            self._order.append(name)
        elif title and title.strip() not in section.title:
            section.title = (section.title + " / " + title.strip()).strip(" /")
        section.blocks.append(block)
        return section

    # ---------------- 统计 / 大纲 ----------------
    def _merged_sections(self) -> Dict[str, Section]:
        """按规范名合并（同名片段按出现顺序拼接），用于渲染。"""
        merged: Dict[str, Section] = {}
        if self._front_blocks:
            merged["front"] = Section(key="front", title="(前置信息)", blocks=list(self._front_blocks))
        for name in self._order:
            section = self.sections[name]
            if name in merged:
                merged[name].merge(section)
            else:
                merged[name] = Section(key=name, title=section.title, blocks=list(section.blocks))
        return merged

    @property
    def clean_chars(self) -> int:
        return sum(len(b.text) for b in self.blocks)

    def stats(self) -> Dict[str, float]:
        raw_tokens = tokens.estimate_tokens(self.raw_text)
        clean_tokens = sum(b.tokens for b in self.blocks)
        return {
            "raw_chars": self.raw_chars,
            "clean_chars": self.clean_chars,
            "raw_tokens_est": raw_tokens,
            "clean_tokens_est": clean_tokens,
            "compression": round(1 - (self.clean_chars / self.raw_chars), 4) if self.raw_chars else 0.0,
            "dropped_noise_lines": len(self.dropped_lines),
            "sections": len(self._order),
        }

    def outline(self) -> List[Dict[str, object]]:
        """章节清单：规范名 / 原标题 / 页范围 / 规模（供模型决定读哪一节）。"""
        items = []
        for name, section in self._merged_sections().items():
            if not section.blocks:
                continue
            start, end = section.pages
            items.append({
                "key": name,
                "title": section.title or "(无标题)",
                "pages": "{}-{}".format(start, end) if start else "-",
                "chars": section.chars,
                "tokens_est": section.tokens,
            })
        return items

    def outline_text(self) -> str:
        lines = ["| 章节 | 原文标题 | 页码 | 估算 token |", "|---|---|---|---|"]
        for item in self.outline():
            lines.append("| {} | {} | {} | {} |".format(
                item["key"], str(item["title"])[:48], item["pages"], item["tokens_est"]))
        return "\n".join(lines)

    # ---------------- 取内容（工具用） ----------------
    def _render_blocks(self, blocks: Sequence[Block]) -> str:
        parts: List[str] = []
        last_page = 0
        for block in blocks:
            if block.page != last_page and block.page:
                parts.append("[p.{}]".format(block.page))
                last_page = block.page
            parts.append(("## " if block.kind == "heading" else "") + block.text)
        return "\n".join(parts)

    def match_section(self, name: str) -> Optional[Section]:
        """按规范名或原标题模糊匹配章节。"""
        probe = (name or "").strip().lower()
        if not probe:
            return None
        merged = self._merged_sections()
        if probe in merged:
            return merged[probe]
        for key, section in merged.items():
            if probe in key or probe in (section.title or "").lower():
                return section
        for key, section in merged.items():
            title = (section.title or "").lower()
            if title and any(word in title for word in probe.split()):
                return section
        return None

    def section_text(self, name: str, max_tokens: int = 8000) -> str:
        """取一节原文（带页码锚点）；超额度时截断并提示。"""
        section = self.match_section(name)
        if section is None:
            available = ", ".join(i["key"] for i in self.outline()) or "(空)"
            return "未找到章节 {!r}。可用章节：{}".format(name, available)
        text = self._render_blocks(section.blocks)
        if tokens.estimate_tokens(text) <= max_tokens:
            return text
        budget = max_tokens * 4      # token -> 字符的保守换算
        return text[:budget] + "\n...(本节过长已截断，可改用 search_text 精确定位)"

    def read_page(self, page: int, span: int = 1) -> str:
        """读取指定页（及其后 span-1 页）。"""
        pages = sorted({b.page for b in self.blocks})
        if not pages:
            return "该论文没有可读正文。"
        if page not in pages:
            near = min(pages, key=lambda p: abs(p - page))
            return "第 {} 页无内容（可用页码：{}-{}，最接近的是第 {} 页）：\n\n{}".format(
                page, pages[0], pages[-1], near, self.read_page(near, span))
        chosen = set(range(page, page + max(1, span)))
        blocks = [b for b in self.blocks if b.page in chosen]
        return self._render_blocks(blocks) or "第 {} 页无正文。".format(page)

    def search(self, query: str, k: int = 4, window: int = 700) -> str:
        """关键词检索：命中段落（带页码）返回给模型；多词按命中词数排序。"""
        terms = [t for t in re.split(r"[\s,，、]+", (query or "").strip().lower()) if t]
        if not terms:
            return "检索词为空。"
        scored: List[Tuple[int, int, Block]] = []
        for idx, block in enumerate(self.blocks):
            low = block.text.lower()
            hits = sum(1 for t in terms if t in low)
            if hits:
                scored.append((hits, idx, block))
        if not scored:
            return "未在正文中检索到 {!r}（可尝试更短的术语或换英文关键词）。".format(query)
        scored.sort(key=lambda item: (-item[0], item[1]))
        out = ["共 {} 段命中，展示前 {} 段：".format(len(scored), min(k, len(scored)))]
        for hits, _, block in scored[:k]:
            text = block.text
            first = min(text.lower().find(t) for t in terms if t in text.lower())
            start = max(0, first - window // 3)
            snippet = text[start:start + window]
            prefix = "..." if start > 0 else ""
            suffix = "..." if start + window < len(text) else ""
            out.append("[p.{}] {}{}{}".format(block.page, prefix, snippet, suffix))
        return "\n\n".join(out)

    # ---------------- 预算渲染（技能上下文） ----------------
    def render(self, tokens_budget: int = 40000, drop: Sequence[str] = _DROP_DEFAULT,
               sections: Optional[Sequence[str]] = None,
               per_section_paras: Optional[int] = None) -> RenderedContext:
        """按 token 预算把正文渲染成模型上下文。

        sections 指定时只渲染这些章节（速读用）；否则按 _KEEP_ORDER 的信息密度优先级填充。
        """
        merged = self._merged_sections()
        if sections:
            keys = [k for k in sections if k in merged and merged[k].blocks]
            if not keys:      # 章节识别失败（例如纯正文 PDF）时回退到全部可用章节
                keys = list(merged)
        else:
            keys = ([k for k in _KEEP_ORDER if k in merged and k not in set(drop)]
                    + [k for k in merged if k not in _KEEP_ORDER and k not in set(drop)])

        pool = [(k, merged[k]) for k in keys]
        if per_section_paras:
            pool = [(k, _trim_section(s, per_section_paras)) for k, s in pool]

        total = sum(s.tokens for _, s in pool)
        caps: Dict[str, int] = {}
        if total > tokens_budget:
            caps = {k: max(400, int(tokens_budget * ratio)) for k, ratio in _RESERVE_RATIO.items()}

        parts: List[str] = []
        used = 0
        included: List[str] = []
        omitted: List[Tuple[str, int]] = []
        truncated = False
        for key, section in pool:
            remaining = tokens_budget - used
            if remaining <= 0:
                omitted.append((key, section.tokens))
                continue
            header = "## {} ({})".format(section.title or key, key)
            header_cost = tokens.estimate_tokens(header)
            cap = min(remaining, caps.get(key, remaining))
            if section.tokens + header_cost <= remaining:
                body, kept = self._render_blocks(section.blocks), section.tokens
            else:
                body, kept = self._render_within(section.blocks, max(1, cap - header_cost))
                truncated = truncated or kept < section.tokens
                if kept < section.tokens:
                    omitted.append((key, section.tokens - kept))
            used += kept + header_cost
            included.append(key)
            parts.append(header + "\n" + body)

        note = ""
        if omitted:
            note = "（为控制上下文，以下内容未展开：{}；如需可调用 read_section / search_text 工具取回）".format(
                "、".join("{}≈{}t".format(k, t) for k, t in omitted))
        return RenderedContext(text="\n\n".join(parts).strip(), tokens=used, sections=included,
                               omitted=omitted, truncated=truncated, note=note)

    def _render_within(self, blocks: Sequence[Block], cap_tokens: int) -> Tuple[str, int]:
        """按段贪心填充到 cap_tokens，返回 (渲染文本, 实际 token)。

        至少保留第一块，保证调用方每次都有进展（避免剩余预算极小的时候死循环或空转）。
        """
        parts: List[str] = []
        used = 0
        last_page = 0
        for block in blocks:
            cost = block.tokens
            if parts and used + cost > cap_tokens:
                break
            if block.page != last_page and block.page:
                parts.append("[p.{}]".format(block.page))
                last_page = block.page
            parts.append(("## " if block.kind == "heading" else "") + block.text)
            used += cost
        if len(parts) < len(blocks):
            parts.append("...(本节剩余部分因预算省略)")
        return "\n".join(parts), used

    def to_prompt_context(self, tokens_budget: int = 40000) -> str:
        """便捷方法：全文渲染（默认丢弃参考文献/致谢）。"""
        return self.render(tokens_budget=tokens_budget).text


def _trim_section(section: Section, limit: int) -> Section:
    """紧凑模式取段：首 (limit-1) 段 + 末段。

    末段往往是最重要的一段（引言末段是贡献列表、结论末段是最终判断），
    只取开头会漏掉它们，因此采用"首段 + 末段"的取法。
    """
    blocks = section.blocks
    if limit <= 0 or len(blocks) <= limit:
        return section
    kept = blocks[:max(1, limit - 1)] + blocks[-1:]
    return Section(key=section.key, title=section.title, blocks=kept)
