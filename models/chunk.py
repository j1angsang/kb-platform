"""行存在 = 该片已到达，断点续传靠查这个"""

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, func, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class UploadChunk(Base):
    __tablename__ = "upload_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    upload_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True, index=True
    )

    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)

    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

    md5: Mapped[str] = mapped_column(String(32), nullable=False)

    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (UniqueConstraint("upload_id", "chunk_index"),)


class UploadTask(Base):
    __tablename__ = "upload_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    upload_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    total_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    total_chunks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_md5: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
