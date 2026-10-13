"""Token 估算：零依赖、按字符类别近似（用于上下文预算与压缩效果对比）。

估算口径（对 Qwen / GPT 系 tokenizer 的经验近似）：
  - ASCII 字符约 4 字符 / token
  - CJK 字符（中日韩统一表意文字、假名）约 1 字符 / token
  - 其它非 ASCII（希腊字母、数学符号、西里尔字母等）约 2 字符 / token

不追求逐 token 精确：用途是"预算控制"和"压缩前后对比"，同一口径下的相对量可靠，
且 API 返回的 usage 会作为真实值一并记入每次调用的 stats（详见 harness/runner.py）。
"""

from typing import Any, Dict, List

_CJK_RANGES = (
    (0x3040, 0x30FF),    # 平假名 / 片假名
    (0x3400, 0x4DBF),    # CJK 扩展 A
    (0x4E00, 0x9FFF),    # CJK 基本区
    (0xF900, 0xFAFF),    # CJK 兼容表意文字
    (0x20000, 0x2FA1F),  # CJK 扩展 B~F（代理对，逐字符计数）
)


def _is_cjk(code: int) -> bool:
    for low, high in _CJK_RANGES:
        if low <= code <= high:
            return True
    return False


def count_categories(text: str) -> Dict[str, int]:
    ascii_n = cjk_n = other_n = 0
    for ch in text or "":
        code = ord(ch)
        if code < 128:
            ascii_n += 1
        elif _is_cjk(code):
            cjk_n += 1
        else:
            other_n += 1
    return {"ascii": ascii_n, "cjk": cjk_n, "other": other_n, "chars": len(text or "")}


def estimate_tokens(text: str) -> int:
    """估算 token 数（向上取整，便于预算保守）。"""
    if not text:
        return 0
    counts = count_categories(text)
    total = counts["ascii"] / 4.0 + counts["cjk"] + counts["other"] / 2.0
    return int(total) + (1 if total % 1 else 0)


def estimate_messages(messages: List[Dict[str, Any]]) -> int:
    """估算一段 messages 的 token 总量（含每条消息的固定开销 4）。"""
    total = 0
    for msg in messages or []:
        content = msg.get("content") or ""
        if isinstance(content, list):    # 多模态分段：只统计文本段
            content = "".join(part.get("text", "") for part in content
                              if isinstance(part, dict))
        total += estimate_tokens(str(content)) + 4
        for call in (msg.get("tool_calls") or []):
            fn = call.get("function") or {}
            total += estimate_tokens(str(fn.get("name", "")) + str(fn.get("arguments", ""))) + 8
    return total


def estimate_tools(tools: List[Dict[str, Any]]) -> int:
    """估算工具 schema 占用的 token（每轮请求都会随带，故必须计入预算）。"""
    import json

    return estimate_tokens(json.dumps(tools or [], ensure_ascii=False))
