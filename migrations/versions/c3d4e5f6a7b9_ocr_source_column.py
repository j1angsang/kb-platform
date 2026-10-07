"""knowledge_chunks.source：OCR 文本块溯源，取值 ocr / ocr_lowconf / NULL"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b9'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'knowledge_chunks',
        sa.Column('source', sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('knowledge_chunks', 'source')
