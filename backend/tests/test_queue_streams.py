import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

import app.main as api
import app.queue as queue
from app.database import ValidationBatch
from app.security import decrypt_job
from test_credential_ip import result_payload, session, redis


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.mark.parametrize("error,status", [
    (queue.QueueFullError(), 503),
    (queue.QueuePayloadTooLargeError(), 413),
    (RedisConnectionError("synthetic-sensitive-connection-details"), 503),
])
def test_upload_queue_failures_are_retryable_or_rejected_without_database_writes(monkeypatch, session, redis, error, status):
    session.get.return_value = ValidationBatch(id=29, total=1)
    enqueuer = AsyncMock(side_effect=error)
    monkeypatch.setattr(api, "enqueue_results", enqueuer)

    async def fake_session():
        yield session

    async def fake_redis():
        yield redis

    previous = api.app.dependency_overrides.copy()
    api.app.dependency_overrides[api.get_session] = fake_session
    api.app.dependency_overrides[api.redis_dependency] = fake_redis

    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as client:
            return await client.post("/api/device/batches/29/complete", json={"results": [result_payload()]})

    try:
        response = run(request())
    finally:
        api.app.dependency_overrides.clear()
        api.app.dependency_overrides.update(previous)
    assert response.status_code == status
    assert "synthetic-sensitive" not in response.text
    assert response.headers["Cache-Control"] == "no-store"
    if status == 503:
        assert response.headers["Retry-After"] == "5"
    session.commit.assert_not_awaited()
    session.execute.assert_not_awaited()


def test_finish_chunks_acknowledgements_after_one_database_commit():
    client = SimpleNamespace(eval=AsyncMock())
    identifiers = [f"123-{index}" for index in range(10000)]
    run(queue.finish_messages(client, identifiers))
    assert client.eval.await_count == 10
    chunks = [call.args[5:] for call in client.eval.await_args_list]
    assert all(len(chunk) == 1000 for chunk in chunks)
    assert [identifier for chunk in chunks for identifier in chunk] == identifiers


def test_real_redis_capacity_acknowledgement_and_dead_letter_preserve_unprocessed_data(monkeypatch):
    url = os.environ.get("TEST_QUEUE_REDIS_URL")
    if not url:
        pytest.skip("Set TEST_QUEUE_REDIS_URL for isolated Redis stream verification")
    prefix = "checkapi:queue-test:" + uuid4().hex
    configuration = SimpleNamespace(
        redis_stream_name=prefix, redis_consumer_group="synthetic-workers",
        redis_dead_letter_stream=prefix + ":dead-letter", redis_retry_hash=prefix + ":retries",
        queue_job_max_length=3, queue_retry_limit=2, queue_max_payload_bytes=2_097_152,
    )
    monkeypatch.setattr(queue, "settings", configuration)

    async def verify():
        client = Redis.from_url(url, decode_responses=False, socket_timeout=3, socket_connect_timeout=3)
        try:
            await queue.ensure_consumer_group(client)
            identifiers = [await queue.enqueue_results(client, 29, [result_payload()]) for _ in range(3)]
            with pytest.raises(queue.QueueFullError):
                await queue.enqueue_results(client, 29, [result_payload()])
            entries = await client.xreadgroup(configuration.redis_consumer_group, "synthetic-consumer", {prefix: ">"}, count=3)
            assert len(entries[0][1]) == 3
            payload = entries[0][1][1][1][b"payload"]
            assert b"synthetic-ip-test-key" not in payload
            assert decrypt_job(payload)["results"] == [result_payload()]
            await queue.finish_messages(client, [identifiers[0]])
            assert await client.xlen(prefix) == 2
            assert (await client.xpending(prefix, configuration.redis_consumer_group))["pending"] == 2
            await queue.handle_failed_messages(client, [identifiers[1]], error_type="ValueError")
            assert await client.xlen(prefix) == 2
            await queue.handle_failed_messages(client, [identifiers[1]], error_type="ValueError")
            dead = await client.xrange(configuration.redis_dead_letter_stream)
            assert len(dead) == 1 and dead[0][1][b"payload"] == payload
            assert dead[0][1][b"error_type"] == b"ValueError"
            assert await client.hget(configuration.redis_retry_hash, identifiers[1]) is None
            assert (await client.xpending(prefix, configuration.redis_consumer_group))["pending"] == 1
            assert len(await client.xrange(prefix, identifiers[2], identifiers[2])) == 1
            await queue.enqueue_results(client, 29, [result_payload()])
            with pytest.raises(queue.QueueFullError):
                await queue.enqueue_results(client, 29, [result_payload()])
        finally:
            # Only this test's UUID-scoped keys are ever deleted.
            await client.delete(prefix, configuration.redis_dead_letter_stream, configuration.redis_retry_hash)
            await client.aclose()

    run(verify())
