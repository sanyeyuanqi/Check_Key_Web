"""Persist credential export records.

Revision ID: 20261016_0002
Revises: 20260902_0001
Create Date: 2026-10-16
"""

from alembic import op

from app.database import ExportRecord


revision = "20261016_0002"
down_revision = "20260902_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The initial revision also creates current metadata on a fresh installation.
    ExportRecord.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    ExportRecord.__table__.drop(bind=op.get_bind(), checkfirst=True)
