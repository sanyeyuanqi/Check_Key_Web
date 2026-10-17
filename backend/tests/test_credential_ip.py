import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

import app.main as api
import app.worker as worker
from app.config import settings
from app.credential_service import list_credentials, process_batch_results
from app.database import AuthIPBlock, Credential, ValidationBatch
from app.queue import enqueue_results
from app.schemas import ValidationResultInput
from app.security import decrypt_job, encrypt_job


def result_payload():
    return {
        "index": 1, "status": "valid", "usable": True,
        "credential": {"provider": "OpenAI", "kind": "openai", "secret": "synthetic-ip-test-key"},
    }


@pytest.fixture
def session():
    db_session = MagicMock(spec=AsyncSession)
    db_session.get.return_value = None
    db_session.get.side_effect = lambda model, *args, **kwargs: None if model is AuthIPBlock else db_session.get.return_value
    return db_session


@pytest.fixture
def redis():
    client = MagicMock(spec=Redis)
    client.xadd = AsyncMock()
    client.xack = AsyncMock()
    return client


@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize("peer,expected_ip", [
    ("203.0.113.7", "203.0.113.7"),
    ("2001:0db8:0000:0000:0000:0000:0000:0001", "2001:db8::1"),
])
def test_device_upload_uses_peer_ip_for_immediate_and_queued_processing(monkeypatch, session, redis, queued, peer, expected_ip):
    async def fake_session():
        yield session

    async def fake_redis():
        yield redis

    processor = AsyncMock(return_value=1)
    enqueuer = AsyncMock(return_value="123-0")
    monkeypatch.setattr(api, "process_batch_results", processor)
    monkeypatch.setattr(api, "enqueue_results", enqueuer)
    monkeypatch.setattr(api, "is_burst", AsyncMock(return_value=queued))
    previous_overrides = api.app.dependency_overrides.copy()
    api.app.dependency_overrides[api.get_session] = fake_session
    api.app.dependency_overrides[api.redis_dependency] = fake_redis

    async def request():
        transport = httpx.ASGITransport(app=api.app, client=(peer, 123))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/api/device/batches/29/complete",
                json={"results": [result_payload()], "source_ip": "198.51.100.10"},
                headers={"X-Forwarded-For": "198.51.100.10", "Forwarded": "for=198.51.100.10"},
            )

    try:
        response = asyncio.run(request())
    finally:
        api.app.dependency_overrides.clear()
        api.app.dependency_overrides.update(previous_overrides)

    assert response.status_code == (202 if queued else 200)
    assert response.json()["queued"] == queued
    if queued:
        processor.assert_not_awaited()
        enqueuer.assert_awaited_once_with(redis, 29, [ValidationResultInput.model_validate(result_payload()).model_dump()], source_ip=expected_ip)
    else:
        enqueuer.assert_not_awaited()
        processor.assert_awaited_once_with(session, 29, [ValidationResultInput.model_validate(result_payload())], source_ip=expected_ip)


@pytest.mark.parametrize("source_ip", [None, "2001:db8::1"])
def test_queue_encrypts_upload_source_ip_with_results(redis, source_ip):
    redis.xadd.return_value = b"123-0"
    results = [result_payload()]
    message_id = asyncio.run(enqueue_results(redis, 29, results, source_ip=source_ip))

    assert message_id == "123-0"
    redis.xadd.assert_awaited_once()
    args = redis.xadd.await_args.args
    assert args[0] == settings.redis_stream_name
    encrypted_payload = args[1]["payload"]
    assert "synthetic-ip-test-key" not in encrypted_payload
    if source_ip:
        assert source_ip not in encrypted_payload
    assert decrypt_job(encrypted_payload) == {"batch_id": 29, "results": results, "source_ip": source_ip}


@pytest.mark.parametrize("source_ip,old_message", [("203.0.113.7", False), (None, True)])
def test_worker_passes_new_source_ip_and_accepts_old_messages(monkeypatch, session, redis, source_ip, old_message):
    payload = {"batch_id": 29, "results": [result_payload()]}
    if not old_message:
        payload["source_ip"] = source_ip
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = session
    processor = AsyncMock(return_value=1)
    monkeypatch.setattr(worker, "SessionFactory", factory)
    monkeypatch.setattr(worker, "process_batch_results", processor)

    asyncio.run(worker.process_message(redis, b"123-0", {b"payload": encrypt_job(payload).encode()}))

    processor.assert_awaited_once_with(
        session, 29, [ValidationResultInput.model_validate(result_payload())], source_ip=source_ip,
    )
    redis.xack.assert_awaited_once_with(settings.redis_stream_name, settings.redis_consumer_group, b"123-0")


@pytest.mark.parametrize("source_ip", ["203.0.113.7", "2001:db8::1", None])
def test_credential_upsert_updates_ip_only_when_supplied(session, source_ip):
    session.scalar.return_value = ValidationBatch(id=29, total=1)
    session.execute.side_effect = [
        SimpleNamespace(),
        SimpleNamespace(scalar_one=lambda: 41),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(one=lambda: (1, 1, 0, 0, 0)),
    ]

    accepted = asyncio.run(process_batch_results(
        session, 29, [ValidationResultInput.model_validate(result_payload())], source_ip=source_ip,
    ))

    assert accepted == 1
    registration_sql = str(session.execute.await_args_list[0].args[0].compile(dialect=postgresql.dialect()))
    assert "INSERT INTO credential_statuses" in registration_sql
    statement = session.execute.await_args_list[1].args[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    update_sql = str(compiled).split("DO UPDATE SET", 1)[1].split("RETURNING", 1)[0]
    if source_ip is None:
        # Legacy queue jobs must preserve a previously saved address on conflict.
        assert "source_ip" not in update_sql
    else:
        assert "source_ip =" in update_sql
        assert source_ip in compiled.params.values()
    session.commit.assert_awaited_once()


def test_credentials_list_serializes_ipv6_and_legacy_null_ip(session):
    created_at = datetime(2026, 10, 16, 9, 30, tzinfo=timezone.utc)
    updated_at = datetime(2026, 10, 17, 10, 45, tzinfo=timezone.utc)
    rows = [
        Credential(
            id=index, provider="OpenAI", storage_type="openai_official", key_prefix="synthetic-prefix",
            masked="synthetic…test", status="valid", source_ip=source_ip, created_at=created_at,
            updated_at=updated_at,
        )
        for index, source_ip in enumerate(["2001:db8::1", None], start=1)
    ]
    session.execute.side_effect = [
        SimpleNamespace(all=lambda: [("valid", 2)]),
        SimpleNamespace(all=lambda: [("valid", "可用", "valid", 0, 2), ("network_error", "网络异常", "neutral", 1, 0)]),
    ]
    session.scalar.return_value = 2
    session.scalars.return_value = SimpleNamespace(all=lambda: rows)

    result = asyncio.run(list_credentials(
        session, status="valid", provider="OpenAI", storage_type=None, query=None, page=1,
    ))

    assert result["total"] == 2
    assert [key["ipAddress"] for key in result["keys"]] == ["2001:db8::1", None]
    assert [key["id"] for key in result["keys"]] == [1, 2]
    assert [key["createdAt"] for key in result["keys"]] == [created_at.isoformat()] * 2
    assert [key["updatedAt"] for key in result["keys"]] == [updated_at.isoformat()] * 2
    assert result["summary"]["valid"] == 2
