"""Opt-in real PostgreSQL migration checks, using isolated synthetic databases.

Set TEST_PLATFORM_DATABASE_URL to an expendable PostgreSQL admin database.
The fixture creates and drops only databases named checkapi_partition_test_*.
No application database or settings URL is used for these tests.
"""

import asyncio
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import quote, quote_plus
import uuid

import asyncpg
import pytest
from sqlalchemy.engine import make_url


BACKEND = Path(__file__).parents[1]
# This fixture represents rows already present before revision 0009. Platforms
# introduced by later migrations need their own revision-specific fixtures.
PLATFORMS = [
    ("OpenAI", "openai_official", "credentials_openai"),
    ("Anthropic", "anthropic_official", "credentials_anthropic"),
    ("Google", "google_ai_studio_gemini", "credentials_google"),
    ("Azure", "azure_gpt", "credentials_azure"),
    ("AWS Bedrock", "aws_bedrock_api", "credentials_aws"),
    ("OpenRouter", "openrouter", "credentials_openrouter"),
    ("TokenRouter", "tokenrouter", "credentials_tokenrouter"),
    ("HistoricalProvider", "historical_unknown_type", "credentials_other"),
]
OBSERVED = datetime(2026, 9, 2, 15, 15, tzinfo=UTC)


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture
def isolated_database():
    raw_url = os.environ.get("TEST_PLATFORM_DATABASE_URL")
    if not raw_url:
        pytest.skip("Set TEST_PLATFORM_DATABASE_URL to run isolated PostgreSQL migration tests")
    admin_url = make_url(raw_url).set(drivername="postgresql")
    database_name = "checkapi_partition_test_" + uuid.uuid4().hex

    async def create():
        connection = await asyncpg.connect(admin_url.render_as_string(hide_password=False))
        try:
            await connection.execute(f'CREATE DATABASE "{database_name}"')
        finally:
            await connection.close()

    run(create())
    test_url = admin_url.set(database=database_name)
    yield test_url

    async def drop():
        assert database_name.startswith("checkapi_partition_test_") and len(database_name) == 56
        connection = await asyncpg.connect(admin_url.render_as_string(hide_password=False))
        try:
            await connection.execute(f'DROP DATABASE "{database_name}" WITH (FORCE)')
        finally:
            await connection.close()

    run(drop())


def migrate(database, target="head", *, downgrade=False, success=True):
    environment = dict(os.environ)
    environment["DATABASE_URL"] = database.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade" if downgrade else "upgrade", target],
        cwd=BACKEND, env=environment, capture_output=True, text=True,
    )
    result.stdout = redact_migration_output(result.stdout, database)
    result.stderr = redact_migration_output(result.stderr, database)
    if success:
        errors = [line.strip() for line in result.stderr.splitlines() if "Error:" in line or "Exception:" in line]
        summary = errors[-1][:240] if errors else "Migration diagnostics captured and redacted"
        assert result.returncode == 0, f"Alembic migration to {target} failed: {summary}"
    else:
        assert result.returncode != 0
    return result


def redact_migration_output(output, database):
    from app.config import settings

    passwords = {database.password, make_url(settings.database_url).password}
    variants = {
        variant
        for password in passwords if password
        for variant in (password, quote(password, safe=""), quote_plus(password, safe=""))
    }
    variants.update(value.replace("%", "%%") for value in list(variants))
    for value in sorted(variants, key=len, reverse=True):
        output = output.replace(value, "[REDACTED]")
    return re.sub(r"(postgres(?:ql)?(?:\+\w+)?://[^:\s/]+):[^@\s]+@", r"\1:[REDACTED]@", output)


async def connect(database):
    return await asyncpg.connect(database.render_as_string(hide_password=False))


async def insert_credential(connection, identifier, provider, storage_type, *, fingerprint=None, status="invalid"):
    return await connection.fetchval("""
        INSERT INTO credentials (
            id, fingerprint, storage_type, provider, kind, encrypted_secret, key_prefix,
            masked, category, status, result_bucket, usable, model, http_status,
            attempts, duration_ms, region, message, reset_at, source, source_ip,
            created_at, updated_at
        ) VALUES ($1, $2, $3, $4, 'synthetic', $5, $6, 'synthetic…test', '测试',
                  $7, $8, $9, 'synthetic-model', 429, 2, 37, 'synthetic-region',
                  'synthetic-message', 'synthetic-reset', 'desktop', '203.0.113.7', $10, $11)
        RETURNING id
    """, identifier, fingerprint or f"synthetic-fingerprint-{identifier}", storage_type,
        provider, f"synthetic-encrypted-payload-{identifier}", f"synthetic-prefix-{identifier}",
        status, "valid" if status == "valid" else "invalid", status == "valid",
        OBSERVED, OBSERVED + timedelta(days=1))


async def seed(database, *, history_table=True):
    connection = await connect(database)
    try:
        async with connection.transaction():
            for identifier, (provider, storage_type, _) in enumerate(PLATFORMS, 1):
                await insert_credential(connection, identifier, provider, storage_type)
            await connection.execute("""
                INSERT INTO validation_batches (id,node_id,provider_label,total,valid_count,
                    recovery_count,insufficient_count,invalid_count,received_count,started_at,completed_at)
                VALUES (1,'synthetic-node','synthetic-provider',8,8,0,0,0,8,$1,$1)
            """, OBSERVED)
            await connection.executemany("""
                INSERT INTO validation_batch_items (id,batch_id,credential_id,item_index,status,result_bucket)
                VALUES ($1,1,$2,$3,'valid','valid')
            """, [(identifier, identifier, identifier) for identifier in range(1, 9)])
            if history_table:
                await connection.executemany("""
                    INSERT INTO valid_key_records (credential_id,first_valid_at,last_valid_at)
                    VALUES ($1,$2,$3)
                """, [(identifier, OBSERVED, OBSERVED + timedelta(hours=1)) for identifier in range(1, 9)])
            await connection.execute("SELECT setval(pg_get_serial_sequence('credentials','id'),5000,true)")
    finally:
        await connection.close()


async def snapshot(database):
    connection = await connect(database)
    try:
        return {
            table: [dict(row) for row in await connection.fetch(f"SELECT * FROM {table} ORDER BY 1")]
            for table in ("credentials", "validation_batch_items", "valid_key_records", "credential_statuses")
        }
    finally:
        await connection.close()


def test_legacy_upgrade_preserves_every_field_histories_and_real_platform_storage(isolated_database):
    database = isolated_database
    migrate(database, "20261017_0008")
    run(seed(database))
    before = run(snapshot(database))

    migrate(database)

    assert run(snapshot(database)) == before

    async def verify():
        connection = await connect(database)
        try:
            assert await connection.fetchval("SELECT relkind FROM pg_class WHERE oid='credentials'::regclass") == b"p"
            assert await connection.fetchval("SELECT COUNT(*) FROM ONLY credentials") == 0
            locations = await connection.fetch("SELECT provider,tableoid::regclass::text AS physical FROM credentials")
            assert {row["provider"]: row["physical"] for row in locations} == {
                provider: table for provider, _, table in PLATFORMS
            }
            assert await connection.fetchval("SELECT COUNT(*) FROM credential_registry") == 8
            fks = await connection.fetch("""
                SELECT conrelid::regclass::text AS source,confrelid::regclass::text AS target
                FROM pg_constraint WHERE contype='f' AND conrelid IN
                    ('validation_batch_items'::regclass,'valid_key_records'::regclass)
            """)
            assert ("valid_key_records", "credential_registry") in {(row["source"], row["target"]) for row in fks}
            assert ("validation_batch_items", "credential_registry") in {(row["source"], row["target"]) for row in fks}
            generated = await connection.fetchval("""
                INSERT INTO credentials (fingerprint,storage_type,provider,kind,encrypted_secret,
                    key_prefix,masked,category,status,result_bucket,usable,source)
                VALUES ('synthetic-next','openai_official','OpenAI','synthetic','synthetic-cipher',
                    'synthetic-next','synthetic','测试','valid','valid',true,'desktop') RETURNING id
            """)
            assert generated > 5000
            assert await connection.fetchval("SELECT provider FROM credential_registry WHERE id=$1", generated) == "OpenAI"
        finally:
            await connection.close()

    run(verify())
    after = run(snapshot(database))
    migrate(database, "20261017_0008", downgrade=True)
    assert run(snapshot(database)) == after
    migrate(database)
    assert run(snapshot(database)) == after


def test_partition_identity_upsert_fk_delete_and_row_movement_are_atomic(isolated_database):
    database = isolated_database
    migrate(database, "20261017_0008")
    run(seed(database))
    migrate(database)

    async def verify():
        connection = await connect(database)
        try:
            async with connection.transaction():
                await connection.execute("UPDATE credentials SET provider='AnotherHistoricalProvider' WHERE id=8")
            assert await connection.fetchval("SELECT provider FROM credential_registry WHERE id=8") == "AnotherHistoricalProvider"
            assert await connection.fetchval("SELECT COUNT(*) FROM valid_key_records WHERE credential_id=8") == 1
            assert await connection.fetchval("SELECT COUNT(*) FROM validation_batch_items WHERE credential_id=8") == 1

            for operation in (
                lambda: insert_credential(connection, 1, "Anthropic", "anthropic_official", fingerprint="synthetic-other-id"),
                lambda: insert_credential(connection, 6001, "Anthropic", "openai_official", fingerprint="synthetic-fingerprint-1"),
            ):
                with pytest.raises(asyncpg.UniqueViolationError):
                    async with connection.transaction():
                        await operation()
            assert await connection.fetchval("SELECT COUNT(*) FROM credentials") == 8
            assert await connection.fetchval("SELECT provider FROM credential_registry WHERE id=1") == "OpenAI"
            with pytest.raises(asyncpg.CheckViolationError):
                await connection.execute("UPDATE credentials SET id=9000 WHERE id=1")
            with pytest.raises(asyncpg.CheckViolationError):
                await connection.execute("UPDATE credentials SET id=9000,provider='AnotherProvider' WHERE id=8")

            with pytest.raises(asyncpg.ForeignKeyViolationError):
                async with connection.transaction():
                    await connection.execute("DELETE FROM credentials WHERE id=1")
            assert await connection.fetchval("SELECT COUNT(*) FROM credentials WHERE id=1") == 1

            copied_id = await connection.fetchval("""
                INSERT INTO credentials (fingerprint,storage_type,provider,kind,encrypted_secret,
                    key_prefix,masked,category,status,result_bucket,usable,source)
                VALUES ('synthetic-fingerprint-1','openai_official','OpenAI','synthetic','synthetic-updated',
                    'synthetic-updated','synthetic','测试','valid','valid',true,'desktop')
                ON CONFLICT ON CONSTRAINT uq_credentials_type_fingerprint
                DO UPDATE SET encrypted_secret=EXCLUDED.encrypted_secret,status=EXCLUDED.status
                RETURNING id
            """)
            assert copied_id == 1
            assert await connection.fetchval("SELECT COUNT(*) FROM credential_registry") == 8
            async with connection.transaction():
                await insert_credential(connection, 7000, "OpenAI", "openai_official")
                await connection.execute("""
                    INSERT INTO validation_batch_items (id,batch_id,credential_id,item_index,status,result_bucket)
                    VALUES (7000,1,7000,7000,'invalid','invalid')
                """)
            with pytest.raises(asyncpg.CheckViolationError):
                await connection.execute("UPDATE credentials SET id=7001,provider='Anthropic' WHERE id=7000")
            async with connection.transaction():
                await connection.execute("DELETE FROM credentials WHERE id=7000")
            assert await connection.fetchval("SELECT COUNT(*) FROM credential_registry WHERE id=7000") == 0
            assert await connection.fetchval("SELECT COUNT(*) FROM validation_batch_items WHERE credential_id=7000") == 0
        finally:
            await connection.close()

    run(verify())


def test_install_stopped_at_status_dictionary_and_fresh_head_both_work(isolated_database):
    database = isolated_database
    migrate(database, "20261016_0006")
    run(seed(database, history_table=False))
    migrate(database)

    async def check_backfill():
        connection = await connect(database)
        try:
            assert await connection.fetchval("SELECT COUNT(*) FROM valid_key_records") == 8
            assert await connection.fetchval("SELECT COUNT(*) FROM credentials WHERE status='invalid'") == 8
        finally:
            await connection.close()

    run(check_backfill())
    migrate(database, "base", downgrade=True)
    migrate(database)

    async def check_fresh():
        connection = await connect(database)
        try:
            assert await connection.fetchval("SELECT COUNT(*) FROM credential_registry") == 0
            identifier = await connection.fetchval("""
                INSERT INTO credentials (fingerprint,storage_type,provider,kind,encrypted_secret,
                    key_prefix,masked,category,status,result_bucket,usable,source)
                VALUES ('synthetic-fresh','tokenrouter','TokenRouter','synthetic','synthetic-cipher',
                    'synthetic-fresh','synthetic','测试','valid','valid',true,'desktop') RETURNING id
            """)
            assert identifier == 1
        finally:
            await connection.close()

    run(check_fresh())


def test_legacy_provider_type_mismatch_cancels_migration_without_changes(isolated_database):
    database = isolated_database
    migrate(database, "20261017_0008")
    run(seed(database))

    async def corrupt_provider():
        connection = await connect(database)
        try:
            await connection.execute("UPDATE credentials SET provider='HistoricalAlias' WHERE id=1")
        finally:
            await connection.close()

    run(corrupt_provider())
    before = run(snapshot(database))
    result = migrate(database, success=False)
    assert "provider/type mismatch" in result.stderr
    assert run(snapshot(database)) == before

    async def check_rollback():
        connection = await connect(database)
        try:
            assert await connection.fetchval("SELECT version_num FROM alembic_version") == "20261017_0008"
            assert await connection.fetchval("SELECT to_regclass('credential_registry') IS NULL")
        finally:
            await connection.close()

    run(check_rollback())


def test_concurrent_cross_partition_id_and_fingerprint_conflicts_are_rejected(isolated_database):
    database = isolated_database
    migrate(database)

    async def verify():
        first = await connect(database)
        second = await connect(database)
        try:
            for first_id, second_id, storage_type, fingerprint in (
                (42, 42, "historical_unknown_type", None),
                (43, 44, "historical_unknown_type", "synthetic-shared-identity"),
            ):
                transaction = first.transaction()
                await transaction.start()
                await insert_credential(first, first_id, "OpenAI", storage_type, fingerprint=fingerprint)

                async def contender():
                    async with second.transaction():
                        await insert_credential(second, second_id, "Anthropic", storage_type, fingerprint=fingerprint)

                pending = asyncio.create_task(contender())
                # Both statements overlap while the first transaction owns registry locks.
                await asyncio.sleep(0.05)
                await transaction.commit()
                with pytest.raises(asyncpg.UniqueViolationError):
                    await pending
            assert await first.fetchval("SELECT COUNT(*) FROM credentials") == 2
            assert await first.fetchval("SELECT COUNT(*) FROM credential_registry") == 2
            assert await first.fetchval("SELECT COUNT(*) FROM credential_registry WHERE provider='OpenAI'") == 2
        finally:
            await first.close()
            await second.close()

    run(verify())
