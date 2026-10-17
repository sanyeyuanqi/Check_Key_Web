import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import Select

import app.bulk_ingest as bulk
from app.platform_catalog import STORAGE_TYPE_PROVIDERS
from app.schemas import ValidationResultInput


BASE = datetime(2026, 10, 17, tzinfo=UTC)
BASE_MS = int(BASE.timestamp() * 1000)


def run(coroutine):
    return asyncio.run(coroutine)


def result(index=1, *, secret="synthetic-bulk-key", status="valid", storage_type="openai_official", **overrides):
    return ValidationResultInput.model_validate({
        "index": index, "status": status, "usable": status == "valid",
        "credential": {
            "provider": "synthetic-ignored-provider", "kind": "openai", "storage_type": storage_type,
            "secret": secret, "masked": "synthetic…tail", "category": "synthetic-category",
        },
        "model": "synthetic-model", "http_status": 200, "attempts": 2,
        "duration_ms": 15, "region": "synthetic-region", "message": "synthetic-message", "reset_at": None,
        **overrides,
    })


def job(offset=0, *, batch_id=1, items=None, source_ip="192.0.2.10", sequence=0):
    identifier = f"{BASE_MS + offset}-{sequence}"
    return bulk.QueuedResultJob(
        message_id=identifier, batch_id=batch_id, results=items if items is not None else [result()],
        source_ip=source_ip, observed_at=bulk.queue_observed_at(identifier),
    )


@pytest.fixture(autouse=True)
def synthetic_crypto(monkeypatch):
    monkeypatch.setattr(bulk, "fingerprint", lambda secret: hashlib.sha256(secret.encode()).hexdigest())
    encryption = MagicMock(side_effect=lambda secret: "synthetic-cipher:" + secret)
    monkeypatch.setattr(bulk, "encrypt_secret", encryption)
    return encryption


def identity(item):
    return item.credential.requested_storage_type, hashlib.sha256(item.credential.secret.encode()).hexdigest()


def captured_session(monkeypatch, jobs, *, batches=None, fail_on=None, missing_registry=False):
    events = []
    statements = []
    identities = sorted({identity(item) for queued in jobs for item in queued.results})
    ids = {key: index for index, key in enumerate(identities, 1)}

    async def execute(statement):
        statements.append(statement)
        if isinstance(statement, Select):
            events.append("registry")
            requested = next(value for value in statement.compile().params.values() if isinstance(value, list))
            return SimpleNamespace(all=lambda: [] if missing_registry else [(*key, ids[key]) for key in requested])
        table = statement.table.name
        events.append(table)
        if table == fail_on:
            raise RuntimeError("synthetic-write-failure")
        return SimpleNamespace()

    async def commit():
        events.append("commit")
        if fail_on == "commit":
            raise RuntimeError("synthetic-commit-failure")

    async def rollback():
        events.append("rollback")

    async def invalidate(family):
        events.append("invalidate:" + family)

    session = SimpleNamespace(
        scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: sorted({queued.batch_id for queued in jobs}) if batches is None else batches)),
        execute=AsyncMock(side_effect=execute), commit=AsyncMock(side_effect=commit), rollback=AsyncMock(side_effect=rollback),
    )
    monkeypatch.setattr(bulk, "invalidate_query_cache", AsyncMock(side_effect=invalidate))
    return session, events, statements, ids


def sql(statement):
    return str(statement.compile(dialect=postgresql.dialect()))


def inserted_rows(statement):
    return [{getattr(key, "key", key): value for key, value in row.items()} for row in statement._multi_values[0]]


@pytest.mark.parametrize("identifier", [
    "", "1", "1-", "-1", "-1-0", "1--1", "1-0\n", "a-1", "1-a", "1-0-extra",
    "18446744073709551616-0", "1-18446744073709551616", "000000000000000000001-0", 1, None, b"1-0",
])
def test_stream_ids_reject_invalid_or_overflowing_input(identifier):
    with pytest.raises(ValueError, match="Invalid queue message ID"):
        bulk.queue_event_key(identifier)


def test_stream_ids_compare_by_numeric_sequence_and_time():
    raw = ["9-9", "10-0", "10-1", "10-10", "11-0"]
    normalized = [bulk.queue_event_key(value) for value in raw]
    assert normalized == sorted(normalized)
    assert all(len(value) == 41 for value in normalized)
    assert bulk.queue_event_key("00010-00001") == bulk.queue_event_key("10-1")
    assert bulk.queue_event_key("18446744073709551615-18446744073709551615") == "18446744073709551615-18446744073709551615"


def test_event_time_uses_exact_stream_milliseconds_in_utc():
    assert bulk.queue_observed_at("1-999") == datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=1)
    assert bulk.queue_observed_at(f"{BASE_MS + 123}-0") == BASE + timedelta(milliseconds=123)
    with pytest.raises(ValueError, match="outside the supported range"):
        bulk.queue_observed_at("18446744073709551615-0")


@pytest.mark.parametrize("storage_type,provider", list(STORAGE_TYPE_PROVIDERS.items()))
def test_all_supported_types_resolve_to_canonical_partition(storage_type, provider):
    observation = bulk._observations([job(items=[result(storage_type=storage_type)])])[0]
    assert observation.storage_type == storage_type
    assert observation.provider == provider


def test_credential_dedup_uses_latest_id_even_if_jobs_arrive_out_of_order_and_retains_last_known_ip(synthetic_crypto):
    jobs = [
        job(30, items=[result(status="invalid")], source_ip=None),
        job(10, items=[result(status="valid")], source_ip="192.0.2.10"),
        job(20, items=[result(status="usage_limit_reached")], source_ip="192.0.2.20"),
        job(5, items=[result(secret="synthetic-second-key", status="valid")], source_ip=None),
    ]
    rows, _ = bulk._credential_rows(bulk._observations(jobs))
    assert len(rows) == 2
    first = next(row for row in rows if row["fingerprint"] == identity(result())[1])
    assert first["status"] == "invalid"
    assert first["source_ip"] == "192.0.2.20"
    assert first["queue_event_id"] == bulk.queue_event_key(jobs[0].message_id)
    assert first["updated_at"] == first["created_at"] == BASE + timedelta(milliseconds=30)
    assert synthetic_crypto.call_count == 2


def test_same_secret_in_different_storage_types_is_not_collapsed():
    rows, _ = bulk._credential_rows(bulk._observations([job(items=[
        result(storage_type="openai_official"), result(2, storage_type="anthropic_official"),
    ])]))
    assert len(rows) == 2
    assert rows[0]["fingerprint"] == rows[1]["fingerprint"]
    assert {row["provider"] for row in rows} == {"OpenAI", "Anthropic"}


def test_stream_time_is_authoritative_over_supplied_payload_clock():
    queued = job()
    changed = bulk.QueuedResultJob(queued.message_id, queued.batch_id, queued.results, queued.source_ip, BASE + timedelta(days=1))
    assert bulk._observations([changed])[0].observed_at == BASE


def test_single_transaction_uses_actual_bulk_statements_for_one_thousand_records(monkeypatch):
    jobs = [job(offset, batch_id=1 if offset < 500 else 2, items=[result(offset + 1, secret=f"synthetic-key-{offset}")]) for offset in range(1000)]
    session, events, statements, _ = captured_session(monkeypatch, jobs)

    assert run(bulk.process_queued_results(session, jobs)) == 1000
    assert events == ["credential_statuses", "credentials", "registry", "valid_key_records", "validation_batch_items", "validation_batches", "commit", "invalidate:credentials"]
    session.scalars.assert_awaited_once()
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()
    lock_sql = sql(session.scalars.await_args.args[0])
    assert "ORDER BY validation_batches.id FOR UPDATE" in lock_sql
    inserts = [statement for statement in statements if getattr(getattr(statement, "table", None), "name", None) in {"credentials", "validation_batch_items", "valid_key_records"}]
    assert len(inserts) == 3
    assert all(len(statement._multi_values[0]) == 1000 for statement in inserts)
    assert "UPDATE validation_batches SET" in sql(statements[-1])
    assert "FROM (SELECT validation_batch_items.batch_id" in sql(statements[-1])


def test_sql_chunks_stay_under_asyncpg_parameter_limit_in_worst_source_ip_case():
    jobs = [
        queued
        for offset in range(1000)
        for queued in (
            job(offset, items=[result(offset + 1, secret=f"synthetic-key-{offset}")]),
            job(offset + 1000, items=[result(offset + 1, secret=f"synthetic-key-{offset}", status="invalid")], source_ip=None),
        )
    ]
    rows, known_ips = bulk._credential_rows(bulk._observations(jobs))
    statement = bulk._credential_upsert(rows, known_ips)
    assert len(rows) == 1000
    assert len(statement.compile(dialect=postgresql.dialect()).params) < 32767


def test_more_than_one_thousand_records_are_split_into_bounded_sql_chunks(monkeypatch):
    jobs = [job(offset, items=[result(offset + 1, secret=f"synthetic-key-{offset}", status="invalid")]) for offset in range(1200)]
    session, events, statements, _ = captured_session(monkeypatch, jobs)
    assert run(bulk.process_queued_results(session, jobs)) == 1200
    for table in ("credentials", "validation_batch_items"):
        inserts = [statement for statement in statements if getattr(getattr(statement, "table", None), "name", None) == table]
        assert [len(statement._multi_values[0]) for statement in inserts] == [1000, 200]
    assert events.count("registry") == 2
    session.commit.assert_awaited_once()
    assert events[-1] == "invalidate:credentials"


def test_upserts_have_replay_stale_timestamp_and_nullable_ip_guards():
    rows, known_ips = bulk._credential_rows(bulk._observations([job(source_ip=None)]))
    statement = bulk._credential_upsert(rows, known_ips)
    compiled = sql(statement)
    assert "ON CONFLICT ON CONSTRAINT uq_credentials_type_fingerprint DO UPDATE" in compiled
    assert "queue_event_id IS NULL OR credentials.queue_event_id < excluded.queue_event_id" in compiled
    assert "credentials.updated_at <= excluded.updated_at" in compiled
    assert "source_ip = coalesce(excluded.source_ip, credentials.source_ip)" in compiled


def test_older_group_ip_cannot_overwrite_newer_existing_ip():
    rows, known_ips = bulk._credential_rows(bulk._observations([job(1), job(20, source_ip=None)]))
    compiled = sql(bulk._credential_upsert(rows, known_ips))
    assert "source_ip = CASE WHEN" in compiled
    assert "credentials.source_ip IS NOT NULL" in compiled
    assert "credentials.queue_event_id >" in compiled
    assert "credentials.updated_at >" in compiled
    assert "THEN credentials.source_ip ELSE coalesce" in compiled


def test_duplicate_batch_indexes_do_not_cause_cardinality_and_intermediate_valid_history_is_preserved(monkeypatch):
    jobs = [
        job(30, items=[result(status="invalid")]),
        job(10, items=[result(status="valid")]),
        job(20, items=[result(status="valid")]),
        job(5, items=[result(secret="synthetic-other-secret", status="valid")]),
    ]
    session, _, statements, ids = captured_session(monkeypatch, jobs)
    assert run(bulk.process_queued_results(session, jobs)) == 4
    archive = next(statement for statement in statements if getattr(getattr(statement, "table", None), "name", None) == "valid_key_records")
    valid_rows = {row["credential_id"]: row for row in inserted_rows(archive)}
    first_identity = identity(result())
    assert valid_rows[ids[first_identity]]["first_valid_at"] == BASE + timedelta(milliseconds=10)
    assert valid_rows[ids[first_identity]]["last_valid_at"] == BASE + timedelta(milliseconds=20)
    assert len(valid_rows) == 2
    assert "least(valid_key_records.first_valid_at, excluded.first_valid_at)" in sql(archive)
    assert "greatest(valid_key_records.last_valid_at, excluded.last_valid_at)" in sql(archive)
    batch_items = next(statement for statement in statements if getattr(getattr(statement, "table", None), "name", None) == "validation_batch_items")
    assert len(batch_items._multi_values[0]) == 1
    assert inserted_rows(batch_items)[0]["status"] == "invalid"
    assert inserted_rows(batch_items)[0]["credential_id"] == ids[first_identity]
    assert "validation_batch_items.queue_event_id < excluded.queue_event_id" in sql(batch_items)


def test_unknown_statuses_are_registered_even_if_intermediate_result_was_superseded(monkeypatch):
    jobs = [job(1, items=[result(status="synthetic_new_status")]), job(2, items=[result(status="valid")])]
    session, _, statements, _ = captured_session(monkeypatch, jobs)
    run(bulk.process_queued_results(session, jobs))
    rows = inserted_rows(statements[0])
    assert {row["value"] for row in rows} == {"synthetic_new_status", "valid"}
    unknown = next(row for row in rows if row["value"] == "synthetic_new_status")
    assert unknown["name"] == "synthetic_new_status"
    assert unknown["tone"] == "neutral"


def test_ten_thousand_unique_statuses_are_registered_in_bounded_parameter_chunks(monkeypatch):
    jobs = [
        job(shard, items=[
            result(offset + 1, status=f"synthetic_unique_status_{shard * 50 + offset:05d}")
            for offset in range(50)
        ])
        for shard in range(200)
    ]
    session, _, statements, _ = captured_session(monkeypatch, jobs)
    assert run(bulk.process_queued_results(session, jobs)) == 10_000
    registrations = [
        statement for statement in statements
        if getattr(getattr(statement, "table", None), "name", None) == "credential_statuses"
    ]
    assert len(registrations) == 10
    assert all(len(inserted_rows(statement)) == 1000 for statement in registrations)
    assert all(len(statement.compile(dialect=postgresql.dialect()).params) == 4000 for statement in registrations)
    assert len({row["value"] for statement in registrations for row in inserted_rows(statement)}) == 10_000
    session.commit.assert_awaited_once()
    bulk.invalidate_query_cache.assert_awaited_once_with("credentials")


@pytest.mark.parametrize("failure", ["credential_statuses", "credentials", "valid_key_records", "validation_batch_items", "validation_batches", "commit"])
def test_any_bulk_write_or_commit_failure_rolls_back_and_does_not_invalidate(monkeypatch, failure):
    jobs = [job()]
    session, events, _, _ = captured_session(monkeypatch, jobs, fail_on=failure)
    with pytest.raises(RuntimeError, match="synthetic-.*failure"):
        run(bulk.process_queued_results(session, jobs))
    session.rollback.assert_awaited_once()
    bulk.invalidate_query_cache.assert_not_awaited()
    assert events[-1] == "rollback"
    assert session.commit.await_count == (1 if failure == "commit" else 0)


def test_missing_batch_rejects_whole_group_before_any_write(monkeypatch):
    jobs = [job(), job(10, batch_id=2)]
    session, _, _, _ = captured_session(monkeypatch, jobs, batches=[1])
    with pytest.raises(HTTPException) as error:
        run(bulk.process_queued_results(session, jobs))
    assert error.value.status_code == 404
    session.execute.assert_not_awaited()
    session.commit.assert_not_awaited()
    session.rollback.assert_awaited_once()
    bulk.invalidate_query_cache.assert_not_awaited()


def test_registry_mismatch_rolls_back_without_partial_items_or_cache_invalidation(monkeypatch):
    jobs = [job()]
    session, events, _, _ = captured_session(monkeypatch, jobs, missing_registry=True)
    with pytest.raises(RuntimeError, match="registry is incomplete"):
        run(bulk.process_queued_results(session, jobs))
    assert events == ["credential_statuses", "credentials", "registry", "rollback"]
    bulk.invalidate_query_cache.assert_not_awaited()


def test_cancelled_database_write_rolls_back_before_propagating(monkeypatch):
    jobs = [job()]
    session, _, _, _ = captured_session(monkeypatch, jobs)
    session.execute.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        run(bulk.process_queued_results(session, jobs))
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()
    bulk.invalidate_query_cache.assert_not_awaited()


@pytest.mark.parametrize("batch_id", [0, -1, True, "1"])
def test_invalid_batch_id_rolls_back_without_querying_or_writing(monkeypatch, batch_id):
    jobs = [job(batch_id=batch_id)]
    session, _, _, _ = captured_session(monkeypatch, jobs)
    with pytest.raises(ValueError, match="Invalid queue batch ID"):
        run(bulk.process_queued_results(session, jobs))
    session.scalars.assert_not_awaited()
    session.execute.assert_not_awaited()
    session.rollback.assert_awaited_once()


def test_empty_job_list_has_no_transaction_or_cache_effect(monkeypatch):
    session, events, _, _ = captured_session(monkeypatch, [])
    assert run(bulk.process_queued_results(session, [])) == 0
    assert events == []
    session.scalars.assert_not_awaited()
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()
    bulk.invalidate_query_cache.assert_not_awaited()
