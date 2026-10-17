"""Allow multiple status buckets in one export record.

Revision ID: 20261016_0005
Revises: 20261016_0004
Create Date: 2026-10-16
"""

from alembic import op
import sqlalchemy as sa


revision = "20261016_0005"
down_revision = "20261016_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Fresh installations already use the current metadata in earlier revisions.
    columns = sa.inspect(op.get_bind()).get_columns("export_records")
    status_type = next(column["type"] for column in columns if column["name"] == "status")
    if not isinstance(status_type, sa.Text) and status_type.length != 128:
        op.alter_column("export_records", "status", existing_type=status_type, type_=sa.String(128), existing_nullable=False)


def downgrade() -> None:
    # PostgreSQL rejects narrowing when multi-status data would be truncated.
    op.alter_column("export_records", "status", existing_type=sa.String(128), type_=sa.String(32), existing_nullable=False)
