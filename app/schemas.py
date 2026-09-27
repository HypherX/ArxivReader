"""Pydantic 请求/响应模型。

ORM 对象通过 model_config = ConfigDict(from_attributes=True) 直接序列化，
其中 authors/categories/has_text/folder_name 等读取的是模型上的属性。
更新类 schema 全部字段可选，路由用 model_fields_set 判断哪些字段被显式传入，
以区分"设为 null"与"不修改"。
"""

import datetime
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

MatchType = Literal["category", "keyword", "author", "title"]


# ---------------- Folder ----------------
class FolderCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    parent_id: Optional[int] = None


class FolderUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    parent_id: Optional[int] = None


class FolderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    parent_id: Optional[int] = None
    direction_node_id: Optional[int] = None


class FolderNode(BaseModel):
    """文件夹树节点（递归）。方向文件夹带 direction_node_id，可直达论文图谱。"""

    id: int
    name: str
    parent_id: Optional[int] = None
    paper_count: int = 0
    direction_node_id: Optional[int] = None
    children: List["FolderNode"] = []


FolderNode.model_rebuild()


# ---------------- Paper ----------------
class PaperFromArxiv(BaseModel):
    url: str = Field(..., min_length=1)
    folder_id: Optional[int] = None


class PaperUpdate(BaseModel):
    folder_id: Optional[int] = None
    title: Optional[str] = None


class PaperOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    arxiv_id: str
    title: str
    authors: List[str] = []
    abstract: str = ""
    categories: List[str] = []
    published: Optional[str] = None
    pdf_url: Optional[str] = None
    folder_id: Optional[int] = None
    folder_name: Optional[str] = None
    folder_is_direction: bool = False
    text_truncated: bool = False
    has_text: bool = False
    text_chars: int = 0
    added_at: Optional[datetime.datetime] = None


# ---------------- FilingRule ----------------
class RuleCreate(BaseModel):
    name: str = ""
    match_type: MatchType
    pattern: str = Field(..., min_length=1)
    folder_id: Optional[int] = None
    priority: int = 100
    enabled: bool = True


class RuleUpdate(BaseModel):
    name: Optional[str] = None
    match_type: Optional[MatchType] = None
    pattern: Optional[str] = None
    folder_id: Optional[int] = None
    priority: Optional[int] = None
    enabled: Optional[bool] = None


class RuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    match_type: str
    pattern: str
    folder_id: Optional[int] = None
    folder_name: Optional[str] = None
    priority: int
    enabled: bool


# ---------------- Chat ----------------
class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    paper_id: int
    title: str
    created_at: Optional[datetime.datetime] = None


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    role: str
    content: str
    reasoning_content: str = ""


class ChatRequest(BaseModel):
    content: str = Field(..., min_length=1)
    cancel_token: Optional[str] = Field(None, description="用于「终止」按钮的取消令牌")


# ---------------- Settings ----------------
class LLMSettingsOut(BaseModel):
    """采样参数为 None 表示"未设置，沿用服务端默认值"。"""

    base_url: str
    model: str
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None
    reasoning_effort: Optional[str] = None
    has_api_key: bool
    api_key_preview: str


class LLMSettingsUpdate(BaseModel):
    """部分更新：字段缺省 = 不修改；字段为空串 = 清除（改用 API 默认值）。"""

    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    temperature: Optional[Union[float, str]] = None
    max_tokens: Optional[Union[int, str]] = None
    top_p: Optional[Union[float, str]] = None
    reasoning_effort: Optional[str] = None


class TestResult(BaseModel):
    ok: bool
    detail: str = ""


# ---------------- Skill（技能） ----------------
class SkillOut(BaseModel):
    """技能元信息（/api/skills 列表用）。"""

    name: str
    title: str
    description: str
    context_mode: str
    context_tokens: Optional[int] = None
    tools: List[str] = []
    max_tool_rounds: int = 0
    output_format: str = "markdown"
    usage_hint: str = ""


class SkillRunRequest(BaseModel):
    """运行技能的可选覆盖项（只开放上下文预算，prompt 模板不开放到 HTTP）。"""

    budget: Optional[int] = Field(None, ge=256, le=400000)
    cancel_token: Optional[str] = Field(None, description="用于「终止」按钮的取消令牌")


class SkillRunOut(BaseModel):
    skill: str
    output: str = ""
    error: str = ""
    stats: Dict[str, Any] = {}


# ---------------- 流式（SSE）帧协议 ----------------
# 一份声明，前后端共用：chat / skills / knowledge 三个流式通道发同样的帧类型。
SSE_KINDS = ("delta", "tool", "step", "error", "done")
# delta : {"text": 增量正文, "reasoning": 增量推理}
# tool  : {"name": 工具名, "ok": bool, "result_chars": n}
# step  : {"step": .., "title": .., "status": "running|done|skipped|failed", "detail": ..}
# error : {"message": 错误信息}
# done  : 会话消息 id / 技能输出 / pipeline 账本（按通道取值）


# ---------------- 知识网络（pipeline） ----------------
class PipelineRunRequest(BaseModel):
    """pipeline 运行参数：steps 缺省 = 全跑；force = 忽略已有产物强制重跑。"""

    steps: Optional[List[str]] = None
    force: bool = False
    cancel_token: Optional[str] = Field(None, description="用于「终止」按钮的取消令牌")


class NodeUpdateRequest(BaseModel):
    """方向节点手工整备：目前只支持改名（后代路径、镜像文件夹与图谱 md 一起平移）。"""

    name: str = Field(..., min_length=1, max_length=120)


class PipelineRunOut(BaseModel):
    paper_id: int
    run_id: Optional[int] = None
    status: str = "done"
    steps: List[Dict[str, Any]] = []
    syntheses: List[Dict[str, Any]] = []
    error: str = ""


# ---------------- ArXiv 检索 Pipeline / 待读缓冲区 ----------------
class SearchRunRequest(BaseModel):
    """检索表单：日期范围 + 多分类 + 关键词组合（只取元信息，不下载全文）。"""

    categories: List[str] = []
    keywords: List[str] = []
    date_from: Optional[str] = None          # YYYY-MM-DD
    date_to: Optional[str] = None
    max_results: int = Field(default=40, ge=1, le=500)
    sort_by: Literal["submitted", "relevance"] = "submitted"
    keyword_mode: Literal["and", "or"] = "and"   # 多关键词之间取交集还是并集
    # 内联覆盖（不填则用 config.py 的 KNOWLEDGE 配置）
    min_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    max_summarize: Optional[int] = Field(default=None, ge=0, le=50)


class SearchRunOut(BaseModel):
    id: int
    status: str
    stage: str = ""
    counters: Dict[str, Any] = {}
    error: str = ""
    cancel_requested: bool = False
    started_at: Optional[datetime.datetime] = None
    finished_at: Optional[datetime.datetime] = None
    request: Dict[str, Any] = {}
    logs: List[Dict[str, Any]] = []
    log_total: int = 0


class BufferPaperOut(BaseModel):
    """待读缓冲区条目：只有元信息 + 预筛/摘要结果，尚未深读与入图谱。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    arxiv_id: str
    title: str
    abstract: str = ""
    authors: List[str] = []
    categories: List[str] = []
    published: Optional[str] = None
    match_score: float = 0.0
    suggested_path: str = ""
    verified_path: str = ""
    screen_reason: str = ""
    verify_reason: str = ""
    summary_text: str = ""
    status: str = "pending"
    promote_state: str = ""
    promote_error: str = ""
    promoted_paper_id: Optional[int] = None
    matched_node_id: Optional[int] = None
    search_run_id: Optional[int] = None
    source: str = "search"
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


class BufferListOut(BaseModel):
    items: List[BufferPaperOut] = []
    counts: Dict[str, int] = {}


class PromoteOut(BaseModel):
    """提升入库（加入详细阅读）：后台执行时只看 promote_state 轮询。"""

    buffer_id: int
    promote_state: str
    paper_id: Optional[int] = None
    pipeline: Optional[Dict[str, Any]] = None
