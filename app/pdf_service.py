"""PDF 全文抽取与页面渲染（PyMuPDF / fitz）。

抽取：采用"全文直塞上下文"策略：把整篇 PDF 文本抽成字符串，页间以 [Page N] 分隔，
便于对话时引用页码。超过 max_chars 的长论文按字符上限截断并回传 truncated=True，
前端据此提示"论文过长已截断"。

渲染：把单页光栅化为 PNG（供前端自绘 PDF 查看器实现滚轮/捏合缩放——浏览器内置的
iframe viewer 无法从外部控制缩放，而项目约定前端零第三方依赖、不可引 CDN 库）。

fitz 采用函数内延迟导入，未安装 PyMuPDF 时不影响其他模块的导入与测试。
"""

import logging
from pathlib import Path
from typing import Dict, List, Tuple, Union

logger = logging.getLogger(__name__)

# 全文直塞的默认字符上限（约对应 3 万 token 量级；可按模型上下文调整）
DEFAULT_MAX_CHARS = 120_000

# 页面渲染的默认倍率：2 约合 144 DPI（字号清晰且单页体积可控）
DEFAULT_RENDER_SCALE = 2.0
RENDER_SCALE_RANGE = (0.5, 4.0)


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


def document_title(pdf_path: Union[str, Path]) -> str:
    """读取 PDF 元数据里的标题；没有则返回空串（调用方自行回退到文件名等）。"""
    import fitz  # PyMuPDF

    try:
        with fitz.open(str(pdf_path)) as doc:
            return ((doc.metadata or {}).get("title") or "").strip()
    except Exception:  # noqa: BLE001  元数据读取失败不影响主流程
        return ""


def page_geometry(pdf_path: Union[str, Path]) -> List[Dict[str, float]]:
    """每页的页面尺寸（PDF point，1pt=1/72inch），供前端查看器做占位与缩放基算。"""
    import fitz  # PyMuPDF

    pages: List[Dict[str, float]] = []
    with fitz.open(str(pdf_path)) as doc:
        for index, page in enumerate(doc, start=1):
            rect = page.rect
            pages.append({"page": index,
                          "width": round(float(rect.width), 2),
                          "height": round(float(rect.height), 2)})
    return pages


def render_page_png(pdf_path: Union[str, Path], page_no: int,
                    scale: float = DEFAULT_RENDER_SCALE) -> bytes:
    """把指定页（1 起）渲染成 PNG 字节；页码越界抛 IndexError。"""
    import fitz  # PyMuPDF

    factor = max(RENDER_SCALE_RANGE[0], min(float(scale or DEFAULT_RENDER_SCALE),
                                            RENDER_SCALE_RANGE[1]))
    with fitz.open(str(pdf_path)) as doc:
        if page_no < 1 or page_no > doc.page_count:
            raise IndexError("页码越界：{}（共 {} 页）".format(page_no, doc.page_count))
        page = doc[page_no - 1]
        pixmap = page.get_pixmap(matrix=fitz.Matrix(factor, factor), alpha=False)
        return pixmap.tobytes("png")
