"""PDF 全文抽取（PyMuPDF / fitz）。

采用"全文直塞上下文"策略：把整篇 PDF 文本抽成字符串，页间以 [Page N] 分隔，
便于对话时引用页码。超过 max_chars 的长论文按字符上限截断并回传 truncated=True，
前端据此提示"论文过长已截断"。

fitz 采用函数内延迟导入，未安装 PyMuPDF 时不影响其他模块的导入与测试。
"""

import logging
from pathlib import Path
from typing import Tuple, Union

logger = logging.getLogger(__name__)

# 全文直塞的默认字符上限（约对应 3 万 token 量级；可按模型上下文调整）
DEFAULT_MAX_CHARS = 120_000


def extract_text(pdf_path: Union[str, Path]) -> str:
    """抽取整篇 PDF 文本，页间以 [Page N] 分隔。"""
    import fitz  # PyMuPDF

    parts = []
    with fitz.open(str(pdf_path)) as doc:
        for i, page in enumerate(doc, start=1):
            try:
                page_text = page.get_text("text") or ""
            except Exception as exc:  # noqa: BLE001  单页解析失败不阻断整体
                logger.warning("第 %d 页抽取失败：%s", i, str(exc)[:200])
                page_text = ""
            parts.append("\n\n[Page {}]\n\n{}".format(i, page_text))
    return "".join(parts).strip()


def extract_text_for_context(pdf_path: Union[str, Path],
                             max_chars: int = DEFAULT_MAX_CHARS) -> Tuple[str, bool]:
    """抽取并按 max_chars 截断，返回 (文本, 是否被截断)。"""
    full = extract_text(pdf_path)
    if len(full) <= max_chars:
        return full, False
    return full[:max_chars], True
