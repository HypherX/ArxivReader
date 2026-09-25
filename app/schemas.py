"""Pydantic 请求/响应模型。

ORM 对象通过 model_config = ConfigDict(from_attributes=True) 直接序列化，
其中 authors/categories/has_text/folder_name 等读取的是模型上的属性。
更新类 schema 全部字段可选，路由用 model_fields_set 判断哪些字段被显式传入，
以区分"设为 null"与"不修改"。
"""

import datetime
from typing import List, Literal, Optional

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


class FolderNode(BaseModel):
    """文件夹树节点（递归）。"""

    id: int
    name: str
    parent_id: Optional[int] = None
    paper_count: int = 0
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


class ChatRequest(BaseModel):
    content: str = Field(..., min_length=1)


# ---------------- Settings ----------------
class LLMSettingsOut(BaseModel):
    base_url: str
    model: str
    temperature: float
    max_tokens: int
    top_p: float
    has_api_key: bool
    api_key_preview: str


class LLMSettingsUpdate(BaseModel):
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None


class TestResult(BaseModel):
    ok: bool
    detail: str = ""
