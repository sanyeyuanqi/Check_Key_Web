"""Preserve the source IP of credential uploads.

Revision ID: 20261016_0004
Revises: 20261016_0003
Create Date: 2026-10-16
"""

from alembic import op
import sqlalchemy as sa


revision = "20261016_0004"
down_revision = "20261016_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Earlier revisions create current metadata when installing a fresh database.
    columns = sa.inspect(op.get_bind()).get_columns("credentials")
    if not any(column["name"] == "source_ip" for column in columns):
        op.add_column("credentials", sa.Column("source_ip", sa.String(45), nullable=True))


def downgrade() -> None:
    columns = sa.inspect(op.get_bind()).get_columns("credentials")
    if any(column["name"] == "source_ip" for column in columns):
        op.drop_column("credentials", "source_ip")
