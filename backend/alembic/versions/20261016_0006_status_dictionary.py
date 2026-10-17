"""Use a status dictionary for credential values and exact export scopes.

Revision ID: 20261016_0006
Revises: 20261016_0005
Create Date: 2026-10-16
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from app.database import CredentialStatus
from app.status_catalog import initial_status_rows


revision = "20261016_0006"
down_revision = "20261016_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    CredentialStatus.__table__.create(bind=bind, checkfirst=True)
    bind.execute(insert(CredentialStatus.__table__).values(initial_status_rows()).on_conflict_do_nothing(index_elements=["value"]))

    # Preserve every historical value before adding foreign keys, including values
    # introduced by older or newer detector versions outside the initial catalogue.
    existing_values = bind.execute(sa.text(
        "SELECT DISTINCT status FROM credentials UNION SELECT DISTINCT status FROM validation_batch_items"
    )).scalars().all()
    if existing_values:
        bind.execute(insert(CredentialStatus.__table__).values([
            {"value": value, "name": value, "tone": "neutral", "sort_order": 1000}
            for value in existing_values
        ]).on_conflict_do_nothing(index_elements=["value"]))

    for table_name, constraint in (("credentials", "fk_credentials_status"), ("validation_batch_items", "fk_batch_items_status")):
        foreign_keys = sa.inspect(bind).get_foreign_keys(table_name)
        if not any(item["name"] == constraint for item in foreign_keys):
            op.create_foreign_key(constraint, table_name, "credential_statuses", ["status"], ["value"])

    indexes = sa.inspect(bind).get_indexes("credentials")
    if not any(item["name"] == "idx_credentials_type_status_created" for item in indexes):
        op.create_index("idx_credentials_type_status_created", "credentials", ["storage_type", "status", sa.text("created_at DESC")])

    export_columns = {item["name"]: item for item in sa.inspect(bind).get_columns("export_records")}
    if not isinstance(export_columns["status"]["type"], sa.Text):
        op.alter_column("export_records", "status", existing_type=export_columns["status"]["type"], type_=sa.Text(), existing_nullable=False)
    if "status_mode" not in export_columns:
        op.add_column("export_records", sa.Column("status_mode", sa.String(16), nullable=False, server_default="bucket"))


def downgrade() -> None:
    op.drop_constraint("fk_batch_items_status", "validation_batch_items", type_="foreignkey")
    op.drop_constraint("fk_credentials_status", "credentials", type_="foreignkey")
    op.drop_index("idx_credentials_type_status_created", table_name="credentials")
    op.drop_table("credential_statuses")
    op.drop_column("export_records", "status_mode")
    # Reject narrowing if long scopes would be lost rather than truncating them.
    op.alter_column("export_records", "status", existing_type=sa.Text(), type_=sa.String(128), existing_nullable=False)
