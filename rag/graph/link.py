"""查询期实体链接：query → 实体 ID。

两路混合，和检索层用 RRF 融合的思路一致：
- 词典路：实体名/别名在 query 里字面出现 → 高置信，直接命中。
  中文里实体名几乎总是原样出现（"技术部员工的年假"），这条路准确率最高。
- 向量路：query embedding 与实体名 embedding 的余弦，兜住"研发部门 vs 技术部"
  这类换说法，以及 query 里没写实体名的情况。

只走词典会漏换说法，只走向量会把"年假"链接到"请假"再扩到无关文档。
两路合并后按分数去重，词典命中的一律排在前面。
"""
import asyncio
import logging
import time

from sqlalchemy import Float, Integer, String, select
from sqlalchemy import text as sa_text

from models.graph_entity import GraphEntity
from rag.embeddings import embed_query
from rag.graph.config import KG_LINK_MIN_SCORE, KG_LINK_STOPWORDS, KG_LINK_TOP_N
from rag.graph.store import entity_names

logger = logging.getLogger(__name__)

# 名字太短必然误匹配（"部"会命中一切），中文实体名最短两个字
_MIN_NAME_LEN = 2

_VECTOR_LINK_SQL = sa_text("""
SELECT id, canonical_name, type, 1 - (name_embedding <=> CAST(:vec AS vector)) AS score
FROM graph_entities
WHERE status = 'active'
  AND name_embedding IS NOT NULL
ORDER BY name_embedding <=> CAST(:vec AS vector)
LIMIT :n
""").columns(id=Integer, canonical_name=String, type=String, score=Float)


def _dict_hits(query: str, names: list[dict]) -> list[dict]:
    """字面命中。长名字优先 —— "年假制度"命中时不该同时算上"年假"这条更模糊的。

    泛实体（员工/公司/部门…）在这里滤掉：它们几乎出现在每个 query 里，
    一旦作为种子，扩出来的是"该实体的全部证据"，与问题无关。
    """
    q = (query or "").strip()
    if not q:
        return []
    hits: list[dict] = []
    for item in names:
        if item["name"] in KG_LINK_STOPWORDS:
            continue
        terms = [item["name"], *item["aliases"]]
        matched = [
            t for t in terms
            if len(t) >= _MIN_NAME_LEN and t in q and t not in KG_LINK_STOPWORDS
        ]
        if not matched:
            continue
        # 命中词越长，置信度越高；上限 0.99 让位给精确相等
        best = max(len(t) for t in matched)
        score = 0.99 if item["name"] in q else min(0.97, 0.9 + best * 0.01)
        hits.append({
            "id": item["id"], "name": item["name"], "type": item["type"],
            "score": round(score, 4), "via": "dict", "matched": max(matched, key=len),
        })
    return hits


async def _vector_hits(db, query: str, top_n: int) -> list[dict]:
    vec = await asyncio.to_thread(embed_query, query)
    rows = (await db.execute(
        _VECTOR_LINK_SQL, {"vec": vec, "n": max(1, top_n)}
    )).all()
    out = []
    for eid, name, etype, score in rows:
        if float(score) < KG_LINK_MIN_SCORE:
            continue
        if name in KG_LINK_STOPWORDS:
            continue
        out.append({
            "id": eid, "name": name, "type": etype,
            "score": round(float(score), 4), "via": "vector", "matched": name,
        })
    return out


async def link_query(db, query: str, top_n: int = KG_LINK_TOP_N) -> list[dict]:
    """返回按置信度排序的实体列表，失败返回空（调用方按"图通道无候选"处理）。

    fail-open：链接失败绝不能让整个检索失败，向量 + BM25 两路仍然要正常工作。
    """
    started = time.perf_counter()
    q = (query or "").strip()
    if not q:
        return []
    try:
        names = await entity_names(db)
        if not names:
            logger.info("graph.link query=%s skip=empty_registry", q[:40])
            return []
        dict_hits = _dict_hits(q, names)
        vector_hits = await _vector_hits(db, q, top_n)
    except Exception:
        logger.exception("graph.link_failed query=%s 图通道本轮跳过", q[:40])
        return []

    merged: dict[int, dict] = {}
    for hit in sorted(dict_hits, key=lambda h: h["score"], reverse=True):
        merged.setdefault(hit["id"], hit)
    for hit in sorted(vector_hits, key=lambda h: h["score"], reverse=True):
        merged.setdefault(hit["id"], hit)

    result = sorted(merged.values(), key=lambda h: h["score"], reverse=True)[:top_n]
    logger.info(
        "graph.link query=%s dict=%d vector=%d merged=%d top=%s elapsed_ms=%.1f",
        q[:40], len(dict_hits), len(vector_hits), len(result),
        [(h["name"], h["via"], h["score"]) for h in result[:3]],
        (time.perf_counter() - started) * 1000,
    )
    return result
