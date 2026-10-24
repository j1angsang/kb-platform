
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class KnowledgeDoc(Base):
    __tablename__ = "knowledge_docs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    file_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("files.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )  # 删文件级联删文档，不靠手写清理顺序

    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    department: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    
    # 状态机：pending→approved / rejected，被拒后可重提
    
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")

    
    # index_status：pending→indexing→ready/failed，failed 可重试
    
    # approved 的 index_status 可能仍停在 indexing/failed：审核通过 ≠ 已入库
    index_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # 知识图谱抽取状态，与 index_status 完全独立的状态机：
    # skipped 默认值 → 部署不会给存量文档自动排队，回填必须显式触发
    # pending → extracting → ready / failed，失败只影响图通道，绝不影响检索
    kg_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="skipped", server_default="skipped"
    )
    kg_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    kg_entity_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    reviewed_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)