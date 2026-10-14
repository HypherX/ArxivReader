"""SQLAlchemy 引擎、会话与建表初始化。

数据库文件路径由 harness.config 决定（默认 paper_reader/data/library.db）。
SQLite 关闭同线程检查以适配 FastAPI 多线程请求处理。

迁移策略：无 Alembic，采用"建表 + 以加列为主的前向迁移"——`create_all` 负责新表，
`_migrate_additive_columns` 负责给已存在的旧表补新增列（幂等，缺什么补什么）。
"""

import logging
import os
import sqlite3
from typing import Generator

from sqlalchemy import create_engine, inspect
from sqlalchemy import text as sql_text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from harness import config

logger = logging.getLogger(__name__)

# 前向迁移清单：表 -> [(列名, SQLite 列定义)]；只在列缺失时 ALTER TABLE ADD COLUMN
_ADDITIVE_COLUMNS = {
    "chat_messages": [("reasoning_content", "TEXT NOT NULL DEFAULT ''")],
    "paper_directions": [("relations_at", "DATETIME"),
                         ("relations_signature", "VARCHAR(64) NOT NULL DEFAULT ''"),
                         ("relations_count", "INTEGER NOT NULL DEFAULT 0"),
                         ("output_language", "VARCHAR(8) NOT NULL DEFAULT 'unknown'")],
    "paper_relations": [("output_language", "VARCHAR(8) NOT NULL DEFAULT 'unknown'")],
    "pipeline_runs": [("output_language", "VARCHAR(8) NOT NULL DEFAULT 'unknown'")],
    "related_runs": [("output_language", "VARCHAR(8) NOT NULL DEFAULT 'unknown'")],
    "related_candidates": [("query_sources_json", "TEXT NOT NULL DEFAULT '[]'"),
                           ("processing_error", "TEXT NOT NULL DEFAULT ''"),
                           ("output_language", "VARCHAR(8) NOT NULL DEFAULT 'unknown'")],
    "folders": [("direction_node_id", "INTEGER")],
    "direction_nodes": [("graph_md_path", "TEXT NOT NULL DEFAULT ''"),
                        ("graph_md_at", "DATETIME")],
}

# 撤除的列：全文不再按字符上限截断，papers.text_truncated 成为死字段。
# 必须真的删掉——模型里已经没有它，留着 NOT NULL 又无默认值的列会让 INSERT 失败。
_DROP_COLUMNS = {"papers": ("text_truncated", "placement_source")}

# 撤除的表：arXiv 批量检索（含待读缓冲区）与归档规则整体下线。
_DROP_TABLES = ("filing_rules", "search_runs", "buffer_papers")

# 表重命名（只在旧表存在且新表不存在时执行，必须先于 create_all）
_TABLE_RENAMES = {
    "paper_graph_edges": "paper_relations",
}


class Base(DeclarativeBase):
    """所有 ORM 模型的声明式基类。"""


def _make_engine():
    db_path = config.get_db_path()
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


def init_db() -> None:
    """迁移 + 建表（幂等）：前向补列、撤除已下线功能的表与列。

    顺序很重要：先做表重命名，再 create_all（否则会同时留下新旧两张表），
    最后补新增列。
    """
    from . import models  # noqa: F401  确保表已注册到 Base.metadata

    _backup_before_migrations()
    _migrate_table_renames()
    Base.metadata.create_all(bind=engine)
    _migrate_additive_columns()
    _migrate_drop_columns()
    _migrate_drop_tables()
    _migrate_single_classification()
    _migrate_legacy_relevance()

    db = SessionLocal()
    try:
        _retire_inbox(db)
    finally:
        db.close()


def _retire_inbox(db) -> None:
    """归档规则与 Inbox 下线：原 Inbox 里的论文回到"全部论文"，空文件夹随后删除。"""
    from . import models
    inbox = db.query(models.Folder).filter_by(name="Inbox", parent_id=None).first()
    if inbox is None:
        return
    db.query(models.Paper).filter_by(folder_id=inbox.id).update({"folder_id": None},
                                                                synchronize_session=False)
    db.delete(inbox)
    db.commit()
    logger.info("Inbox 已下线：其中的论文改为只属于「全部论文」")


def _migrate_drop_columns() -> None:
    """撤除已废弃列（幂等）。列在模型里已不存在，留着会让 INSERT 撞上 NOT NULL。"""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in _DROP_COLUMNS.items():
            if table not in tables:
                continue
            existing = {column["name"] for column in inspector.get_columns(table)}
            for column in columns:
                if column not in existing:
                    continue
                conn.execute(sql_text("ALTER TABLE {} DROP COLUMN {}".format(table, column)))
                logger.info("已撤除废弃列：%s.%s", table, column)


def _migrate_drop_tables() -> None:
    """撤除已下线功能的表（幂等）：留着只会让老库多几张没人读的空表。"""
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table in _DROP_TABLES:
            if table in existing:
                conn.execute(sql_text("DROP TABLE IF EXISTS {}".format(table)))
                logger.info("已撤除废弃表：%s", table)


def _migrate_legacy_relevance() -> None:
    """旧 weak 混合了接口失败与低相关，保留原判定并改为可人工重试的待核实。"""
    import json
    from .models import RelatedCandidate
    with SessionLocal() as db:
        for row in db.query(RelatedCandidate).filter_by(relevance="weak").all():
            stats = row.stats or {}
            stats["legacy_verdict"] = {"relevance": row.relevance, "status": row.status,
                                       "reason": row.relevance_reason, "score": row.relevance_score}
            row.summary_stats_json = json.dumps(stats, ensure_ascii=False)
            row.relevance = "uncertain"
            if row.status == "discarded":
                row.status = "pending"
        db.commit()


def _backup_before_migrations() -> None:
    """SQLite 在线备份包含 WAL，失败则阻止迁移，不覆盖首次备份。"""
    path = engine.url.database
    if engine.dialect.name != "sqlite" or not path or path == ":memory:" or not os.path.isfile(path):
        return
    if not inspect(engine).get_table_names():
        return
    backup_path = path + ".before-readable-v1.bak"
    if os.path.exists(backup_path):
        with sqlite3.connect("file:" + backup_path + "?mode=ro", uri=True) as existing:
            if (existing.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                    or not existing.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone()):
                raise RuntimeError("已有迁移备份损坏，请先保留并修复备份文件")
        return
    temporary = backup_path + ".partial"
    with sqlite3.connect(path) as source, sqlite3.connect(temporary) as destination:
        source.backup(destination)
        if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("迁移前备份完整性检查失败，迁移已停止")
    os.replace(temporary, backup_path)
    logger.info("数据库迁移前备份已保存：%s", backup_path)


def _migrate_single_classification() -> None:
    """确定性归并历史多归属；归档原映射，原子完成唯一索引与落位。"""
    from .models import PaperDirection, PaperRelation, Paper, DirectionNode, Folder, Setting
    from knowledge import store
    db = SessionLocal()
    try:
        if db.get(Setting, "migration_single_classification_v1"):
            return
        db.execute(sql_text("CREATE TABLE IF NOT EXISTS paper_directions_archive_v1 AS SELECT * FROM paper_directions"))
        rows = db.query(PaperDirection).order_by(
            sql_text("CASE WHEN role = 'primary' THEN 0 ELSE 1 END"),
            PaperDirection.confidence.desc(), PaperDirection.id.asc()).all()
        keep = {}
        for row in rows:
            if row.paper_id in keep or db.get(Paper, row.paper_id) is None or db.get(DirectionNode, row.node_id) is None:
                db.delete(row)
            else:
                keep[row.paper_id] = row
        db.flush()
        for row in keep.values():
            row.role = "primary"
            paper = db.get(Paper, row.paper_id)
            folder = store.sync_node_folders(db, row.node, commit=False, assign_papers=False)
            paper.folder_id = folder.id
        db.flush()
        for paper in db.query(Paper).join(Folder).filter(Folder.direction_node_id.isnot(None)).all():
            if paper.id not in keep:
                row = PaperDirection(paper_id=paper.id, node_id=paper.folder.direction_node_id, role="primary")
                db.add(row)
                keep[paper.id] = row
        db.flush()
        for edge in db.query(PaperRelation).all():
            endpoints = (keep.get(edge.src_paper_id), keep.get(edge.dst_paper_id))
            if any(d is None or d.node_id != edge.node_id for d in endpoints):
                db.delete(edge)
        store._recount_ids(db, [n.id for n in db.query(DirectionNode).all()])
        db.execute(sql_text("CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_single_direction_idx ON paper_directions(paper_id)"))
        db.add(Setting(key="migration_single_classification_v1", value="done"))
        db.commit()
    except Exception:
        db.rollback()
        raise
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
