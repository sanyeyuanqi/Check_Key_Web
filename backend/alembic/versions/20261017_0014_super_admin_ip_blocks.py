"""Add persistent IP blocks without changing credential storage or existing roles.

Revision ID: 20261017_0014
Revises: 20261017_0013
Create Date: 2026-10-17

Bootstrap role promotion is deliberately handled by the guarded application
startup path, using the configured bootstrap account rather than an arbitrary row.
"""

from alembic import op
import sqlalchemy as sa


revision = "20261017_0014"
down_revision = "20261017_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    op.create_table(
        "auth_ip_blocks",
        sa.Column("ip_address", sa.String(45), primary_key=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("reason", sa.String(200), nullable=False, server_default=""),
        sa.Column("created_by", sa.BigInteger(), sa.ForeignKey("auth_users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    op.drop_table("auth_ip_blocks")
