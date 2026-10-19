"""PDF 全文抽取（PyMuPDF / fitz）。

抽取：把整篇 PDF 文本一次性抽成字符串，页间以 [Page N] 分隔，便于引用页码。
这里不设字符上限，入库时不丢掉附录或文末。对话和深度精读使用这份全文；
快速总结和论文关系比较仍按 token 预算压缩。

页面渲染不在这里：阅读器改由前端本地打包的 PDF.js 直接解析 PDF 文件，
服务端只负责把原始文件送出去（/api/papers/{id}/pdf 支持 Range 分段请求）。

fitz 采用函数内延迟导入，未安装 PyMuPDF 时不影响其他模块的导入与测试。
"""

import logging
from pathlib import Path
from typing import Union

from harness.paper import readable_text

logger = logging.getLogger(__name__)

def extract_text(pdf_path: Union[str, Path]) -> str:
    """抽取整篇 PDF 文本，页间以 [Page N] 分隔。"""
    import fitz  # PyMuPDF

    parts = []
    with fitz.open(str(pdf_path)) as doc:
        for i, page in enumerate(doc, start=1):
            try:
                page_text = readable_text(page.get_text("text") or "")
            except Exception as exc:  # noqa: BLE001  单页解析失败不阻断整体
                logger.warning("第 %d 页抽取失败：%s", i, str(exc)[:200])
                page_text = ""
            parts.append("\n\n[Page {}]\n\n{}".format(i, page_text))
    return "".join(parts).strip()


def document_title(pdf_path: Union[str, Path]) -> str:
    """读取 PDF 元数据里的标题；没有则返回空串（调用方自行回退到文件名等）。"""
    import fitz  # PyMuPDF

    try:
        with fitz.open(str(pdf_path)) as doc:
            return ((doc.metadata or {}).get("title") or "").strip()
    except Exception:  # noqa: BLE001  元数据读取失败不影响主流程
        return ""
