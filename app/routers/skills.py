"""技能路由：列出可用技能 + 对某篇论文运行技能（批量 / 流式两种取法）。

设计原则：本层零 prompt —— 上下文装配、工具循环、token 账本全部由 harness 完成，
路由只负责"取论文 -> 交给 runner -> 返回结果"，因此新增技能不需要改这里。

两种返回：
  POST /api/papers/{id}/skills/{name}            默认：一次性 JSON（含 output 与 stats）
  POST /api/papers/{id}/skills/{name}?stream=true 流式 SSE：delta（正文 + reasoning）/ tool / done

SSE 帧与对话通道同构：
  delta : {"text": 增量正文, "reasoning": 增量推理内容}
  tool  : {"name": ..., "ok": ..., "result_chars": ...}
  error : {"message": ...}
  done  : {"skill": 技能名, "output": 完整输出, "stats": {...}, "reasoning_chars": n}

之所以不开放"任意 prompt 覆盖"到 HTTP：prompt 改版属于开发行为，应该落到 harness/prompts/ 下的模板文件并配单测，
而不是在运行时开放一个调试口子（避免线上被改坏 prompt 且难以复盘）。
"""

import json
import logging
from typing import Any, Dict, Iterator, List

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from harness import config as harness_config, llm as harness_llm, runner, skills
from harness import cancel as cancel_registry
from harness.cancel import Cancelled

from .. import database, schemas, settings_store
from ..models import Paper

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["skills"])

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def _sse(payload: Dict[str, Any]) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


@router.get("/skills", response_model=List[schemas.SkillOut])
def list_skills():
    """列出已注册技能（harness 注册表的直接映射，新增技能自动出现）。"""
    return [spec.to_dict() for spec in skills.list_skills()]


def _load_paper_or_404(db: Session, paper_id: int) -> Paper:
    paper = db.get(Paper, paper_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="论文不存在")
    return paper


def _skill_event_stream(spec: Any, paper: Paper, settings: Any,
                        cfg: Dict[str, Any], budget: Any,
                        should_stop: Any = None, token: Any = None) -> Iterator[str]:
    """把技能事件流翻译成 SSE 帧（正文 + 推理内容双通道）。

    带 cancel_token 时支持「终止」（见 harness.cancel）：终止后已生成的部分不回滚，
    前端可自行保留已收到的文字。
    """
    try:
        for event in runner.iter_skill_events(spec, paper, settings, cfg=cfg, budget=budget,
                                             should_stop=should_stop):
            kind = event.get("type")
            if kind == "delta":
                content = event.get("content", "") or ""
                reasoning = event.get("reasoning_content", "") or ""
                if content or reasoning:
                    yield _sse({"type": "delta", "text": content, "reasoning": reasoning})
            elif kind == "tool":
                yield _sse({"type": "tool", "name": event.get("name", ""),
                            "ok": bool(event.get("ok")), "result_chars": event.get("result_chars", 0)})
            elif kind == "error":
                yield _sse({"type": "error", "message": event.get("message", "调用失败")})
                return
            elif kind == "final":
                reasoning = event.get("reasoning_content") or ""
                yield _sse({"type": "done", "skill": event.get("skill", spec.name),
                            "output": event.get("output") or "", "stats": event.get("stats") or {},
                            "reasoning_chars": len(reasoning)})
                return
    except Cancelled:
        logger.info("技能被用户终止：%s", getattr(spec, "name", ""))
        yield _sse({"type": "cancelled", "skill": getattr(spec, "name", "")})
    except Exception as exc:  # noqa: BLE001  兜底：让前端收到 error 帧而不是断流
        logger.warning("技能流式处理失败：%s", str(exc)[:300])
        yield _sse({"type": "error", "message": str(exc)[:300]})
    finally:
        cancel_registry.release(token)


@router.post("/papers/{paper_id}/skills/{skill_name}")
def run_skill(paper_id: int, skill_name: str,
              body: schemas.SkillRunRequest = schemas.SkillRunRequest(),
              stream: bool = Query(False, description="true = SSE 流式（含推理内容）"),
              db: Session = Depends(database.get_db)):
    """对指定论文运行一个技能；stream=true 时返回 SSE，否则返回一次性 JSON。"""
    paper = _load_paper_or_404(db, paper_id)
    try:
        spec = skills.get_skill(skill_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    settings = settings_store.get_llm_settings(db)
    if not harness_llm.is_configured(settings):
        raise HTTPException(status_code=400,
                            detail="尚未配置 LLM（base_url / model / api_key），请先在设置中填写并保存。")

    cfg = harness_config.get_harness_config()
    if stream:
        return StreamingResponse(
            _skill_event_stream(spec, paper, settings, cfg, body.budget,
                                should_stop=cancel_registry.watcher(body.cancel_token),
                                token=body.cancel_token),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    result = runner.run_skill(spec, paper, settings, cfg=cfg, budget=body.budget)
    if result.error:
        logger.warning("技能 %s 运行失败（paper=%s）：%s", spec.name, paper_id, result.error)
    return schemas.SkillRunOut(skill=result.skill, output=result.output,
                               error=result.error, stats=result.stats)
