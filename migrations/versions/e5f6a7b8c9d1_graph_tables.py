"""知识图谱三张表 + knowledge_docs 的 kg_* 列

设计要点（改之前先读 models/graph_*.py 的 docstring）：
- graph_entities 不存 department / 原文 / 属性值，实体是跨文档合并的
- graph_relations.chunk_id 挂 ON DELETE CASCADE，是权限唯一锚点
- graph_jobs 独立成表，不碰 IndexJob 的 unique(doc_id) 语义
- kg_status 默认 skipped：部署不给存量文档自动排队，回填必须显式触发
"""
import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

# revision identifiers, used by Alembic.
revision: str = 'e5f6a7b8c9d1'
down_revision: Union[str, Sequence[str], None] = 'd4e5f6a7b8c0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 与 knowledge_chunks.embedding 同维度，否则实体名向量不能和 chunk 向量比余弦
_EMBEDDING_DIM = int(os.environ["EMBEDDING_DIM"])


def upgrade() -> None:
    op.create_table(
        'graph_entities',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('canonical_name', sa.String(length=128), nullable=False),
        sa.Column('type', sa.String(length=32), nullable=False),
        sa.Column('aliases', sa.Text(), nullable=False),
        sa.Column('name_embedding', Vector(dim=_EMBEDDING_DIM), nullable=True),
        sa.Column('mention_count', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('merged_into', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['merged_into'], ['graph_entities.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('canonical_name', 'type', name='uq_graph_entities_name_type'),
    )
    op.create_index(op.f('ix_graph_entities_canonical_name'), 'graph_entities', ['canonical_name'], unique=False)
    op.create_index(op.f('ix_graph_entities_type'), 'graph_entities', ['type'], unique=False)

    op.create_table(
        'graph_relations',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('subject_id', sa.Integer(), nullable=False),
        sa.Column('predicate', sa.String(length=64), nullable=False),
        sa.Column('object_id', sa.Integer(), nullable=True),
        sa.Column('value', sa.String(length=256), nullable=True),
        sa.Column('chunk_id', sa.String(length=64), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(
            'object_id IS NOT NULL OR value IS NOT NULL',
            name='ck_graph_relations_object_or_value',
        ),
        sa.ForeignKeyConstraint(['subject_id'], ['graph_entities.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['object_id'], ['graph_entities.id'], ondelete='CASCADE'),
        # 删 chunk（= 删文件/重入库）→ 边自动清，不靠手写清理顺序
        sa.ForeignKeyConstraint(['chunk_id'], ['knowledge_chunks.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'subject_id', 'predicate', 'object_id', 'value', 'chunk_id',
            name='uq_graph_relations_edge',
        ),
    )
    op.create_index(op.f('ix_graph_relations_subject_id'), 'graph_relations', ['subject_id'], unique=False)
    op.create_index(op.f('ix_graph_relations_object_id'), 'graph_relations', ['object_id'], unique=False)
    op.create_index(op.f('ix_graph_relations_predicate'), 'graph_relations', ['predicate'], unique=False)
    op.create_index(op.f('ix_graph_relations_chunk_id'), 'graph_relations', ['chunk_id'], unique=False)

    op.create_table(
        'graph_jobs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('doc_id', sa.Integer(), nullable=False),
        sa.Column('state', sa.String(length=20), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('next_run_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['doc_id'], ['knowledge_docs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('doc_id'),
    )
    op.create_index(op.f('ix_graph_jobs_doc_id'), 'graph_jobs', ['doc_id'], unique=False)

    op.add_column(
        'knowledge_docs',
        sa.Column('kg_status', sa.String(length=20), server_default='skipped', nullable=False),
    )
    op.add_column(
        'knowledge_docs',
        sa.Column('kg_error', sa.Text(), nullable=True),
    )
    op.add_column(
        'knowledge_docs',
        sa.Column('kg_entity_count', sa.Integer(), server_default='0', nullable=False),
    )


def downgrade() -> None:
    op.drop_column('knowledge_docs', 'kg_entity_count')
    op.drop_column('knowledge_docs', 'kg_error')
    op.drop_column('knowledge_docs', 'kg_status')
    op.drop_index(op.f('ix_graph_jobs_doc_id'), table_name='graph_jobs')
    op.drop_table('graph_jobs')
    op.drop_index(op.f('ix_graph_relations_chunk_id'), table_name='graph_relations')
    op.drop_index(op.f('ix_graph_relations_predicate'), table_name='graph_relations')
    op.drop_index(op.f('ix_graph_relations_object_id'), table_name='graph_relations')
    op.drop_index(op.f('ix_graph_relations_subject_id'), table_name='graph_relations')
    op.drop_table('graph_relations')
    op.drop_index(op.f('ix_graph_entities_type'), table_name='graph_entities')
    op.drop_index(op.f('ix_graph_entities_canonical_name'), table_name='graph_entities')
    op.drop_table('graph_entities')
