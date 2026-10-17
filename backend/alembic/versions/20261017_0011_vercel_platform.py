"""Add a dedicated Vercel credential partition.

Revision ID: 20261017_0011
Revises: 20261017_0010
Create Date: 2026-10-17
"""

from alembic import op


revision = "20261017_0011"
down_revision = "20261017_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    op.execute(
        "CREATE TABLE credentials_vercel PARTITION OF credentials "
        "FOR VALUES IN ('Vercel')"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TABLE credentials_vercel")
