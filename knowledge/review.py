"""手动文件夹综述：不可变子树快照、全材料分片、证据校验与持久化续跑。"""

import datetime
import hashlib
import json
import re
import threading
from dataclasses import replace
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app import database, settings_store
from app.models import Folder, FolderReview
from harness import cancel, tokens
from harness.language import PROMPT_VERSION, choose, normalize_language
from harness.skills.base import load_prompt
from . import llm_steps, store

MAP_BUDGET = 9000
REPORT_BUDGET = 18000


class ReviewStartError(RuntimeError):
    """后台执行器未启动，失败记录可从历史列表续跑。"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def split_text(text: str, budget: int) -> List[str]:
    """无损分片；按估算 token 二分定位，不丢弃长段落的尾部。"""
    if budget < 1:
        raise ValueError("材料分片预算必须大于零")
    result = []
    while text:
        if tokens.estimate_tokens(text) <= budget:
            result.append(text)
            break
        lo, hi = 1, min(len(text), budget * 4)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if tokens.estimate_tokens(text[:mid]) <= budget:
                lo = mid
            else:
                hi = mid - 1
        boundary = text.rfind("\n", max(0, lo // 2), lo)
        end = boundary + 1 if boundary >= 0 else lo
        result.append(text[:end])
        text = text[end:]
    return result or [""]


def _batches(items: List[dict], budget: int, language: str = "zh") -> List[List[dict]]:
    groups, current = [], []
    for item in items:
        if current and tokens.estimate_tokens(_json(current + [item])) > budget:
            groups.append(current)
            current = []
        if tokens.estimate_tokens(_json([item])) > budget:
            raise ValueError(choose({"output_language": language}, "单份材料元信息超出批次预算，请增大上下文预算", "Item metadata exceeds the batch budget; increase the context budget"))
        current.append(item)
    if current:
        groups.append(current)
    return groups


def snapshot_folder(db: Session, folder_id: int) -> tuple:
    folder = db.get(Folder, folder_id)
    if folder is None:
        raise ValueError(choose({"output_language": settings_store.get_output_language(db)}, "文件夹不存在", "Folder not found"))
    snapshot = []
    for paper in store.folder_papers(db, folder_id):
        notes = [{"kind": a.kind, "content_md": a.content_md, "stats": a.stats}
                 for a in paper.artifacts if a.content_md]
        source = "notes" if notes else ("full_text" if paper.full_text else "abstract_only")
        snapshot.append({"paper_id": paper.id, "arxiv_id": paper.arxiv_id,
                         "title": paper.title, "year": (paper.published or "")[:4],
                         "authors": paper.authors, "abstract": paper.abstract,
                         "notes": notes, "source_text": "" if notes else (paper.full_text or ""),
                         "coverage": source,
                         "folder_id": paper.folder_id})
    return store.folder_path(db, folder), snapshot


def _parts(snapshot: List[dict], budget: int) -> List[dict]:
    parts = []
    for paper in snapshot:
        material = _json({k: paper[k] for k in ("abstract", "notes", "source_text")})
        chunks = split_text(material, max(256, budget - 1500))
        for index, chunk in enumerate(chunks):
            parts.append({"id": "{}:{}".format(paper["paper_id"], index),
                          "paper_id": paper["paper_id"], "title": paper["title"],
                          "year": paper["year"], "coverage": paper["coverage"],
                          "part": index + 1, "parts": len(chunks), "material": chunk})
    return parts


def preview(db: Session, folder_id: int) -> Dict[str, Any]:
    path, snapshot = snapshot_folder(db, folder_id)
    groups = _batches(_parts(snapshot, MAP_BUDGET), MAP_BUDGET, settings_store.get_output_language(db))
    coverage = {kind: sum(p["coverage"] == kind for p in snapshot)
                for kind in ("notes", "full_text", "abstract_only")}
    return {"folder_id": folder_id, "path": path, "paper_count": len(snapshot),
            "coverage": coverage,
            "estimated_evidence_batches": len(groups), "estimated_min_calls": len(groups) + 1,
            "papers": [{k: p[k] for k in ("paper_id", "title", "year", "coverage")} for p in snapshot]}


def create_review(db: Session, folder_id: int, *, title: str = "", focus: str = "",
                  language: Optional[str] = None, map_budget: int = MAP_BUDGET,
                  report_budget: int = REPORT_BUDGET) -> FolderReview:
    language = normalize_language(language or settings_store.get_output_language(db))
    cfg = {"output_language": language}
    path, snapshot = snapshot_folder(db, folder_id)
    if not snapshot:
        raise ValueError(choose(cfg, "文件夹及其子目录没有论文", "No papers in this folder or its descendants"))
    reserve = tokens.estimate_tokens(focus + title + path) + 512
    map_input = map_budget - reserve - tokens.estimate_tokens(load_prompt("review_evidence", language))
    report_input = report_budget - reserve - max(tokens.estimate_tokens(load_prompt(p, language))
                                               for p in ("review_reduce", "field_synthesis"))
    if min(map_input, report_input) < 512:
        raise ValueError(choose(cfg, "关注问题或提示词超出上下文预算，请缩短输入或提高预算", "Focus or prompts exceed the context budget; shorten the input or increase the budget"))
    groups = _batches(_parts(snapshot, map_input), map_input, language)
    row = FolderReview(folder_id=folder_id, folder_path=path,
                       title=title.strip() or path + (" 文献综述" if language == "zh" else " Literature Review"),
                       focus=focus.strip(), output_language=language, prompt_version=PROMPT_VERSION,
                       snapshot_json=_json(snapshot), batches_json=_json([
                           {"index": i, "input": group, "status": "pending", "cards": [], "error": ""}
                           for i, group in enumerate(groups)]),
                       params_json=_json({"map_budget": map_budget, "report_budget": report_budget,
                                                                 "report_input_budget": report_input}),
                       status="queued")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def payload(row: FolderReview, *, content: bool = False) -> Dict[str, Any]:
    damaged = False
    try:
        batches = json.loads(row.batches_json)
        snapshot = json.loads(row.snapshot_json)
        usage, params = json.loads(row.usage_json), json.loads(row.params_json)
        if (not isinstance(batches, list) or not isinstance(snapshot, list)
                or not isinstance(usage, dict) or not isinstance(params, dict)):
            raise ValueError("Invalid persisted data")
        for p in snapshot:
            for key in ("paper_id", "title", "arxiv_id", "coverage"):
                p[key]
        done = [b for b in batches if b["status"] == "done"]
        for batch in batches:
            batch["index"]
            if batch in done:
                _validate_cards({"cards": batch["cards"]}, batch, row.output_language)
        covered_parts = {card["id"] for b in done for card in b["cards"]}
        required = {p["paper_id"]: set() for p in snapshot}
        for batch in batches:
            for part in batch["input"]:
                required[part["paper_id"]].add(part["id"])
    except (ValueError, TypeError, KeyError):
        batches, snapshot, done, required, covered_parts = [], [], [], {}, set()
        usage, params, damaged = {}, {}, True
    result = {"id": row.id, "folder_id": row.folder_id, "folder_path": row.folder_path,
              "title": row.title, "focus": row.focus, "output_language": row.output_language,
              "prompt_version": row.prompt_version, "status": row.status, "error": row.error,
              "paper_count": len(snapshot), "covered_papers": sum(bool(ids) and ids <= covered_parts for ids in required.values()),
              "completed_batches": len(done), "total_batches": len(batches),
              "batches": [{"index": b["index"], "status": b["status"], "error": b.get("error", "")} for b in batches],
              "usage": usage, "params": params,
              "created_at": store._iso(row.created_at), "updated_at": store._iso(row.updated_at),
              "finished_at": store._iso(row.finished_at)}
    legacy = row.prompt_version == "legacy"
    result["legacy"] = legacy
    result["coverage_known"] = not legacy and not damaged
    if damaged:
        result["covered_papers"] = None
        result["error"] = row.error or choose({"output_language": row.output_language}, "持久化材料损坏，无法验证覆盖；原始记录已保留", "Saved materials are damaged; coverage cannot be verified and originals are preserved")
    if legacy:
        result["paper_count"] = result["params"].get("paper_count", 0)
        result["covered_papers"] = None
    if content:
        result["content_md"] = row.content_md
        result["papers"] = [{k: p[k] for k in ("paper_id", "title", "arxiv_id", "coverage")} for p in snapshot]
    return result


def _validate_cards(data: Any, batch: dict, language: str = "zh") -> List[dict]:
    cfg = {"output_language": language}
    cards = data.get("cards") if isinstance(data, dict) else None
    if not isinstance(cards, list) or len(cards) != len(batch["input"]):
        raise ValueError(choose(cfg, "证据卡数量不匹配，批次未完整覆盖", "Evidence card count does not match; batch coverage is incomplete"))
    expected = {p["id"]: p["paper_id"] for p in batch["input"]}
    seen = set()
    for card in cards:
        if (not isinstance(card, dict) or card.get("id") not in expected
                or card["id"] in seen or card.get("paper_id") != expected[card["id"]]
                or not isinstance(card.get("content_md"), str) or not card["content_md"].strip()):
            raise ValueError(choose(cfg, "证据卡 ID、论文映射或正文非法", "Invalid evidence card ID, paper mapping or content"))
        seen.add(card["id"])
    return cards


def _escape(text: str) -> str:
    return re.sub(r"([\\`*_{}\[\]<>])", r"\\\1", str(text)).replace("\n", " ")


def _strip_duplicate_h1(body: str, title: str) -> str:
    """模型常在正文开头重复报告标题（intro 已带 H1），相同或互为子串时去掉。"""
    if not body.startswith("# "):
        return body
    first, _, rest = body.partition("\n")
    norm = lambda s: re.sub(r"\s+", "", s).lstrip("#").strip().lower()
    head, want = norm(first), norm(title)
    if head and want and (head in want or want in head):
        return rest.lstrip("\n")
    return body


def run_review(db: Session, row: FolderReview, settings: Any, *, should_stop: Any = None) -> FolderReview:
    """复用已成功批次；任何漏项、失败或取消都不能标记为完整成功。"""
    settings = replace(settings, output_language=row.output_language)
    cfg = {"output_language": row.output_language}
    batches = json.loads(row.batches_json)
    snapshot = json.loads(row.snapshot_json)
    reductions = json.loads(row.reductions_json)
    params = json.loads(row.params_json)
    usages = json.loads(row.usage_json)
    row.status, row.error, row.finished_at = "running", "", None
    params["model"] = settings.model
    row.params_json = _json(params)
    db.commit()
    token = "review:{}".format(row.id)
    watcher = should_stop or cancel.watcher(token)

    def check() -> bool:
        db.refresh(row, ["cancel_requested"])
        return bool(row.cancel_requested or watcher())

    def persist() -> None:
        row.batches_json, row.reductions_json = _json(batches), _json(reductions)
        row.usage_json = _json(usages)
        row.updated_at = datetime.datetime.utcnow()
        db.commit()

    def call(prompt: str, data: dict, as_json: bool = True) -> Any:
        if check():
            raise cancel.Cancelled("已取消综述")
        limit = params["map_budget" if prompt == "review_evidence" else "report_budget"]
        if tokens.estimate_tokens(load_prompt(prompt, row.output_language) + _json(data)) > limit:
            raise ValueError(choose(cfg, "完整请求超出上下文预算，未静默截断材料", "The full request exceeds the context budget; materials were not silently truncated"))
        out = llm_steps.call_structured(prompt, _json(data), settings, as_json=as_json,
                                        cfg=cfg, should_stop=check)
        for key, value in out.usage.items():
            usages[key] = usages.get(key, 0) + value
        persist()
        if not out.ok:
            raise RuntimeError(out.error or choose(cfg, "模型调用失败", "Model call failed"))
        if check():
            raise cancel.Cancelled("已取消综述")
        return out.data if as_json else out.text

    try:
        for batch in batches:
            if batch["status"] == "done":
                _validate_cards({"cards": batch["cards"]}, batch, row.output_language)
                continue
            batch["status"], batch["error"] = "running", ""
            persist()
            try:
                data = call("review_evidence", {"papers": batch["input"], "focus": row.focus})
                batch["cards"] = _validate_cards(data, batch, row.output_language)
                batch["status"] = "done"
            except cancel.Cancelled:
                batch["status"] = "pending"
                raise
            except Exception as exc:
                batch["status"], batch["error"] = "failed", str(exc)[:1000]
                raise
            finally:
                persist()
        if payload(row)["covered_papers"] != len(snapshot):
            raise ValueError(choose(cfg, "论文证据覆盖校验失败", "Paper evidence coverage validation failed"))
        evidence = [{"paper_ids": [card["paper_id"]], "content_md": card["content_md"]}
                    for b in batches for card in b["cards"]]
        budget = params.get("report_input_budget", params["report_budget"] - 1024)
        for level in range(12):
            if tokens.estimate_tokens(_json(evidence)) <= budget:
                break
            pieces = [{"paper_ids": e["paper_ids"], "content_md": chunk}
                      for e in evidence for chunk in split_text(e["content_md"], budget // 3)]
            next_level = []
            for group in _batches(pieces, budget, row.output_language):
                ids = sorted({pid for e in group for pid in e["paper_ids"]})
                key = hashlib.sha256(_json(group).encode()).hexdigest()
                cached = next((r for r in reductions if r["key"] == key), None)
                if cached:
                    result = cached["result"]
                else:
                    result = call("review_reduce", {"evidence": group, "paper_ids": ids,
                                                     "target_tokens": max(128, budget // 4), "focus": row.focus})
                    if (not isinstance(result, dict) or sorted(result.get("paper_ids", [])) != ids
                            or not isinstance(result.get("content_md"), str) or not result["content_md"].strip()):
                        raise ValueError(choose(cfg, "分层归纳丢失论文引用映射", "Hierarchical synthesis lost paper citation mappings"))
                    reductions.append({"key": key, "level": level, "result": result})
                    persist()
                next_level.append(result)
            if tokens.estimate_tokens(_json(next_level)) >= tokens.estimate_tokens(_json(evidence)):
                raise ValueError(choose(cfg, "分层归纳未缩小材料规模，请调整模型输出预算后续跑", "Hierarchical synthesis did not reduce the material size; adjust the output budget and resume"))
            evidence = next_level
        if tokens.estimate_tokens(_json(evidence)) > budget:
            raise ValueError(choose(cfg, "材料仍超出最终报告预算，未将部分覆盖标为成功", "Materials still exceed the final report budget; partial coverage was not marked as success"))
        text = call("field_synthesis", {"title": row.title, "focus": row.focus,
                                       "folder_path": row.folder_path, "paper_count": len(snapshot),
                                       "evidence": evidence}, as_json=False)
        if not text or not text.strip():
            raise ValueError(choose(cfg, "综述正文为空", "Review content is empty"))
        unknown_citations = {int(x) for x in re.findall(r"/\?paper=(\d+)", text)} - {p["paper_id"] for p in snapshot}
        if unknown_citations:
            raise ValueError(choose(cfg, "综述引用了快照以外的论文", "Review cites papers outside the input snapshot"))
        coverage = choose(cfg, "材料覆盖", "Material coverage")
        bibliography = choose(cfg, "完整参考文献", "Complete bibliography")
        intro = "# {}\n\n> {}: {}/{} · {}\n\n".format(_escape(row.title), coverage, len(snapshot), len(snapshot), _escape(row.folder_path))
        body = _strip_duplicate_h1(text.strip(), row.title)
        lines = []
        labels = {"notes": choose(cfg, "已有阅读笔记", "Existing reading notes"),
                  "full_text": choose(cfg, "可用全文", "Available full text"),
                  "abstract_only": choose(cfg, "仅摘要", "Abstract only")}
        for p in snapshot:
            source = labels[p["coverage"]]
            lines.append("- [{}]({}) — {} · {} · {}".format(_escape(p["title"]), "/?paper={}".format(p["paper_id"]),
                         p["year"], _escape(p["arxiv_id"]), source))
        row.content_md = intro + body + "\n\n## " + bibliography + "\n\n" + "\n".join(lines)
        row.status = "done"
    except cancel.Cancelled:
        row.status, row.error = "cancelled", choose(cfg, "已取消；已完成批次可复用", "Cancelled; completed batches can be resumed")
    except Exception as exc:
        db.rollback()
        row.status, row.error = "failed", str(exc)[:2000]
    finally:
        row.finished_at = datetime.datetime.utcnow()
        persist()
        cancel.release(token)
    return row


def start_background(review_id: int, settings: Any, *, resume: bool = False) -> None:
    db = database.SessionLocal()
    try:
        allowed = ["failed", "cancelled", "interrupted"] if resume else ["queued"]
        count = db.query(FolderReview).filter(FolderReview.id == review_id,
                    FolderReview.status.in_(allowed)).update(
                    {"status": "running", "cancel_requested": False, "error": "", "finished_at": None}, synchronize_session=False)
        db.commit()
        if not count:
            raise ValueError(choose({"output_language": settings.output_language},
                "报告状态已变化；请刷新历史，明确续跑或创建新版本", "Report state changed; refresh history, then resume or create a new version"))
    finally:
        db.close()
    cancel.release("review:{}".format(review_id))

    def worker() -> None:
        worker_db = database.SessionLocal()
        try:
            row = worker_db.get(FolderReview, review_id)
            if row:
                run_review(worker_db, row, settings)
        except Exception as exc:
            worker_db.rollback()
            row = worker_db.get(FolderReview, review_id)
            if row:
                row.status, row.error = "failed", str(exc)[:2000]
                row.finished_at = datetime.datetime.utcnow()
                worker_db.commit()
        finally:
            cancel.release("review:{}".format(review_id))
            worker_db.close()
    try:
        threading.Thread(target=worker, name="folder-review-{}".format(review_id), daemon=True).start()
    except Exception as exc:
        with database.SessionLocal() as failed_db:
            row = failed_db.get(FolderReview, review_id)
            if row:
                row.status, row.error = "failed", str(exc)[:2000]
                row.finished_at = datetime.datetime.utcnow()
                failed_db.commit()
        raise ReviewStartError("综述执行器启动失败；运行记录已保留，可从历史列表续跑") from exc


def recover_interrupted(db: Session) -> None:
    for row in db.query(FolderReview).filter(FolderReview.status.in_(["queued", "running"])).all():
        row.status = "interrupted"
        row.error = choose({"output_language": row.output_language},
                           "服务重启导致中断，可续跑已保存的批次", "Interrupted by a server restart; saved batches can be resumed")
        row.finished_at = datetime.datetime.utcnow()
    db.commit()
