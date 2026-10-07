import logging
import os
import time
import uuid
from datetime import datetime, timezone

from config.db_conf import LANGGRAPH_DB_URL

logger = logging.getLogger(__name__)


def _user_msg(content: str):
    from langchain_core.messages import HumanMessage

    return HumanMessage(content=content)


def _env_bool(name: str, default: str) -> bool:
    return (os.getenv(name) or default).strip().lower() in ("1", "true", "yes", "on")


USE_CHECKPOINTER = _env_bool("USE_CHECKPOINTER", "true")
MEMORY_ENABLED = _env_bool("MEMORY_ENABLED", "true")
CHECKPOINT_MESSAGE_WINDOW = int(os.getenv("CHECKPOINT_MESSAGE_WINDOW", "20"))
LONG_TERM_MEMORY_LIMIT = int(os.getenv("LONG_TERM_MEMORY_LIMIT", "10"))
MEMORY_EXTRACT_MAX_ITEMS = int(os.getenv("MEMORY_EXTRACT_MAX_ITEMS", "5"))

_pool = None        # psycopg_pool.AsyncConnectionPool，saver/store 共用
_checkpointer = None
_store = None


def thread_config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def thread_id_for(session_id: int) -> str:
    return f"sess-{session_id}"


def get_checkpointer():
    return _checkpointer if USE_CHECKPOINTER else None


def get_store():
    return _store if MEMORY_ENABLED else None


async def delete_thread_checkpoint(thread_id: str) -> None:
    # 业务库与 LangGraph 池无法共享事务：checkpoint 删除失败只留残留
    checkpointer = get_checkpointer()
    if checkpointer is None or not thread_id:
        return
    try:
        await checkpointer.adelete_thread(thread_id)
        logger.info("memory.checkpoint_deleted thread_id=%s", thread_id)
    except Exception as exc:
        logger.warning("memory.checkpoint_delete_failed thread_id=%s error=%s", thread_id, exc)


async def init_memory_runtime() -> None:
    global _pool, _checkpointer, _store
    if not USE_CHECKPOINTER:
        logger.info("memory.checkpointer_disabled use_checkpointer=false")
        return
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.store.postgres.aio import AsyncPostgresStore

    try:
        _pool = AsyncConnectionPool(
            conninfo=LANGGRAPH_DB_URL,
            min_size=1,
            max_size=10,
            open=False,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        )
        await _pool.open()
        if USE_CHECKPOINTER:
            _checkpointer = AsyncPostgresSaver(_pool)
            await _checkpointer.setup()  # 幂等：建 checkpoints 等表，只在启动时一次
        if MEMORY_ENABLED:
            _store = AsyncPostgresStore(_pool)
            await _store.setup()  # 幂等：建 store 等表，只在启动时一次
    except Exception:
        logger.exception("memory.init_failed langgraph 表 setup 失败，fail-fast 退出")
        await close_memory_runtime()
        raise
    logger.info(
        "memory.ready backend=postgres checkpointer=%s store=%s window=%d",
        USE_CHECKPOINTER, MEMORY_ENABLED, CHECKPOINT_MESSAGE_WINDOW,
    )


async def close_memory_runtime() -> None:
    global _pool, _checkpointer, _store
    _checkpointer = None
    if _store is not None:
        try:
            await _store.aclose()
        except Exception:
            logger.exception("memory.store_close_failed")
        _store = None
    if _pool is not None:
        try:
            await _pool.close()
        except Exception:
            logger.exception("memory.pool_close_failed")
        _pool = None


# namespace 必须带 user_id：长期记忆跨用户即事故
_MEMORY_NAMESPACE_PREFIX = "memories"

_EXTRACT_PROMPT = """你是记忆抽取器。只从【用户消息】中抽取用户明确说出的、关于用户自己的稳定事实或偏好，
每条一句话，不超过 100 字。不要从任何助手回答/知识库/工具结果中抽取。
没有值得长期记住的就输出 []。只输出 JSON 数组，不要解释。"""

_CITATION_RE = None


def _hard_filter(text: str) -> bool:
    import re

    global _CITATION_RE
    if _CITATION_RE is None:
        _CITATION_RE = re.compile(r"\[\d+\]")
    return bool(_CITATION_RE.search(text)) or len(text) > 100


def _parse_extract_output(raw: str) -> list[str]:
    import json

    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    return [item.strip() for item in parsed if isinstance(item, str) and item.strip()]


async def load_memories(user_id: int) -> str:
    if not MEMORY_ENABLED:
        return ""
    store = get_store()
    if store is None:
        return ""
    try:
        items = await _fetch_all_memory_items(store, user_id)
    except Exception as exc:
        logger.warning("memory.load_failed user_id=%s error=%s action=skip", user_id, exc)
        return ""
    ranked = sorted(
        items,
        key=lambda it: (
            (it.value or {}).get("seq", 0),
            (it.value or {}).get("created_at", ""),
        ),
        reverse=True,
    )[:LONG_TERM_MEMORY_LIMIT]
    lines = [f"- {(it.value or {}).get('text', '').strip()}" for it in ranked if (it.value or {}).get("text")]
    if not lines:
        return ""
    return (
        "你可能记得关于这位用户的以下信息（供参考，不确定时不要编造；"
        "如有冲突，以靠前的较新条目为准）：\n" + "\n".join(lines)
    )


_MEMORY_PAGE_SIZE = 100
_MEMORY_MAX_PAGES = 100  # 1 万条/用户硬上限，超了停取并告警，防失控循环


async def _fetch_all_memory_items(store, user_id: int) -> list:
    namespace = (_MEMORY_NAMESPACE_PREFIX, str(user_id))
    collected: dict[str, object] = {}
    for page in range(_MEMORY_MAX_PAGES):
        batch = await store.asearch(namespace, limit=_MEMORY_PAGE_SIZE, offset=page * _MEMORY_PAGE_SIZE)
        for it in batch:
            collected[it.key] = it
        if len(batch) < _MEMORY_PAGE_SIZE:
            break
    else:
        logger.warning("memory.fetch_truncated user_id=%s max_pages=%d", user_id, _MEMORY_MAX_PAGES)
    return list(collected.values())


async def extract_and_store(user_id: int, user_message: str, session_id: int) -> None:
    if not MEMORY_ENABLED:
        logger.info("memory.extract_skipped user_id=%s reason=memory_disabled", user_id)
        return
    store = get_store()
    if store is None:
        logger.info("memory.extract_skipped user_id=%s reason=store_unavailable", user_id)
        return
    started = time.monotonic()
    try:
        from agents.graph import get_memory_llm  # 延迟导入避免 agents↔services 循环

        raw = await get_memory_llm().ainvoke([_user_msg(_EXTRACT_PROMPT + "\n\n用户消息：" + user_message)])
        candidates = _parse_extract_output(getattr(raw, "content", "") if not isinstance(raw, str) else raw)
        candidates = [c for c in candidates if not _hard_filter(c)][:MEMORY_EXTRACT_MAX_ITEMS]
        if not candidates:
            return
        namespace = (_MEMORY_NAMESPACE_PREFIX, str(user_id))
        existing = await _fetch_all_memory_items(store, user_id)
        known = {(it.value or {}).get("text", "").strip() for it in existing}
        seq = max(
            (int((it.value or {}).get("seq", 0)) for it in existing), default=0
        ) + 1
        stored = 0
        for item in candidates:
            if item in known:  # 精确去重：只追加不覆盖
                continue
            await store.aput(
                namespace,
                str(uuid.uuid4()),
                {
                    "text": item,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "session_id": session_id,
                    "seq": seq,  # 单调序号，"最新 N 条"的排序权威
                },
            )
            known.add(item)
            seq += 1
            stored += 1
        logger.info(
            "memory.extract user_id=%s session_id=%s extracted=%d stored=%d elapsed_ms=%.1f",
            user_id, session_id, len(candidates), stored, (time.monotonic() - started) * 1000,
        )
    except Exception as exc:
        logger.warning(
            "memory.extract_failed user_id=%s session_id=%s error=%s action=skip",
            user_id, session_id, exc,
        )
