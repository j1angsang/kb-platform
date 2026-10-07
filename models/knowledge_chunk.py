"""文本/图片块 + 向量 + 权限元数据同表，让召回与部门过滤走同一条 SQL"""
import os

from dotenv import load_dotenv
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base

load_dotenv()

EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "") or 0)
if EMBEDDING_DIM <= 0:
    raise RuntimeError(
        "EMBEDDING_DIM 未配置或非法：请先用 rag.embeddings.embed_query('测试') "
        "查看维度并写入 .env，禁止硬编码 1152"
    )


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"

    # 确定性 id f"{file_id}:{seq}"，同文件重入即 upsert 覆盖
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    file_id: Mapped[int | None] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"), nullable=True, index=True
    )
    doc_id: Mapped[int | None] = mapped_column(
        ForeignKey("knowledge_docs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)

    content: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    image_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sheet: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 扫描件 OCR 文本块标 ocr / ocr_lowconf，普通块与图片块为 NULL
    source: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # 权限字段必须进 SQL WHERE，禁止先召回再内存过滤
    department: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    is_shared: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    shared_departments: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("file_id", "chunk_index", name="uq_knowledge_chunks_file_chunk"),
        Index(
            "ix_knowledge_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            # m=16 / ef_construction=64 是 pgvector 默认平衡点，万级 chunk 内召回与建索引耗时都稳
            postgresql_with={"m": 16, "ef_construction": 64},
        ),
    )