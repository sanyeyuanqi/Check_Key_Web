"""Index exact status, key prefix, and upload time together.

Revision ID: 20261017_0008
Revises: 20261016_0007
Create Date: 2026-10-17
"""

from alembic import op
import sqlalchemy as sa


revision = "20261017_0008"
down_revision = "20261016_0007"
branch_labels = None
depends_on = None

INDEX_NAME = "idx_credentials_status_prefix_created"


def upgrade() -> None:
    context = op.get_context()
    # PostgreSQL concurrent DDL must run outside a transaction, so ongoing
    # detector uploads remain writable while existing rows are indexed.
    with context.autocommit_block():
        if not context.as_sql:
            is_valid = op.get_bind().scalar(sa.text("""
                SELECT indisvalid FROM pg_index
                WHERE indexrelid = to_regclass(:index_name)
            """), {"index_name": INDEX_NAME})
            if is_valid:
                # The initial migration creates current metadata on fresh installs.
                return
            if is_valid is False:
                # A cancelled concurrent build can leave an unusable index behind.
                op.drop_index(INDEX_NAME, table_name="credentials", if_exists=True, postgresql_concurrently=True)
        op.create_index(
            INDEX_NAME,
            "credentials",
            ["status", "key_prefix", sa.text("created_at DESC")],
            postgresql_ops={"key_prefix": "varchar_pattern_ops"},
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(INDEX_NAME, table_name="credentials", if_exists=True, postgresql_concurrently=True)
