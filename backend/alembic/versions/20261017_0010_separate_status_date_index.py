"""Separate status/time lookup from independent key-prefix lookup.

Revision ID: 20261017_0010
Revises: 20261017_0009
Create Date: 2026-10-17
"""

from alembic import op
import sqlalchemy as sa


revision = "20261017_0010"
down_revision = "20261017_0009"
branch_labels = None
depends_on = None

OLD_INDEX = "idx_credentials_status_prefix_created"
NEW_INDEX = "idx_credentials_status_created"


def _set_timeouts() -> None:
    # Partitioned parent indexes cannot use CONCURRENTLY. Bound lock waits and
    # construction time; PostgreSQL rolls this transaction back on either timeout.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '60s'")


def _assert_index_tree_valid(name: str) -> None:
    if op.get_context().as_sql:
        return
    valid = op.get_bind().scalar(sa.text("""
        WITH RECURSIVE index_tree(oid) AS (
            SELECT to_regclass(:name)::oid
            UNION ALL
            SELECT child.inhrelid FROM pg_inherits AS child
            JOIN index_tree AS parent ON child.inhparent = parent.oid
        )
        SELECT bool_and(index.indisvalid AND index.indisready)
        FROM index_tree JOIN pg_index AS index ON index.indexrelid = index_tree.oid
    """), {"name": name})
    if valid is not True:
        raise RuntimeError("Credential index or an attached partition index is invalid; migration cancelled")


def upgrade() -> None:
    _set_timeouts()
    op.create_index(
        NEW_INDEX, "credentials", ["status", sa.text("created_at DESC")],
        if_not_exists=True,
    )
    _assert_index_tree_valid(NEW_INDEX)
    # Dropping the partitioned parent index also removes its attached indexes.
    op.drop_index(OLD_INDEX, table_name="credentials", if_exists=True)


def downgrade() -> None:
    _set_timeouts()
    op.create_index(
        OLD_INDEX, "credentials", ["status", "key_prefix", sa.text("created_at DESC")],
        postgresql_ops={"key_prefix": "varchar_pattern_ops"},
        if_not_exists=True,
    )
    _assert_index_tree_valid(OLD_INDEX)
    op.drop_index(NEW_INDEX, table_name="credentials", if_exists=True)
