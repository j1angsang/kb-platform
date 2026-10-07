# 阈值 0.9 由标注集标定，换语料必须重标
import asyncio
import logging
import os
import time

logger = logging.getLogger(__name__)

RERANK_ENABLED = (os.getenv("RERANK_ENABLED") or "false").strip().lower() in (
    "1", "true", "yes", "on",
)
RERANK_MODEL = (os.getenv("RERANK_MODEL") or "qwen3.7-text-rerank").strip()
RERANK_TOP_N = int(os.getenv("RERANK_TOP_N", "30"))
RERANK_MIN_SCORE = float(os.getenv("RERANK_MIN_SCORE", "0.9"))
# 冷启动约 2.8s，超时取 8s
RERANK_TIMEOUT_SECONDS = float(os.getenv("RERANK_TIMEOUT_SECONDS", "8"))
if not (0 < RERANK_TIMEOUT_SECONDS < 60):
    raise RuntimeError(
        f"RERANK_TIMEOUT_SECONDS={RERANK_TIMEOUT_SECONDS} 越界：必须在 (0, 60) 区间；"
        "须小于检索链路任何上游超时，否则外层先掐断 → 静默 fail-safe 拒答"
    )
RERANK_INSTRUCT = (
    "Given a web search query, retrieve relevant passages that answer the query."
)


def _preview(text: str, limit: int = 40) -> str:
    return (text or "").replace("\n", " ")[:limit]


def _rerank_sync(query: str, docs: list[str], top_n: int) -> tuple[list[tuple[int, float]], int]:
    """必须显式传 request_timeout：SDK 默认 300s，传 timeout= 会被当请求体"""
    import dashscope  # 延迟导入：开关关闭的进程不加载

    dashscope.api_key = dashscope.api_key or os.getenv("EMBEDDING_API_KEY") or ""
    resp = dashscope.TextReRank.call(
        model=RERANK_MODEL,
        query=query,
        documents=docs,
        top_n=top_n,
        instruct=RERANK_INSTRUCT,
        request_timeout=RERANK_TIMEOUT_SECONDS,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"rerank API status={resp.status_code} code={resp.code} msg={resp.message}")
    pairs = [(int(item["index"]), float(item["relevance_score"])) for item in resp.output["results"]]
    total_tokens = int(getattr(resp, "usage", None).get("total_tokens", 0) or 0) if getattr(resp, "usage", None) else 0
    return pairs, total_tokens


async def rerank(query: str, hits: list[dict], top_n: int = RERANK_TOP_N) -> list[dict]:
    """绝不覆盖 score；image 块不重排、原样返回"""
    started = time.perf_counter()
    text_positions = [i for i, h in enumerate(hits) if h.get("metadata", {}).get("type") != "image"]
    if not text_positions:
        return list(hits)
    docs = [str(hits[i].get("text") or "") for i in text_positions]
    api_top_n = max(1, min(int(top_n), len(docs)))
    fallback_deadline = RERANK_TIMEOUT_SECONDS + 0.5
    try:
        pairs, total_tokens = await asyncio.wait_for(
            asyncio.to_thread(_rerank_sync, query, docs, api_top_n),
            timeout=fallback_deadline,
        )
    except Exception as exc:
        # 重排失败 = 本轮无块过门控必须拒答；退回旧排序会放行 0.93 分的无关内容
        logger.warning(
            "rag.rerank query_preview=%s candidates=%d elapsed_ms=%.1f model=%s "
            "total_tokens=0 fallback=true failsafe=true error=%s:%s",
            _preview(query), len(docs), (time.perf_counter() - started) * 1000, RERANK_MODEL,
            type(exc).__name__, str(exc)[:150],
        )
        return [dict(h, rerank_score=None, rerank_fallback=True) for h in hits]

    scored = {idx: sc for idx, sc in pairs}
    scored_hits: list[dict] = []
    unscored_hits: list[dict] = []
    text_position_set = set(text_positions)
    for local_pos, hit_idx in enumerate(text_positions):
        h = dict(hits[hit_idx])  # 浅拷贝：不污染调用方原 dict
        if local_pos in scored:
            h["rerank_score"] = scored[local_pos]
            scored_hits.append(h)
        else:
            h["rerank_score"] = None
            unscored_hits.append(h)
    scored_hits.sort(key=lambda h: h["rerank_score"], reverse=True)
    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "rag.rerank query_preview=%s candidates=%d returned=%d elapsed_ms=%.1f "
        "model=%s total_tokens=%d fallback=false top=%.4f",
        _preview(query), len(docs), len(scored_hits), elapsed_ms, RERANK_MODEL,
        total_tokens, scored_hits[0]["rerank_score"] if scored_hits else 0.0,
    )
    passthrough = [dict(hits[i], rerank_score=None)
                   for i in range(len(hits)) if i not in text_position_set]
    return [*scored_hits, *unscored_hits, *passthrough]


async def warmup_rerank() -> None:
    started = time.perf_counter()
    await asyncio.to_thread(_rerank_sync, "预热", ["warmup"], 1)
    logger.info("rag.rerank warmup elapsed_ms=%.1f", (time.perf_counter() - started) * 1000)
