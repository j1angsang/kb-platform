import asyncio
import logging
import time
from weakref import WeakValueDictionary

from config.db_conf import AsyncSessionLocal
from services.object_storage import object_storage
from crud import file as file_crud
from crud import graph_job as graph_crud
from crud import knowledge as knowledge_crud
from rag.graph.config import KG_ENABLED
from rag.graph.pipeline import build_document_graph
from rag.parser import parse_file
from rag.store import store

logger = logging.getLogger(__name__)

_doc_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
_doc_locks_guard = asyncio.Lock()

# 图抽取必须用独立的锁：它要跑几分钟，和入库共用一把锁会把入库整个堵死。
# 但同一 doc 的两个图任务仍然要互斥，否则同一份文档的边会被两边同时删/写。
_graph_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
_graph_locks_guard = asyncio.Lock()


async def _get_doc_lock(doc_id: int) -> asyncio.Lock:
    async with _doc_locks_guard:
        lock = _doc_locks.get(doc_id)
        if lock is None:
            lock = asyncio.Lock()
            _doc_locks[doc_id] = lock
        return lock


async def _get_graph_lock(doc_id: int) -> asyncio.Lock:
    async with _graph_locks_guard:
        lock = _graph_locks.get(doc_id)
        if lock is None:
            lock = asyncio.Lock()
            _graph_locks[doc_id] = lock
        return lock


async def index_document(doc_id: int) -> None:
    async with await _get_doc_lock(doc_id):
        async with AsyncSessionLocal() as db:
            claimed = await knowledge_crud.claim_index_job(db, doc_id)
            await db.commit()
        if not claimed:
            logger.info("indexer doc=%s skip=job_not_claimed", doc_id)
            return
        try:
            await _index_document_inner(doc_id)
        except Exception as exc:
            try:
                async with AsyncSessionLocal() as db:
                    await knowledge_crud.finish_index_job(
                        db, doc_id, ok=False, error=str(exc)[:500]
                    )
                    await db.commit()
            except Exception:
                logger.exception("indexer doc=%s job 收尾失败", doc_id)
            raise
        else:
            try:
                async with AsyncSessionLocal() as db:
                    await knowledge_crud.finish_index_job(db, doc_id, ok=True)
                    await db.commit()
            except Exception:
                logger.exception("indexer doc=%s job done 记账失败", doc_id)


async def _index_document_inner(doc_id: int) -> None:
    started = time.perf_counter()
    file_id: int | None = None
    async with AsyncSessionLocal() as db:
        try:
            doc = await knowledge_crud.get_doc(db, doc_id)
            if doc is None:
                logger.warning("indexer doc=%s skip=doc_not_found", doc_id)
                return
            file_id = doc.file_id
            if doc.status != "approved":
                logger.warning(
                    "indexer doc=%s file_id=%s skip=doc_status_%s",
                    doc_id, file_id, doc.status,
                )
                return
            if doc.index_status in ("ready", "pending"):
                logger.info(
                    "indexer doc=%s file_id=%s skip=status_%s",
                    doc_id, file_id, doc.index_status,
                )
                return

            file = await file_crud.get_file(db, doc.file_id)
            if file is None:
                raise ValueError(f"文件实体不存在: doc={doc_id}, file_id={doc.file_id}")
            path = await object_storage.ensure_local_async(file.stored_path)
            if not path.exists():
                raise ValueError(f"磁盘实体丢失: {path}")

            chunks = parse_file(path)
            await store.delete_by_file(db, doc.file_id)
            await store.upsert_chunks(
                db,
                file_id=file.id,
                doc_id=doc.id,
                department=doc.department,
                status="ready",
                chunks=chunks,
                is_shared=file.is_shared,
                shared_departments=file.shared_departments,
            )

            doc.index_status = "ready"
            doc.chunk_count = len(chunks)
            doc.error_message = None
            if KG_ENABLED:
                # 图抽取与入库状态解耦：同一个事务里先落"可检索"，再排队建图。
                # 顺序不能反 —— 建图是分钟级，绑在一起会让检索可用性退化到建图速度。
                doc.kg_status = "pending"
                doc.kg_error = None
                await graph_crud.enqueue_graph_job(db, doc.id)
            await db.commit()
            logger.info(
                "indexer doc=%s file_id=%s chunks=%d status=ready kg=%s elapsed_ms=%.1f",
                doc_id, file_id, len(chunks), "queued" if KG_ENABLED else "off",
                (time.perf_counter() - started) * 1000,
            )
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.exception(
                "indexer doc=%s file_id=%s 入库失败 elapsed_ms=%.1f error=%s",
                doc_id, file_id, elapsed_ms, str(exc)[:200],
            )
            try:
                async with AsyncSessionLocal() as db2:
                    doc = await knowledge_crud.get_doc(db2, doc_id)
                    if doc is not None:
                        doc.index_status = "failed"
                        doc.error_message = str(exc)[:500]
                        await db2.commit()
            except Exception:
                logger.exception("indexer doc=%s 失败状态落库失败", doc_id)
            raise  # 让 BackgroundTasks/服务端日志也能看到真实异常栈

async def reindex_document(doc_id: int) -> None:
    async with await _get_doc_lock(doc_id):
        await _reindex_document_inner(doc_id)


async def _reindex_document_inner(doc_id: int) -> None:
    started = time.perf_counter()
    file_id: int | None = None
    async with AsyncSessionLocal() as db:
        try:
            doc = await knowledge_crud.get_doc(db, doc_id)
            if doc is None:
                logger.warning("reindex doc=%s skip=doc_not_found", doc_id)
                return
            file_id = doc.file_id
            # 必须从库里重读审核状态，否则会把 rejected/pending 内容写成全公司可检索
            if doc.status != "approved":
                logger.warning(
                    "reindex doc=%s file_id=%s skip=doc_status_%s",
                    doc_id, file_id, doc.status,
                )
                return
            file = await file_crud.get_file(db, doc.file_id)
            if file is None:
                logger.warning("reindex doc=%s file_id=%s skip=file_not_found", doc_id, file_id)
                return
            try:
                path = await object_storage.ensure_local_async(file.stored_path)
            except FileNotFoundError:
                logger.warning(
                    "reindex doc=%s file_id=%s skip=storage_file_missing stored_path=%s",
                    doc_id, file_id, file.stored_path,
                )
                return
            chunks = parse_file(path)
            await store.delete_by_file(db, doc.file_id)
            await store.upsert_chunks(
                db,
                file_id=file.id,
                doc_id=doc.id,
                department=doc.department,
                status="ready",
                chunks=chunks,
                is_shared=file.is_shared,
                shared_departments=file.shared_departments,
            )
            doc.index_status = "ready"
            doc.chunk_count = len(chunks)
            doc.error_message = None
            if KG_ENABLED:
                # 重入库后 chunk 内容变了，图必须重抽，否则边指向的是旧文本
                doc.kg_status = "pending"
                doc.kg_error = None
                await graph_crud.enqueue_graph_job(db, doc.id)
            await db.commit()
            logger.info(
                "reindex doc=%s file_id=%s chunks=%d shared=%s status=ready kg=%s elapsed_ms=%.1f",
                doc_id, file_id, len(chunks), file.is_shared,
                "queued" if KG_ENABLED else "off",
                (time.perf_counter() - started) * 1000,
            )
        except Exception:
            logger.exception(
                "reindex doc=%s file_id=%s 失败 elapsed_ms=%.1f",
                doc_id, file_id, (time.perf_counter() - started) * 1000,
            )


async def extract_doc_graph(doc_id: int) -> None:
    """执行一份文档的图抽取任务（claim → 抽取 → 记账）。

    与 index_document 完全独立：独立锁、独立 job 表、独立状态字段。
    任何异常都只影响 doc.kg_status，绝不触碰 index_status ——
    建图失败时已有 chunk 仍然可被向量/BM25 检索到。
    """
    async with await _get_graph_lock(doc_id):
        async with AsyncSessionLocal() as db:
            claimed = await graph_crud.claim_graph_job(db, doc_id)
            await db.commit()
        if not claimed:
            logger.info("graph_job doc=%s skip=job_not_claimed", doc_id)
            return
        try:
            await build_document_graph(doc_id)
        except Exception as exc:
            try:
                async with AsyncSessionLocal() as db:
                    await graph_crud.finish_graph_job(
                        db, doc_id, ok=False, error=str(exc)[:500]
                    )
                    from models.knowledge_doc import KnowledgeDoc
                    from sqlalchemy import select as _select

                    doc = (await db.execute(
                        _select(KnowledgeDoc).where(KnowledgeDoc.id == doc_id)
                    )).scalar_one_or_none()
                    if doc is not None:
                        doc.kg_status = "failed"
                        doc.kg_error = str(exc)[:500]
                    await db.commit()
            except Exception:
                logger.exception("graph_job doc=%s 失败收尾异常", doc_id)
            raise
        else:
            try:
                async with AsyncSessionLocal() as db:
                    await graph_crud.finish_graph_job(db, doc_id, ok=True)
                    await db.commit()
            except Exception:
                logger.exception("graph_job doc=%s done 记账失败", doc_id)
