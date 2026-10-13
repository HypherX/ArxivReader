"""面向界面的错误和 SSE 稳定消息标识；原始诊断不冒充翻译。"""
import json


def error_detail(detail, status=500):
    if isinstance(detail, dict) and detail.get("code"):
        return detail
    text = str(detail or "")
    code = "error.{}".format(status)
    if text.startswith(("尚未配置 LLM", "请先配置 LLM")):
        code = "settings.not_configured"
    return {"code": code, "params": {}}


def encode_event(payload):
    event = dict(payload)
    if event.get("type") == "error":
        event.update(error_detail(event.get("message"), 502))
        event["original"] = event.pop("message", "")
    elif event.get("type") in ("step", "stage"):
        event.update(code="log.stage", params={})
        event["stage"] = event.get("stage") or event.get("step")
        event["original"] = event.get("detail", "")
    return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
