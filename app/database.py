"""SQLAlchemy 引擎、会话与建表初始化。

数据库文件路径由 config_loader 决定（默认 paper_reader/data/library.db）。
SQLite 关闭同线程检查以适配 FastAPI 多线程请求处理。
"""

import os
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from . import config_loader

# 未命中任何归档规则的论文落入该系统默认文件夹
INBOX_NAME = "Inbox"


class Base(DeclarativeBase):
    """所有 ORM 模型的声明式基类。"""


def _make_engine():
    db_path = config_loader.get_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    url = "sqlite:///" + db_path
    return create_engine(
        url,
        connect_args={"check_same_thread": False},
        future=True,
    )


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：每个请求一个会话，结束自动关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_inbox(db: Session) -> "models.Folder":
    """取得（或创建）顶层 Inbox 文件夹，作为未归档论文的兜底位置。"""
    from . import models

    inbox = (
        db.query(models.Folder)
        .filter(models.Folder.name == INBOX_NAME, models.Folder.parent_id.is_(None))
        .first()
    )
    if inbox is None:
        inbox = models.Folder(name=INBOX_NAME, parent_id=None)
        db.add(inbox)
        db.commit()
        db.refresh(inbox)
    return inbox


def init_db() -> None:
    """建表（幂等）+ 播种默认 Inbox 文件夹与 LLM 设置。"""
    from . import models, settings_store  # noqa: F401  确保表已注册到 Base.metadata

    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        ensure_inbox(db)
        settings_store.seed_from_config(db)
    finally:
        db.close()
