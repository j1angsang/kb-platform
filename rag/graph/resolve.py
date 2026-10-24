"""实体消歧与落库：三层归一，最后一层留给人工。

图的成败在这一步，不在抽取。中文企业文档里"年假 / 带薪年休假 / annual leave"
不归一，图就是一堆碎片，多跳根本走不通；而阈值调低把"技术部"和"技术支持部"
并成一个，多跳就会走到完全无关的文档上。

三层策略：
1. 精确匹配 canonical_name / aliases  —— 覆盖绝大多数重复提及
2. 名称向量近邻（pgvector <=>，阈值 KG_MERGE_MIN_SCORE）
3. 人工合并/拆分（routers/graph.py）—— 自动判错的兜底，不能省
"""
import asyncio
import logging
import time

from sqlalchemy import Float, Integer, String, select
from sqlalchemy import text as sa_text

from models.graph_entity import GraphEntity
from rag.embeddings import embed_texts
from rag.graph.config import (
    KG_MERGE_MIN_SCORE,
    KG_MERGE_SUBSTRING_MIN_LEN,
    KG_MERGE_SUBSTRING_SCORE,
)
from rag.graph.store import upsert_entities, upsert_relations

logger = logging.getLogger(__name__)


def _should_merge(name: str, target: str, score: float) -> tuple[bool, str]:
    """两道门：纯向量几乎要求字面全同；词面互为子串且短词够长才放宽。

    为什么这么保守：中文里「销售经理」和「销售主管」余弦 0.9744，
    「直属上级」和「直属领导」0.9719 —— 一个该并一个不该，分数几乎一样，
    纯余弦根本分不出来。错并的下游代价远大于漏并，所以把判断交给人工。
    """
    if score >= KG_MERGE_MIN_SCORE:
        return True, "vector"
    shorter = name if len(name) <= len(target) else target
    if (
        (name in target or target in name)
        and len(shorter) >= KG_MERGE_SUBSTRING_MIN_LEN
        and score >= KG_MERGE_SUBSTRING_SCORE
    ):
        return True, "substring"
    return False, ""

# 别名精确匹配：aliases 是逗号分隔字符串，用数组语义避免 "技术部" 命中 "技术部A"
_ALIAS_MATCH_SQL = sa_text("""
SELECT id, canonical_name, type
FROM graph_entities
WHERE status = 'active'
  AND :name = ANY(string_to_array(COALESCE(aliases, ''), ','))
""").columns(id=Integer, canonical_name=String, type=String)

# 向量近邻：type 限定必须加，"技术部"(department) 和"技术"(system) 在不同类型里不能互相合并
_VECTOR_MATCH_SQL = sa_text("""
SELECT id, canonical_name, 1 - (name_embedding <=> CAST(:vec AS vector)) AS score
FROM graph_entities
WHERE status = 'active'
  AND type = :type
  AND name_embedding IS NOT NULL
ORDER BY name_embedding <=> CAST(:vec AS vector)
LIMIT 1
""").columns(id=Integer, canonical_name=String, score=Float)


async def _exact_matches(db, names: list[tuple[str, str]]) -> dict[tuple[str, str], int]:
    if not names:
        return {}
    hit: dict[tuple[str, str], int] = {}
    plain = [n for n, _ in names]

    rows = (await db.execute(
        select(GraphEntity.id, GraphEntity.canonical_name, GraphEntity.type).where(
            GraphEntity.status == "active",
            GraphEntity.canonical_name.in_(plain),
        )
    )).all()
    for eid, cname, etype in rows:
        if (cname, etype) in names:
            hit[(cname, etype)] = eid

    missing = [n for n in names if n not in hit]
    for name, etype in missing:
        row = (await db.execute(_ALIAS_MATCH_SQL, {"name": name})).first()
        if row is not None and row[2] == etype:
            hit[(name, etype)] = row[0]
    return hit


async def _vector_matches(db, names: list[tuple[str, str]]) -> dict[tuple[str, str], int]:
    """向量近邻，逐个查。数量小（每份文档通常十几个新实体），不值得批量化。"""
    if not names:
        return {}
    vectors = await asyncio.to_thread(embed_texts, [n for n, _ in names])
    hit: dict[tuple[str, str], int] = {}
    for (name, etype), vec in zip(names, vectors):
        row = (await db.execute(
            _VECTOR_MATCH_SQL, {"vec": vec, "type": etype}
        )).first()
        if row is None:
            continue
        score = float(row[2])
        ok, reason = _should_merge(name, row[1], score)
        if not ok:
            continue
        # 已存在的名字挂到已有实体上，但把新写法记进别名，下次就走精确匹配
        target = (await db.execute(
            select(GraphEntity).where(GraphEntity.id == row[0])
        )).scalar_one_or_none()
        if target is None:
            continue
        aliases = [a.strip() for a in (target.aliases or "").split(",") if a.strip()]
        if name not in aliases and name != target.canonical_name:
            aliases.append(name)
            target.aliases = ",".join(aliases)
        hit[(name, etype)] = target.id
        logger.info(
            "graph.resolve merged name=%s -> %s type=%s score=%.4f via=%s "
            "vector_min=%.2f substring_min=%.2f",
            name, target.canonical_name, etype, score, reason,
            KG_MERGE_MIN_SCORE, KG_MERGE_SUBSTRING_SCORE,
        )
    await db.flush()
    return hit


async def resolve_names(db, names: list[tuple[str, str]]) -> dict[tuple[str, str], int]:
    """把 (name, type) 映射到 entity_id，缺的建新实体。返回完整映射。"""
    unique = list(dict.fromkeys(n for n in names if n[0] and n[1]))
    if not unique:
        return {}
    started = time.perf_counter()

    mapping = await _exact_matches(db, unique)
    unresolved = [n for n in unique if n not in mapping]
    if unresolved:
        mapping.update(await _vector_matches(db, unresolved))
    unresolved = [n for n in unique if n not in mapping]

    if unresolved:
        # 新建实体必须带名称向量，否则下次遇到同义写法只能靠精确匹配，消歧能力退化
        vectors = await asyncio.to_thread(embed_texts, [n for n, _ in unresolved])
        items = [
            {
                "canonical_name": name,
                "type": etype,
                "aliases": "",
                "name_embedding": vec,
                "mention_count": 1,
                "status": "active",
            }
            for (name, etype), vec in zip(unresolved, vectors)
        ]
        created = await upsert_entities(db, items)
        mapping.update(created)

    logger.info(
        "graph.resolve names=%d exact=%d total=%d created=%d elapsed_ms=%.1f",
        len(unique), len(unique) - len([n for n in unique if n not in mapping]) - len(unresolved),
        len(mapping), len(unresolved), (time.perf_counter() - started) * 1000,
    )
    return mapping


async def store_triples(
    db,
    chunk_triples: list[tuple[str, list[dict]]],
    file_id: int | None = None,
) -> dict:
    """把「chunk_id → 三元组列表」落成实体 + 边。

    同一个 chunk 里对同一实体的多个提及只累加一次 mention_count，
    否则重复抽取会把支撑度刷到虚高，实体排序和筛选全部失真。
    """
    pairs: list[tuple[str, str]] = []
    for _, triples in chunk_triples:
        for t in triples:
            pairs.append((t["subject"], t["subject_type"]))
            if t.get("object") and t.get("object_type"):
                pairs.append((t["object"], t["object_type"]))

    mapping = await resolve_names(db, pairs)
    if not mapping:
        return {"entities": 0, "relations": 0}

    rows: list[dict] = []
    for chunk_id, triples in chunk_triples:
        for t in triples:
            sub_id = mapping.get((t["subject"], t["subject_type"]))
            if sub_id is None:
                continue
            if t.get("object"):
                obj_id = mapping.get((t["object"], t["object_type"]))
                if obj_id is None:
                    continue
                rows.append({
                    "subject_id": sub_id,
                    "predicate": t["predicate"],
                    "object_id": obj_id,
                    "value": None,
                    "chunk_id": chunk_id,
                    "confidence": float(t.get("confidence", 1.0)),
                })
            else:
                rows.append({
                    "subject_id": sub_id,
                    "predicate": t["predicate"],
                    "object_id": None,
                    "value": t["value"],
                    "chunk_id": chunk_id,
                    "confidence": float(t.get("confidence", 0.9)),
                })

    written = await upsert_relations(db, rows)
    logger.info(
        "graph.store file_id=%s chunks=%d entities=%d edge_rows=%d written=%d",
        file_id, len(chunk_triples), len(mapping), len(rows), written,
    )
    return {"entities": len(mapping), "relations": written, "edge_rows": len(rows)}
