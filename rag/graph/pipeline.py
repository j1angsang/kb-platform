"""单文档图谱构建编排。

三条顺序约束，改之前先想清楚：

1. LLM 调用期间绝不持有 DB session。抽取一份 200 chunk 的文档要几分钟，
   拿着连接不放会打穿连接池，进而拖死检索接口。
   所以是「读 chunk → 关 session → LLM 抽取 → 新 session 写库」三段式。

2. 先删该文件的旧边再写新边。chunk_id 是确定性 id（f"{file_id}:{seq}"），
   重新入库时同 id 会被 on_conflict_do_update 覆盖而不是删除，FK 级联不触发，
   旧边会残留在新 chunk 上。

3. 失败只写 kg_status=failed，绝不碰 index_status。
   检索可用性不能被建图拖累 —— 这是整条链路最重要的一条隔离。
"""
import logging
import time

from sqlalchemy import select

from config.db_conf import AsyncSessionLocal
from models.knowledge_chunk import KnowledgeChunk
from models.knowledge_doc import KnowledgeDoc
from rag.graph.config import KG_MAX_CHUNKS_PER_DOC
from rag.graph.extract import extract_chunks
from rag.graph.resolve import store_triples
from rag.graph.store import delete_edges_by_file

logger = logging.getLogger(__name__)

# 不抽的类型：表格行块和低置信 OCR 块。
# - xlsx 的 "a | b | c" 是结构化数据，该走 Text2SQL；硬抽只会得到一堆列名当实体
# - ocr_lowconf 的文字本身不可靠，抽出来的实体名会污染消歧的向量空间
_SKIP_SHEET = True
_SKIP_LOWCONF = True


def _selectable(chunk: KnowledgeChunk) -> tuple[bool, str]:
    if chunk.type != "text":
        return False, "not_text"
    if _SKIP_SHEET and chunk.sheet:
        return False, "xlsx_sheet"
    if _SKIP_LOWCONF and chunk.source == "ocr_lowconf":
        return False, "ocr_lowconf"
    if not (chunk.content or "").strip():
        return False, "empty"
    return True, ""


async def _load_chunks(doc_id: int, file_id: int) -> tuple[list[dict], dict]:
    """读 chunk 并过滤。返回 (可抽列表, 跳过统计)。"""
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(KnowledgeChunk)
            .where(
                KnowledgeChunk.file_id == file_id,
                KnowledgeChunk.type == "text",
                KnowledgeChunk.status == "ready",
            )
            .order_by(KnowledgeChunk.chunk_index)
        )).scalars().all()

    skipped: dict[str, int] = {}
    candidates: list[dict] = []
    for chunk in rows:
        ok, reason = _selectable(chunk)
        if not ok:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        candidates.append({"id": chunk.id, "text": chunk.content})

    if len(candidates) > KG_MAX_CHUNKS_PER_DOC:
        skipped["over_limit"] = len(candidates) - KG_MAX_CHUNKS_PER_DOC
        candidates = candidates[:KG_MAX_CHUNKS_PER_DOC]
    return candidates, skipped


async def _set_status(doc_id: int, status: str, error: str | None = None,
                      entity_count: int | None = None) -> None:
    async with AsyncSessionLocal() as db:
        doc = (await db.execute(
            select(KnowledgeDoc).where(KnowledgeDoc.id == doc_id)
        )).scalar_one_or_none()
        if doc is None:
            return
        doc.kg_status = status
        doc.kg_error = (error or None) and error[:500]
        if entity_count is not None:
            doc.kg_entity_count = entity_count
        await db.commit()


async def build_document_graph(doc_id: int) -> dict:
    """建一份文档的图。调用方负责 claim job；本函数只管干活和记账。"""
    started = time.perf_counter()

    async with AsyncSessionLocal() as db:
        doc = (await db.execute(
            select(KnowledgeDoc).where(KnowledgeDoc.id == doc_id)
        )).scalar_one_or_none()
        if doc is None:
            logger.warning("graph.build doc=%s skip=doc_not_found", doc_id)
            return {"doc_id": doc_id, "skipped": "doc_not_found"}
        if doc.status != "approved":
            logger.warning("graph.build doc=%s skip=doc_status_%s", doc_id, doc.status)
            return {"doc_id": doc_id, "skipped": f"doc_status_{doc.status}"}
        # chunk 还没入库，抽不出东西；这不是错误，等重入库后再排队
        if doc.index_status != "ready":
            logger.info("graph.build doc=%s skip=index_status_%s", doc_id, doc.index_status)
            return {"doc_id": doc_id, "skipped": f"index_status_{doc.index_status}"}
        file_id = doc.file_id

    candidates, skipped = await _load_chunks(doc_id, file_id)
    if not candidates:
        logger.warning(
            "graph.build doc=%s file_id=%s skip=no_extractable_chunk skipped_detail=%s",
            doc_id, file_id, skipped,
        )
        await _set_status(doc_id, "skipped", "没有可抽取的文本块", entity_count=0)
        return {"doc_id": doc_id, "skipped": "no_extractable_chunk", "detail": skipped}

    await _set_status(doc_id, "extracting")

    result = await extract_chunks(candidates)
    chunk_triples = [
        (chunk["id"], triples)
        for chunk, triples in zip(candidates, result["triples"])
        if triples
    ]

    if not chunk_triples:
        await _set_status(doc_id, "ready", None, entity_count=0)
        logger.info(
            "graph.build doc=%s file_id=%s chunks=%d triples=0 无有效三元组，标记 ready",
            doc_id, file_id, len(candidates),
        )
        return {"doc_id": doc_id, "triples": 0, "entities": 0}

    async with AsyncSessionLocal() as db:
        removed = await delete_edges_by_file(db, file_id)
        stats = await store_triples(db, chunk_triples, file_id=file_id)
        await db.commit()

    await _set_status(doc_id, "ready", None, entity_count=stats["entities"])
    logger.info(
        "graph.build doc=%s file_id=%s chunks=%d extracted=%d stale_edges_removed=%d "
        "entities=%d relations=%d skipped=%s elapsed_ms=%.1f",
        doc_id, file_id, len(candidates), len(chunk_triples), removed,
        stats["entities"], stats["relations"], skipped,
        (time.perf_counter() - started) * 1000,
    )
    return {
        "doc_id": doc_id,
        "chunks": len(candidates),
        "triples": sum(len(t) for _, t in chunk_triples),
        "entities": stats["entities"],
        "relations": stats["relations"],
        "stale_edges_removed": removed,
        "extract_failed": result["failed"],
        "skipped": skipped,
    }
