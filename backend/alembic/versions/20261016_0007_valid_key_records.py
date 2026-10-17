"""Retain records of every credential known to have been valid.

Revision ID: 20261016_0007
Revises: 20261016_0006
Create Date: 2026-10-16
"""

from alembic import op
import sqlalchemy as sa

revision = "20261016_0007"
down_revision = "20261016_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("valid_key_records"):
        # Keep this pre-partition migration independent of future ORM FK targets.
        op.create_table(
            "valid_key_records",
            sa.Column("credential_id", sa.BigInteger(), primary_key=True),
            sa.Column("first_valid_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_valid_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["credential_id"], ["credentials.id"]),
        )
        op.create_index(
            "idx_valid_key_records_recent", "valid_key_records",
            [sa.text("last_valid_at DESC"), sa.text("credential_id DESC")],
        )
    bind.execute(sa.text("""
        WITH evidence AS (
            SELECT id AS credential_id, updated_at AS observed_at
            FROM credentials WHERE status = 'valid'
            UNION ALL
            SELECT item.credential_id, COALESCE(batch.completed_at, batch.started_at) AS observed_at
            FROM validation_batch_items AS item
            JOIN validation_batches AS batch ON batch.id = item.batch_id
            WHERE item.status = 'valid'
        )
        INSERT INTO valid_key_records (credential_id, first_valid_at, last_valid_at)
        SELECT credential_id, MIN(observed_at), MAX(observed_at)
        FROM evidence GROUP BY credential_id
        ON CONFLICT (credential_id) DO UPDATE SET
            first_valid_at = LEAST(valid_key_records.first_valid_at, EXCLUDED.first_valid_at),
            last_valid_at = GREATEST(valid_key_records.last_valid_at, EXCLUDED.last_valid_at)
    """))


def downgrade() -> None:
    op.drop_table("valid_key_records")
