"""防止模型服务的原始异常把当前会话密钥写入日志。"""
import logging
import threading
import traceback

_secrets = set()
_lock = threading.Lock()
_installed = False


def protect(value):
    global _installed
    if not value:
        return
    with _lock:
        _secrets.add(value)
        if _installed:
            return
        previous = logging.getLogRecordFactory()

        def factory(*args, **kwargs):
            record = previous(*args, **kwargs)
            with _lock:
                values = tuple(_secrets)

            def scrub(text):
                for secret in values:
                    text = text.replace(secret, "[REDACTED]")
                return text

            # 就地脱敏而不是把记录压平成一条字符串：某些 Formatter（如 uvicorn 的
            # AccessFormatter）会直接解包 record.args，清空它会抛 "expected 5, got 0"。
            if isinstance(record.msg, str):
                record.msg = scrub(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: scrub(v) if isinstance(v, str) else v
                                   for k, v in record.args.items()}
                else:
                    record.args = tuple(scrub(a) if isinstance(a, str) else a
                                        for a in record.args)
            if record.exc_info:
                exception = "".join(traceback.format_exception(*record.exc_info))
                record.exc_text, record.exc_info = scrub(exception), None
            return record

        logging.setLogRecordFactory(factory)
        _installed = True
