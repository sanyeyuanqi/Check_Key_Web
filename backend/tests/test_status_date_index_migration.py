"""Real PostgreSQL partition-index migration tests using synthetic databases only."""

import asyncio

from test_platform_partition_migration import (
    PLATFORMS,
    connect,
    isolated_database,
    migrate,
    run,
    seed,
    snapshot,
)


OLD_INDEX = "idx_credentials_status_prefix_created"
NEW_INDEX = "idx_credentials_status_created"
PREFIX_INDEX = "idx_credentials_prefix_created"


async def index_tree(database, name):
    connection = await connect(database)
    try:
        return [dict(row) for row in await connection.fetch("""
            WITH RECURSIVE index_tree(oid) AS (
                SELECT to_regclass($1)::oid
                UNION ALL
                SELECT child.inhrelid FROM pg_inherits AS child
                JOIN index_tree AS parent ON child.inhparent=parent.oid
            )
            SELECT index_state.indexrelid::bigint AS oid,
                relation.relname AS table_name, index_state.indisvalid,
                index_state.indisready, index_state.indnkeyatts,
                pg_get_indexdef(index_state.indexrelid,1,false) AS first_column,
                pg_get_indexdef(index_state.indexrelid,2,false) AS second_column,
                (index_state.indoption[1] & 1)=1 AS second_desc,
                (index_state.indoption[2] & 1)=1 AS third_desc,
                second_operator.opcname AS second_opclass
            FROM index_tree JOIN pg_index AS index_state ON index_state.indexrelid=index_tree.oid
            JOIN pg_class AS relation ON relation.oid=index_state.indrelid
            JOIN pg_opclass AS second_operator ON second_operator.oid=index_state.indclass[1]
            ORDER BY relation.relname
        """, name)]
    finally:
        await connection.close()


async def oids_exist(database, identifiers):
    connection = await connect(database)
    try:
        return await connection.fetchval("SELECT COUNT(*) FROM pg_class WHERE oid=ANY($1::oid[])", identifiers)
    finally:
        await connection.close()


async def all_partition_indexes(database):
    connection = await connect(database)
    try:
        return {row["oid"]: dict(row) for row in await connection.fetch("""
            SELECT index_state.indexrelid::bigint AS oid,
                index_state.indisvalid,index_state.indisready,
                pg_get_indexdef(index_state.indexrelid) AS definition
            FROM pg_index AS index_state
            JOIN pg_partition_tree('credentials') AS partition ON partition.relid=index_state.indrelid
        """)}
    finally:
        await connection.close()


def assert_complete_tree(tree, *, columns):
    assert {row["table_name"] for row in tree} == {"credentials", *(table for _, _, table in PLATFORMS)}
    assert len(tree) == 9
    assert all(row["indisvalid"] and row["indisready"] for row in tree)
    assert all(row["indnkeyatts"] == columns for row in tree)


def test_partition_index_upgrade_downgrade_reupgrade_preserves_rows_and_prefix_index(isolated_database):
    database = isolated_database
    migrate(database, "20261017_0009")
    run(seed(database))
    before = run(snapshot(database))
    old_tree = run(index_tree(database, OLD_INDEX))
    assert_complete_tree(old_tree, columns=3)
    all_before = run(all_partition_indexes(database))
    unaffected = {identifier: row for identifier, row in all_before.items() if identifier not in {item["oid"] for item in old_tree}}
    prefix_tree = run(index_tree(database, PREFIX_INDEX))
    assert_complete_tree(prefix_tree, columns=2)

    migrate(database, "20261017_0010")

    assert run(snapshot(database)) == before
    assert run(index_tree(database, OLD_INDEX)) == []
    assert run(oids_exist(database, [row["oid"] for row in old_tree])) == 0
    assert run(index_tree(database, PREFIX_INDEX)) == prefix_tree
    new_tree = run(index_tree(database, NEW_INDEX))
    assert_complete_tree(new_tree, columns=2)
    assert all((row["first_column"], row["second_column"]) == ("status", "created_at") for row in new_tree)
    assert all(row["second_desc"] for row in new_tree)
    all_after = run(all_partition_indexes(database))
    assert all(row["indisvalid"] and row["indisready"] for row in all_after.values())
    assert {identifier: all_after[identifier] for identifier in unaffected} == unaffected

    migrate(database, "20261017_0009", downgrade=True)

    assert run(snapshot(database)) == before
    assert run(index_tree(database, NEW_INDEX)) == []
    assert run(oids_exist(database, [row["oid"] for row in new_tree])) == 0
    assert run(index_tree(database, PREFIX_INDEX)) == prefix_tree
    after_downgrade = run(all_partition_indexes(database))
    assert {identifier: after_downgrade[identifier] for identifier in unaffected} == unaffected
    restored = run(index_tree(database, OLD_INDEX))
    assert_complete_tree(restored, columns=3)
    assert all((row["first_column"], row["second_column"]) == ("status", "key_prefix") for row in restored)
    assert all(row["second_opclass"] == "varchar_pattern_ops" and row["third_desc"] for row in restored)

    migrate(database, "20261017_0010")

    assert run(snapshot(database)) == before
    assert_complete_tree(run(index_tree(database, NEW_INDEX)), columns=2)
    assert run(index_tree(database, OLD_INDEX)) == []
    assert run(index_tree(database, PREFIX_INDEX)) == prefix_tree


def test_lock_timeout_rolls_back_entire_index_swap(isolated_database):
    database = isolated_database
    migrate(database, "20261017_0009")
    run(seed(database))
    before = run(snapshot(database))
    old_tree = run(index_tree(database, OLD_INDEX))

    async def block_swap():
        connection = await connect(database)
        try:
            async with connection.transaction():
                await connection.execute("LOCK TABLE credentials IN ACCESS SHARE MODE")
                result = await asyncio.to_thread(migrate, database, "20261017_0010", success=False)
                assert "lock timeout" in result.stderr
        finally:
            await connection.close()

    run(block_swap())

    assert run(snapshot(database)) == before
    assert run(index_tree(database, OLD_INDEX)) == old_tree
    assert run(index_tree(database, NEW_INDEX)) == []
    migrate(database, "20261017_0010")
    assert_complete_tree(run(index_tree(database, NEW_INDEX)), columns=2)
