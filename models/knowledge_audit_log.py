
from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class KnowledgeAuditLog(Base):
    __tablename__ = "knowledge_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("knowledge_docs.id", ondelete="SET NULL"), nullable=True, index=True
    )  # 文档删除时审计流水必须留档，谁审的不可追就是合规事故

    # 只记人工操作 submit/resubmit/approve/reject，机器入库过程不记这里
    action: Mapped[str] = mapped_column(String(20), nullable=False)

    actor_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    comment: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)