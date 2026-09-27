"""SQLAlchemy 引擎、会话与建表初始化。

数据库文件路径由 config_loader 决定（默认 paper_reader/data/library.db）。
SQLite 关闭同线程检查以适配 FastAPI 多线程请求处理。

迁移策略：无 Alembic，采用"建表 + 以加列为主的前向迁移"——`create_all` 负责新表，
`_migrate_additive_columns` 负责给已存在的旧表补新增列（幂等，缺什么补什么）。
"""

import logging
import os
from typing import Generator

from sqlalchemy import create_engine, inspect
from sqlalchemy import text as sql_text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from . import config_loader

logger = logging.getLogger(__name__)

# 未命中任何归档规则的论文落入该系统默认文件夹
INBOX_NAME = "Inbox"

# 前向迁移清单：表 -> [(列名, SQLite 列定义)]；只在列缺失时 ALTER TABLE ADD COLUMN
_ADDITIVE_COLUMNS = {
    "chat_messages": [("reasoning_content", "TEXT NOT NULL DEFAULT ''")],
    "paper_directions": [("relations_at", "DATETIME"),
                         ("relations_count", "INTEGER NOT NULL DEFAULT 0")],
    "folders": [("direction_node_id", "INTEGER")],
    "direction_nodes": [("graph_md_path", "TEXT NOT NULL DEFAULT ''"),
                        ("graph_md_at", "DATETIME")],
    "buffer_papers": [("promote_state", "VARCHAR(16) NOT NULL DEFAULT ''"),
                      ("promote_error", "TEXT NOT NULL DEFAULT ''"),
                      ("promoted_paper_id", "INTEGER")],
}

# 表重命名（只在旧表存在且新表不存在时执行，必须先于 create_all）
_TABLE_RENAMES = {
    "paper_graph_edges": "paper_relations",
}


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
    """迁移 + 建表（幂等）+ 播种默认 Inbox 文件夹与 LLM 设置。

    顺序很重要：先做表重命名，再 create_all（否则会同时留下新旧两张表），
    最后补新增列。
    """
    from . import models, settings_store  # noqa: F401  确保表已注册到 Base.metadata

    _migrate_table_renames()
    Base.metadata.create_all(bind=engine)
    _migrate_additive_columns()

    db = SessionLocal()
    try:
        ensure_inbox(db)
        settings_store.seed_from_config(db)
    finally:
        db.close()


def _migrate_table_renames() -> None:
    """旧表名 -> 新表名（SQLite ALTER TABLE RENAME），保证历史数据不丢。"""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    renamed = []
    with engine.begin() as conn:
        for old, new in _TABLE_RENAMES.items():
            if old in tables and new not in tables:
                conn.execute(sql_text("ALTER TABLE {} RENAME TO {}".format(old, new)))
                renamed.append("{}->{}".format(old, new))
    if renamed:
        logger.info("数据库前向迁移：表重命名 %s", ", ".join(renamed))


def _migrate_additive_columns() -> None:
    """给已存在的旧表补新增列（幂等）；新表由 create_all 直接建好，无需在此处理。"""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    applied = []
    with engine.begin() as conn:
        for table, columns in _ADDITIVE_COLUMNS.items():
            if table not in tables:
                continue
            existing = {col["name"] for col in inspector.get_columns(table)}
            for name, ddl in columns:
                if name in existing:
                    continue
                conn.execute(sql_text("ALTER TABLE {} ADD COLUMN {} {}".format(table, name, ddl)))
                applied.append("{}.{}".format(table, name))
    if applied:
        logger.info("数据库前向迁移：新增列 %s", ", ".join(applied))
