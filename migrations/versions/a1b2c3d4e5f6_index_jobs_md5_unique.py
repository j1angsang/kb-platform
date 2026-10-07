"""files.md5 建唯一索引前去重，被 knowledge_docs 引用的行不删"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'c572f5312718'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 保留行 = knowledge_docs 引用行优先，否则最小 id
_KEEPER_SQL = """
SELECT md5,
       COALESCE(
           (SELECT min(f2.id) FROM files f2
            JOIN knowledge_docs d ON d.file_id = f2.id
            WHERE f2.md5 = g.md5),
           min(g.id)
       ) AS keep_id
FROM files g
GROUP BY g.md5
"""


def upgrade() -> None:
    bind = op.get_bind()
    # 同一 md5 有两行都被 knowledge_docs 引用：拒绝执行，合并文档是业务决策
    conflict = bind.execute(
        sa.text(
            "SELECT md5 FROM files f JOIN knowledge_docs d ON d.file_id = f.id "
            "GROUP BY f.md5 HAVING count(*) > 1"
        )
    ).fetchall()
    if conflict:
        raise RuntimeError(
            f"files.md5 存在多行同时被 knowledge_docs 引用，无法自动去重: "
            f"{[r[0] for r in conflict]}，请先人工合并文档"
        )
    # 删重复行里没被文档引用的
    op.execute(f"""
        DELETE FROM files f
        USING ({_KEEPER_SQL}) dup
        WHERE f.md5 = dup.md5
          AND f.id <> dup.keep_id
          AND NOT EXISTS (SELECT 1 FROM knowledge_docs d WHERE d.file_id = f.id)
    """)
    # 流水改指保留行
    op.execute(f"""
        UPDATE download_logs dl
        SET file_id = dup.keep_id
        FROM files victim, ({_KEEPER_SQL}) dup
        WHERE dl.file_id = victim.id
          AND victim.md5 = dup.md5
          AND victim.id <> dup.keep_id
    """)
    op.drop_index(op.f('ix_files_md5'), table_name='files')
    op.create_index(op.f('ix_files_md5'), 'files', ['md5'], unique=True)

    # type+status 复合索引：BM25 重建查询用
    op.create_index(
        'ix_knowledge_chunks_type_status',
        'knowledge_chunks',
        ['type', 'status'],
        unique=False,
    )

    # upload_tasks 对账列，老数据为 NULL
    op.add_column('upload_tasks', sa.Column('total_size', sa.BigInteger(), nullable=True))
    op.add_column('upload_tasks', sa.Column('total_chunks', sa.Integer(), nullable=True))

    op.create_table(
        'index_jobs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('doc_id', sa.Integer(), nullable=False),
        sa.Column('state', sa.String(length=20), nullable=False, server_default='pending'),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('next_run_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['doc_id'], ['knowledge_docs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_index_jobs_doc_id'), 'index_jobs', ['doc_id'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_index_jobs_doc_id'), table_name='index_jobs')
    op.drop_table('index_jobs')
    op.drop_column('upload_tasks', 'total_chunks')
    op.drop_column('upload_tasks', 'total_size')
    op.drop_index('ix_knowledge_chunks_type_status', table_name='knowledge_chunks')
    op.drop_index(op.f('ix_files_md5'), table_name='files')
    op.create_index(op.f('ix_files_md5'), 'files', ['md5'], unique=False)
