"""OpenCode migration tests run only in opt-in, isolated synthetic PostgreSQL databases."""

from datetime import timedelta

import asyncpg
import pytest

from test_platform_partition_migration import (
    OBSERVED,
    connect,
    insert_credential,
    isolated_database,
    migrate,
    migration_snapshot_row,
    run,
    snapshot,
)


async def complete_state(database):
    state = await snapshot(database)
    connection = await connect(database)
    try:
        for table in ("credential_registry", "validation_batches"):
            state[table] = [migration_snapshot_row(row) for row in await connection.fetch(f"SELECT * FROM {table} ORDER BY 1")]
        sequence = await connection.fetchval("SELECT pg_get_serial_sequence('credentials','id')")
        state["sequence"] = dict(await connection.fetchrow(f"SELECT last_value,is_called FROM {sequence}"))
        return state
    finally:
        await connection.close()


async def seed_history(database):
    connection = await connect(database)
    try:
        async with connection.transaction():
            await insert_credential(connection, 1, "Anthropic", "anthropic_official")
            await insert_credential(connection, 2, "HistoricalProvider", "historical_type")
            await insert_credential(connection, 9001, "OpenCode", "opencode_zen", status="invalid")
            await insert_credential(connection, 9002, "OpenCode", "opencode_zen", status="valid")
            await connection.execute("""
                INSERT INTO validation_batches (id,node_id,provider_label,total,valid_count,
                    recovery_count,insufficient_count,invalid_count,received_count,started_at,completed_at)
                VALUES (77,'synthetic-node','OpenCode',2,2,0,0,0,2,$1,$1)
            """, OBSERVED)
            await connection.executemany("""
                INSERT INTO validation_batch_items (id,batch_id,credential_id,item_index,status,result_bucket)
                VALUES ($1,77,$2,$3,'valid','valid')
            """, [(9001, 9001, 1), (9002, 9002, 2)])
            await connection.executemany("""
                INSERT INTO valid_key_records (credential_id,first_valid_at,last_valid_at)
                VALUES ($1,$2,$3)
            """, [(identifier, OBSERVED, OBSERVED + timedelta(hours=1)) for identifier in (9001, 9002)])
            await connection.execute("SELECT setval(pg_get_serial_sequence('credentials','id'),10000,true)")
    finally:
        await connection.close()


async def physical_locations(database):
    connection = await connect(database)
    try:
        return {row["id"]: row["physical"] for row in await connection.fetch(
            "SELECT id,tableoid::regclass::text AS physical FROM credentials ORDER BY id"
        )}
    finally:
        await connection.close()


async def assert_partition_integrity(database, *, present=True):
    connection = await connect(database)
    try:
        assert await connection.fetchval("SELECT to_regclass('credentials_opencode') IS NOT NULL") is present
        targets = ["credentials_other"] + (["credentials_opencode"] if present else [])
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
                SELECT COUNT(*) FROM pg_inherits WHERE inhrelid='credentials_opencode'::regclass
                    AND inhparent='credentials'::regclass
            """) == 1
            assert await connection.fetchval("""
                SELECT COUNT(*) FROM pg_constraint WHERE conrelid='credentials_opencode'::regclass
                    AND contype='f' AND confrelid='credential_statuses'::regclass
            """) == 1
        assert await connection.fetchval("SELECT to_regclass('pg_temp.opencode_partition_stage') IS NULL")
    finally:
        await connection.close()


async def vercel_indexes(database):
    connection = await connect(database)
    try:
        return [dict(row) for row in await connection.fetch("""
            SELECT indexrelid::bigint AS oid,indisvalid,indisready,pg_get_indexdef(indexrelid) AS definition
            FROM pg_index WHERE indrelid='credentials_vercel'::regclass ORDER BY indexrelid
        """)]
    finally:
        await connection.close()


def test_fresh_head_creates_opencode_and_preserves_vercel_inherited_behavior(isolated_database):
    database = isolated_database
    migrate(database)
    run(assert_partition_integrity(database))

    async def verify():
        connection = await connect(database)
        try:
            async with connection.transaction():
                await insert_credential(connection, 1, "OpenCode", "opencode_zen", status="valid")
                await insert_credential(connection, 2, "Vercel", "vercel", status="valid")
                await connection.execute("""
                    INSERT INTO valid_key_records (credential_id,first_valid_at,last_valid_at)
                    VALUES (1,$1,$1)
                """, OBSERVED)
            assert await connection.fetchval("SELECT provider FROM credential_registry WHERE id=1") == "OpenCode"
            with pytest.raises(asyncpg.UniqueViolationError):
                await insert_credential(connection, 1, "OpenAI", "openai_official", fingerprint="synthetic-duplicate-global-id")
            with pytest.raises(asyncpg.CheckViolationError):
                await connection.execute("UPDATE credentials SET id=999,provider='HistoricalProvider' WHERE id=1")
        finally:
            await connection.close()

    run(verify())
    assert run(physical_locations(database)) == {1: "credentials_opencode", 2: "credentials_vercel"}


def test_default_opencode_rows_and_histories_survive_upgrade_downgrade_reupgrade(isolated_database):
    database = isolated_database
    # Production can be on 0010 while 0011 is present only in the source tree.
    migrate(database, "20261017_0010")
    run(seed_history(database))
    before = run(complete_state(database))
    assert run(physical_locations(database))[9001] == "credentials_other"
    migrate(database, "20261017_0011")
    vercel_before = run(vercel_indexes(database))

    migrate(database)

    assert run(complete_state(database)) == before
    assert run(physical_locations(database)) == {
        1: "credentials_anthropic", 2: "credentials_other",
        9001: "credentials_opencode", 9002: "credentials_opencode",
    }
    assert run(vercel_indexes(database)) == vercel_before
    run(assert_partition_integrity(database))

    async def append_after_upgrade():
        connection = await connect(database)
        try:
            async with connection.transaction():
                await insert_credential(connection, 9003, "OpenCode", "opencode_zen", status="valid")
                await connection.execute("""
                    INSERT INTO valid_key_records (credential_id,first_valid_at,last_valid_at)
                    VALUES (9003,$1,$1)
                """, OBSERVED)
        finally:
            await connection.close()

    run(append_after_upgrade())
    expanded = run(complete_state(database))
    migrate(database, "20261017_0011", downgrade=True)

    assert run(complete_state(database)) == expanded
    assert all(run(physical_locations(database))[identifier] == "credentials_other" for identifier in (9001, 9002, 9003))
    assert run(vercel_indexes(database)) == vercel_before
    run(assert_partition_integrity(database, present=False))

    migrate(database)

    assert run(complete_state(database)) == expanded
    assert all(run(physical_locations(database))[identifier] == "credentials_opencode" for identifier in (9001, 9002, 9003))
    assert run(vercel_indexes(database)) == vercel_before
    run(assert_partition_integrity(database))


def test_failed_partition_creation_rolls_back_rows_and_trigger_state(isolated_database):
    database = isolated_database
    migrate(database, "20261017_0011")
    run(seed_history(database))

    async def reserve_target_name():
        connection = await connect(database)
        try:
            await connection.execute("CREATE TABLE credentials_opencode (synthetic_sentinel integer)")
            await connection.execute("INSERT INTO credentials_opencode VALUES (77)")
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
            assert await connection.fetchval("SELECT version_num FROM alembic_version") == "20261017_0011"
            assert await connection.fetchval("SELECT synthetic_sentinel FROM credentials_opencode") == 77
            assert await connection.fetchval("""
                SELECT tgenabled='O' FROM pg_trigger WHERE tgrelid='credentials_other'::regclass
                    AND tgname='cleanup_credential_registry'
            """)
            await connection.execute("DROP TABLE credentials_opencode")
        finally:
            await connection.close()

    run(verify())
    migrate(database)
    assert run(complete_state(database)) == before
    run(assert_partition_integrity(database))
