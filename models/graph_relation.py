"""图关系：(主体, 谓词, 客体) + 证据 chunk。

chunk_id 是权限的唯一锚点，也是这张表最重要的一个字段：
- 挂 FK ON DELETE CASCADE，store.delete_by_file 删 chunk 时边自动清，
  不用在 indexer 里手写清理顺序（手写一定会漏，漏了就是幽灵召回：
  图返回已删除的 chunk_id，回表查不到，静默丢弃，还会污染 graph-only 指标）。
- 任何图查询最终都要拿 chunk_id 回 knowledge_chunks 过 _permission_clause，
  图本身不产生任何数据出口。

object_id 与 value 二选一：
- 宾语是实体（"年假 依据 请假与休假管理规定"）→ object_id
- 宾语是字面值（"年假 标准为 10 天"）→ value，不建节点
  这一条砍掉一半实体爆炸：否则图里会塞满 500元 / 30天 / 80% 这种一次性名词。
"""
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class GraphRelation(Base):
    __tablename__ = "graph_relations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subject_id: Mapped[int] = mapped_column(
        ForeignKey("graph_entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 谓词白名单，见 rag/graph/extract.py:PREDICATES
    predicate: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    object_id: Mapped[int | None] = mapped_column(
        ForeignKey("graph_entities.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # 字面值宾语：金额/天数/比例/时限，不建节点
    value: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # 证据来源。删文件 → 删 chunk → 级联删边，权限锚点
    chunk_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("knowledge_chunks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "object_id IS NOT NULL OR value IS NOT NULL",
            name="ck_graph_relations_object_or_value",
        ),
        # 同一 chunk 里重复抽到同一条边 → 重入幂等，不产生重复行
        UniqueConstraint(
            "subject_id", "predicate", "object_id", "value", "chunk_id",
            name="uq_graph_relations_edge",
        ),
    )
