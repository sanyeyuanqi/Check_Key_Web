"""Add an OpenCode partition without discarding historical default-partition rows.

Revision ID: 20261017_0012
Revises: 20261017_0011
Create Date: 2026-10-17
"""

from alembic import op
import sqlalchemy as sa


revision = "20261017_0012"
down_revision = "20261017_0011"
branch_labels = None
depends_on = None

PARTITION = "credentials_opencode"
STAGE = "opencode_partition_stage"


def _set_timeouts() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '60s'")


def _columns(bind) -> str:
    quote = bind.dialect.identifier_preparer.quote
    return ", ".join(quote(column["name"]) for column in sa.inspect(bind).get_columns("credentials"))


def _verify_copy(bind, source: str, target: str, columns: str) -> None:
    same = bind.scalar(sa.text(f"""
        SELECT NOT EXISTS (
            (SELECT {columns} FROM {source} EXCEPT ALL
                SELECT {columns} FROM {target} WHERE provider = 'OpenCode')
            UNION ALL
            (SELECT {columns} FROM {target} WHERE provider = 'OpenCode' EXCEPT ALL
                SELECT {columns} FROM {source})
        )
    """))
    if same is not True:
        raise RuntimeError("OpenCode partition copy verification failed; migration cancelled")


def upgrade() -> None:
    _set_timeouts()
    op.execute("LOCK TABLE credentials, credentials_other IN ACCESS EXCLUSIVE MODE")
    bind = op.get_bind()
    columns = _columns(bind)
    cleanup_enabled = bind.scalar(sa.text("""
        SELECT tgenabled = 'O' FROM pg_trigger WHERE tgrelid = 'credentials_other'::regclass
            AND tgname = 'cleanup_credential_registry'
    """))
    if cleanup_enabled is not True:
        raise RuntimeError("Default credential cleanup trigger is missing or disabled; migration cancelled")

    op.execute(f"""
        CREATE TEMPORARY TABLE {STAGE} ON COMMIT DROP AS
        SELECT {columns} FROM credentials_other WHERE provider = 'OpenCode'
    """)
    # These rows are moved, not removed. Avoid both orphan cleanup and pending
    # deferred-trigger events while PostgreSQL changes the default partition bound.
    op.execute("ALTER TABLE credentials_other DISABLE TRIGGER cleanup_credential_registry")
    op.execute("DELETE FROM credentials_other WHERE provider = 'OpenCode'")
    op.execute(f"CREATE TABLE {PARTITION} PARTITION OF credentials FOR VALUES IN ('OpenCode')")
    op.execute(f"INSERT INTO credentials ({columns}) SELECT {columns} FROM pg_temp.{STAGE}")
    _verify_copy(bind, f"pg_temp.{STAGE}", PARTITION, columns)
    op.execute("ALTER TABLE credentials_other ENABLE TRIGGER cleanup_credential_registry")


def downgrade() -> None:
    _set_timeouts()
    op.execute(f"LOCK TABLE credentials, credentials_other, {PARTITION} IN ACCESS EXCLUSIVE MODE")
    bind = op.get_bind()
    columns = _columns(bind)
    # Detach retains the complete rows and expands the default partition bound.
    # Dropping this detached table after copying does not fire row-delete triggers.
    op.execute(f"ALTER TABLE credentials DETACH PARTITION {PARTITION}")
    op.execute(f"INSERT INTO credentials ({columns}) SELECT {columns} FROM {PARTITION}")
    _verify_copy(bind, PARTITION, "credentials_other", columns)
    op.drop_table(PARTITION)
