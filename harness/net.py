"""内网/本机端点判定：这类地址走 HTTP 代理必然失败（代理出不了内网），需要直连。

LLM 客户端（harness.llm）与翻译子进程（pdftranslate.engine）共用这一套：
  - IP 字面量：直接按 ipaddress 判断 private / loopback / link-local；
  - 域名：线程里做 DNS 解析（带超时），任一解析结果为内网地址即视为内网域名；
  - 公网地址与解析失败一律返回 False（不改变现有代理行为）。
"""

import ipaddress
import socket
import threading
from urllib.parse import urlparse


def _is_private_ip(text: str) -> bool:
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback or addr.is_link_local


def host_of(base_url: str) -> str:
    return (urlparse(base_url or "").hostname or "").strip()


def is_private_base_url(base_url: str, timeout: float = 3.0) -> bool:
    """base_url 指向内网/本机地址返回 True；公网或无法判定返回 False。"""
    host = host_of(base_url)
    if not host:
        return False
    if _is_private_ip(host):
        return True
    resolved = []

    def _resolve() -> None:
        try:
            for family, _, _, _, sockaddr in socket.getaddrinfo(host, None):
                if family in (socket.AF_INET, socket.AF_INET6):
                    resolved.append(sockaddr[0])
        except Exception:  # noqa: BLE001
            pass

    worker = threading.Thread(target=_resolve, daemon=True)
    worker.start()
    worker.join(timeout=timeout)
    return any(_is_private_ip(ip) for ip in resolved)
