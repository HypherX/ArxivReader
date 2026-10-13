"""稳定阶段日志，以及仅对可识别历史记录的兼容转换。"""
import re

ALIASES = {
    "search": ("ArXiv 批量检索", "双路 arXiv 检索", "Two-route arXiv search"),
    "screen": ("方向树预筛",), "summarize": ("快速总结验证", "保留候选快速总结", "Summarize retained candidates"),
    "buffer": ("写入待读缓冲区",), "collect": ("读取论文与引用材料", "Collect source material"),
    "extract": ("提取引用与主题查询", "Extract references and topics"),
    "judge": ("完整摘要批量筛选", "Batch relevance screening"),
}


def normalize(record):
    result = dict(record)
    if result.get("code"):
        return result
    text = str(result.get("message") or "")
    match = re.match(r"^\[([^]]+)\]\s+(running|done|failed|skipped)\b", text)
    if match:
        stage = next((key for key, names in ALIASES.items() if match[1] in names), None)
        if stage:
            result.update(code="log.stage", stage=stage, status=match[2], params={})
    if not result.get("code"):
        result.update(code="log.updated", params={})
    result["original"] = text
    return result
