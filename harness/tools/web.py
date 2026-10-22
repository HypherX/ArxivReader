"""联网工具：抓取网页正文（fetch_web_page）。

安全要点：
  - 只允许 http/https；解析目标主机 IP，回环/内网/链路本地地址一律拒绝（SSRF 防线——
    本应用本身监听 127.0.0.1，绝不能被模型的 URL 打回自己）。
  - 重定向链每一跳都重新过同一道闸（redirect 到内网同样拒绝）。
  - 正文抽取用标准库 html.parser：去 script/style/head，块级标签换行，压缩空白；
    结果按 Tool.max_result_chars 截断，防止一页长文顶爆上下文。
"""

import html.parser
import ipaddress
import logging
import re
import socket
from typing import Any, List, Optional, Tuple
from urllib.parse import urlsplit

import requests

from .base import Tool, ToolContext, register_tool

logger = logging.getLogger(__name__)

_TIMEOUT = 20.0
_MAX_BYTES = 2 * 1024 * 1024        # 页面下载上限（只取正文，大页面不值得全拉）
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) ArxivReader/1.1"


class FetchBlocked(ValueError):
    """URL 不允许抓取（协议/内网地址）；消息可直接给模型看。"""


def _public_url(url: str) -> str:
    """校验 URL 可抓取，返回归一化后的 URL；否则抛 FetchBlocked。"""
    parts = urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FetchBlocked("只支持 http/https 链接")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except OSError:
        raise FetchBlocked("域名解析失败")
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved \
                or addr.is_multicast or addr.is_unspecified:
            raise FetchBlocked("不允许访问内网或本机地址")
    return parts.geturl()


class _GuardedRedirect(requests.adapters.HTTPAdapter):
    """重定向链每一跳都重新校验目标地址。"""

    def send(self, request, **kwargs):  # noqa: D102
        request.url = _public_url(request.url)
        return super().send(request, **kwargs)


class _TextExtractor(html.parser.HTMLParser):
    _SKIP = {"script", "style", "head", "noscript", "template", "svg", "iframe", "form"}
    _BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
              "tr", "table", "section", "article", "header", "footer", "blockquote", "pre"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.parts: List[str] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data.strip()
            return
        text = data.strip()
        if text:
            self.parts.append(text + " ")

    def text(self, limit: int) -> str:
        raw = "".join(self.parts)
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n\s*\n+", "\n\n", raw).strip()
        return raw[:limit]


def fetch_text(url: str, *, max_chars: int = 9000) -> str:
    """抓取并抽取正文；返回 '标题 + 正文' 文本。失败抛带可读信息的异常。"""
    url = _public_url(url)
    session = requests.Session()
    session.mount("http://", _GuardedRedirect())
    session.mount("https://", _GuardedRedirect())
    try:
        resp = session.get(url, timeout=_TIMEOUT, headers={"User-Agent": _UA},
                           stream=True, allow_redirects=True)
    except FetchBlocked:
        raise
    except requests.exceptions.RequestException as exc:
        raise ValueError("网页请求失败：{}".format(type(exc).__name__))
    try:
        if resp.status_code >= 400:
            raise ValueError("网页返回 HTTP {}".format(resp.status_code))
        content_type = (resp.headers.get("Content-Type") or "").lower()
        chunks, size = [], 0
        for chunk in resp.iter_content(64 * 1024):
            chunks.append(chunk)
            size += len(chunk)
            if size >= _MAX_BYTES:
                break
        body = b"".join(chunks)
    finally:
        resp.close()
        session.close()
    if "pdf" in content_type or body[:5] == b"%PDF-":
        raise ValueError("目标是 PDF，请改用 arXiv 工具查论文信息")
    encoding = resp.encoding or resp.apparent_encoding or "utf-8"
    page = body.decode(encoding, "replace")
    if "html" not in content_type and "<" not in page[:2000]:
        # 纯文本（markdown / txt / json 等）直接截断返回
        return page[:max_chars]
    parser = _TextExtractor()
    try:
        parser.feed(page)
    except Exception:  # noqa: BLE001  HTML 再脏也不让抽取失败，已收集的部分照常返回
        pass
    text = parser.text(max_chars)
    if not text.strip():
        raise ValueError("页面没有可抽取的正文")
    header = "# " + parser.title.strip() + "\n\n" if parser.title.strip() else ""
    return (header + text).strip()


@register_tool("fetch_web_page")
def _fetch_web_page(ctx: ToolContext) -> Tool:
    def run(url: str) -> str:
        return fetch_text(url)

    return Tool(
        name="fetch_web_page",
        description=ctx.text(
            "抓取一个网页的正文（博客、GitHub 页面、项目主页、新闻等）。需要核实论文里提到的链接、"
            "查看作者主页/代码仓库说明、或阅读某篇网页文章时使用。只能抓 http/https 公开网页，"
            "不能下载 PDF（查论文请用 arXiv 工具）。",
            "Fetch the text content of a web page (blog posts, GitHub pages, project homepages, news). "
            "Use it to verify links mentioned in the paper, inspect the authors' homepage or repo README, "
            "or read a web article. Public http/https pages only; PDFs are not supported "
            "(use the arXiv tools for papers)."),
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string", "description": ctx.text("完整 URL", "Full URL")}},
            "required": ["url"],
        },
        handler=run,
        max_result_chars=9000,
        needs_document=False,
    )
