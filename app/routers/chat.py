"""对话阅读路由：会话管理 + 流式（SSE）问答。

采用"全文直塞上下文"：把论文全文与历史对话一起发给 LLM，逐块以 SSE 推回增量文本。

SSE 帧统一为 `data: {json}\n\n`，json 内 type ∈ {delta, error, done}：
  delta : {"text": 增量文本}
  error : {"message": 错误信息}
  done  : {"message_id": 助手消息 id, "full_text": 完整回复}

注意：流式生成器内部使用独立的数据库会话保存助手回复，不依赖请求级会话
（FastAPI 对 StreamingResponse 的 yield 依赖清理时机不确定，独立会话最稳妥）。
"""

import json
import logging
from typing import Dict, Iterator, List

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from .. import database, llm_client, schemas, settings_store
from ..models import ChatMessage, ChatSession, Paper

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def _sse(payload: Dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _system_prompt(paper: Paper) -> str:
    authors = ", ".join((paper.authors or [])[:20])
    categories = ", ".join(paper.categories or [])
    body = (paper.full_text or "").strip()
    if not body:
        body = "（未能抽取到论文正文，请主要依据标题与摘要回答，并说明正文缺失。）"
    note = "\n\n注意：论文正文过长已被截断，可能缺少后半部分内容。" if paper.text_truncated else ""
    return (
        "你是一位严谨的学术论文阅读助手。下面提供一篇论文的元信息与正文全文，"
        "请仅依据这些内容、用与提问相同的语言回答用户问题；引用具体内容时尽量指出页码"
        "（正文中以 [Page N] 标注）。若所问内容不在论文中，请如实说明，不要编造。\n\n"
        "标题：{title}\n作者：{authors}\narXiv 分类：{categories}\n发表时间：{published}\n\n"
        "摘要：\n{abstract}\n\n正文全文：\n{body}{note}"
    ).format(
        title=paper.title, authors=authors, categories=categories,
        published=paper.published or "未知", abstract=paper.abstract, body=body, note=note,
    )


def _event_stream(session_id: int, settings: llm_client.LLMSettings,
                  prompt_messages: List[Dict[str, str]]) -> Iterator[str]:
    collected: List[str] = []
    try:
        for piece in llm_client.chat_stream(settings, prompt_messages):
            collected.append(piece)
            yield _sse({"type": "delta", "text": piece})
    except Exception as exc:  # noqa: BLE001  建连或流中断
        logger.warning("对话流式调用失败：%s", str(exc)[:300])
        yield _sse({"type": "error", "message": str(exc)[:300]})
        return

    full = "".join(collected).strip()
    message_id = None
    if full:
        db = database.SessionLocal()
        try:
            msg = ChatMessage(session_id=session_id, role="assistant", content=full)
            db.add(msg)
            db.commit()
            db.refresh(msg)
            message_id = msg.id
        except Exception as exc:  # noqa: BLE001
            logger.warning("保存助手回复失败：%s", exc)
            db.rollback()
        finally:
            db.close()
    yield _sse({"type": "done", "message_id": message_id, "full_text": full})


# ---------------- 会话管理 ----------------
@router.post("/papers/{paper_id}/sessions", response_model=schemas.SessionOut, status_code=201)
def create_session(paper_id: int, db: Session = Depends(database.get_db)):
    if db.get(Paper, paper_id) is None:
        raise HTTPException(status_code=404, detail="论文不存在")
    session = ChatSession(paper_id=paper_id, title="新对话")
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


@router.get("/papers/{paper_id}/sessions", response_model=List[schemas.SessionOut])
def list_sessions(paper_id: int, db: Session = Depends(database.get_db)):
    return (db.query(ChatSession)
            .filter(ChatSession.paper_id == paper_id)
            .order_by(ChatSession.created_at.desc(), ChatSession.id.desc())
            .all())


@router.get("/sessions/{session_id}/messages", response_model=List[schemas.MessageOut])
def list_messages(session_id: int, db: Session = Depends(database.get_db)):
    session = db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session.messages


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(session_id: int, db: Session = Depends(database.get_db)):
    session = db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    db.delete(session)   # 级联删除消息
    db.commit()
    return Response(status_code=204)


# ---------------- 发送消息 + 流式回复 ----------------
@router.post("/sessions/{session_id}/messages")
def post_message(session_id: int, body: schemas.ChatRequest,
                 db: Session = Depends(database.get_db)):
    session = db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    paper = session.paper
    if paper is None:
        raise HTTPException(status_code=404, detail="会话关联的论文不存在")
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="消息不能为空")

    # 保存用户消息；首条用户消息用作会话标题
    prior_user = (db.query(ChatMessage)
                  .filter(ChatMessage.session_id == session_id, ChatMessage.role == "user")
                  .count())
    db.add(ChatMessage(session_id=session_id, role="user", content=content))
    if prior_user == 0:
        session.title = content[:40]
    db.commit()
    db.refresh(session)

    settings = settings_store.get_llm_settings(db)
    if not settings.base_url or not settings.model or not settings.api_key:
        def _unconfigured() -> Iterator[str]:
            yield _sse({"type": "error",
                        "message": "尚未配置 LLM（base_url / model / api_key），请先在右上角设置中填写并保存。"})
            yield _sse({"type": "done", "message_id": None, "full_text": ""})
        return StreamingResponse(_unconfigured(), media_type="text/event-stream", headers=_SSE_HEADERS)

    # 组装 system + 历史（含刚保存的用户消息）
    prompt_messages: List[Dict[str, str]] = [{"role": "system", "content": _system_prompt(paper)}]
    for m in session.messages:
        if m.role in ("user", "assistant"):
            prompt_messages.append({"role": m.role, "content": m.content})

    return StreamingResponse(
        _event_stream(session_id, settings, prompt_messages),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )
