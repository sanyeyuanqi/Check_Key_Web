"""Opt-in bulk-ingestion tests against disposable, synthetic PostgreSQL databases.

Set TEST_PLATFORM_DATABASE_URL to an expendable PostgreSQL admin database and run
``python -m pytest tests/test_bulk_ingest_postgres.py -q``. Every test creates and
drops a new checkapi_bulk_ingest_test_* database. No application URL is used and
no Redis client is opened. The default run skips these database tests.
"""

import asyncio
from datetime import UTC, datetime, timedelta
import importlib
import os
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import asyncpg
from cryptography.fernet import Fernet
import pytest
from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.security as security
from app.schemas import ValidationResultInput
from test_platform_partition_migration import migrate


OBSERVED = datetime(2026, 10, 17, 4, tzinfo=UTC)
PLATFORMS = [
    ("OpenAI", "openai_official", "credentials_openai"),
    ("Anthropic", "anthropic_official", "credentials_anthropic"),
    ("Google", "google_ai_studio_gemini", "credentials_google"),
    ("Azure", "azure_gpt", "credentials_azure"),
    ("AWS Bedrock", "aws_bedrock_api", "credentials_aws"),
    ("OpenRouter", "openrouter", "credentials_openrouter"),
    ("TokenRouter", "tokenrouter", "credentials_tokenrouter"),
    ("Vercel", "vercel", "credentials_vercel"),
    ("OpenCode", "opencode_zen", "credentials_opencode"),
    ("Cloudflare", "cloudflare_workers_ai", "credentials_cloudflare"),
]
TABLES = (
    "credentials", "credential_registry", "credential_statuses", "valid_key_records",
    "validation_batch_items", "validation_batches",
)


def run(coroutine):
    return asyncio.run(coroutine)


def database_error(action, error):
    # Never interpolate a connection exception: driver messages may contain a URL.
    raise AssertionError(
        f"Isolated PostgreSQL {action} failed ({type(error).__name__}); connection details withheld"
    ) from None


async def connect(database):
    try:
        return await asyncpg.connect(database.render_as_string(hide_password=False), timeout=10)
    except Exception as error:
        database_error("connection", error)


@pytest.fixture
def bulk_database():
    raw_url = os.environ.get("TEST_PLATFORM_DATABASE_URL")
    if not raw_url:
        pytest.skip("Set TEST_PLATFORM_DATABASE_URL to run isolated PostgreSQL bulk-ingestion tests")
    try:
        admin_url = make_url(raw_url)
        if admin_url.get_backend_name() != "postgresql":
            raise ValueError("Unsupported database driver")
        admin_url = admin_url.set(drivername="postgresql")
    except Exception as error:
        database_error("URL validation", error)
    name = "checkapi_bulk_ingest_test_" + uuid.uuid4().hex
    database = admin_url.set(database=name)

    async def create():
        connection = await connect(admin_url)
        try:
            await connection.execute(f'CREATE DATABASE "{name}"')
        except Exception as error:
            database_error("creation", error)
        finally:
            await connection.close()

    async def drop():
        assert re.fullmatch(r"checkapi_bulk_ingest_test_[0-9a-f]{32}", name)
        connection = await connect(admin_url)
        try:
            await connection.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        except Exception as error:
            database_error("cleanup", error)
        finally:
            await connection.close()

    run(create())
    try:
        try:
            migrate(database)
        except Exception as error:
            database_error("migration", error)
        yield database
    finally:
        run(drop())


@pytest.fixture
def bulk(monkeypatch, bulk_database):
    module = importlib.import_module("app.bulk_ingest")
    # Use a one-test encryption key, not application credentials or configuration.
    monkeypatch.setattr(security, "settings", SimpleNamespace(fernet_key=Fernet.generate_key().decode("ascii")))
    monkeypatch.setattr(module, "invalidate_query_cache", AsyncMock())
    return module


def result(index, secret, *, status="valid", provider="OpenAI", storage_type="openai_official", **metadata):
    return ValidationResultInput(
        index=index, status=status, usable=status == "valid",
        credential={
            # Requested type is authoritative over an untrusted provider label.
            "provider": provider, "kind": "synthetic", "storage_type": storage_type,
            "secret": secret, "masked": "synthetic…test", "category": "合成测试",
        },
        **metadata,
    )


def job(bulk, batch_id, results, *, second=0, sequence=0, source_ip="203.0.113.7"):
    when = OBSERVED + timedelta(seconds=second)
    return bulk.QueuedResultJob(
        message_id=f"{int(when.timestamp() * 1000)}-{sequence}", batch_id=batch_id,
        results=results, source_ip=source_ip, observed_at=when,
    )


def queue_key(message_id):
    milliseconds, sequence = message_id.split("-")
    return f"{int(milliseconds):020d}-{int(sequence):020d}"


async def seed_batches(database, totals):
    connection = await connect(database)
    try:
        await connection.executemany("""
            INSERT INTO validation_batches (
                id,node_id,provider_label,total,valid_count,recovery_count,
                insufficient_count,invalid_count,received_count
            ) VALUES ($1,'synthetic-bulk-node','synthetic-bulk-providers',$2,0,0,0,0,0)
        """, list(totals.items()))
    finally:
        await connection.close()


async def snapshot(database):
    connection = await connect(database)
    try:
        return {
            table: [dict(row) for row in await connection.fetch(f"SELECT * FROM {table} ORDER BY 1,2")]
            for table in TABLES
        }
    finally:
        await connection.close()


def session_factory(database):
    assert re.fullmatch(r"checkapi_bulk_ingest_test_[0-9a-f]{32}", database.database or "")
    engine = create_async_engine(database.set(drivername="postgresql+asyncpg"))
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def observe_transactions(session):
    events = []
    event.listen(session.sync_session, "after_commit", lambda _: events.append("commit"))
    event.listen(session.sync_session, "after_rollback", lambda _: events.append("rollback"))
    return events


def test_one_thousand_results_across_ten_platforms_and_four_batches_commit_once(bulk_database, bulk, monkeypatch):
    async def scenario():
        await seed_batches(bulk_database, {batch: 250 for batch in range(1, 5)})
        statuses = ("valid", "usage_limit_reached", "insufficient_credit", "invalid", "synthetic_future_state")
        jobs = []
        for shard in range(20):
            rows = []
            for offset in range(50):
                absolute = shard * 50 + offset
                provider, storage_type, _ = PLATFORMS[absolute % len(PLATFORMS)]
                metadata = {} if absolute == 0 else {
                    "model": "synthetic-model", "region": "合成区域", "message": "合成检测说明",
                    "http_status": 200, "attempts": 2, "duration_ms": 37,
                }
                rows.append(result(
                    (shard % 5) * 50 + offset + 1, f"synthetic-token-{absolute:04d}",
                    status=statuses[absolute % len(statuses)], provider="untrusted-alias",
                    storage_type=storage_type, **metadata,
                ))
            jobs.append(job(bulk, shard // 5 + 1, rows, second=shard, source_ip="2001:db8::7"))

        engine, sessions = session_factory(bulk_database)
        try:
            async with sessions() as session:
                events = observe_transactions(session)

                async def invalidation(family):
                    events.append(f"invalidate:{family}")

                invalidator = AsyncMock(side_effect=invalidation)
                monkeypatch.setattr(bulk, "invalidate_query_cache", invalidator)
                assert await bulk.process_queued_results(session, jobs) == 1000
                assert events == ["commit", "invalidate:credentials"]
                invalidator.assert_awaited_once_with("credentials")
        finally:
            await engine.dispose()

        connection = await connect(bulk_database)
        try:
            assert await connection.fetchval("SELECT COUNT(*) FROM credentials") == 1000
            assert await connection.fetchval("SELECT COUNT(*) FROM credential_registry") == 1000
            assert await connection.fetchval("SELECT COUNT(*) FROM validation_batch_items") == 1000
            assert await connection.fetchval("SELECT COUNT(*) FROM valid_key_records") == 200
            assert await connection.fetchval("SELECT COUNT(*) FROM credentials WHERE source_ip='2001:db8::7'") == 1000
            partitions = await connection.fetch("""
                SELECT provider, tableoid::regclass::text AS partition, COUNT(*) AS count
                FROM credentials GROUP BY provider,tableoid ORDER BY provider
            """)
            assert {row["provider"]: (row["partition"], row["count"]) for row in partitions} == {
                provider: (partition, 100) for provider, _, partition in PLATFORMS
            }
            counts = await connection.fetch("""
                SELECT total,received_count,valid_count,recovery_count,insufficient_count,
                    invalid_count,completed_at FROM validation_batches ORDER BY id
            """)
            assert len(counts) == 4
            assert all(tuple(row.values())[:6] == (250, 250, 50, 50, 50, 100) for row in counts)
            assert all(row["completed_at"] is not None for row in counts)
            unknown = await connection.fetchrow("SELECT * FROM credential_statuses WHERE value='synthetic_future_state'")
            assert unknown["name"] == "synthetic_future_state"
            first = await connection.fetchrow("SELECT * FROM credentials WHERE fingerprint=$1", security.fingerprint("synthetic-token-0000"))
            last = await connection.fetchrow("SELECT * FROM credentials WHERE fingerprint=$1", security.fingerprint("synthetic-token-0999"))
            assert first["model"] is first["region"] is first["message"] is None
            assert (last["model"], last["region"], last["message"]) == ("synthetic-model", "合成区域", "合成检测说明")
            assert security.decrypt_secret(first["encrypted_secret"]) == "synthetic-token-0000"
            assert first["encrypted_secret"] != "synthetic-token-0000"
            assert last["queue_event_id"] == queue_key(jobs[-1].message_id)
            assert last["updated_at"] == jobs[-1].observed_at
        finally:
            await connection.close()

    run(scenario())


def test_duplicate_keys_and_indices_keep_latest_values_and_all_intermediate_valid_history(bulk_database, bulk):
    async def scenario():
        await seed_batches(bulk_database, {1: 3, 2: 2})
        shared = "synthetic-shared-token"
        oldest = job(bulk, 1, [
            result(1, shared), result(2, shared, status="invalid"),
            result(3, "synthetic-other-token", status="invalid"),
        ])
        middle = job(bulk, 1, [result(1, shared, status="invalid")], second=1, source_ip=None)
        valid_again = job(bulk, 2, [result(1, shared)], second=2, source_ip="2001:db8::9")
        latest = job(bulk, 2, [
            result(1, shared, status="synthetic_future_state"),
            result(2, shared, provider="Anthropic", storage_type="anthropic_official"),
        ], second=3, source_ip=None)
        # Arrival order is intentionally different from stream order.
        jobs = [latest, oldest, valid_again, middle]
        engine, sessions = session_factory(bulk_database)
        try:
            async with sessions() as session:
                assert await bulk.process_queued_results(session, jobs) == 7
        finally:
            await engine.dispose()

        connection = await connect(bulk_database)
        try:
            assert await connection.fetchval("SELECT COUNT(*) FROM credentials") == 3
            assert await connection.fetchval("SELECT COUNT(*) FROM credential_registry") == 3
            assert await connection.fetchval("SELECT COUNT(*) FROM validation_batch_items") == 5
            shared_row = await connection.fetchrow("""
                SELECT * FROM credentials WHERE storage_type='openai_official' AND fingerprint=$1
            """, security.fingerprint(shared))
            assert shared_row["status"] == "synthetic_future_state"
            assert shared_row["source_ip"] == "2001:db8::9"
            assert shared_row["updated_at"] == latest.observed_at
            assert shared_row["queue_event_id"] == queue_key(latest.message_id)
            assert security.decrypt_secret(shared_row["encrypted_secret"]) == shared
            history = await connection.fetchrow("SELECT * FROM valid_key_records WHERE credential_id=$1", shared_row["id"])
            assert (history["first_valid_at"], history["last_valid_at"]) == (oldest.observed_at, valid_again.observed_at)
            assert await connection.fetchval("SELECT COUNT(*) FROM valid_key_records") == 2
            items = await connection.fetch("SELECT batch_id,item_index,status FROM validation_batch_items ORDER BY batch_id,item_index")
            assert [tuple(row.values()) for row in items] == [
                (1, 1, "invalid"), (1, 2, "invalid"), (1, 3, "invalid"),
                (2, 1, "synthetic_future_state"), (2, 2, "valid"),
            ]
            batches = await connection.fetch("SELECT received_count,valid_count,invalid_count FROM validation_batches ORDER BY id")
            assert [tuple(row.values()) for row in batches] == [(3, 0, 3), (2, 1, 1)]
        finally:
            await connection.close()

        # Retrying the complete group must not re-encrypt, re-date, or duplicate rows.
        before = await snapshot(bulk_database)
        engine, sessions = session_factory(bulk_database)
        try:
            async with sessions() as session:
                assert await bulk.process_queued_results(session, jobs) == 7
        finally:
            await engine.dispose()
        assert await snapshot(bulk_database) == before

    run(scenario())


def test_old_and_identical_stream_messages_cannot_overwrite_new_results_or_newer_legacy_upload(bulk_database, bulk):
    async def scenario():
        await seed_batches(bulk_database, {1: 1})
        secret = "synthetic-replay-token"
        old = job(bulk, 1, [result(1, secret)], source_ip="203.0.113.1")
        new = job(bulk, 1, [result(1, secret, status="invalid")], second=2, source_ip="203.0.113.2")
        same_id_changed_payload = job(
            bulk, 1, [result(1, secret, status="invalid", model="synthetic-replayed-metadata")],
            second=2, source_ip="203.0.113.99",
        )
        engine, sessions = session_factory(bulk_database)
        try:
            async with sessions() as session:
                await bulk.process_queued_results(session, [old])
                await bulk.process_queued_results(session, [new])
                before = await snapshot(bulk_database)
                await bulk.process_queued_results(session, [old, same_id_changed_payload])
                assert await snapshot(bulk_database) == before

                # Pre-upgrade synchronous uploads have no event ID. A queued result
                # older than their existing updated_at must still leave them intact.
                connection = await connect(bulk_database)
                try:
                    await connection.execute("""
                        UPDATE credentials SET status='rate_limited',result_bucket='invalid',
                            queue_event_id=NULL,source_ip='203.0.113.3',updated_at=$1
                    """, OBSERVED + timedelta(seconds=3))
                finally:
                    await connection.close()
                legacy_newer = await snapshot(bulk_database)
                await bulk.process_queued_results(session, [new])
                assert await snapshot(bulk_database) == legacy_newer
        finally:
            await engine.dispose()

    run(scenario())


def test_missing_source_ip_preserves_known_ip_across_committed_groups(bulk_database, bulk):
    async def scenario():
        await seed_batches(bulk_database, {1: 1})
        secret = "synthetic-source-ip-token"
        jobs = [
            job(bulk, 1, [result(1, secret, status="invalid")], source_ip="203.0.113.7"),
            job(bulk, 1, [result(1, secret, status="rate_limited")], second=1, source_ip=None),
            job(bulk, 1, [result(1, secret)], second=2, source_ip="2001:db8::8"),
        ]
        engine, sessions = session_factory(bulk_database)
        try:
            async with sessions() as session:
                for current, expected in zip(jobs, ("203.0.113.7", "203.0.113.7", "2001:db8::8"), strict=True):
                    await bulk.process_queued_results(session, [current])
                    connection = await connect(bulk_database)
                    try:
                        row = await connection.fetchrow("SELECT source_ip,status,queue_event_id FROM credentials")
                        assert row["source_ip"] == expected
                        assert row["status"] == current.results[0].status
                        assert row["queue_event_id"] == queue_key(current.message_id)
                    finally:
                        await connection.close()
        finally:
            await engine.dispose()

    run(scenario())


def test_database_failure_rolls_back_every_table_and_never_invalidates_cache(bulk_database, bulk):
    async def scenario():
        await seed_batches(bulk_database, {1: 1000})
        engine, sessions = session_factory(bulk_database)
        try:
            async with sessions() as session:
                await bulk.process_queued_results(session, [job(bulk, 1, [result(1, "synthetic-existing", status="invalid")])])
                before = await snapshot(bulk_database)
                bulk.invalidate_query_cache.reset_mock()
                connection = await connect(bulk_database)
                try:
                    # Fail inside PostgreSQL after preceding credential, directory,
                    # registry and valid-history mutations have been attempted.
                    await connection.execute("""
                        CREATE FUNCTION synthetic_reject_bulk_item() RETURNS trigger LANGUAGE plpgsql AS $$
                        BEGIN
                            IF NEW.item_index=999 THEN
                                RAISE EXCEPTION 'synthetic bulk failure' USING ERRCODE='23514';
                            END IF;
                            RETURN NEW;
                        END; $$;
                        CREATE TRIGGER synthetic_reject_bulk_item BEFORE INSERT ON validation_batch_items
                        FOR EACH ROW EXECUTE FUNCTION synthetic_reject_bulk_item();
                    """)
                finally:
                    await connection.close()
                events = observe_transactions(session)
                failing = job(bulk, 1, [
                    result(1, "synthetic-existing"),
                    result(999, "synthetic-new-token", status="synthetic_rollback_state"),
                ], second=1, source_ip="2001:db8::99")
                with pytest.raises(SQLAlchemyError):
                    await bulk.process_queued_results(session, [failing])
                assert events == ["rollback"]
                bulk.invalidate_query_cache.assert_not_awaited()
                assert await snapshot(bulk_database) == before
        finally:
            await engine.dispose()

    run(scenario())
