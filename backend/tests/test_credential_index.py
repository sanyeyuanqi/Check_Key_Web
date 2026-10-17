from contextlib import nullcontext
import importlib.util
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from app.database import Credential


INDEX_NAME = "idx_credentials_status_prefix_created"
MIGRATION_PATH = Path(__file__).parents[1] / "alembic/versions/20261017_0008_status_prefix_created_index.py"


@pytest.fixture
def migration():
    spec = importlib.util.spec_from_file_location("credential_index_migration", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_postgresql_metadata_separates_status_time_and_key_prefix_indexes():
    indexes = {index.name: index for index in Credential.__table__.indexes}
    statement = str(CreateIndex(indexes["idx_credentials_status_created"]).compile(dialect=postgresql.dialect()))

    assert statement == (
        "CREATE INDEX idx_credentials_status_created ON credentials "
        "(status, created_at DESC)"
    )
    assert INDEX_NAME not in indexes
    prefix_statement = str(CreateIndex(indexes["idx_credentials_prefix_created"]).compile(dialect=postgresql.dialect()))
    assert prefix_statement == (
        "CREATE INDEX idx_credentials_prefix_created ON credentials "
        "(key_prefix varchar_pattern_ops, created_at DESC)"
    )
    assert {
        "idx_credentials_prefix_created", "idx_credentials_type_status_created",
        "idx_credentials_type_bucket_created", "idx_credentials_provider_created",
    } <= indexes.keys()


def test_historical_migration_generates_concurrent_postgresql_ddl(migration):
    output = StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={
        "as_sql": True, "output_buffer": output,
    })
    with Operations.context(context):
        migration.upgrade()
        migration.downgrade()

    script = output.getvalue()
    assert (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_credentials_status_prefix_created "
        "ON credentials (status, key_prefix varchar_pattern_ops, created_at DESC);"
    ) in script
    assert "DROP INDEX CONCURRENTLY IF EXISTS idx_credentials_status_prefix_created;" in script
    assert "DROP TABLE" not in script
    assert "SELECT" not in script
    assert migration.down_revision == "20261016_0007"


@pytest.mark.parametrize("is_valid,creates,drops", [
    (None, 1, 0), (True, 0, 0), (False, 1, 1),
])
def test_online_migration_handles_missing_fresh_and_interrupted_indexes(
    monkeypatch, migration, is_valid, creates, drops,
):
    operations = MagicMock()
    operations.get_context.return_value.as_sql = False
    operations.get_context.return_value.autocommit_block.return_value = nullcontext()
    operations.get_bind.return_value.scalar.return_value = is_valid
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    operations.get_context.return_value.autocommit_block.assert_called_once()
    assert operations.create_index.call_count == creates
    assert operations.drop_index.call_count == drops
    query, parameters = operations.get_bind.return_value.scalar.call_args.args
    assert "pg_index" in str(query)
    assert parameters == {"index_name": INDEX_NAME}
    if creates:
        args, options = operations.create_index.call_args
        assert args[0:2] == (INDEX_NAME, "credentials")
        assert [str(item) for item in args[2]] == ["status", "key_prefix", "created_at DESC"]
        assert options == {
            "postgresql_ops": {"key_prefix": "varchar_pattern_ops"},
            "postgresql_concurrently": True,
            "if_not_exists": True,
        }
    if drops:
        operations.drop_index.assert_called_once_with(
            INDEX_NAME, table_name="credentials", if_exists=True, postgresql_concurrently=True,
        )
        names = [call[0] for call in operations.mock_calls]
        assert names.index("drop_index") < names.index("create_index")


def test_new_migration_generates_transactional_parent_index_swap_with_timeouts():
    path = Path(__file__).parents[1] / "alembic/versions/20261017_0010_separate_status_date_index.py"
    spec = importlib.util.spec_from_file_location("separate_status_date_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={
        "as_sql": True, "output_buffer": output,
    })
    with Operations.context(context):
        module.upgrade()
        module.downgrade()

    script = output.getvalue()
    assert "SET LOCAL lock_timeout = '5s';" in script
    assert "SET LOCAL statement_timeout = '60s';" in script
    assert (
        "CREATE INDEX IF NOT EXISTS idx_credentials_status_created "
        "ON credentials (status, created_at DESC);"
    ) in script
    assert "DROP INDEX IF EXISTS idx_credentials_status_prefix_created;" in script
    assert (
        "CREATE INDEX IF NOT EXISTS idx_credentials_status_prefix_created "
        "ON credentials (status, key_prefix varchar_pattern_ops, created_at DESC);"
    ) in script
    assert "DROP INDEX IF EXISTS idx_credentials_status_created;" in script
    assert "CONCURRENTLY" not in script
    assert "idx_credentials_prefix_created" not in script
    assert "DROP TABLE" not in script
    assert module.down_revision == "20261017_0009"
