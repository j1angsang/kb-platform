"""图抽取任务队列：条件 UPDATE + rowcount 判定并发，与 crud/knowledge.py 的 IndexJob 同构。

不用进程内锁：进程内锁跨 commit 失效，而且 BackgroundTasks 和轮询循环可能同时抢同一个 doc。
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from models.graph_job import GraphJob
from rag.graph.config import KG_JOB_LEASE_SECONDS, KG_JOB_MAX_ATTEMPTS


async def enqueue_graph_job(db: AsyncSession, doc_id: int) -> None:
    now = datetime.now(timezone.utc)
    stmt = (
        pg_insert(GraphJob)
        .values(doc_id=doc_id, state="pending", attempts=0, last_error=None,
                next_run_at=now, updated_at=now)
        .on_conflict_do_update(
            index_elements=["doc_id"],
            set_={"state": "pending", "attempts": 0, "last_error": None,
                  "next_run_at": now, "updated_at": now},
        )
    )
    await db.execute(stmt)


async def claim_graph_job(db: AsyncSession, doc_id: int) -> bool:
    now = datetime.now(timezone.utc)
    stmt = (
        update(GraphJob)
        .where(GraphJob.doc_id == doc_id, GraphJob.state == "pending")
        .values(state="running", attempts=GraphJob.attempts + 1,
                next_run_at=now + timedelta(seconds=KG_JOB_LEASE_SECONDS),
                updated_at=now)
    )
    result = await db.execute(stmt)
    return result.rowcount > 0


async def finish_graph_job(db: AsyncSession, doc_id: int, *,
                           ok: bool, error: str | None = None) -> None:
    now = datetime.now(timezone.utc)
    if ok:
        await db.execute(
            update(GraphJob)
            .where(GraphJob.doc_id == doc_id, GraphJob.state == "running")
            .values(state="done", last_error=None, updated_at=now)
        )
        return
    row = (await db.execute(
        select(GraphJob.attempts).where(GraphJob.doc_id == doc_id)
    )).first()
    used = row[0] if row else 0
    exhausted = used >= KG_JOB_MAX_ATTEMPTS
    next_state = "failed" if exhausted else "pending"
    backoff = 0 if exhausted else min(60 * (2 ** (used - 1)), 300)
    await db.execute(
        update(GraphJob)
        .where(GraphJob.doc_id == doc_id, GraphJob.state == "running")
        .values(state=next_state, last_error=(error or "")[:500],
                next_run_at=now + timedelta(seconds=backoff), updated_at=now)
    )


async def requeue_stuck_graph_jobs(db: AsyncSession) -> list[int]:
    """租约过期的 running 重排；耗尽的置 failed。进程重启后的恢复路径。"""
    now = datetime.now(timezone.utc)
    stuck = (await db.execute(
        select(GraphJob.doc_id, GraphJob.attempts)
        .where(GraphJob.state == "running", GraphJob.next_run_at < now)
    )).all()
    exhausted_docs: list[int] = []
    for doc_id, attempts in stuck:
        if (attempts or 0) >= KG_JOB_MAX_ATTEMPTS:
            await db.execute(
                update(GraphJob)
                .where(GraphJob.doc_id == doc_id, GraphJob.state == "running")
                .values(state="failed", last_error="执行租约过期，进程重启或卡死",
                        updated_at=now)
            )
            exhausted_docs.append(doc_id)
        else:
            await db.execute(
                update(GraphJob)
                .where(GraphJob.doc_id == doc_id, GraphJob.state == "running")
                .values(state="pending", next_run_at=now, updated_at=now)
            )
    return exhausted_docs


async def list_due_graph_job_doc_ids(db: AsyncSession) -> list[int]:
    now = datetime.now(timezone.utc)
    rows = (await db.execute(
        select(GraphJob.doc_id)
        .where(GraphJob.state == "pending", GraphJob.next_run_at <= now)
        .order_by(GraphJob.id)
        .limit(10)
    )).all()
    return [r[0] for r in rows]


async def graph_job_state(db: AsyncSession, doc_id: int) -> str | None:
    return await db.scalar(
        select(GraphJob.state).where(GraphJob.doc_id == doc_id)
    )
