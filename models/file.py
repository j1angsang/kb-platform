from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, false, func
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class FileInfo(Base):
    __tablename__ = "files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False, default="application/octet-stream")
    md5: Mapped[str] = mapped_column(String(32), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"),nullable=True)
    department: Mapped[str] = mapped_column(String(50),nullable=True,index=True)
    is_shared: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false(), comment="共享空间标记")
    shared_departments: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="共享部门,逗号分隔;空=全部共享")
    stored_path: Mapped[str] = mapped_column(String(255), nullable=False)
    # 软删除位，NULL=正常；列表/下载/检索都带 IS NULL，磁盘删除交给 30 天清理任务
    deleted_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # md5 唯一：普通索引挡不住并发首传，唯一索引 + ON CONFLICT 才是库级兜底
    __table_args__ = (Index("ix_files_md5", "md5", unique=True),)
