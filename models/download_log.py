
from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column
from models.base import Base


class DownloadLog(Base):
    __tablename__ = "download_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[int] = mapped_column(Integer, ForeignKey("files.id"), nullable=False, index=True)
    ip: Mapped[str] = mapped_column(String(45), nullable=False)
    referer: Mapped[str] = mapped_column(String(255), nullable=True)
    downloaded_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)