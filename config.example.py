"""ArxivReader 配置文件模板。

用法：把本文件复制为同目录下的 config.py，填入真实值即可。
config.py 已被 .gitignore 忽略，不会入库；请勿把真实 api_key 写进本模板。

约定：全系统不读取任何环境变量，所有配置集中在 config.py。
本文件的值仅作为"首次启动"写入数据库的默认值；之后可在网页右上角
"设置"里修改 base_url/api_key/model/采样参数，网页修改持久化到 SQLite，
不会回写本文件。
"""

# ============ LLM 调用配置（OpenAI 兼容接口） ============
# 填好 base_url / api_key / model 三项即可直连任意 OpenAI 兼容服务：
#   OpenAI        https://api.openai.com/v1
#   DeepSeek      https://api.deepseek.com/v1
#   Qwen/DashScope https://dashscope.aliyuncs.com/compatible-mode/v1
#   本地 vLLM     http://127.0.0.1:8000/v1
#   本地 Ollama   http://127.0.0.1:11434/v1
LLM = {
    "base_url": "https://api.openai.com/v1",
    "api_key": "sk-xxxxxxxxxxxxxxxxxxxxxxxx",   # 必填；严禁提交入库
    "model": "gpt-4o-mini",
    "temperature": 0.3,        # 采样温度
    "max_tokens": 4096,        # 单次回复最大 token 数
    "top_p": 1.0,
    "request_timeout": 120.0,  # 单次 HTTP 请求超时（秒）
    "max_retries": 3,          # 调用失败重试次数（指数退避）
}

# ============ 存储配置 ============
STORAGE = {
    # 数据库(library.db)与下载 PDF 的存放根目录。
    # 留空 = paper_reader/data/（推荐，已被 .gitignore 忽略）。
    "base_dir": "",
}
