"""同步错误。界面只看 code，message 里不放令牌。"""

import re

_TOKEN = re.compile(r"(ya29\.[0-9A-Za-z_\-]+|Bearer\s+\S+)", re.I)


class SyncError(Exception):
    def __init__(self, code, params=None, status=400):
        super().__init__(code)
        self.code = code
        self.params = params or {}
        self.status = status


def public_message(text):
    cleaned = _TOKEN.sub("[token]", str(text or "")).replace("\n", " ").strip()
    return cleaned[:180]
