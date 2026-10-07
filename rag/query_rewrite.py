# 改写结果只用于检索，不当答案、不进 sources；失败一律 fail-open
import asyncio
import logging
import os
import time

logger = logging.getLogger(__name__)


QUERY_REWRITE_ENABLED = (os.getenv("QUERY_REWRITE_ENABLED") or "false").strip().lower() in (
    "1", "true", "yes", "on",
)
QUERY_REWRITE_MODEL = (os.getenv("QUERY_REWRITE_MODEL") or "").strip()
# 硬超时 4s：冷启动首调 >2s，超时 fail-open 用原 query
QUERY_REWRITE_TIMEOUT_SECONDS = float(os.getenv("QUERY_REWRITE_TIMEOUT_SECONDS", "4"))
QUERY_REWRITE_HISTORY_TURNS = int(os.getenv("QUERY_REWRITE_HISTORY_TURNS", "2"))

_QUERY_SKIP_MIN_LEN = 12
_ANAPHORA_MARKERS = ("这个", "那个", "它", "该", "此", "上面", "刚才", "那",
                     "这些", "那些", "他", "她", "他们", "上面提到的")
_REWRITE_MAX_LEN = 200

_REWRITE_PROMPT = """你是企业知识库的检索查询改写器。根据最近对话，把用户的最新问题改写成一个独立、自足的检索查询。
规则（必须全部遵守）：
1. 只补全指代与省略（例如"这个/那个/它/该/上面/刚才"所指的事物），不得改变用户意图，不得引入用户没说过的实体；
2. 保持与用户相同的语言；
3. 如果问题已经完整、或你不确定该如何补全，就原样输出用户的问题；
4. 输出只有一行查询文本，不要解释、不要引号、不要 markdown。"""

_llm = None


def _get_llm():
    """不复用绑了工具的主模型 get_llm()"""
    global _llm
    if _llm is None:
        from langchain_openai import ChatOpenAI

        model = QUERY_REWRITE_MODEL or os.getenv("LLM_MODEL", "")
        base_url = os.getenv("LLM_BASE_URL", "")
        api_key = os.getenv("LLM_API_KEY", "")
        _llm = ChatOpenAI(
            model=model, api_key=api_key, base_url=base_url,
            temperature=0, timeout=QUERY_REWRITE_TIMEOUT_SECONDS,
            max_retries=0, max_tokens=100,
        )
    return _llm


def _extract_history(messages: list, turns: int) -> str:
    from langchain_core.messages import AIMessage, HumanMessage

    texts: list[str] = []
    for m in messages:
        if isinstance(m, HumanMessage):
            if isinstance(m.content, str) and m.content.strip():
                texts.append(f"用户：{m.content.strip()}")
        elif isinstance(m, AIMessage):
            content = m.content
            if isinstance(content, str) and content.strip():
                texts.append(f"助手：{content.strip()[:200]}")
    keep = max(0, turns) * 2
    return "\n".join(texts[-keep:])


def _has_anaphora(text: str) -> bool:
    return any(marker in text for marker in _ANAPHORA_MARKERS)


async def rewrite_query(user_text: str, history_messages: list | None = None) -> str:
    """返回值只能喂给 store.search，绝不当答案或进注入内容"""
    if not QUERY_REWRITE_ENABLED:
        return user_text
    trimmed = (user_text or "").strip()
    if not trimmed:
        return user_text
    if len(trimmed) >= _QUERY_SKIP_MIN_LEN and not _has_anaphora(trimmed):
        logger.info(
            "rag.rewrite skipped reason=self_sufficient len=%d original=%s",
            len(trimmed), trimmed[:40],
        )
        return user_text

    history = _extract_history(history_messages or [], QUERY_REWRITE_HISTORY_TURNS)
    prompt = (_REWRITE_PROMPT
              + (f"\n\n最近对话：\n{history}" if history else "")
              + f"\n\n用户最新问题：{trimmed}\n改写后的检索查询：")
    started = time.perf_counter()
    try:
        raw = await asyncio.wait_for(
            _get_llm().ainvoke(prompt), timeout=QUERY_REWRITE_TIMEOUT_SECONDS
        )
        rewritten = str(getattr(raw, "content", "") or "").strip().strip('"').strip("'").strip()
    except Exception as exc:
        logger.warning(
            "rag.rewrite fallback=true reason=%s:%s elapsed_ms=%.1f original=%s",
            type(exc).__name__, str(exc)[:120], (time.perf_counter() - started) * 1000,
            trimmed[:40],
        )
        return user_text
    elapsed_ms = (time.perf_counter() - started) * 1000
    if not rewritten or len(rewritten) > _REWRITE_MAX_LEN or "\n" in rewritten:
        logger.warning(
            "rag.rewrite fallback=true reason=bad_output len=%d elapsed_ms=%.1f original=%s",
            len(rewritten), elapsed_ms, trimmed[:40],
        )
        return user_text
    changed = rewritten != trimmed
    usage = getattr(raw, "usage_metadata", None) or {}
    logger.info(
        "rag.rewrite original=%s rewritten=%s changed=%s elapsed_ms=%.1f "
        "tokens=%s fallback=false",
        trimmed[:40], rewritten[:40], changed, elapsed_ms,
        usage.get("total_tokens", 0),
    )
    return rewritten or user_text
