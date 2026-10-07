from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True, comment="消息id")
    session_id: Mapped[int] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True, comment="所属会话"
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False, comment="user / assistant")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="消息内容，多模态 list 存 JSON 字符串")
    sources: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True, comment="该轮文件来源，占 [N] 编号")
    data_sources: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True, comment="该轮数据来源，不占 [N] 编号")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, comment="创建时间"
    )
