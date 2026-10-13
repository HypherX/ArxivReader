"""显式请求的显示译文；缓存按原文哈希隔离，不修改分类或关系事实。"""
import hashlib
import json
import threading
from dataclasses import replace

from sqlalchemy.exc import IntegrityError

from app.models import ContentTranslation, DirectionNode, PaperRelation
from harness import llm
from harness.language import PROMPT_VERSION, choose
from . import llm_steps

# 固定分片锁避免同进程重复调用；跨进程写入由数据库唯一约束兜底。
_LOCKS = [threading.Lock() for _ in range(32)]


def source_for(db, kind, target_id):
    if kind == "node":
        row = db.get(DirectionNode, target_id)
        if row is None:
            raise LookupError("方向节点不存在")
        data = {"kind": "category_display_name", "id": row.id, "name": row.name, "path": row.path}
    elif kind == "relation":
        row = db.get(PaperRelation, target_id)
        if row is None:
            raise LookupError("论文关系不存在")
        data = {"kind": "relation_description", "id": row.id,
                "source_paper_id": row.src_paper_id, "target_paper_id": row.dst_paper_id,
                "relation": row.relation, "direction": row.direction,
                "rationale": row.rationale, "evidence": row.evidence}
    else:
        raise ValueError("仅支持 node 或 relation 的显示翻译")
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def source_hash(source):
    return hashlib.sha256((PROMPT_VERSION + "\n" + source).encode("utf-8")).hexdigest()


def cached(db, kind, target_id, language, source):
    return db.query(ContentTranslation).filter_by(
        kind=kind, target_id=target_id, language=language, source_hash=source_hash(source)).first()


def node_aliases(db, language):
    """列表读取只使用已请求的译文，绝不隐式启动模型调用。"""
    nodes = {n.id: n for n in db.query(DirectionNode).all()}
    aliases = {}
    for row in db.query(ContentTranslation).filter_by(kind="node", language=language).all():
        if row.target_id in nodes and row.source_hash == source_hash(source_for(db, "node", row.target_id)):
            aliases[row.target_id] = row.content_md
    return aliases


def translate(db, kind, target_id, settings, language=None):
    language = language or settings.output_language
    if language not in ("zh", "en"):
        raise ValueError("输出语言必须为 zh 或 en")
    settings = replace(settings, output_language=language)
    lock = _LOCKS[hash((kind, target_id, language)) % len(_LOCKS)]
    with lock:
        db.expire_all()
        source = source_for(db, kind, target_id)
        row = cached(db, kind, target_id, language, source)
        reused = row is not None
        if row is None:
            if not llm.is_configured(settings):
                raise ValueError("尚未配置 LLM，请在设置中填写并保存")
            out = llm_steps.call_structured("translate", source, settings, as_json=False)
            if not out.ok or not out.text.strip():
                raise RuntimeError(out.error or choose({"output_language": language}, "翻译结果为空", "Empty translation"))
            content = out.text.strip()
            if kind == "node" and (len(content) > 200 or "\n" in content):
                raise RuntimeError("显示别名必须是 200 字符以内的单行名称")
            db.expire_all()
            if source_for(db, kind, target_id) != source:
                raise ValueError("原文在翻译期间发生变化，请重试")
            row = ContentTranslation(kind=kind, target_id=target_id, language=language,
                                     source_hash=source_hash(source), content_md=content)
            db.add(row)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                row = cached(db, kind, target_id, language, source)
                if row is None:
                    raise
                reused = True
        return {"id": row.id, "kind": kind, "target_id": target_id, "output_language": language,
                "content_md": row.content_md, "cached": reused, "source_hash": row.source_hash}
