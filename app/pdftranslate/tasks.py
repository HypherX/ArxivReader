"""全文翻译任务编排：DB 记账 + 后台线程跑引擎 + 取消与断点恢复。

模式与 knowledge.related 一致：请求线程建行，worker 线程用自己的 DB 会话推进，
所有退出路径都写入明确终态；应用重启后把 running/queued 标记为 interrupted。
"""

import datetime
import json
import logging
import os
import shutil
import threading
import time
from typing import Optional

from sqlalchemy.orm import Session

from .. import database
from ..models import Paper, PdfTranslationTask
from . import engine, env as env_mod
from .settings import (DUAL_MODES, OUTPUT_MODES, SERVICES, get_translation_settings,
                       resolve_engine_credentials)

logger = logging.getLogger(__name__)

# task_id -> threading.Event：取消信号在进程内直传（DB 里的 cancel_requested 只是记账）
_CANCEL_EVENTS = {}
_CANCEL_LOCK = threading.Lock()

TERMINAL_STATES = ("done", "failed", "cancelled", "interrupted")
_MAX_LOG_TAIL = 40


def _utcnow() -> datetime.datetime:
    return datetime.datetime.utcnow()


# ---------------------------------------------------------------- 序列化
def task_payload(task: PdfTranslationTask) -> dict:
    options = task.options
    return {
        "id": task.id,
        "paper_id": task.paper_id,
        "status": task.status,
        "progress": task.progress,
        "message": task.message,
        "options": {k: v for k, v in options.items() if k != "api_key"},
        "has_mono": bool(task.mono_path and os.path.isfile(task.mono_path)),
        "has_dual": bool(task.dual_path and os.path.isfile(task.dual_path)),
        "mono_name": os.path.basename(task.mono_path) if task.mono_path else "",
        "dual_name": os.path.basename(task.dual_path) if task.dual_path else "",
        "error": task.error,
        "log_tail": task.log_tail,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
    }


def latest_task(db: Session, paper_id: int) -> Optional[PdfTranslationTask]:
    return (db.query(PdfTranslationTask)
            .filter_by(paper_id=paper_id)
            .order_by(PdfTranslationTask.id.desc())
            .first())


def running_task(db: Session, paper_id: int) -> Optional[PdfTranslationTask]:
    return (db.query(PdfTranslationTask)
            .filter_by(paper_id=paper_id)
            .filter(PdfTranslationTask.status.in_(("queued", "running")))
            .order_by(PdfTranslationTask.id.desc())
            .first())


def history(db: Session, paper_id: int, limit: int = 10) -> list:
    rows = (db.query(PdfTranslationTask)
            .filter_by(paper_id=paper_id)
            .order_by(PdfTranslationTask.id.desc())
            .limit(limit).all())
    return [task_payload(row) for row in rows]


# ---------------------------------------------------------------- 启动 / 取消
def normalize_options(db: Session, patch: dict) -> dict:
    """合并保存的设置与本次覆盖项，产出任务的完整参数快照。"""
    saved = get_translation_settings(db)
    options = dict(saved)
    for key in ("output_mode", "dual_mode", "lang_in", "lang_out", "qps", "pages",
                "dual_translate_first", "translate_table_text", "ocr_workaround",
                "enhance_compatibility"):
        value = (patch or {}).get(key)
        if value is None:
            continue
        options[key] = value
    if options.get("service") not in SERVICES:
        options["service"] = "openaicompatible"
    if options.get("output_mode") not in OUTPUT_MODES:
        options["output_mode"] = "dual"
    options["dual_mode"] = str(options.get("dual_mode") or "LR").upper()
    if options["dual_mode"] not in DUAL_MODES:
        options["dual_mode"] = "LR"
    try:
        options["qps"] = max(1, min(100, int(options.get("qps") or 4)))
    except (TypeError, ValueError):
        options["qps"] = 4
    pages = str(options.get("pages") or "").strip()
    if pages:
        # 非法页码不能静默当成"翻全文"：全文翻译成本高，必须让用户知道写错了。
        # 消息即 i18n 错误码，路由层会原样透传给前端。
        if not engine.PAGES_RE.match(pages):
            raise ValueError("pdftr.bad_pages")
        bounds = [int(p) for p in pages.split("-")]
        if bounds[0] < 1 or (len(bounds) == 2 and bounds[1] < bounds[0]):
            raise ValueError("pdftr.bad_pages")
    options["pages"] = pages
    # 输出方式 -> pdf2zh_next 的 no_mono / no_dual 开关
    mode = options["output_mode"]
    options["no_mono"] = mode == "dual"
    options["no_dual"] = mode == "mono"
    options.pop("api_key", None)
    return options


def start_task(db: Session, paper: Paper, patch: dict) -> PdfTranslationTask:
    """建行并起后台线程；调用方负责论文存在性与"是否在跑"检查。"""
    if not paper.pdf_path or not os.path.isfile(paper.pdf_path):
        raise ValueError("该论文的 PDF 文件不存在")
    options = normalize_options(db, patch)
    if not env_mod.status()["installed"]:
        raise LookupError("翻译引擎尚未安装")
    task = PdfTranslationTask(paper_id=paper.id, status="queued", progress=0,
                              message="排队中", options_json=json.dumps(options, ensure_ascii=False))
    db.add(task)
    db.commit()
    db.refresh(task)
    event = threading.Event()
    with _CANCEL_LOCK:
        _CANCEL_EVENTS[task.id] = event
    threading.Thread(target=_worker, args=(task.id, event),
                     name="pdftr-task-{}".format(task.id), daemon=True).start()
    return task


def request_cancel(db: Session, task: PdfTranslationTask) -> PdfTranslationTask:
    if task.status in ("queued", "running"):
        task.cancel_requested = True
        db.commit()
        with _CANCEL_LOCK:
            event = _CANCEL_EVENTS.get(task.id)
        if event is not None:
            event.set()
    return task


# ---------------------------------------------------------------- worker
def _worker(task_id: int, cancel_event: threading.Event) -> None:
    db = database.SessionLocal()
    log_lines = []
    last_flush = [0.0]
    try:
        task = db.get(PdfTranslationTask, task_id)
        if task is None:
            return
        paper = db.get(Paper, task.paper_id)
        if paper is None or not paper.pdf_path or not os.path.isfile(paper.pdf_path):
            _finish(db, task, "failed", error="论文或 PDF 文件不存在")
            return
        task.status = "running"
        task.message = "正在初始化"
        db.commit()

        options = task.options
        # 凭据在运行时刻从当前设置解析（含主 LLM 回退），快照里不落 api_key；
        # 服务类型以任务快照为准（默认模型 / 是否免 Key 跟着任务走）
        saved = get_translation_settings(db)
        creds = resolve_engine_credentials(db, dict(saved, service=options["service"]))
        service = SERVICES[options["service"]]
        if service.get("needs_key") and not creds.get("api_key"):
            raise ValueError("翻译服务需要 API Key：请在设置中填写，或改用免费服务（Bing/Google）")

        work_dir = os.path.join(env_mod.output_dir(), "task-{}".format(task_id))

        def progress_cb(percent, message):
            now = time.monotonic()
            if percent is None and now - last_flush[0] < 1.0:
                return
            if percent is not None and now - last_flush[0] < 0.5 and percent < 99:
                return
            last_flush[0] = now
            try:
                with database.SessionLocal() as session:
                    row = session.get(PdfTranslationTask, task_id)
                    if row is None:
                        return
                    if percent is not None:
                        row.progress = max(row.progress, min(100, int(percent)))
                    if message:
                        row.message = str(message)[:200]
                    row.log_tail_json = json.dumps(log_lines[-_MAX_LOG_TAIL:], ensure_ascii=False)
                    session.commit()
            except Exception:  # noqa: BLE001  进度写库失败不打断翻译
                pass

        def log_cb(line):
            line = str(line).rstrip()
            if line:
                log_lines.append(line[-500:])
                del log_lines[:max(0, len(log_lines) - 200)]

        outputs = engine.run_translation(
            paper.pdf_path, options["service"], creds, options, work_dir,
            cancel_event, progress_cb, log_cb)
        with database.SessionLocal() as session:
            row = session.get(PdfTranslationTask, task_id)
            if row is not None:
                row.mono_path = outputs.get("mono_path") or ""
                row.dual_path = outputs.get("dual_path") or ""
                row.progress = 100
                row.log_tail_json = json.dumps(log_lines[-_MAX_LOG_TAIL:], ensure_ascii=False)
                session.commit()
        db.expire_all()
        _finish(db, db.get(PdfTranslationTask, task_id), "done", message="翻译完成")
        logger.info("全文翻译完成：task=%s mono=%s dual=%s",
                    task_id, outputs.get("mono_path"), outputs.get("dual_path"))
    except engine.TranslationCancelled:
        with database.SessionLocal() as session:
            row = session.get(PdfTranslationTask, task_id)
            if row is not None:
                row.log_tail_json = json.dumps(log_lines[-_MAX_LOG_TAIL:], ensure_ascii=False)
                session.commit()
        db.expire_all()
        _finish(db, db.get(PdfTranslationTask, task_id), "cancelled", message="已取消")
    except Exception as exc:  # noqa: BLE001
        logger.warning("全文翻译失败（task=%s）：%s", task_id, str(exc)[:300])
        log_lines.append("ERROR: " + str(exc)[:300])
        with database.SessionLocal() as session:
            row = session.get(PdfTranslationTask, task_id)
            if row is not None:
                row.log_tail_json = json.dumps(log_lines[-_MAX_LOG_TAIL:], ensure_ascii=False)
                session.commit()
        db.expire_all()
        _finish(db, db.get(PdfTranslationTask, task_id), "failed", error=str(exc)[:300])
    finally:
        db.close()
        with _CANCEL_LOCK:
            _CANCEL_EVENTS.pop(task_id, None)


def _finish(db: Session, task: Optional[PdfTranslationTask], status: str,
            message: str = "", error: str = "") -> None:
    if task is None:
        return
    task.status = status
    if message:
        task.message = message
    task.error = error
    task.finished_at = _utcnow()
    db.commit()


def recover_interrupted(db: Session) -> None:
    """启动时把卡在 queued/running 的任务标记为 interrupted（进程已不在，子进程必死）。"""
    rows = (db.query(PdfTranslationTask)
            .filter(PdfTranslationTask.status.in_(("queued", "running"))).all())
    for row in rows:
        row.status = "interrupted"
        row.message = "应用重启，任务中断"
        row.finished_at = _utcnow()
    if rows:
        db.commit()
        logger.info("全文翻译：%d 个未完成任务标记为已中断", len(rows))


def delete_task(db: Session, task: PdfTranslationTask) -> None:
    """删除任务记录与产物目录（不影响论文本体）。"""
    if task.status in ("queued", "running"):
        request_cancel(db, task)
    work_dir = os.path.join(env_mod.output_dir(), "task-{}".format(task.id))
    db.delete(task)
    db.commit()
    shutil.rmtree(work_dir, ignore_errors=True)
