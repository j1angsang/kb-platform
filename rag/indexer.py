import asyncio
import logging
import time
from weakref import WeakValueDictionary

from config.db_conf import AsyncSessionLocal
from services.storage import STORAGE_DIR
from crud import file as file_crud
from crud import knowledge as knowledge_crud
from rag.parser import parse_file
from rag.store import store

logger = logging.getLogger(__name__)

_doc_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
_doc_locks_guard = asyncio.Lock()


async def _get_doc_lock(doc_id: int) -> asyncio.Lock:
    async with _doc_locks_guard:
        lock = _doc_locks.get(doc_id)
        if lock is None:
            lock = asyncio.Lock()
            _doc_locks[doc_id] = lock
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
            path = STORAGE_DIR / file.stored_path
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
            await db.commit()
            logger.info(
                "indexer doc=%s file_id=%s chunks=%d status=ready elapsed_ms=%.1f",
                doc_id, file_id, len(chunks), (time.perf_counter() - started) * 1000,
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
            path = STORAGE_DIR / file.stored_path
            if not path.exists():
                logger.warning(
                    "reindex doc=%s file_id=%s skip=storage_file_missing path=%s",
                    doc_id, file_id, path,
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
            await db.commit()
            logger.info(
                "reindex doc=%s file_id=%s chunks=%d shared=%s status=ready elapsed_ms=%.1f",
                doc_id, file_id, len(chunks), file.is_shared,
                (time.perf_counter() - started) * 1000,
            )
        except Exception:
            logger.exception(
                "reindex doc=%s file_id=%s 失败 elapsed_ms=%.1f",
                doc_id, file_id, (time.perf_counter() - started) * 1000,
            )
