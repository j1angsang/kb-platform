"""业务侧的「人的记忆」，与 checkpointer 的「图的记忆」分离"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True, comment="会话id")
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True, comment="归属用户"
    )
    title: Mapped[str | None] = mapped_column(String(80), nullable=True, comment="标题=首条用户消息前30字，已设置不覆盖")
    message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0", comment="消息条数"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, comment="创建时间"
    )
    # updated_at 必须显式刷新，ORM onupdate 只在发消息时兜底，列表按"最近活动"排序
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False, comment="最近活动时间"
    )
