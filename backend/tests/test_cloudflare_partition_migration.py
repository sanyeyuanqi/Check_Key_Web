"""Cloudflare migration checks against opt-in isolated synthetic PostgreSQL databases."""

from datetime import timedelta

import asyncpg
import pytest

from test_opencode_partition_migration import complete_state, physical_locations
from test_platform_partition_migration import (
    OBSERVED,
    connect,
    insert_credential,
    isolated_database,
    migrate,
    run,
)


async def insert_cloudflare(connection, identifier, *, status="invalid"):
    await insert_credential(connection, identifier, "Cloudflare", "cloudflare_workers_ai", status=status)
    await connection.execute("UPDATE credentials SET kind='cloudflare' WHERE id=$1", identifier)


async def seed_history(database):
    connection = await connect(database)
    try:
        async with connection.transaction():
            await insert_credential(connection, 1, "Anthropic", "anthropic_official")
            await insert_credential(connection, 2, "HistoricalProvider", "historical_type")
            await insert_credential(connection, 3, "OpenCode", "opencode_zen")
            await insert_credential(connection, 4, "Vercel", "vercel")
            await insert_cloudflare(connection, 9001, status="invalid")
            await insert_cloudflare(connection, 9002, status="valid")
            await connection.execute("""
                INSERT INTO validation_batches (id,node_id,provider_label,total,valid_count,
                    recovery_count,insufficient_count,invalid_count,received_count,started_at,completed_at)
                VALUES (78,'synthetic-cloudflare-node','Cloudflare',2,2,0,0,0,2,$1,$1)
            """, OBSERVED)
            await connection.executemany("""
                INSERT INTO validation_batch_items (id,batch_id,credential_id,item_index,status,result_bucket)
                VALUES ($1,78,$2,$3,'valid','valid')
            """, [(9001, 9001, 1), (9002, 9002, 2)])
            await connection.executemany("""
                INSERT INTO valid_key_records (credential_id,first_valid_at,last_valid_at)
                VALUES ($1,$2,$3)
            """, [(identifier, OBSERVED, OBSERVED + timedelta(hours=1)) for identifier in (9001, 9002)])
            await connection.execute("SELECT setval(pg_get_serial_sequence('credentials','id'),10000,true)")
    finally:
        await connection.close()


async def partition_indexes(database):
    connection = await connect(database)
    try:
        return {row["oid"]: dict(row) for row in await connection.fetch("""
            SELECT index_state.indexrelid::bigint AS oid,index_state.indisvalid,
                index_state.indisready,pg_get_indexdef(index_state.indexrelid) AS definition
            FROM pg_index AS index_state
            JOIN pg_partition_tree('credentials') AS partition ON partition.relid=index_state.indrelid
        """)}
    finally:
        await connection.close()


async def assert_partition_integrity(database, *, present=True):
    connection = await connect(database)
    try:
        assert await connection.fetchval("SELECT to_regclass('credentials_cloudflare') IS NOT NULL") is present
        targets = ["credentials_other"] + (["credentials_cloudflare"] if present else [])
        for name in targets:
            triggers = await connection.fetch("""
                SELECT tgname,tgenabled='O' AS enabled FROM pg_trigger
                WHERE tgrelid=to_regclass($1) AND NOT tgisinternal
            """, name)
            assert {row["tgname"] for row in triggers} == {
                "sync_credential_registry", "cleanup_credential_registry", "enforce_credential_id",
            }
            assert all(row["enabled"] for row in triggers)
            indexes = await connection.fetch("""
                SELECT indisvalid,indisready FROM pg_index WHERE indrelid=to_regclass($1)
            """, name)
            assert len(indexes) == 7
            assert all(row["indisvalid"] and row["indisready"] for row in indexes)
        if present:
            assert await connection.fetchval("""
                SELECT COUNT(*) FROM pg_inherits WHERE inhrelid='credentials_cloudflare'::regclass
                    AND inhparent='credentials'::regclass
            """) == 1
            assert await connection.fetchval("""
                SELECT COUNT(*) FROM pg_constraint WHERE conrelid='credentials_cloudflare'::regclass
                    AND contype='f' AND confrelid='credential_statuses'::regclass
            """) == 1
        assert await connection.fetchval("SELECT to_regclass('pg_temp.cloudflare_partition_stage') IS NULL")
    finally:
        await connection.close()


def test_fresh_head_creates_cloudflare_with_inherited_identity_and_history_constraints(isolated_database):
    database = isolated_database
    migrate(database)
    run(assert_partition_integrity(database))

    async def verify():
        connection = await connect(database)
        try:
            async with connection.transaction():
                await insert_cloudflare(connection, 1, status="valid")
                await insert_credential(connection, 2, "OpenCode", "opencode_zen", status="valid")
                await insert_credential(connection, 3, "Vercel", "vercel", status="valid")
                await connection.execute("""
                    INSERT INTO valid_key_records (credential_id,first_valid_at,last_valid_at)
                    VALUES (1,$1,$1)
                """, OBSERVED)
            assert await connection.fetchval("SELECT provider FROM credential_registry WHERE id=1") == "Cloudflare"
            with pytest.raises(asyncpg.UniqueViolationError):
                await insert_credential(connection, 1, "OpenAI", "openai_official", fingerprint="synthetic-duplicate-global-id")
            with pytest.raises(asyncpg.CheckViolationError):
                await connection.execute("UPDATE credentials SET id=999,provider='HistoricalProvider' WHERE id=1")
        finally:
            await connection.close()

    run(verify())
    assert run(physical_locations(database)) == {
        1: "credentials_cloudflare", 2: "credentials_opencode", 3: "credentials_vercel",
    }


def test_default_cloudflare_history_and_other_platforms_survive_upgrade_rollback_reupgrade(isolated_database):
    database = isolated_database
    migrate(database, "20261017_0012")
    run(seed_history(database))
    before = run(complete_state(database))
    other_indexes = run(partition_indexes(database))
    assert run(physical_locations(database))[9001] == "credentials_other"

    migrate(database)

    assert run(complete_state(database)) == before
    assert run(physical_locations(database)) == {
        1: "credentials_anthropic", 2: "credentials_other", 3: "credentials_opencode",
        4: "credentials_vercel", 9001: "credentials_cloudflare", 9002: "credentials_cloudflare",
    }
    new_indexes = run(partition_indexes(database))
    assert {identifier: new_indexes[identifier] for identifier in other_indexes} == other_indexes
    run(assert_partition_integrity(database))

    async def append_after_upgrade():
        connection = await connect(database)
        try:
            async with connection.transaction():
                await insert_cloudflare(connection, 9003, status="valid")
                await connection.execute("""
                    INSERT INTO valid_key_records (credential_id,first_valid_at,last_valid_at)
                    VALUES (9003,$1,$1)
                """, OBSERVED)
        finally:
            await connection.close()

    run(append_after_upgrade())
    expanded = run(complete_state(database))
    migrate(database, "20261017_0012", downgrade=True)

    assert run(complete_state(database)) == expanded
    locations = run(physical_locations(database))
    assert all(locations[identifier] == "credentials_other" for identifier in (9001, 9002, 9003))
    restored_indexes = run(partition_indexes(database))
    assert restored_indexes == other_indexes
    run(assert_partition_integrity(database, present=False))

    migrate(database)

    assert run(complete_state(database)) == expanded
    locations = run(physical_locations(database))
    assert all(locations[identifier] == "credentials_cloudflare" for identifier in (9001, 9002, 9003))
    reupgraded_indexes = run(partition_indexes(database))
    assert {identifier: reupgraded_indexes[identifier] for identifier in other_indexes} == other_indexes
    run(assert_partition_integrity(database))


def test_failed_cloudflare_partition_creation_preserves_rows_histories_and_trigger_state(isolated_database):
    database = isolated_database
    migrate(database, "20261017_0012")
    run(seed_history(database))

    async def reserve_target_name():
        connection = await connect(database)
        try:
            await connection.execute("CREATE TABLE credentials_cloudflare (synthetic_sentinel integer)")
            await connection.execute("INSERT INTO credentials_cloudflare VALUES (78)")
        finally:
            await connection.close()

    run(reserve_target_name())
    before = run(complete_state(database))
    result = migrate(database, success=False)
    assert "already exists" in result.stderr
    assert run(complete_state(database)) == before
    assert run(physical_locations(database))[9001] == "credentials_other"

    async def verify():
        connection = await connect(database)
        try:
            assert await connection.fetchval("SELECT version_num FROM alembic_version") == "20261017_0012"
            assert await connection.fetchval("SELECT synthetic_sentinel FROM credentials_cloudflare") == 78
            assert await connection.fetchval("""
                SELECT tgenabled='O' FROM pg_trigger WHERE tgrelid='credentials_other'::regclass
                    AND tgname='cleanup_credential_registry'
            """)
            await connection.execute("DROP TABLE credentials_cloudflare")
        finally:
            await connection.close()

    run(verify())
    migrate(database)
    assert run(complete_state(database)) == before
    run(assert_partition_integrity(database))
