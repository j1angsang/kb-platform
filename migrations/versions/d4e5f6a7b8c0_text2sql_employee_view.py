"""text2sql employee view

Revision ID: d4e5f6a7b8c0
Revises: c3d4e5f6a7b9
"""
from alembic import op

revision = "d4e5f6a7b8c0"
down_revision = "c3d4e5f6a7b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS analytics")
    op.execute(
        """
        CREATE OR REPLACE VIEW analytics.v_employee_directory AS
        SELECT department, position, role, created_at::date AS joined_at
        FROM users
        WHERE department IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS analytics.v_employee_directory")
    op.execute("DROP SCHEMA IF EXISTS analytics")
