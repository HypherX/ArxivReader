"""Pydantic 请求/响应模型。

ORM 对象通过 model_config = ConfigDict(from_attributes=True) 直接序列化，
其中 authors/categories/has_text/folder_name 等读取的是模型上的属性。
更新类 schema 全部字段可选，路由用 model_fields_set 判断哪些字段被显式传入，
以区分"设为 null"与"不修改"。
"""

import datetime
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field



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
    subtree_paper_count: int = 0
    path: str = ""
    display_name: str = ""
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


class ReadingProgressIn(BaseModel):
    """阅读进度上报：page 为当前读到的页码，pages 为总页数，seconds 为本次新增的阅读秒数。"""

    page: int = 0
    pages: int = 0
    seconds: int = 0


class ReadingMarkIn(BaseModel):
    """手动标记：read=True 标为已读完，False 标为未读（清零进度）。"""

    read: bool


class PaperOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    arxiv_id: str
    source: str = "arxiv"
    title: str
    authors: List[str] = []
    abstract: str = ""
    categories: List[str] = []
    published: Optional[str] = None
    pdf_url: Optional[str] = None
    folder_id: Optional[int] = None
    folder_name: Optional[str] = None
    folder_is_direction: bool = False
    has_text: bool = False
    text_chars: int = 0
    added_at: Optional[datetime.datetime] = None
    read_page: int = 0
    read_pages: int = 0
    read_seconds: int = 0
    read_percent: int = 0
    read_state: str = "unread"
    last_read_at: Optional[datetime.datetime] = None


# ---------------- Chat ----------------
class SessionCreate(BaseModel):
    """chat 每次新建一页空白对话。summary / deep_reading 复用该产物自己的追问页。"""

    scope: str = "chat"


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    paper_id: int
    title: str
    scope: str = "chat"
    created_at: Optional[datetime.datetime] = None


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    role: str
    content: str
    reasoning_content: str = ""
    tools_json: str = "[]"


class ChatRequest(BaseModel):
    content: str = Field(..., min_length=1)
    cancel_token: Optional[str] = Field(None, description="用于「终止」按钮的取消令牌")


class RegenerateRequest(BaseModel):
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
    request_timeout: float = 300.0
    max_retries: int = 2
    output_language: Literal["zh", "en"] = "zh"
    # 划词翻译自定义端点；空串 = 跟随主设置
    seltr_base_url: str = ""
    seltr_model: str = ""
    seltr_has_api_key: bool = False
    seltr_api_key_preview: str = ""


class LLMSettingsUpdate(BaseModel):
    """部分更新：字段缺省 = 不修改；字段为空串 = 清除（改用 API 默认值）。"""

    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    output_language: Optional[Literal["zh", "en"]] = None
    temperature: Optional[Union[float, Literal[""]]] = Field(None, description="0–2 或空")
    max_tokens: Optional[Union[int, Literal[""]]] = None
    top_p: Optional[Union[float, Literal[""]]] = None
    reasoning_effort: Optional[Literal["", "low", "medium", "high", "max"]] = None
    request_timeout: Optional[float] = Field(None, ge=5, le=3600)
    max_retries: Optional[int] = Field(None, ge=0, le=10)
    # 划词翻译自定义端点（seltr.*）；空串 = 清除自定义，跟随主设置
    seltr_base_url: Optional[str] = None
    seltr_model: Optional[str] = None
    seltr_api_key: Optional[str] = None


class TestResult(BaseModel):
    ok: bool
    detail: str = ""
    code: str = ""
    params: Dict[str, Any] = {}


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


# ---------------- 智能关联检索（Related Work） ----------------
class RelatedRunOut(BaseModel):
    """关联检索 run 状态（轮询用）：阶段 / 计数 / 实时日志。"""

    id: int
    paper_id: int = 0
    status: str
    stage: str = ""
    counters: Dict[str, Any] = {}
    error: str = ""
    stages_done: List[str] = []
    cancel_requested: bool = False
    started_at: Optional[datetime.datetime] = None
    finished_at: Optional[datetime.datetime] = None
    logs: List[Dict[str, Any]] = []
    log_total: int = 0


class RelatedStageOut(BaseModel):
    """关联检索步骤（名称与标题由后端提供，前端不写死中文名）。"""

    name: str
    title: str


class RelatedCandidateOut(BaseModel):
    """候选论文：强相关（pending/promoted）才会出现在视图列表里。"""

    id: int
    arxiv_id: str
    title: str
    abstract: str = ""
    authors: List[str] = []
    categories: List[str] = []
    published: Optional[str] = None
    seed_title: str = ""
    match_score: float = 0.0
    summary_text: str = ""
    relevance: str = ""
    relevance_score: float = 0.0
    relevance_reason: str = ""
    output_language: str = "unknown"
    summary_language: str = "unknown"
    query_sources: List[Dict[str, Any]] = []
    processing_error: str = ""
    retryable: bool = False
    status: str = "pending"
    promote_state: str = ""
    promote_error: str = ""
    promoted_paper_id: Optional[int] = None
    library_paper_id: Optional[int] = None
    in_library: bool = False
    created_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


class RelatedViewOut(BaseModel):
    """关联检索视图：步骤清单 + 最新 run + 强相关候选 + 计数。"""

    paper_id: int
    stages: List[RelatedStageOut] = []
    run: Optional[RelatedRunOut] = None
    candidates: List[RelatedCandidateOut] = []
    counts: Dict[str, int] = {}


class RelatedPromoteOut(BaseModel):
    """候选「加入文献库」：后台下载时用 promote_state 轮询。"""

    candidate_id: int
    promote_state: str
    paper_id: Optional[int] = None
    pipeline: Optional[Dict[str, Any]] = None
