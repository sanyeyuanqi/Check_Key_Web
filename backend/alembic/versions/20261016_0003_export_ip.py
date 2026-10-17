"""Capture the export client's IP address.

Revision ID: 20261016_0003
Revises: 20261016_0002
Create Date: 2026-10-16
"""

from alembic import op
import sqlalchemy as sa


revision = "20261016_0003"
down_revision = "20261016_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Earlier revisions create current metadata when installing a fresh database.
    columns = sa.inspect(op.get_bind()).get_columns("export_records")
    if not any(column["name"] == "ip_address" for column in columns):
        op.add_column("export_records", sa.Column("ip_address", sa.String(45), nullable=True))


def downgrade() -> None:
    columns = sa.inspect(op.get_bind()).get_columns("export_records")
    if any(column["name"] == "ip_address" for column in columns):
        op.drop_column("export_records", "ip_address")
