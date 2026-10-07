import asyncio
import json
import logging
import os
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agents.graph import run_agent, stream_agent_events
from config.db_conf import AsyncSessionLocal, get_db
from config.rate_limit import check_or_429, client_ip, make_limiter
from models.chat_message import ChatMessage
from models.chat_session import ChatSession
from models.user import User
from routers.auth import get_current_user
from schemas.chat import ChatMessageOut, ChatSessionOut
from services.memory import (
    CHECKPOINT_MESSAGE_WINDOW,
    delete_thread_checkpoint,
    extract_and_store,
    load_memories,
    thread_id_for,
)
from services.permissions import can_view_all_departments

router = APIRouter(prefix="/chat", tags=["智能问答"])
logger = logging.getLogger(__name__)

CHAT_USER_LIMITER = make_limiter(
    int(os.getenv("RATE_LIMIT_CHAT_MAX", "10")),
    int(os.getenv("RATE_LIMIT_CHAT_WINDOW", "60")),
)

HISTORY_MAX_TURNS = 20
HISTORY_CONTENT_MAX_CHARS = 4000


class ChatAskIn(BaseModel):
    # history 是旧前端回退参数；历史权威在服务端库，不能让它伪造助手发言
    message: str = Field(..., min_length=1, max_length=500, description="用户问题，限长 500 防 token 滥用")
    history: list[dict] = Field(default_factory=list, description="历史窗口，仅回退用，最多 20 条，逐条清洗")
    session_id: int | None = Field(None, description="会话 id；不传自动新建会话")


def _sanitize_history(raw: list) -> list[dict]:
    # 未知 role 一律丢弃，否则客户端可伪造模型发言
    turns: list[dict] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            logger.warning("chat.history_dropped index=%d reason=not_dict", i)
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str) or not content:
            logger.warning("chat.history_dropped index=%d reason=invalid_role_or_content role=%r",
                           i, role if isinstance(role, str) else type(role).__name__)
            continue
        if len(content) > HISTORY_CONTENT_MAX_CHARS:
            logger.warning("chat.history_dropped index=%d reason=content_too_long", i)
            continue
        turns.append({"role": role, "content": content})
    return turns[-HISTORY_MAX_TURNS:]


class ChatSourceOut(BaseModel):
    file_id: int
    filename: str | None = None
    department: str | None = None
    size_bytes: int | None = None
    content_type: str | None = None
    type: str | None = None
    image_path: str | None = None


class ChatDataSourceOut(BaseModel):
    kind: str
    label: str
    department: str | None = None


class ChatAskOut(BaseModel):
    answer: str
    sources: list[ChatSourceOut] = Field(default_factory=list)
    data_sources: list[ChatDataSourceOut] = Field(default_factory=list)
    session_id: int = Field(..., description="本轮会话 id，新会话时为新建的 id")


def _stored_content(content) -> str:
    if isinstance(content, list):
        return json.dumps(content, ensure_ascii=False)
    return content


def _restored_content(content: str):
    try:
        parsed = json.loads(content)
    except Exception:
        return content
    return parsed if isinstance(parsed, list) else content


async def _load_owned_session(session_id: int, user_id: int, db: AsyncSession) -> ChatSession:
    result = await db.execute(select(ChatSession).where(ChatSession.id == session_id))
    session = result.scalar_one_or_none()
    if session is None or session.user_id != user_id:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session


@router.get("/sessions", response_model=list[ChatSessionOut])
async def list_sessions(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (
        await db.execute(
            select(ChatSession)
            .where(ChatSession.user_id == current_user.id)
            .order_by(ChatSession.updated_at.desc(), ChatSession.id.desc())
        )
    ).scalars().all()
    lasts: dict[int, tuple[str, str]] = {}
    if rows:
        last_rows = (
            await db.execute(
                select(ChatMessage.session_id, ChatMessage.role, ChatMessage.content)
                .where(ChatMessage.session_id.in_([s.id for s in rows]))
                .distinct(ChatMessage.session_id)  # PG DISTINCT ON：每个会话取最后一条
                .order_by(ChatMessage.session_id, ChatMessage.id.desc())
            )
        ).all()
        lasts = {r[0]: (r[1], r[2]) for r in last_rows}
    return [
        ChatSessionOut(
            id=s.id,
            user_id=s.user_id,
            title=s.title,
            message_count=s.message_count,
            created_at=s.created_at,
            updated_at=s.updated_at,
            last_message=lasts[s.id][1] if s.id in lasts else None,
            last_role=lasts[s.id][0] if s.id in lasts else None,
        )
        for s in rows
    ]


@router.post("/sessions", response_model=ChatSessionOut, status_code=201)
async def create_session(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    session = ChatSession(user_id=current_user.id)
    db.add(session)
    await db.flush()
    await db.commit()
    return ChatSessionOut(
        id=session.id,
        user_id=session.user_id,
        title=session.title,
        message_count=session.message_count,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # checkpoint 是另一条连接，只能业务 commit 之后再清
    session = await _load_owned_session(session_id, current_user.id, db)
    await db.execute(delete(ChatMessage).where(ChatMessage.session_id == session.id))
    await db.delete(session)
    await db.commit()
    thread_id = thread_id_for(session.id)
    await delete_thread_checkpoint(thread_id)
    logger.info(
        "chat.session_deleted session_id=%s thread_id=%s user_id=%s",
        session.id, thread_id, current_user.id,
    )
    return {"ok": True, "id": session.id}


@router.get("/sessions/{session_id}/messages", response_model=list[ChatMessageOut])
async def list_session_messages(
    session_id: int,
    limit: int = 200,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    session = await _load_owned_session(session_id, current_user.id, db)
    rows = (
        await db.execute(
            select(ChatMessage)
            .where(ChatMessage.session_id == session.id)
            .order_by(ChatMessage.id.asc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return [
        ChatMessageOut(
            id=m.id,
            session_id=m.session_id,
            role=m.role,
            content=_restored_content(m.content),
            sources=m.sources,
            data_sources=m.data_sources,
            created_at=m.created_at,
        )
        for m in rows
    ]


async def _load_server_history(db: AsyncSession, session_id: int) -> list[dict]:
    rows = (
        await db.execute(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.id.desc())
            .limit(CHECKPOINT_MESSAGE_WINDOW)
        )
    ).scalars().all()
    return [
        {"role": m.role, "content": _restored_content(m.content)}
        for m in reversed(list(rows))
        if m.role in ("user", "assistant")
    ]


@router.post("/ask", response_model=ChatAskOut)
async def chat_ask(
    request: Request,
    payload: ChatAskIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await check_or_429(CHAT_USER_LIMITER, f"chat:user:{current_user.id}", action="问答请求", dimension="user", user_id=current_user.id)
    if payload.session_id is not None:
        session = await _load_owned_session(payload.session_id, current_user.id, db)
    else:
        session = ChatSession(user_id=current_user.id)
        db.add(session)
        await db.flush()
    seed_history = await _load_server_history(db, session.id)
    if not seed_history:
        seed_history = _sanitize_history(payload.history or [])
    elif payload.history:
        logger.info(
            "chat.history_ignored user_id=%s session_id=%s reason=server_history_authoritative",
            current_user.id, session.id,
        )

    db.add(ChatMessage(session_id=session.id, role="user", content=_stored_content(payload.message)))
    # 计数必须原子 UPDATE，Python 读改写并发下会丢
    await db.execute(
        update(ChatSession)
        .where(ChatSession.id == session.id)
        .values(message_count=ChatSession.message_count + 1)
    )
    if session.title is None:
        session.title = payload.message.strip()[:30] or None  # 标题=首条用户消息前30字，已设置不覆盖
    await db.commit()

    await db.close()

    has_image_content = any(
        isinstance(item.get("content"), list)
        for item in seed_history
        if isinstance(item, dict)
    ) or isinstance(payload.message, list)
    started = time.perf_counter()
    logger.info(
        "chat.ask start user_id=%s department=%s message_chars=%d history_turns=%d has_image_content=%s",
        current_user.id, current_user.department or "未分配",
        len(payload.message), len(seed_history), has_image_content,
    )
    memory_text = await load_memories(current_user.id)
    try:
        result = await run_agent(
            payload.message,
            history=seed_history,
            user_department=current_user.department or "未分配",
            allow_all_departments=can_view_all_departments(current_user),
            thread_id=thread_id_for(session.id),
            long_term_memory=memory_text,
        )
    except Exception:
        logger.exception(
            "chat.ask failed user_id=%s department=%s message_chars=%d history_turns=%d elapsed_ms=%.1f",
            current_user.id, current_user.department or "未分配",
            len(payload.message), len(seed_history), (time.perf_counter() - started) * 1000,
        )
        raise
    sources = result.get("sources", [])
    data_sources = result.get("data_sources", [])
    db.add(ChatMessage(
        session_id=session.id,
        role="assistant",
        content=_stored_content(result["answer"]),
        sources=sources,
        data_sources=data_sources,
    ))
    await db.execute(
        update(ChatSession)
        .where(ChatSession.id == session.id)
        .values(message_count=ChatSession.message_count + 1)
    )
    await db.commit()
    try:
        await extract_and_store(current_user.id, payload.message, session.id)
    except Exception:
        logger.warning("chat.memory_extract_outer_failed user_id=%s session_id=%s", current_user.id, session.id)
    logger.info(
        "chat.ask done user_id=%s department=%s answer_chars=%d sources=%d data_sources=%d "
        "session_id=%s token_usage=%s elapsed_ms=%.1f",
        current_user.id, current_user.department or "未分配",
        len(result.get("answer") or ""), len(sources), len(data_sources),
        session.id, result.get("usage", "N/A"), (time.perf_counter() - started) * 1000,
    )
    return ChatAskOut(
        answer=result["answer"],
        sources=sources,
        data_sources=data_sources,
        session_id=session.id,
    )


SSE_NEWLINE = chr(10)
# 首 token 前的长等待里发一行 SSE 注释，防代理断空闲连接
SSE_HEARTBEAT_SECONDS = float(os.getenv("SSE_HEARTBEAT_SECONDS", "15"))


async def _persist_assistant_message(session_id: int, acc: dict) -> None:
    from config.db_conf import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            db.add(ChatMessage(
                session_id=session_id,
                role="assistant",
                content=_stored_content(acc.get("answer") or ""),
                sources=acc.get("sources") or [],
                data_sources=acc.get("data_sources") or [],
            ))
            await db.execute(
                update(ChatSession)
                .where(ChatSession.id == session_id)
                .values(message_count=ChatSession.message_count + 1)
            )
            await db.commit()
        logger.info("chat.stream_persisted session_id=%s answer_chars=%d",
                    session_id, len(acc.get("answer") or ""))
    except Exception:
        logger.exception("chat.stream_persist_failed session_id=%s", session_id)


@router.post("/stream")
async def chat_stream(
    request: Request,
    payload: ChatAskIn,
    current_user: User = Depends(get_current_user),
):
    await check_or_429(CHAT_USER_LIMITER, f"chat:user:{current_user.id}", action="问答请求", dimension="user", user_id=current_user.id)
    async with AsyncSessionLocal() as db:
        if payload.session_id is not None:
            session = await _load_owned_session(payload.session_id, current_user.id, db)
        else:
            session = ChatSession(user_id=current_user.id)
            db.add(session)
            await db.flush()
        seed_history = await _load_server_history(db, session.id)
        if not seed_history:
            seed_history = _sanitize_history(payload.history or [])
        db.add(ChatMessage(session_id=session.id, role="user", content=_stored_content(payload.message)))
        await db.execute(
            update(ChatSession)
            .where(ChatSession.id == session.id)
            .values(message_count=ChatSession.message_count + 1)
        )
        if session.title is None:
            session.title = payload.message.strip()[:30] or None
        await db.commit()
        session_id = session.id
    memory_text = await load_memories(current_user.id)

    queue: asyncio.Queue = asyncio.Queue()
    acc: dict = {"answer": "", "sources": [], "data_sources": []}

    async def consume() -> None:
        try:
            async for ev in stream_agent_events(
                payload.message,
                history=seed_history,
                user_department=current_user.department or "未分配",
                allow_all_departments=can_view_all_departments(current_user),
                thread_id=thread_id_for(session_id),
                long_term_memory=memory_text,
            ):
                if ev.get("type") == "token":
                    acc["answer"] += ev.get("text", "")
                elif ev.get("type") == "final":
                    acc["answer"] = ev.get("answer") or acc["answer"]
                    acc["sources"] = ev.get("sources") or []
                    acc["data_sources"] = ev.get("data_sources") or []
                await queue.put(ev)
        except Exception as exc:
            logger.exception("chat.stream_agent_failed session_id=%s", session_id)
            await queue.put({"type": "error", "message": "生成中断，请稍后再试"})
        finally:
            await _persist_assistant_message(session_id, acc)
            await queue.put(None)

    consume_task = asyncio.create_task(consume())

    async def sse():
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=SSE_HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": ping" + SSE_NEWLINE * 2  # SSE 以冒号开头即注释行
                    continue
                if ev is None:
                    break
                yield "data: " + json.dumps(ev, ensure_ascii=False) + SSE_NEWLINE*2
        finally:
            pass

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "X-Session-Id": str(session_id)},
    )
