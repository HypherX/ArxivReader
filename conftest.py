"""pytest 根配置。

1) 把项目根目录放到 sys.path，测试可直接 import app / harness / knowledge；
2) 注入一份"假 LLM 配置"，让测试不依赖本地 config.py（发布包里它是空的）——
   各测试都会 monkeypatch 掉真正的模型调用，这里只负责让 is_configured 通过，
   且端点指向 127.0.0.1:9，即使漏了 mock 也不会误连外网。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_DUMMY_LLM_CONFIG = {
    "base_url": "http://127.0.0.1:9/v1",
    "api_key": "test-key",
    "model": "test-model",
    "temperature": None,
    "max_tokens": None,
    "top_p": None,
    "reasoning_effort": None,
    "request_timeout": 5.0,
    "max_retries": 0,
}


@pytest.fixture(autouse=True)
def _hermetic_llm_config(monkeypatch):
    """全局（autouse）：用假配置替换 config.py 的 LLM 节。"""
    import app.config_loader as config_loader
    from harness import config as harness_config

    monkeypatch.setattr(config_loader, "get_llm_config", lambda: dict(_DUMMY_LLM_CONFIG))
    monkeypatch.setattr(harness_config, "get_llm_config", lambda: dict(_DUMMY_LLM_CONFIG))
