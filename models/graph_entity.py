"""图实体：只存"名字"，不存内容。

三条硬约束，破坏任何一条都会出问题：
1. 不存 department —— 实体是跨文档合并的（"年假"聚合人事部/技术部/财务部三份文件），
   带上单一部门字段会在 N 个部门间分裂成 N 份，图退化成按部门切片的索引，跨文档价值归零。
2. 不存原文/属性值 —— 权限闸门在 knowledge_chunks 的 SQL WHERE 上，实体一旦携带内容，
   就成了绕过权限的新出口。要读内容，沿 GraphRelation.chunk_id 回表。
3. canonical_name + type 唯一 —— 归一后的标准名，别名进 aliases 字段。
"""
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from pgvector.sqlalchemy import Vector

from models.base import Base
# 复用 chunk 的 EMBEDDING_DIM：实体名向量与 chunk 向量必须同一模型同一空间，才能直接比余弦
from models.knowledge_chunk import EMBEDDING_DIM


class GraphEntity(Base):
    __tablename__ = "graph_entities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    canonical_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # 白名单类型，见 rag/graph/extract.py:ENTITY_TYPES
    type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # 逗号分隔别名；人工合并时追加，供链接与抽取复用
    aliases: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 名称向量：实体链接（query→实体）与自动合并（实体→实体）共用
    name_embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIM), nullable=True
    )
    # 支撑度：被多少个 chunk 提及。低于 KG_MIN_MENTION 的实体只保留关系不建正式节点
    mention_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # active=正常；merged=已被合并进 merged_into，保留行避免边悬空
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    merged_into: Mapped[int | None] = mapped_column(
        ForeignKey("graph_entities.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("canonical_name", "type", name="uq_graph_entities_name_type"),
    )
