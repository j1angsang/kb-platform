import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, select, or_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from crud.file import shared_departments_clause
from models import FileInfo, KnowledgeAuditLog, KnowledgeDoc, User
from models.index_job import IndexJob


INDEX_JOB_LEASE_SECONDS = int(os.getenv("INDEX_JOB_LEASE_SECONDS", "600"))
INDEX_JOB_MAX_ATTEMPTS = 3
INDEX_JOB_POLL_SECONDS = int(os.getenv("INDEX_JOB_POLL_SECONDS", "10"))


async def create_doc(db:AsyncSession,file_id:int,user_id:int,department:str) ->KnowledgeDoc:
    doc = KnowledgeDoc(file_id=file_id,department=department,user_id=user_id)
    db.add(doc)
    await db.flush()
    await db.refresh(doc)
    return doc

async def get_doc(db:AsyncSession,doc_id:int)->KnowledgeDoc | None:
    stmt = select(KnowledgeDoc).where(KnowledgeDoc.id == doc_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()

async def get_doc_by_file(db:AsyncSession,file_id:int)->KnowledgeDoc | None:
    stmt = select(KnowledgeDoc).where(KnowledgeDoc.file_id == file_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()

async def list_docs(db: AsyncSession, department: str | None = None,
                    status: str | None = None,
                    include_shared: bool = False,
                    exclude_shared: bool = False,
                    limit: int | None = None, offset: int = 0) -> list[tuple[KnowledgeDoc, FileInfo, str | None]]:
    # include_shared / exclude_shared 互斥，同时为 True 直接报错
    if include_shared and exclude_shared:
        raise ValueError("include_shared 与 exclude_shared 不能同时为 True")
    stmt = (
        select(KnowledgeDoc, FileInfo, User.username)
        .join(FileInfo, KnowledgeDoc.file_id == FileInfo.id)
        .outerjoin(User, KnowledgeDoc.user_id == User.id)
    )
    if exclude_shared:
        stmt = stmt.where(FileInfo.is_shared == False)  # noqa: E712
    if department is not None:
        if include_shared:
            stmt = stmt.where(or_(
                KnowledgeDoc.department == department,
                shared_departments_clause(department),
            ))
        else:
            stmt = stmt.where(KnowledgeDoc.department == department)
    if status is not None:
        stmt = stmt.where(KnowledgeDoc.status == status)
    stmt = stmt.order_by(KnowledgeDoc.id.desc())
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    result = await db.execute(stmt)
    return list(result.all())


async def count_docs(db: AsyncSession, department: str | None = None,
                    status: str | None = None,
                    include_shared: bool = False,
                    exclude_shared: bool = False) -> int:
    if include_shared and exclude_shared:
        raise ValueError("include_shared 与 exclude_shared 不能同时为 True")
    stmt = select(func.count(KnowledgeDoc.id)).join(FileInfo, KnowledgeDoc.file_id == FileInfo.id)
    if exclude_shared:
        stmt = stmt.where(FileInfo.is_shared == False)  # noqa: E712
    if department is not None:
        if include_shared:
            stmt = stmt.where(or_(
                KnowledgeDoc.department == department,
                shared_departments_clause(department),
            ))
        else:
            stmt = stmt.where(KnowledgeDoc.department == department)
    if status is not None:
        stmt = stmt.where(KnowledgeDoc.status == status)
    result = await db.execute(stmt)
    return result.scalar_one()



# 并发判定靠条件 UPDATE + rowcount，进程内锁跨 commit 失效

async def enqueue_index_job(db: AsyncSession, doc_id: int) -> None:
    now = datetime.now(timezone.utc)
    stmt = (
        pg_insert(IndexJob)
        .values(doc_id=doc_id, state="pending", attempts=0, last_error=None,
                next_run_at=now, updated_at=now)
        .on_conflict_do_update(
            index_elements=["doc_id"],
            set_={"state": "pending", "attempts": 0, "last_error": None,
                  "next_run_at": now, "updated_at": now},
        )
    )
    await db.execute(stmt)


async def claim_index_job(db: AsyncSession, doc_id: int) -> bool:
    now = datetime.now(timezone.utc)
    stmt = (
        update(IndexJob)
        .where(
            IndexJob.doc_id == doc_id,
            IndexJob.state == "pending",
        )
        .values(state="running", attempts=IndexJob.attempts + 1,
                next_run_at=now + timedelta(seconds=INDEX_JOB_LEASE_SECONDS),
                updated_at=now)
    )
    result = await db.execute(stmt)
    return result.rowcount > 0


async def finish_index_job(db: AsyncSession, doc_id: int, *,
                           ok: bool, error: str | None = None) -> None:
    now = datetime.now(timezone.utc)
    if ok:
        await db.execute(
            update(IndexJob)
            .where(IndexJob.doc_id == doc_id, IndexJob.state == "running")
            .values(state="done", last_error=None, updated_at=now)
        )
        return
    row = (await db.execute(
        select(IndexJob.attempts).where(IndexJob.doc_id == doc_id)
    )).first()
    used = row[0] if row else 0
    exhausted = used >= INDEX_JOB_MAX_ATTEMPTS
    next_state = "failed" if exhausted else "pending"
    backoff = 0 if exhausted else min(60 * (2 ** (used - 1)), 300)
    await db.execute(
        update(IndexJob)
        .where(IndexJob.doc_id == doc_id, IndexJob.state == "running")
        .values(state=next_state, last_error=(error or "")[:500],
                next_run_at=now + timedelta(seconds=backoff), updated_at=now)
    )


async def requeue_stuck_index_jobs(db: AsyncSession) -> list[int]:
    # 租约过期的 running 重排；耗尽的置 failed，是进程重启后卡住的恢复路径
    now = datetime.now(timezone.utc)
    stuck = (await db.execute(
        select(IndexJob.doc_id, IndexJob.attempts)
        .where(IndexJob.state == "running", IndexJob.next_run_at < now)
    )).all()
    failed_docs: list[int] = []
    for doc_id, attempts in stuck:
        if (attempts or 0) >= INDEX_JOB_MAX_ATTEMPTS:
            await db.execute(
                update(IndexJob)
                .where(IndexJob.doc_id == doc_id, IndexJob.state == "running")
                .values(state="failed", last_error="执行租约过期，进程重启或卡死",
                        updated_at=now)
            )
            await db.execute(
                update(KnowledgeDoc)
                .where(KnowledgeDoc.id == doc_id, KnowledgeDoc.index_status == "indexing")
                .values(index_status="failed", error_message="入库任务租约过期，已自动停止")
            )
            failed_docs.append(doc_id)
        else:
            await db.execute(
                update(IndexJob)
                .where(IndexJob.doc_id == doc_id, IndexJob.state == "running")
                .values(state="pending", next_run_at=now, updated_at=now)
            )
    return failed_docs


async def requeue_stale_job_for_doc(db: AsyncSession, doc_id: int) -> bool:
    now = datetime.now(timezone.utc)
    stmt = (
        update(IndexJob)
        .where(
            IndexJob.doc_id == doc_id,
            IndexJob.state == "running",
            IndexJob.next_run_at < now,
        )
        .values(state="pending", next_run_at=now, updated_at=now)
    )
    result = await db.execute(stmt)
    return result.rowcount > 0


async def list_due_index_job_doc_ids(db: AsyncSession) -> list[int]:
    now = datetime.now(timezone.utc)
    rows = (await db.execute(
        select(IndexJob.doc_id)
        .where(IndexJob.state == "pending", IndexJob.next_run_at <= now)
        .order_by(IndexJob.id)
        .limit(20)
    )).all()
    return [r[0] for r in rows]


async def add_log(db: AsyncSession, doc_id: int, action: str,
                  actor_id: int, comment: str | None = None) -> KnowledgeAuditLog:
    
    log = KnowledgeAuditLog(doc_id=doc_id, action=action, actor_id=actor_id, comment=comment)
    db.add(log)
    await db.flush()
    return log