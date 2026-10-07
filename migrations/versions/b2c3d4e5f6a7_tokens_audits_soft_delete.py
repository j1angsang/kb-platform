"""加 token_version/is_active、expected_md5、审计表、files 软删除位"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _fk_names() -> dict[str, str]:
    bind = op.get_bind()
    rows = bind.execute(sa.text(
        "SELECT conname, conrelid::regclass::text AS tbl "
        "FROM pg_constraint WHERE contype = 'f' "
        "AND conrelid::regclass::text IN ('knowledge_docs', 'knowledge_audit_logs')"
    )).fetchall()
    return {r[1]: r[0] for r in rows}


def upgrade() -> None:
    op.add_column('users', sa.Column('token_version', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('users', sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column('upload_tasks', sa.Column('expected_md5', sa.String(length=32), nullable=True))
    op.add_column('files', sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True))

    names = _fk_names()
    op.drop_constraint(names['knowledge_docs'], 'knowledge_docs', type_='foreignkey')
    op.create_foreign_key(
        'fk_knowledge_docs_file_id_cascade', 'knowledge_docs', 'files',
        ['file_id'], ['id'], ondelete='CASCADE',
    )
    # 审计留档：删文档不能抹掉审计流水
    op.drop_constraint(names['knowledge_audit_logs'], 'knowledge_audit_logs', type_='foreignkey')
    op.alter_column('knowledge_audit_logs', 'doc_id',
                    existing_type=sa.Integer(), nullable=True)
    op.create_foreign_key(
        'fk_knowledge_audit_logs_doc_id_setnull', 'knowledge_audit_logs', 'knowledge_docs',
        ['doc_id'], ['id'], ondelete='SET NULL',
    )

    op.create_table(
        'ops_audit_logs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('actor_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('action', sa.String(length=50), nullable=False),
        sa.Column('target_type', sa.String(length=30), nullable=False),
        sa.Column('target_id', sa.String(length=64), nullable=True),
        sa.Column('detail', postgresql.JSONB(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_ops_audit_logs_actor_id'), 'ops_audit_logs', ['actor_id'], unique=False)
    op.create_index(op.f('ix_ops_audit_logs_action'), 'ops_audit_logs', ['action'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_ops_audit_logs_action'), table_name='ops_audit_logs')
    op.drop_index(op.f('ix_ops_audit_logs_actor_id'), table_name='ops_audit_logs')
    op.drop_table('ops_audit_logs')

    names = _fk_names()
    op.drop_constraint(names['knowledge_audit_logs'], 'knowledge_audit_logs', type_='foreignkey')
    # 旧列 NOT NULL 放不回：先清掉 doc_id 为空的审计行
    op.execute("DELETE FROM knowledge_audit_logs WHERE doc_id IS NULL")
    op.alter_column('knowledge_audit_logs', 'doc_id',
                    existing_type=sa.Integer(), nullable=False)
    op.create_foreign_key(None, 'knowledge_audit_logs', 'knowledge_docs',
                          ['doc_id'], ['id'])

    op.drop_constraint(names['knowledge_docs'], 'knowledge_docs', type_='foreignkey')
    op.create_foreign_key(None, 'knowledge_docs', 'files', ['file_id'], ['id'])

    op.drop_column('files', 'deleted_at')
    op.drop_column('upload_tasks', 'expected_md5')
    op.drop_column('users', 'is_active')
    op.drop_column('users', 'token_version')
