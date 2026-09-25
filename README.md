# ArxivReader · 本地文献管理系统

> 产品名为 **ArxivReader**；代码目前位于 `paper_reader/` 目录下（下文涉及文件系统路径处仍沿用该目录名）。

一个类 Zotero 的**本地单用户**文献管理系统。后端 FastAPI + SQLite，前端为 FastAPI
托管的浏览器单页应用（**原生 JS，无 Node 构建、无外部 CDN**，可完全离线运行）。

核心能力：

- **嵌套文件夹管理** —— 任意层级创建 / 重命名 / 移动 / 删除文件夹，组织你的论文库。
- **ArXiv 一键入库** —— 粘贴 ArXiv 链接（或裸 id），自动解析元数据、下载 PDF、抽取全文并归档。
- **规则自动归档** —— 按 `分类 / 关键词 / 作者 / 标题` 规则把新论文自动分到指定文件夹。
- **对话式阅读** —— 将论文全文直接塞进上下文，用任意 OpenAI 兼容 LLM 提问，**流式（SSE）逐字返回**。

---

## 技术栈

| 层 | 选型 |
|---|---|
| Web 后端 | FastAPI + Uvicorn（原生 async、SSE 流式、自动 OpenAPI 文档） |
| 前端 | 原生 JavaScript（ES2017）+ 原生 CSS，`fetch` + `ReadableStream` 消费 SSE |
| PDF 展示 | 浏览器原生 PDF 查看器（`<iframe>` 指向 `/api/papers/{id}/pdf`） |
| 数据库 | SQLite + SQLAlchemy 2.0 ORM |
| ArXiv | 官方 `arxiv` Python 包（2.x API） |
| PDF 解析 | PyMuPDF（`fitz`） |
| LLM | `openai` SDK（OpenAI 兼容接口） |

> **关于前端选型**：原计划使用 Vue 3（CDN 引入）。实现时改为**原生 JS**——本地工具应可离线
> 运行，且引入无 SRI 校验的第三方 CDN 脚本存在供应链风险。此调整不影响任何功能与交互设计。

运行环境：**Python 3.9+**（已在 3.9 验证）。全系统**不读取任何环境变量**，配置集中在 `config.py`。

---

## 目录结构

```
paper_reader/
├── config.py               # 本地实际配置（gitignore，不入库）
├── config.example.py       # 入库的配置模板
├── requirements.txt
├── README.md
├── .gitignore
├── conftest.py             # pytest 根配置（sys.path 注入）
├── app/
│   ├── main.py             # FastAPI 入口：lifespan 建库、路由挂载、static 托管
│   ├── config_loader.py    # 从 config.py 读取 LLM / STORAGE 配置（无环境变量）
│   ├── database.py         # engine/SessionLocal/Base、init_db、ensure_inbox
│   ├── models.py           # ORM：Folder/Paper/FilingRule/ChatSession/ChatMessage/Setting
│   ├── schemas.py          # Pydantic v2 请求/响应模型
│   ├── settings_store.py   # config 播种 + DB 持久化 + 运行时读取 LLM 设置
│   ├── llm_client.py       # OpenAI 兼容：客户端缓存 + 重试退避 + chat/chat_stream
│   ├── arxiv_service.py    # id 规范化 / 元数据获取 / PDF 下载
│   ├── pdf_service.py      # 全文抽取（[Page N] 标记）/ 超长截断
│   ├── filing.py           # 规则匹配引擎（priority 升序，first-match，Inbox 兜底）
│   └── routers/
│       ├── folders.py      # 文件夹树 CRUD
│       ├── papers.py       # ArXiv 入库 / 列表 / 移动 / PDF / 全文
│       ├── rules.py        # 归档规则 CRUD
│       ├── chat.py         # 会话 + 流式对话（SSE）
│       └── settings.py     # LLM 设置读写 + 连通性测试
├── static/
│   ├── index.html          # 三栏布局骨架
│   ├── app.js              # 原生 JS 应用：状态、API、SSE 流式对话
│   └── style.css           # 浅色主题样式
├── tests/                  # pytest：链接解析 / 规则引擎 / API 集成（含 SSE）
└── data/                   # 运行时生成（gitignore）：library.db、pdfs/
```

---

## 安装

```bash
cd paper_reader

# 建议用虚拟环境
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

## 配置

复制模板并填入真实值（`config.py` 已被 `.gitignore` 忽略，**切勿把真实 api_key 提交入库**）：

```bash
cp config.example.py config.py
```

`config.py` 内容：

```python
# ============ LLM 调用配置（OpenAI 兼容接口） ============
# 填好 base_url / api_key / model 三项即可直连任意 OpenAI 兼容服务：
#   OpenAI         https://api.openai.com/v1
#   DeepSeek       https://api.deepseek.com/v1
#   Qwen/DashScope https://dashscope.aliyuncs.com/compatible-mode/v1
#   本地 vLLM      http://127.0.0.1:8000/v1
#   本地 Ollama    http://127.0.0.1:11434/v1
LLM = {
    "base_url": "https://api.openai.com/v1",
    "api_key": "sk-xxxxxxxxxxxxxxxxxxxxxxxx",   # 必填；严禁提交入库
    "model": "gpt-4o-mini",
    "temperature": 0.3,
    "max_tokens": 4096,
    "top_p": 1.0,
    "request_timeout": 120.0,
    "max_retries": 3,
}

# ============ 存储配置 ============
STORAGE = {
    # 数据库(library.db)与下载 PDF 的根目录。留空 = paper_reader/data/
    "base_dir": "",
}
```

> `config.py` 的值仅在**首次启动**时写入 SQLite 作为默认值。之后可直接在网页右上角
> **“设置”**里修改 `base_url/api_key/model/采样参数`，网页修改持久化到数据库，不会回写本文件。

## 启动

```bash
# 方式一：uvicorn（推荐，可加 --reload 热重载）
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765

# 方式二：直接运行入口
python -m app.main
```

启动后浏览器打开 **http://127.0.0.1:8765/** 。首次启动会自动建库、创建默认 `Inbox`
文件夹并播种 LLM 设置。API 文档见 http://127.0.0.1:8765/docs 。

> 若 8765 端口被占用，换用其它端口即可，例如 `--port 8770`。

---

## 使用说明

1. **添加论文**：把 ArXiv 链接（`https://arxiv.org/abs/2401.12345`、`.../pdf/...`、
   裸 id `2401.12345`、旧式 `math/0211159` 均可）粘贴到顶部输入框，可选目标文件夹，点击“添加”。
   系统会解析元数据 → 下载 PDF → 抽取全文 → 归档入库。未指定文件夹时按规则自动归档，
   无规则命中则落入 `Inbox`。
2. **浏览与阅读**：左栏选择文件夹，中栏点击论文即在右侧 `<iframe>` 内联预览 PDF。
3. **对话阅读**：右栏“新建会话”，输入问题，回答以 SSE 流式逐字返回。论文全文已塞入上下文，
   可要求模型**引用页码**（全文按 `[Page N]` 分页标记）。
4. **归档规则**：顶部“归档规则”新增规则，选择匹配字段（category/keyword/author/title）、
   匹配值、目标文件夹与 priority（越小越先）。入库时按 priority 升序取**首条命中**。
   - `category`：支持精确（`cs.CL`）或前缀（`cs` 匹配所有 `cs.*`）。
   - `keyword`：命中标题 + 摘要（不区分大小写）。
   - `author` / `title`：子串匹配（不区分大小写）。
5. **LLM 设置**：顶部“设置”修改端点与采样参数，点击“测试连接”发一条 ping 验证连通性。

### 全文直塞与截断

论文问答采用**全文直塞上下文**策略（不引入向量库 / RAG）。当抽取全文超过约
`120,000` 字符时会**截断**，并在 UI 明确提示“论文过长已截断”，以避免超出模型上下文窗口。

---

## REST API 概览

| 分组 | 端点 |
|---|---|
| 健康 | `GET /api/health` |
| 文件夹 | `GET /api/folders`（树）· `POST /api/folders` · `PATCH /api/folders/{id}` · `DELETE /api/folders/{id}` |
| 论文 | `POST /api/papers/from-arxiv` · `GET /api/papers?folder_id=&q=` · `GET /api/papers/{id}` · `PATCH /api/papers/{id}` · `DELETE /api/papers/{id}` · `GET /api/papers/{id}/pdf` · `GET /api/papers/{id}/text` |
| 规则 | `GET /api/rules` · `POST /api/rules` · `PATCH /api/rules/{id}` · `DELETE /api/rules/{id}` |
| 对话 | `POST /api/papers/{id}/sessions` · `GET /api/papers/{id}/sessions` · `GET /api/sessions/{sid}/messages` · `POST /api/sessions/{sid}/messages`（SSE）· `DELETE /api/sessions/{sid}` |
| 设置 | `GET /api/settings` · `PUT /api/settings` · `POST /api/settings/test` |

删除文件夹不会删论文：其子文件夹与论文上移到父级（顶层移入 `Inbox`）；`Inbox` 禁止删除。

---

## 测试

```bash
python -m pytest -q
```

测试覆盖：`normalize_arxiv_id` 各链接格式、`filing.apply_rules` 的四类匹配 / 优先级 / 停用 /
兜底、以及 API 集成（文件夹 CRUD、防环、入库归档、去重、列表过滤、PDF/全文端点、规则 CRUD、
设置脱敏、对话 SSE 流式与多轮历史）。集成测试用临时 SQLite + mock 的 arxiv/pdf/llm，
**不触网、不依赖真实 LLM**。

---

## 约定与假设

- 单本地用户，无鉴权；服务默认监听 `127.0.0.1`。
- `config.py` 与 `data/` 均被 gitignore；全系统不读环境变量。
- 文件夹支持任意嵌套；论文全文缓存于 DB 以加速重复对话。
- Python 3.9+；`arxiv` 包使用 2.x API。
