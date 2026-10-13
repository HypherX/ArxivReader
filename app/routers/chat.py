"""对话阅读路由：会话管理 + 流式（SSE）问答。

Agent Harness 接线（本层不含任何 prompt 拼接）：
  - 规则 prompt、论文材料、历史裁剪都由 harness.chat 装配；
  - 回答前模型可按需调用论文工具（search_text / read_section / read_page / get_outline）取证，
    工具走 OpenAI function calling（tools 参数），不写进 prompt。

SSE 帧统一为 `data: {json}\n\n`，json 内 type ∈ {delta, tool, error, cancelled, done}：
  delta     : {"text": 增量正文, "reasoning": 增量推理内容}   # 推理模型下两者都有
  tool      : {"name": 工具名, "ok": 是否成功, "result_chars": 结果规模}
  error     : {"message": 错误信息}
  cancelled : {"full_text": 已生成部分, "chars": 字符数}       # 用户点了「终止」
  done      : {"message_id": 助手消息 id, "full_text": 完整回复, "reasoning_chars": 推理内容长度}
（旧前端只处理 delta/error/done，未知字段会被忽略；推理内容同时落库到 ChatMessage.reasoning_content，
刷新会话时可从 GET /api/sessions/{sid}/messages 重新拿到并折叠展示。）

可终止：请求体带 cancel_token 时，前端可调 POST /api/cancel/{token} 终止本次生成（见 harness.cancel）。

注意：流式生成器内部使用独立的数据库会话保存助手回复，不依赖请求级会话
（FastAPI 对 StreamingResponse 的 yield 依赖清理时机不确定，独立会话最稳妥）。
"""

import json
import logging
from typing import Any, Dict, Iterator, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from harness import agent, chat as harness_chat, config as harness_config, llm as harness_llm
from harness import cancel as cancel_registry
from harness.cancel import Cancelled
from harness.language import choose

from .. import database, schemas, settings_store
from ..models import ChatMessage, ChatSession, Paper

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def _sse(payload: Dict[str, Any]) -> str:
    from ..messages import encode_event
    return encode_event(payload)


def _save_assistant_message(session_id: int, full_text: str, reasoning: str = "", message_id=None):
    """用独立会话保存助手回复（流式场景下不依赖请求级会话）。"""
    if not full_text and not reasoning:
        return None
    db = database.SessionLocal()
    try:
        msg = db.get(ChatMessage, message_id) if message_id else None
        if message_id and msg is None:
            return None
        if msg is None:
            msg = ChatMessage(session_id=session_id, role="assistant", content=full_text)
            db.add(msg)
        msg.content = full_text
        msg.reasoning_content = reasoning or ""
        db.commit()
        db.refresh(msg)
        return msg.id
    except Exception as exc:  # noqa: BLE001
        logger.warning("保存助手回复失败：%s", exc)
        db.rollback()
        return None
    finally:
        db.close()


def _event_stream(session_id: int, settings: harness_llm.LLMSettings,
                  messages: List[Dict[str, Any]], tools: List[Any],
                  max_rounds: int,
                  should_stop: Optional[Any] = None) -> Iterator[str]:
    """把 harness 的 agent 事件流翻译成 SSE 帧（正文与推理内容双通道）。

    用户点「终止」时（should_stop 命中）：把**已经生成的部分**落库并打上终止标记，
    再下发 cancelled 帧；否则刷新页面后那段文字就丢了。
    """
    collected: List[str] = []
    reasoning_collected: List[str] = []
    message_id, complete = None, False
    cfg = {"output_language": settings.output_language}
    interrupted_note = choose(cfg, "\n\n> ⏹ 本次回复已被终止或连接中断", "\n\n> ⏹ Reply stopped or connection interrupted")
    events = agent.iter_agent_events(settings, messages, tools=tools,
                                     max_rounds=max_rounds, should_stop=should_stop)
    try:
        for event in events:
            kind = event.get("type")
            if kind == "delta":
                content = event.get("content", "") or ""
                reasoning = event.get("reasoning_content", "") or ""
                if content:
                    collected.append(content)
                if reasoning:
                    reasoning_collected.append(reasoning)
                if content or reasoning:
                    # 先持久化再发送：客户端断开不应让已经显示的片段丢失。
                    message_id = _save_assistant_message(session_id, "".join(collected) + interrupted_note,
                                                         "".join(reasoning_collected), message_id)
                    yield _sse({"type": "delta", "text": content, "reasoning": reasoning})
            elif kind == "tool":
                yield _sse({"type": "tool", "name": event.get("name", ""),
                            "ok": bool(event.get("ok")), "result_chars": event.get("result_chars", 0)})
            elif kind == "error":
                yield _sse({"type": "error", "message": event.get("message", "调用失败")})
                return
            elif kind == "final":
                full = (event.get("full_content") or event.get("content") or "".join(collected)).strip()
                reasoning_full = (event.get("full_reasoning_content")
                                  or "".join(reasoning_collected)).strip()
                message_id = _save_assistant_message(session_id, full, reasoning_full, message_id)
                complete = True
                yield _sse({"type": "done", "message_id": message_id, "full_text": full,
                            "reasoning_chars": len(reasoning_full)})
                return
    except Cancelled:
        partial = "".join(collected).strip()
        reasoning_partial = "".join(reasoning_collected).strip()
        message_id = _save_assistant_message(session_id, partial + interrupted_note if partial or reasoning_partial else "",
                                             reasoning_partial, message_id)
        complete = True
        logger.info("对话被用户终止（session=%s，已生成 %d 字）", session_id, len(partial))
        yield _sse({"type": "cancelled", "message_id": message_id,
                    "full_text": partial, "chars": len(partial)})
    except Exception as exc:  # noqa: BLE001  兜底：任何未预期异常都要让前端收到 error 帧
        logger.warning("对话流式处理失败：%s", str(exc)[:300])
        yield _sse({"type": "error", "message": str(exc)[:300]})
    finally:
        events.close()
        if not complete and (collected or reasoning_collected):
            _save_assistant_message(session_id, "".join(collected).strip() + interrupted_note,
                                    "".join(reasoning_collected).strip(), message_id)


# ---------------- 会话管理 ----------------
@router.post("/papers/{paper_id}/sessions", response_model=schemas.SessionOut, status_code=201)
def create_session(paper_id: int, db: Session = Depends(database.get_db)):
    if db.get(Paper, paper_id) is None:
        raise HTTPException(status_code=404, detail="论文不存在")
    session = ChatSession(paper_id=paper_id, title=choose(settings_store.get_harness_settings(db), "新对话", "New chat"))
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
    if not harness_llm.is_configured(settings):
        def _unconfigured() -> Iterator[str]:
            yield _sse({"type": "error",
                        "message": "尚未配置 LLM（base_url / model / api_key），请先在右上角设置中填写并保存。"})
            yield _sse({"type": "done", "message_id": None, "full_text": ""})
        return StreamingResponse(_unconfigured(), media_type="text/event-stream", headers=_SSE_HEADERS)

    cfg = settings_store.get_harness_settings(db)
    # 历史不含刚保存的这条（build_chat_messages 会把当前提问单独追加在末尾）
    history = [{"role": m.role, "content": m.content} for m in session.messages[:-1]
               if m.role in ("user", "assistant")]
    messages, _info = harness_chat.build_chat_messages(paper, history, content, cfg=cfg)
    tools = harness_chat.build_chat_tools(paper, cfg)

    token = body.cancel_token

    def stream() -> Iterator[str]:
        frames = _event_stream(session_id, settings, messages, tools,
                               harness_chat.max_tool_rounds(cfg),
                               should_stop=cancel_registry.watcher(token))
        try:
            yield from frames
        finally:
            frames.close()
            cancel_registry.release(token)

    return StreamingResponse(stream(), media_type="text/event-stream", headers=_SSE_HEADERS)