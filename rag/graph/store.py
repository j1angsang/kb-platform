"""图查询层：实体/关系读写、多跳扩展、孤儿清理、人工合并。

最重要的一条：本模块产出的永远是 chunk_id，绝不返回实体内容。
图是地址簿，不是内容库 —— 任何绕过 knowledge_chunks 权限 SQL 的读法都是越权。
"""
import logging
import time

from sqlalchemy import Float, Integer, String, and_, delete, func, or_, select
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from models.graph_entity import GraphEntity
from models.graph_relation import GraphRelation
from models.knowledge_chunk import KnowledgeChunk
from rag.graph.config import (
    KG_MAX_HOPS,
    KG_MIN_CONFIDENCE,
    KG_MIN_MENTION,
)

logger = logging.getLogger(__name__)


def _store_deps():
    """延迟导入 rag.store：rag.store 会在模块级 import 本模块，模块级反向引用会成环。
    权限表达式和 metadata 构造必须复用同一份实现，复制一份必然漂移。"""
    from rag.store import PgVectorStore

    return PgVectorStore


async def upsert_entities(db, items: list[dict]) -> dict[tuple[str, str], int]:
    """按 (canonical_name, type) 幂等写入，返回 {(name, type): id}。

    已存在的实体只累加 mention_count，不覆盖 name_embedding ——
    人工合并过的实体，向量是人工确认过的更可信。
    """
    if not items:
        return {}
    stmt = pg_insert(GraphEntity).values(items)
    stmt = stmt.on_conflict_do_update(
        index_elements=["canonical_name", "type"],
        set_={
            "mention_count": GraphEntity.mention_count + 1,
            "updated_at": func.now(),
            "aliases": func.coalesce(GraphEntity.aliases, "") + stmt.excluded.aliases,
        },
    )
    await db.execute(stmt)

    names = {(i["canonical_name"], i["type"]) for i in items}
    rows = (await db.execute(
        select(GraphEntity.id, GraphEntity.canonical_name, GraphEntity.type).where(
            GraphEntity.canonical_name.in_([n for n, _ in names])
        )
    )).all()
    return {(r[1], r[2]): r[0] for r in rows if (r[1], r[2]) in names}


async def upsert_relations(db, rows: list[dict]) -> int:
    """幂等写入边。同一 chunk 抽到同一条边不会产生重复行。"""
    if not rows:
        return 0
    stmt = pg_insert(GraphRelation).values(rows)
    stmt = stmt.on_conflict_do_nothing(
        constraint="uq_graph_relations_edge",
    ).returning(GraphRelation.id)
    # 不能用 rowcount：批量 INSERT ... ON CONFLICT 时 psycopg 常返回 -1，
    # 用 RETURNING 数实际写入行数，日志和指标才可信
    result = await db.execute(stmt)
    return len(result.all())


async def delete_edges_by_file(db, file_id: int) -> int:
    """抽新一轮前先清该文件的旧边。

    为什么不能只靠 FK CASCADE：chunk_id 是确定性 id（f"{file_id}:{seq}"），
    重新入库时同 id 会被复用，而 upsert_chunks 是 on_conflict_do_update 不是 delete+insert，
    行没被删过，级联就不会触发 —— 旧边会和新边混在一起。
    """
    result = await db.execute(
        delete(GraphRelation).where(
            GraphRelation.chunk_id.in_(
                select(KnowledgeChunk.id).where(KnowledgeChunk.file_id == file_id)
            )
        )
    )
    return result.rowcount or 0


async def entity_names(db) -> list[dict]:
    """实体名录：词典匹配的输入。只有名字/类型/别名，无内容。

    刻意不取 name_embedding：这是每个检索请求都要走的热路径，
    几千个 1152 维向量搬进 Python 只为比余弦，纯属浪费。
    向量匹配交给 entity_vector_match 用 pgvector 的 <=> 在库里算。
    """
    rows = (await db.execute(
        select(
            GraphEntity.id,
            GraphEntity.canonical_name,
            GraphEntity.type,
            GraphEntity.aliases,
        ).where(GraphEntity.status == "active")
    )).all()
    return [
        {
            "id": r[0],
            "name": r[1],
            "type": r[2],
            "aliases": [a.strip() for a in (r[3] or "").split(",") if a.strip()],
        }
        for r in rows
    ]


_NEIGHBOR_SQL = """
WITH RECURSIVE walk AS (
    SELECT r.object_id AS node_id, 1 AS hop, r.chunk_id AS chunk_id, r.confidence AS confidence
    FROM graph_relations r
    WHERE r.subject_id = ANY(CAST(:seeds AS integer[]))
      AND r.object_id IS NOT NULL
      AND r.confidence >= :min_conf
    UNION
    SELECT r.object_id, w.hop + 1, r.chunk_id, LEAST(r.confidence, w.confidence)
    FROM graph_relations r
    JOIN walk w ON r.subject_id = w.node_id
    WHERE w.hop < :max_hops
      AND r.object_id IS NOT NULL
      AND r.confidence >= :min_conf
),
edges AS (
    SELECT chunk_id, hop, confidence FROM walk
    UNION ALL
    SELECT r.chunk_id, 0 AS hop, r.confidence
    FROM graph_relations r
    WHERE (r.subject_id = ANY(CAST(:seeds AS integer[]))
           OR r.object_id = ANY(CAST(:seeds AS integer[])))
      AND r.confidence >= :min_conf
)
SELECT chunk_id, MIN(hop) AS hop, MAX(confidence) AS confidence
FROM edges
GROUP BY chunk_id
ORDER BY hop ASC, confidence DESC
LIMIT :limit
"""


async def neighbor_chunks(
    db,
    entity_ids: list[int],
    department: str | None = None,
    max_hops: int = KG_MAX_HOPS,
    limit: int = 12,
) -> list[dict]:
    """从种子实体出发扩展，返回候选 chunk 的 hit dict（结构与 store.search 一致）。

    权限判定复用 PgVectorStore._permission_clause —— 和向量通道走同一条 SQL，
    保证"图召回的 chunk"和"向量召回的 chunk"受同一套部门规则约束。
    """
    if not entity_ids:
        return []
    started = time.perf_counter()
    PgVectorStore = _store_deps()

    walk = sa_text(_NEIGHBOR_SQL).columns(
        chunk_id=String(64), hop=Integer, confidence=Float
    ).cte("graph_walk")

    stmt = (
        select(KnowledgeChunk, walk.c.hop, walk.c.confidence)
        .join(walk, walk.c.chunk_id == KnowledgeChunk.id)
        .where(KnowledgeChunk.status == "ready")
    )
    # 软删文件的内容不允许经图"死而复生"
    from models.file import FileInfo as _FileInfo

    stmt = stmt.where(
        KnowledgeChunk.file_id.is_(None)
        | KnowledgeChunk.file_id.not_in(
            select(_FileInfo.id).where(_FileInfo.deleted_at.is_not(None))
        )
    )
    if department:
        stmt = stmt.where(PgVectorStore._permission_clause(department))

    rows = (await db.execute(
        stmt,
        {"seeds": entity_ids, "max_hops": max_hops,
         "min_conf": KG_MIN_CONFIDENCE, "limit": limit},
    )).all()

    hits = []
    for row, hop, confidence in rows:
        hits.append({
            "id": row.id,
            "text": row.content,
            "metadata": PgVectorStore._metadata(row),
            "score": round(float(confidence) * (0.9 ** int(hop)), 4),
            "_graph_hop": int(hop),
            "_graph_confidence": round(float(confidence), 4),
        })
    logger.info(
        "graph.neighbor seeds=%d department=%s hops=%d returned=%d min_conf=%.2f elapsed_ms=%.1f",
        len(entity_ids), department or "-all-", max_hops, len(hits),
        KG_MIN_CONFIDENCE, (time.perf_counter() - started) * 1000,
    )
    return hits


async def list_entities(
    db,
    q: str | None = None,
    type: str | None = None,
    limit: int = 30,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """管理端列表：按名字/别名模糊搜索。只返回名字类信息，不含内容。"""
    stmt = select(GraphEntity).where(GraphEntity.status == "active")
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(
            GraphEntity.canonical_name.ilike(like),
            GraphEntity.aliases.ilike(like),
        ))
    if type:
        stmt = stmt.where(GraphEntity.type == type)
    total = int(await db.scalar(
        select(func.count()).select_from(stmt.subquery())
    ) or 0)
    rows = (await db.execute(
        stmt.order_by(GraphEntity.mention_count.desc(), GraphEntity.id.desc())
        .limit(limit).offset(offset)
    )).scalars().all()
    return [
        {
            "id": e.id,
            "name": e.canonical_name,
            "type": e.type,
            "aliases": [a.strip() for a in (e.aliases or "").split(",") if a.strip()],
            "mention_count": e.mention_count,
        }
        for e in rows
    ], total


async def entity_neighbors(db, entity_id: int, limit: int = 40) -> dict:
    """邻域：给前端画图用。只返回实体名 + 谓词 + 证据 chunk_id，不返回 chunk 正文。

    正文要不要给，取决于查看者对该 chunk 的部门权限 —— 那是 routers/graph.py 的事，
    本函数只提供结构。
    """
    entity = (await db.execute(
        select(GraphEntity).where(GraphEntity.id == entity_id)
    )).scalar_one_or_none()
    if entity is None:
        return {"entity": None, "edges": []}

    # outerjoin 不是 inner join：字面值边（人事部 --发放--> "offer"）的 object_id 为 NULL，
    # inner join 会把它们整条丢掉，邻域看起来"少了一半关系"
    out_rows = (await db.execute(
        select(GraphRelation, GraphEntity)
        .outerjoin(GraphEntity, GraphEntity.id == GraphRelation.object_id)
        .where(GraphRelation.subject_id == entity_id)
        .limit(limit)
    )).all()
    in_rows = (await db.execute(
        select(GraphRelation, GraphEntity)
        .join(GraphEntity, GraphEntity.id == GraphRelation.subject_id)
        .where(GraphRelation.object_id == entity_id)
        .limit(limit)
    )).all()

    edges = []
    for rel, other in out_rows:
        edges.append({
            "direction": "out",
            "predicate": rel.predicate,
            "other_id": other.id if other else None,
            "other_name": other.canonical_name if other else None,
            "other_type": other.type if other else None,
            "value": rel.value,
            "chunk_id": rel.chunk_id,
            "confidence": rel.confidence,
        })
    for rel, other in in_rows:
        edges.append({
            "direction": "in",
            "predicate": rel.predicate,
            "other_id": other.id if other else None,
            "other_name": other.canonical_name if other else None,
            "other_type": other.type if other else None,
            "value": None,
            "chunk_id": rel.chunk_id,
            "confidence": rel.confidence,
        })
    return {
        "entity": {
            "id": entity.id,
            "name": entity.canonical_name,
            "type": entity.type,
            "aliases": [a.strip() for a in (entity.aliases or "").split(",") if a.strip()],
            "mention_count": entity.mention_count,
        },
        "edges": edges,
    }


async def merge_entities(db, source_id: int, target_id: int) -> dict:
    """人工合并：source 的所有边改挂到 target，source 标 merged 保留跳转。

    不物理删除 source —— 边上的 chunk_id 是历史证据，删了实体会让边悬空，
    而且合并是可以反悔的（人工判断会错），保留 merged_into 才能追溯。
    """
    if source_id == target_id:
        raise ValueError("不能把实体合并到自己")

    source = (await db.execute(
        select(GraphEntity).where(GraphEntity.id == source_id)
    )).scalar_one_or_none()
    target = (await db.execute(
        select(GraphEntity).where(GraphEntity.id == target_id)
    )).scalar_one_or_none()
    if source is None or target is None:
        raise ValueError("实体不存在")
    if source.type != target.type:
        raise ValueError(f"类型不同不能合并：{source.type} vs {target.type}")

    moved_out = (await db.execute(
        GraphRelation.__table__.update()
        .where(GraphRelation.subject_id == source_id)
        .values(subject_id=target_id)
    )).rowcount or 0
    moved_in = (await db.execute(
        GraphRelation.__table__.update()
        .where(GraphRelation.object_id == source_id)
        .values(object_id=target_id)
    )).rowcount or 0

    merged_aliases = [a.strip() for a in (target.aliases or "").split(",") if a.strip()]
    for alias in [source.canonical_name, *[a.strip() for a in (source.aliases or "").split(",") if a.strip()]]:
        if alias and alias not in merged_aliases:
            merged_aliases.append(alias)

    target.aliases = ",".join(merged_aliases)
    target.mention_count = (target.mention_count or 0) + (source.mention_count or 0)
    source.status = "merged"
    source.merged_into = target_id
    await db.flush()
    logger.info(
        "graph.merge source=%d(%s) target=%d(%s) moved_out=%d moved_in=%d",
        source_id, source.canonical_name, target_id, target.canonical_name,
        moved_out, moved_in,
    )
    return {
        "source_id": source_id,
        "target_id": target_id,
        "moved_edges": moved_out + moved_in,
        "target_aliases": merged_aliases,
    }


async def split_entity(db, entity_id: int, alias: str) -> int:
    """拆分：把一个别名拎出来单独建实体，并把含该别名的边迁过去。

    人工合并会错（"技术部"和"技术支持部"被并），必须留后悔路。
    """
    entity = (await db.execute(
        select(GraphEntity).where(GraphEntity.id == entity_id)
    )).scalar_one_or_none()
    if entity is None:
        raise ValueError("实体不存在")
    alias = (alias or "").strip()
    if not alias:
        raise ValueError("别名不能为空")

    stmt = pg_insert(GraphEntity).values(
        canonical_name=alias, type=entity.type, aliases="",
        mention_count=1, status="active",
    ).on_conflict_do_nothing(index_elements=["canonical_name", "type"])
    await db.execute(stmt)
    new_id = await db.scalar(
        select(GraphEntity.id).where(
            GraphEntity.canonical_name == alias, GraphEntity.type == entity.type
        )
    )
    # 关键：合并时源实体是标 merged 保留的，拆分必须把它恢复成 active 并断掉 merged_into，
    # 否则 insert 被 on_conflict_do_nothing 挡掉、查回来的还是那条 merged 行 ——
    # 表面返回了 id，实际什么也没拆开（这个 bug 实测踩过）
    revived = (await db.execute(
        select(GraphEntity).where(GraphEntity.id == new_id)
    )).scalar_one_or_none()
    if revived is not None and revived.status == "merged":
        revived.status = "active"
        revived.merged_into = None
        revived.aliases = ""
        logger.info("graph.split revived entity=%s name=%s", new_id, alias)

    remain = [a.strip() for a in (entity.aliases or "").split(",")
              if a.strip() and a.strip() != alias]
    entity.aliases = ",".join(remain)
    await db.flush()
    logger.info("graph.split source=%d alias=%s new_id=%s", entity_id, alias, new_id)
    return int(new_id)


async def gc_orphans(db) -> int:
    """清理没有任何边的实体。

    删文件时边靠 CASCADE 自动清，但实体节点不会删（可能还被别的文档引用，
    也可能 mention_count 归零）。不跑这个，图会持续膨胀。
    """
    has_edge = select(GraphRelation.subject_id).union(
        select(GraphRelation.object_id).where(GraphRelation.object_id.is_not(None))
    )
    result = await db.execute(
        delete(GraphEntity).where(
            GraphEntity.id.not_in(has_edge),
            GraphEntity.status == "active",
        )
    )
    count = result.rowcount or 0
    if count:
        logger.info("graph.gc orphans_deleted=%d min_mention=%d", count, KG_MIN_MENTION)
    return count


async def stats(db) -> dict:
    entities = int(await db.scalar(
        select(func.count()).select_from(GraphEntity).where(GraphEntity.status == "active")
    ) or 0)
    merged = int(await db.scalar(
        select(func.count()).select_from(GraphEntity).where(GraphEntity.status == "merged")
    ) or 0)
    relations = int(await db.scalar(select(func.count()).select_from(GraphRelation)) or 0)
    value_edges = int(await db.scalar(
        select(func.count()).select_from(GraphRelation).where(GraphRelation.object_id.is_(None))
    ) or 0)
    by_type = (await db.execute(
        select(GraphEntity.type, func.count())
        .where(GraphEntity.status == "active")
        .group_by(GraphEntity.type)
        .order_by(func.count().desc())
    )).all()
    return {
        "entities": entities,
        "merged_entities": merged,
        "relations": relations,
        "value_relations": value_edges,
        "by_type": [{"type": t, "count": c} for t, c in by_type],
    }
