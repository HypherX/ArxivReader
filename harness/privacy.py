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
            message = record.getMessage()
            exception = "".join(traceback.format_exception(*record.exc_info)) if record.exc_info else ""
            for secret in values:
                message = message.replace(secret, "[REDACTED]")
                exception = exception.replace(secret, "[REDACTED]")
            record.msg, record.args = message, ()
            if record.exc_info:
                record.exc_text, record.exc_info = exception, None
            return record

        logging.setLogRecordFactory(factory)
        _installed = True
