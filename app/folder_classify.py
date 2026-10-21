"""文件夹一键自动分小类：一次 LLM 调用把文件夹当前层的论文按主题分进子文件夹。

设计要点：
  - 后台线程执行，前端轮询状态；同一文件夹同时只允许一个任务（幂等返回进行中的任务）。
  - 只处理直接挂在该文件夹的论文（不递归子文件夹），方向镜像文件夹（🧭）拒绝执行——
    方向树由阅读 pipeline 维护，不走这个入口。
  - 论文以编号列表喂给模型，模型只回编号，杜绝幻觉出库外论文；无法归类的论文留在原地。
  - 子文件夹同名复用、不存在才新建；移动统一走 store.move_paper_to_folder，保持与手动移动同语义。
"""

import logging
import threading
import time
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from harness import llm as harness_llm
from knowledge import llm_steps, store

from . import database, settings_store
from .models import Folder, Paper

logger = logging.getLogger(__name__)

# 喂给模型的单篇摘要长度上限：50 篇 × (标题+400 字符) 仍在常见上下文预算内
_ABSTRACT_LIMIT = 400
_MAX_PAPERS = 80

_LOCK = threading.Lock()
_TASKS: Dict[int, Dict[str, Any]] = {}


class ClassifyError(ValueError):
    """可预期的不满足条件（论文太少、方向文件夹等），code 对应前端 i18n 词条。"""

    def __init__(self, code: str, params: Optional[Dict[str, Any]] = None):
        super().__init__(code)
        self.code = code
        self.params = params or {}


def _task_snapshot(task: Dict[str, Any]) -> Dict[str, Any]:
    return {key: task.get(key) for key in
            ("folder_id", "state", "total", "error", "error_code", "error_params",
             "groups", "moved", "skipped", "started_at")}


def get_task(folder_id: int) -> Optional[Dict[str, Any]]:
    with _LOCK:
        task = _TASKS.get(folder_id)
        return _task_snapshot(task) if task else None


def start_task(folder_id: int) -> Dict[str, Any]:
    """启动（或幂等返回）该文件夹的自动分小类任务。不满足条件抛 ClassifyError。"""
    with _LOCK:
        existing = _TASKS.get(folder_id)
        if existing and existing.get("state") == "running":
            return _task_snapshot(existing)

    db = database.SessionLocal()
    try:
        folder = db.get(Folder, folder_id)
        if folder is None:
            raise ClassifyError("classify.folder_missing")
        if folder.direction_node_id:
            raise ClassifyError("classify.direction_folder")
        settings = settings_store.get_llm_settings(db)
        if not harness_llm.is_configured(settings):
            raise ClassifyError("settings.not_configured")
        papers = (db.query(Paper).filter(Paper.folder_id == folder_id)
                  .order_by(Paper.added_at.asc(), Paper.id.asc()).all())
        if len(papers) < 3:
            raise ClassifyError("classify.too_few")
    finally:
        db.close()

    task: Dict[str, Any] = {"folder_id": folder_id, "state": "running", "total": len(papers),
                            "error": "", "groups": [], "moved": 0, "skipped": 0,
                            "started_at": time.time()}
    with _LOCK:
        _TASKS[folder_id] = task
    threading.Thread(target=_run, args=(folder_id,), daemon=True,
                     name="folder-classify-{}".format(folder_id)).start()
    return _task_snapshot(task)


def _run(folder_id: int) -> None:
    db = database.SessionLocal()
    try:
        folder = db.get(Folder, folder_id)
        papers = (db.query(Paper).filter(Paper.folder_id == folder_id)
                  .order_by(Paper.added_at.asc(), Paper.id.asc()).all())
        if folder is None or len(papers) < 3:
            raise ClassifyError("classify.stale")

        settings = settings_store.get_llm_settings(db)
        result = _classify(db, folder, papers[:_MAX_PAPERS], settings)
        with _LOCK:
            _TASKS[folder_id].update(state="done", groups=result["groups"],
                                     moved=result["moved"], skipped=result["skipped"])
        logger.info("文件夹自动分小类完成（folder=%s）：%s 组 / 移动 %s 篇 / 保留 %s 篇",
                    folder_id, len(result["groups"]), result["moved"], result["skipped"])
    except Exception as exc:  # noqa: BLE001  失败落进任务状态给前端看
        logger.warning("文件夹自动分小类失败（folder=%s）：%s", folder_id, str(exc)[:300])
        code = exc.code if isinstance(exc, ClassifyError) else ""
        params = exc.params if isinstance(exc, ClassifyError) else {}
        with _LOCK:
            _TASKS[folder_id].update(state="failed", error=str(exc)[:300],
                                     error_code=code, error_params=params)
    finally:
        db.close()


def _classify(db: Session, folder: Folder, papers: List[Paper],
              settings) -> Dict[str, Any]:
    lines = []
    for index, paper in enumerate(papers, start=1):
        abstract = (paper.abstract or "").replace("\n", " ")[:_ABSTRACT_LIMIT]
        categories = ", ".join(paper.categories[:3])
        lines.append("{}. 《{}》{}{}".format(
            index, (paper.title or paper.arxiv_id).strip(),
            " [{}]".format(categories) if categories else "",
            "\n   " + abstract if abstract else ""))
    out = llm_steps.call_structured("folder_classify.md", "\n".join(lines), settings)
    if not out.ok or not isinstance(out.data, dict):
        raise ClassifyError("classify.no_groups")

    groups = _normalize_groups(out.data.get("groups") or [], papers)
    if not groups:
        raise ClassifyError("classify.no_groups")

    moved = 0
    created: List[Dict[str, Any]] = []
    assigned = set()
    for group in groups:
        sub = _get_or_create_subfolder(db, folder, group["name"])
        for paper in group["papers"]:
            if paper.id in assigned:
                continue
            store.move_paper_to_folder(db, paper, sub, source="auto_classify", commit=False)
            assigned.add(paper.id)
            moved += 1
        created.append({"name": sub.name, "folder_id": sub.id, "count": len(group["papers"])})
    db.commit()
    return {"groups": created, "moved": moved, "skipped": len(papers) - moved}


def _normalize_groups(raw_groups: Any, papers: List[Paper]) -> List[Dict[str, Any]]:
    """模型输出 -> 干净分组：编号映射回论文、组名合并去重、越界编号丢弃。"""
    if not isinstance(raw_groups, list):
        return []
    by_name: Dict[str, Dict[str, Any]] = {}
    for raw in raw_groups:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip().strip("/").strip()[:40]
        items = raw.get("items")
        if not name or not isinstance(items, list):
            continue
        group = by_name.setdefault(name, {"name": name, "papers": []})
        seen = {p.id for p in group["papers"]}
        for item in items:
            if not isinstance(item, int) or isinstance(item, bool):
                continue
            if 1 <= item <= len(papers) and papers[item - 1].id not in seen:
                group["papers"].append(papers[item - 1])
                seen.add(papers[item - 1].id)
    groups = [g for g in by_name.values() if g["papers"]]
    # 一篇论文被分进多组时，只保留它在第一组的归属（后面的组里剔除）
    claimed = set()
    for group in groups:
        group["papers"] = [p for p in group["papers"]
                           if not (p.id in claimed or claimed.add(p.id))]
    return [g for g in groups if g["papers"]][:8]


def _get_or_create_subfolder(db: Session, parent: Folder, name: str) -> Folder:
    existing = db.query(Folder).filter_by(name=name, parent_id=parent.id).first()
    if existing is not None:
        if existing.direction_node_id:
            raise ClassifyError("classify.name_conflict", {"name": name})
        return existing
    sub = Folder(name=name, parent_id=parent.id)
    db.add(sub)
    db.flush()
    return sub
