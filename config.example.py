"""ArxivReader 本地配置（实际值）。

本文件由 config.example.py 复制而来，已被 .gitignore 忽略，不会入库。
全系统不读取任何环境变量，所有配置集中在本文件。

首次启动时下列值写入数据库作为默认值；之后可在网页"设置"里修改，
网页修改持久化到 SQLite，不回写本文件。
"""

# ============ LLM 调用配置（OpenAI 兼容接口） ============
LLM = {
    "base_url": "https://token-plan.maas.qianwenaiapi.com/compatible-mode/v1",
    "api_key": "sk-sp-H.DEIDRD.4n2Y.MEYCIQDe-A_QITwJkKIyNaj9B4Nf91r9ApND52arKiP_Fh3w7gIhAPM05KhPXcMvBxMSOsuawPil8h8XBrD85GByYzTlcDo6",   # 请填入真实 key（本文件不入库）
    "model": "qwen3.8-flash",
    "temperature": 1.0,
    "max_tokens": 256000,
    "top_p": 1.0,
    "request_timeout": 120.0,
    "max_retries": 3,
}

# ============ 存储配置 ============
STORAGE = {
    # 留空 = paper_reader/data/
    "base_dir": "data/",
}
