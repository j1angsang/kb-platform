from datetime import datetime
from typing import Any

from pydantic import BaseModel


class ChatSessionOut(BaseModel):
    id: int
    user_id: int
    title: str | None = None
    message_count: int = 0
    created_at: datetime
    updated_at: datetime
    last_message: str | None = None
    last_role: str | None = None


class ChatMessageOut(BaseModel):
    # 老消息 content 是 JSON 字符串、解析出 list 时还原成 list 返回
    id: int
    session_id: int
    role: str
    content: str | list[Any]
    sources: list[dict[str, Any]] | None = None
    data_sources: list[dict[str, Any]] | None = None
    created_at: datetime
