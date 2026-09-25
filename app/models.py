"""ORM 数据模型。

表：folders / papers / filing_rules / chat_sessions / chat_messages / settings
authors、categories 以 JSON 文本存储，通过属性辅助方法读写。
"""

import datetime
import json
from typing import List, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.utcnow()


class Folder(Base):
    """自定义分类文件夹，支持通过 parent_id 嵌套成树。"""

    __tablename__ = "folders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("folders.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)

    # 自引用邻接表：parent 为多对一，remote_side 指向主键
    parent: Mapped[Optional["Folder"]] = relationship(
        "Folder", back_populates="children", remote_side=[id]
    )
    children: Mapped[List["Folder"]] = relationship(
        "Folder", back_populates="parent", order_by="Folder.name"
    )
    papers: Mapped[List["Paper"]] = relationship("Paper", back_populates="folder")


class Paper(Base):
    """一篇已入库的论文（当前来源为 ArXiv）。"""

    __tablename__ = "papers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    arxiv_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    authors_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    abstract: Mapped[str] = mapped_column(Text, nullable=False, default="")
    categories_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    published: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    pdf_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pdf_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    folder_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("folders.id"), nullable=True, index=True
    )
    # 抽取的论文全文缓存，供对话阅读直塞上下文；text_truncated 标记是否被截断
    full_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    text_truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    added_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)

    folder: Mapped[Optional["Folder"]] = relationship("Folder", back_populates="papers")
    sessions: Mapped[List["ChatSession"]] = relationship(
        "ChatSession", back_populates="paper", cascade="all, delete-orphan"
    )

    @property
    def authors(self) -> List[str]:
        try:
            return json.loads(self.authors_json or "[]")
        except (ValueError, TypeError):
            return []

    @property
    def categories(self) -> List[str]:
        try:
            return json.loads(self.categories_json or "[]")
        except (ValueError, TypeError):
            return []

    @property
    def has_text(self) -> bool:
        return bool(self.full_text)

    @property
    def text_chars(self) -> int:
        """已缓存全文的字符数，供前端展示“入上下文”规模。"""
        return len(self.full_text or "")

    @property
    def folder_name(self) -> Optional[str]:
        return self.folder.name if self.folder else None


class FilingRule(Base):
    """自动归档规则：命中后把论文放入 folder_id。priority 越小越先匹配。"""

    __tablename__ = "filing_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # category | keyword | author | title
    match_type: Mapped[str] = mapped_column(String(32), nullable=False)
    pattern: Mapped[str] = mapped_column(String(255), nullable=False)
    folder_id: Mapped[Optional[int]] = mapped_column(ForeignKey("folders.id"), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    folder: Mapped[Optional["Folder"]] = relationship("Folder")

    @property
    def folder_name(self) -> Optional[str]:
        return self.folder.name if self.folder else None


class ChatSession(Base):
    """针对某篇论文的一次对话会话。"""

    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    paper_id: Mapped[int] = mapped_column(ForeignKey("papers.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="新对话")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)

    paper: Mapped["Paper"] = relationship("Paper", back_populates="sessions")
    messages: Mapped[List["ChatMessage"]] = relationship(
        "ChatMessage",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="ChatMessage.id",
    )


class ChatMessage(Base):
    """会话中的一条消息。role ∈ {system, user, assistant}。"""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("chat_sessions.id"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)

    session: Mapped["ChatSession"] = relationship("ChatSession", back_populates="messages")


class Setting(Base):
    """运行时可编辑的键值设置（当前存 LLM 端点与采样参数）。"""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
