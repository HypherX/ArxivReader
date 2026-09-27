"""ArxivReader 配置文件模板。

用法：把本文件复制为同目录下的 config.py，填入真实值即可。
config.py 已被 .gitignore 忽略，不会入库；请勿把真实 api_key 写进本模板。

约定：全系统不读取任何环境变量，所有配置集中在 config.py。
本文件的值仅作为"首次启动"写入数据库的默认值；之后可在网页右上角
"设置"里修改 base_url/api_key/model/采样参数，网页修改持久化到 SQLite，
不会回写本文件。
"""

# ============ LLM 调用配置（OpenAI 兼容接口） ============
# 三个值默认留空：启动后如果还没配置，网页右上角会显示「⚠ 设置」，填一次即可（写入数据库，不回写本文件）。
# 也可以在这里预先填好，作为首次启动的默认值：
#   OpenAI         base_url=https://api.openai.com/v1          model=gpt-4o-mini
#   DeepSeek       base_url=https://api.deepseek.com/v1        model=deepseek-chat
#   Qwen/DashScope base_url=https://dashscope.aliyuncs.com/compatible-mode/v1
#   本地 vLLM      base_url=http://127.0.0.1:8000/v1
#   本地 Ollama    base_url=http://127.0.0.1:11434/v1
LLM = {
    "base_url": "",
    "api_key": "",                     # 必填；严禁提交入库
    "model": "",
    # 采样参数为 None = 不显式下发，沿用服务端（API）默认值；需要时再填数值
    "temperature": None,       # 采样温度
    "max_tokens": None,        # 单次回复最大 token 数
    "top_p": None,
    # 推理强度：low / medium / high / max；None = 不下发（可在网页设置里改）
    "reasoning_effort": None,
    "request_timeout": 300.0,  # 单次 HTTP 请求超时（秒）
    "max_retries": 2,          # 调用失败重试次数（指数退避）
}

# ============ Agent Harness 配置 ============
# 所有 AI 能力（技能/工具）共用的预算与开关，集中在此，避免散落在各模块。
HARNESS = {
    # 技能单次送入模型的论文正文 token 预算（估算值，超出按信息密度优先级裁剪）
    "skill_context_tokens": 40000,
    # 速读技能的紧凑上下文预算（摘要 + 引言 + 结论）
    "quick_context_tokens": 3000,
    # 对话上下文预算：>0 直接塞压缩全文；0 = 只用摘要 + 章节大纲（最省 token）
    "chat_context_tokens": 24000,
    # 对话是否向模型开放检索工具（search_text / read_section / read_page）
    "chat_tools": True,
    # 对话历史最多带多少条消息（超出则丢弃最早的，防止长会话线性膨胀）
    "chat_history_messages": 12,
    # 工具调用最大轮数（防失控）
    "max_tool_rounds": 3,
    # 输出语言（技能与对话统一）
    "output_language": "中文（方法名/指标名/论文术语保留英文原文）",
}

# ============ 知识网络（论文阅读 -> 方向树 / 局部图谱 / 阶段综述） ============
# 每读一篇论文，系统自动跑一遍：摘要 -> 归入方向树 -> 深度精读 -> 局部图谱 -> （阶段）综述
KNOWLEDGE = {
    # 某个方向节点下论文数每增加 N 篇，触发一次阶段性综述
    "synthesis_every": 5,
    # 关系抽取时最多对比该节点下多少篇已有论文
    "relation_max_papers": 20,
    # 阶段综述单次最多汇总多少篇论文
    "synthesis_max_papers": 40,
    # 方向树提示里最多列出多少条已有路径
    "max_tree_paths": 200,
    # 是否把推理过程（reasoning_content）落库（体积较大）
    "store_reasoning": False,
    # 论文入库后是否自动触发知识网络 pipeline
    "auto_run_on_import": False,
    # 各步已有产物时是否跳过（force 参数可强制重跑）
    "reuse_artifacts": True,

    # ---- ArXiv 检索 Pipeline（检索 -> 预筛 -> 总结验证 -> 缓冲区）----
    "discovery_batch_size": 8,        # 预筛一次送多少篇给模型
    "discovery_min_score": 0.55,      # 预筛匹配分阈值（低于此值丢弃）
    "discovery_max_summarize": 12,    # 单轮最多复核多少篇（跑 quick_summary）
    "discovery_default_days": 14,     # 表单未填日期时的默认回看天数
}

# ============ 存储配置 ============
STORAGE = {
    # 数据库(library.db)与下载 PDF 的存放根目录。
    # 留空 = paper_reader/data/（推荐，已被 .gitignore 忽略）。
    "base_dir": "",
}
