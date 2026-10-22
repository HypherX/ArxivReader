"""对话阅读路由：会话管理 + 流式（SSE）问答。

Agent Harness 接线（本层不含任何 prompt 拼接）：
  - 规则 prompt、论文材料、历史裁剪都由 harness.chat 装配；
  - 普通对话窗口（chat）开放 agent 工具（arXiv 检索 / 网页抓取 / 库内检索 /
    关联图谱 / 本文定位），模型按需调用并在流里下发 tool 帧；
  - 快速总结 / 深度精读的追问窗口不传工具（它们的 prompt 约定只依据论文全文
    与该份产物回答）。三个窗口各用自己的会话，历史不交叉。

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
import threading
from typing import Any, Dict, Iterator, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from harness import agent, chat as harness_chat, config as harness_config, llm as harness_llm
from harness import cancel as cancel_registry
from harness.cancel import Cancelled
from harness.language import choose

from .. import database, schemas, settings_store
from .. import chat_tools as _chat_tools  # noqa: F401  导入即注册对话工具集
from ..models import ChatMessage, ChatSession, Paper
from knowledge import store as knowledge_store

_SCOPES = ("chat", "summary", "deep_reading")
_SCOPE_ARTIFACT = {"summary": knowledge_store.ARTIFACT_SUMMARY, "deep_reading": knowledge_store.ARTIFACT_DEEP}

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])

# 同一会话只允许一轮生成落库。编辑、重新生成或新提问会推进序号，
# 被打断的那一轮即使还在收尾，也不能再把已经丢掉的回复写回来。
_GEN_LOCK = threading.Lock()
_GENERATION: Dict[int, int] = {}


def _bump_generation(session_id: int) -> int:
    with _GEN_LOCK:
        generation = _GENERATION.get(session_id, 0) + 1
        _GENERATION[session_id] = generation
        return generation


def _is_current(session_id: int, generation: int) -> bool:
    with _GEN_LOCK:
        return _GENERATION.get(session_id) == generation

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def _sse(payload: Dict[str, Any]) -> str:
    from ..messages import encode_event
    return encode_event(payload)


def _save_assistant_message(session_id: int, full_text: str, reasoning: str = "", message_id=None,
                            generation: Optional[int] = None,
                            tools: Optional[List[Dict[str, Any]]] = None):
    """用独立会话保存助手回复（流式场景下不依赖请求级会话）。

    tools：本轮已完成的工具调用账本；None 表示不改动已有记录（中间帧），
    传入列表（含空列表）则覆盖写入（最终/终止帧）。
    """
    if not full_text and not reasoning:
        return None
    if generation is not None and not _is_current(session_id, generation):
        return None
    db = database.SessionLocal()
    try:
        with _GEN_LOCK:
            if generation is not None and _GENERATION.get(session_id) != generation:
                return None
            msg = db.get(ChatMessage, message_id) if message_id else None
            if message_id and msg is None:
                return None
            if msg is None:
                msg = ChatMessage(session_id=session_id, role="assistant", content=full_text)
                db.add(msg)
            msg.content = full_text
            msg.reasoning_content = reasoning or ""
            if tools is not None:
                msg.tools_json = json.dumps(tools, ensure_ascii=False)
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
                  should_stop: Optional[Any] = None,
                  generation: Optional[int] = None) -> Iterator[str]:
    """把 harness 的 agent 事件流翻译成 SSE 帧（正文与推理内容双通道）。

    用户点「终止」时（should_stop 命中）：把**已经生成的部分**落库并打上终止标记，
    再下发 cancelled 帧；否则刷新页面后那段文字就丢了。
    """
    collected: List[str] = []
    reasoning_collected: List[str] = []
    tool_records: List[Dict[str, Any]] = []
    message_id, complete = None, False
    cfg = {"output_language": settings.output_language}
    interrupted_note = choose(cfg, "\n\n> ⏹ 本次回复已被终止或连接中断", "\n\n> ⏹ Reply stopped or connection interrupted")
    events = agent.iter_agent_events(settings, messages, tools=tools,
                                     max_rounds=max_rounds, should_stop=should_stop)
    try:
        for event in events:
            if generation is not None and not _is_current(session_id, generation):
                return
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
                                                         "".join(reasoning_collected), message_id, generation)
                    yield _sse({"type": "delta", "text": content, "reasoning": reasoning})
            elif kind == "tool":
                tool_records.append({"name": event.get("name", ""), "ok": bool(event.get("ok")),
                                     "result_chars": int(event.get("result_chars") or 0)})
                yield _sse({"type": "tool", "name": event.get("name", ""),
                            "ok": bool(event.get("ok")), "result_chars": event.get("result_chars", 0)})
            elif kind == "error":
                yield _sse({"type": "error", "message": event.get("message", "调用失败")})
                return
            elif kind == "final":
                full = (event.get("full_content") or event.get("content") or "".join(collected)).strip()
                reasoning_full = (event.get("full_reasoning_content")
                                  or "".join(reasoning_collected)).strip()
                message_id = _save_assistant_message(session_id, full, reasoning_full, message_id, generation,
                                                     tools=tool_records)
                complete = True
                yield _sse({"type": "done", "message_id": message_id, "full_text": full,
                            "reasoning_chars": len(reasoning_full)})
                return
    except Cancelled:
        partial = "".join(collected).strip()
        reasoning_partial = "".join(reasoning_collected).strip()
        message_id = _save_assistant_message(session_id, partial + interrupted_note if partial or reasoning_partial else "",
                                             reasoning_partial, message_id, generation, tools=tool_records)
        complete = True
        logger.info("对话被用户终止（session=%s，已生成 %d 字）", session_id, len(partial))
        yield _sse({"type": "cancelled", "message_id": message_id,
                    "full_text": partial, "chars": len(partial)})
    except Exception as exc:  # noqa: BLE001  兜底：任何未预期异常都要让前端收到 error 帧
        logger.warning("对话流式处理失败：%s", str(exc)[:300])
        yield _sse({"type": "error", "message": str(exc)[:300]})
    finally:
        events.close()
        if (generation is None or _is_current(session_id, generation)) and not complete and (collected or reasoning_collected):
            _save_assistant_message(session_id, "".join(collected).strip() + interrupted_note,
                                    "".join(reasoning_collected).strip(), message_id, generation,
                                    tools=tool_records)


# ---------------- 会话管理 ----------------
def _scope(value: Optional[str]) -> str:
    scope = (value or "chat").strip()
    if scope not in _SCOPES:
        raise HTTPException(status_code=400, detail="未知对话窗口")
    return scope


@router.post("/papers/{paper_id}/sessions", response_model=schemas.SessionOut, status_code=201)
def create_session(paper_id: int, body: Optional[schemas.SessionCreate] = None,
                   db: Session = Depends(database.get_db)):
    if db.get(Paper, paper_id) is None:
        raise HTTPException(status_code=404, detail="论文不存在")
    scope = _scope(body.scope if body else "chat")
    if scope != "chat":
        existing = (db.query(ChatSession)
                    .filter(ChatSession.paper_id == paper_id, ChatSession.scope == scope)
                    .order_by(ChatSession.id.desc())
                    .first())
        if existing is not None:
            return existing
    titles = {
        "summary": ("快速总结", "Quick summary"),
        "deep_reading": ("深度精读", "Deep reading"),
    }
    title = choose(settings_store.get_harness_settings(db), *(titles.get(scope) or ("新对话", "New chat")))
    session = ChatSession(paper_id=paper_id, title=title, scope=scope)
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


@router.get("/papers/{paper_id}/sessions", response_model=List[schemas.SessionOut])
def list_sessions(paper_id: int, scope: str = Query("chat"), db: Session = Depends(database.get_db)):
    """默认只列出普通对话页。总结和精读的追问要通过对应的 scope 单独取。"""
    scope = _scope(scope)
    return (db.query(ChatSession)
            .filter(ChatSession.paper_id == paper_id, ChatSession.scope == scope)
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


def _drop_after(db: Session, session_id: int, message_id: int) -> None:
    """丢掉某一条用户提问之后的回复。当前轮次重新生成时，中断前已经写出的内容不保留。"""
    (db.query(ChatMessage)
     .filter(ChatMessage.session_id == session_id, ChatMessage.id > message_id)
     .delete(synchronize_session=False))


def _stream_reply(session_id: int, user_message_id: int, token: Optional[str], generation: int):
    """从一条已经落库的用户提问继续生成。历史只取这条之前的对话。"""
    db = database.SessionLocal()
    try:
        session = db.get(ChatSession, session_id)
        user_msg = db.get(ChatMessage, user_message_id)
        if session is None or user_msg is None or session.paper is None:
            def _missing() -> Iterator[str]:
                yield _sse({"type": "error", "message": "会话不存在"})
                yield _sse({"type": "done", "message_id": None, "full_text": ""})
            return StreamingResponse(_missing(), media_type="text/event-stream", headers=_SSE_HEADERS)
        paper = session.paper
        content = user_msg.content
        settings = settings_store.get_llm_settings(db)
        if not harness_llm.is_configured(settings):
            def _unconfigured() -> Iterator[str]:
                yield _sse({"type": "error",
                            "message": "尚未配置 LLM（base_url / model / api_key），请先在右上角设置中填写并保存。"})
                yield _sse({"type": "done", "message_id": None, "full_text": ""})
            return StreamingResponse(_unconfigured(), media_type="text/event-stream", headers=_SSE_HEADERS)
        cfg = settings_store.get_harness_settings(db)
        history = [{"role": m.role, "content": m.content}
                   for m in session.messages
                   if m.id < user_message_id and m.role in ("user", "assistant")]
        if session.scope in _SCOPE_ARTIFACT:
            # 追问窗口不开放工具：prompt 约定只依据论文全文与该份产物回答
            artifact = knowledge_store.get_artifact(db, paper.id, _SCOPE_ARTIFACT[session.scope])
            if artifact is None or not (artifact.content_md or "").strip():
                def _missing_artifact() -> Iterator[str]:
                    yield _sse({"type": "error", "message": "还没有可以追问的结果"})
                    yield _sse({"type": "done", "message_id": None, "full_text": ""})
                return StreamingResponse(_missing_artifact(), media_type="text/event-stream", headers=_SSE_HEADERS)
            messages, _info = harness_chat.build_followup_messages(
                paper, history, content, artifact.content_md, scope=session.scope, cfg=cfg)
            tools = []
            rounds = 0
        else:
            messages, _info = harness_chat.build_chat_messages(paper, history, content, cfg=cfg)
            tools = harness_chat.build_chat_tools(paper, cfg)
            rounds = harness_chat.max_tool_rounds(cfg)
    finally:
        db.close()

    def stream() -> Iterator[str]:
        frames = _event_stream(session_id, settings, messages, tools, rounds,
                               should_stop=cancel_registry.watcher(token), generation=generation)
        try:
            yield from frames
        finally:
            frames.close()
            cancel_registry.release(token)

    return StreamingResponse(stream(), media_type="text/event-stream", headers=_SSE_HEADERS)


# ---------------- 发送消息 + 流式回复 ----------------
@router.post("/sessions/{session_id}/messages")
def post_message(session_id: int, body: schemas.ChatRequest,
                 db: Session = Depends(database.get_db)):
    session = db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session.paper is None:
        raise HTTPException(status_code=404, detail="会话关联的论文不存在")
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="消息不能为空")

    generation = _bump_generation(session_id)
    # 保存用户消息；首条用户消息用作会话标题
    prior_user = (db.query(ChatMessage)
                  .filter(ChatMessage.session_id == session_id, ChatMessage.role == "user")
                  .count())
    user_msg = ChatMessage(session_id=session_id, role="user", content=content)
    db.add(user_msg)
    if prior_user == 0:
        session.title = content[:40]
    db.commit()
    db.refresh(user_msg)
    return _stream_reply(session_id, user_msg.id, body.cancel_token, generation)


@router.post("/sessions/{session_id}/messages/{message_id}/edit")
def edit_message(session_id: int, message_id: int, body: schemas.ChatRequest,
                 db: Session = Depends(database.get_db)):
    """改一条用户提问，丢掉它后面这一轮已经生成的内容，再按新内容重新回答。"""
    session = db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    message = db.get(ChatMessage, message_id)
    if message is None or message.session_id != session_id:
        raise HTTPException(status_code=404, detail="消息不存在")
    if message.role != "user":
        raise HTTPException(status_code=400, detail="只能编辑自己发出的提问")
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="消息不能为空")

    generation = _bump_generation(session_id)
    _drop_after(db, session_id, message.id)
    message.content = content
    first_user = (db.query(ChatMessage)
                  .filter(ChatMessage.session_id == session_id, ChatMessage.role == "user")
                  .order_by(ChatMessage.id.asc())
                  .first())
    if first_user is not None and first_user.id == message.id:
        session.title = content[:40]
    db.commit()
    return _stream_reply(session_id, message.id, body.cancel_token, generation)


@router.post("/sessions/{session_id}/regenerate")
def regenerate_message(session_id: int, body: schemas.RegenerateRequest,
                       db: Session = Depends(database.get_db)):
    """当前轮次重新生成：保留最后一条提问，丢掉它后面的回复。"""
    session = db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    last_user = (db.query(ChatMessage)
                 .filter(ChatMessage.session_id == session_id, ChatMessage.role == "user")
                 .order_by(ChatMessage.id.desc())
                 .first())
    if last_user is None:
        raise HTTPException(status_code=400, detail="这页对话还没有可以重新生成的提问")
    generation = _bump_generation(session_id)
    _drop_after(db, session_id, last_user.id)
    db.commit()
    return _stream_reply(session_id, last_user.id, body.cancel_token, generation)