"""图抽取任务表。

为什么必须是独立表，不给 IndexJob 加 stage 列：
IndexJob.doc_id 是 unique=True，crud/knowledge.py:claim_index_job 靠
「条件 UPDATE + rowcount」做并发判定。改成 (doc_id, stage) 复合唯一会同时
破坏唯一约束语义和 claim 逻辑，风险远大于收益。独立表零风险，两套租约互不干扰。

state 语义与 IndexJob 一致：pending 待执行 / running 执行中
next_run_at 双重身份：pending 时=最早执行时间（退避），running 时=租约到期时间
"""
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base

GRAPH_JOB_STATES = ("pending", "running", "done", "failed")


class GraphJob(Base):
    __tablename__ = "graph_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_docs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_run_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
