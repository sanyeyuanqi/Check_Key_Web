"""Opt-in synthetic export cursor benchmark; never reads application records.

Set EXPORT_READ_BENCHMARK=1 and TEST_PLATFORM_DATABASE_URL to run. The fixture
creates and removes a new checkapi_bulk_ingest_test_* database, migrated exactly
like the app. Timings are diagnostic, not a CI performance threshold.
"""

import asyncio
from datetime import UTC, datetime, timedelta
import json
import os
from statistics import median
from time import perf_counter

from cryptography.fernet import Fernet
import pytest
from sqlalchemy.dialects import postgresql

from app.credential_service import credential_export_statement
from app.valid_key_service import valid_key_export_statement
from test_bulk_ingest_postgres import bulk_database, connect, database_error, session_factory


pytestmark = pytest.mark.skipif(
    os.environ.get("EXPORT_READ_BENCHMARK") != "1",
    reason="Set EXPORT_READ_BENCHMARK=1 for disposable synthetic speed diagnostics",
)


def _plan_nodes(plan):
    result = [{
        "node": plan["Node Type"], "rows": plan.get("Actual Rows"),
        "total_ms": plan.get("Actual Total Time"),
        **({"index": plan["Index Name"]} if "Index Name" in plan else {}),
        **({"sort": plan["Sort Method"], "space_kib": plan.get("Sort Space Used")}
           if "Sort Method" in plan else {}),
    }]
    for child in plan.get("Plans", []):
        result.extend(_plan_nodes(child))
    return result


def _sql(statement):
    # Every literal is fixed synthetic scope; no real credential is compiled.
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_synthetic_one_hundred_thousand_export_cursor_read_speed(bulk_database):
    async def scenario():
        connection = await connect(bulk_database)
        seed_started = perf_counter()
        try:
            token = Fernet(Fernet.generate_key()).encrypt(b"synthetic-export-speed-key-00000000000000000000").decode("ascii")
            await connection.execute("""
                INSERT INTO credentials (
                    id,fingerprint,storage_type,provider,kind,encrypted_secret,key_prefix,
                    masked,category,status,result_bucket,usable,source,source_ip,created_at,updated_at
                ) SELECT n,lpad(n::text,64,'0'),'openai_official','OpenAI',
                    'synthetic-speed',$1,'synthetic-speed','synthetic...test',
                    'synthetic', CASE WHEN n%3=0 THEN 'invalid' ELSE 'valid' END,
                    CASE WHEN n%3=0 THEN 'invalid' ELSE 'valid' END,
                    n%3<>0,'synthetic','203.0.113.7',
                    timestamptz '2026-10-17 00:00:00+00' + n*interval '1 second',
                    timestamptz '2026-10-17 00:00:00+00' + n*interval '1 second'
                FROM generate_series(1,100000) n
            """, token)
            await connection.execute("ANALYZE credentials_openai")
            await connection.execute("ANALYZE credentials")
            await connection.execute("""
                INSERT INTO valid_key_records (credential_id,first_valid_at,last_valid_at)
                SELECT n,timestamptz '2026-10-17 00:00:00+00',
                    timestamptz '2026-10-17 00:00:00+00' + n*interval '1 second'
                FROM generate_series(1,100000) n
            """)
            await connection.execute("ANALYZE valid_key_records")
            assert await connection.fetchval("SELECT COUNT(*) FROM credentials") == 100000
            assert await connection.fetchval("SELECT COUNT(*) FROM export_records") == 0
            seed_seconds = perf_counter() - seed_started

            statements = {
                "all": credential_export_statement(status="all", provider="OpenAI", storage_type="openai_official"),
                "archives": valid_key_export_statement(provider="OpenAI"),
                "status_dates": credential_export_statement(
                    status="valid", status_mode="exact", provider="OpenAI", storage_type="openai_official",
                    created_from=datetime(2026, 10, 17, tzinfo=UTC) + timedelta(seconds=25000),
                    created_before=datetime(2026, 10, 17, tzinfo=UTC) + timedelta(seconds=75000),
                ),
            }
            plans = {}
            for name, statement in statements.items():
                raw = await connection.fetchval("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + _sql(statement))
                document = json.loads(raw)[0]
                plans[name] = {"planning_ms": document["Planning Time"], "execution_ms": document["Execution Time"], "nodes": _plan_nodes(document["Plan"])}

            engine, sessions = session_factory(bulk_database)
            measurements = []
            try:
                for method in ("session", "core", "asyncpg"):
                    for batch_size in (1000, 5000, 10000):
                        samples = []
                        first_samples = []
                        for _ in range(3):
                            count = 0
                            total_chars = 0
                            first_seconds = None
                            if method == "session":
                                async with sessions() as session:
                                    start = perf_counter()
                                    stream = await session.stream_scalars(statements["all"].execution_options(yield_per=batch_size))
                                    try:
                                        async for partition in stream.partitions(batch_size):
                                            first_seconds = first_seconds or perf_counter() - start
                                            count += len(partition)
                                            total_chars += sum(map(len, partition))
                                    finally:
                                        await stream.close()
                                    seconds = perf_counter() - start
                            elif method == "core":
                                async with engine.connect() as core:
                                    start = perf_counter()
                                    stream = await core.stream_scalars(statements["all"].execution_options(yield_per=batch_size))
                                    try:
                                        async for partition in stream.partitions(batch_size):
                                            first_seconds = first_seconds or perf_counter() - start
                                            count += len(partition)
                                            total_chars += sum(map(len, partition))
                                    finally:
                                        await stream.close()
                                    seconds = perf_counter() - start
                            else:
                                async with connection.transaction():
                                    start = perf_counter()
                                    cursor = await connection.cursor(_sql(statements["all"]))
                                    while partition := await cursor.fetch(batch_size):
                                        first_seconds = first_seconds or perf_counter() - start
                                        count += len(partition)
                                        total_chars += sum(len(row[0]) for row in partition)
                                    seconds = perf_counter() - start
                            assert count == 100000 and total_chars == len(token) * 100000
                            samples.append(seconds)
                            first_samples.append(first_seconds)
                        measurements.append({
                            "method": method, "batch": batch_size, "records": count,
                            "median_seconds": round(median(samples), 4),
                            "first_batch_seconds": round(median(first_samples), 4),
                            "samples_seconds": [round(value, 4) for value in samples],
                        })
            finally:
                await engine.dispose()
            assert await connection.fetchval("SELECT COUNT(*) FROM export_records") == 0
            print("SYNTHETIC_EXPORT_READ_SPEED=" + json.dumps({
                "records": 100000, "seed_seconds": round(seed_seconds, 3),
                "measurements": measurements, "plans": plans,
            }, ensure_ascii=True))
        finally:
            await connection.close()

    try:
        asyncio.run(scenario())
    except (AssertionError, KeyboardInterrupt):
        raise
    except Exception as error:
        database_error("synthetic cursor speed benchmark", error)
