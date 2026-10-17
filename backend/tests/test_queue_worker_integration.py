"""Opt-in real Redis/isolated PostgreSQL verification of the 1000/30s worker."""

import asyncio
import os
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import select

import app.queue as queue
import app.worker as worker
from app.database import ValidationBatch
from test_bulk_ingest_postgres import bulk, bulk_database, result, seed_batches, session_factory


def test_real_worker_flushes_1000_immediately_and_partial_batch_after_30_seconds(monkeypatch, bulk_database, bulk):
    url = os.environ.get("TEST_QUEUE_REDIS_URL")
    if not url:
        pytest.skip("Set TEST_QUEUE_REDIS_URL and TEST_PLATFORM_DATABASE_URL for worker integration")
    prefix = "checkapi:worker-test:" + uuid4().hex
    config = SimpleNamespace(
        redis_stream_name=prefix, redis_consumer_group="synthetic-workers",
        redis_dead_letter_stream=prefix + ":dead-letter", redis_retry_hash=prefix + ":retries",
        queue_job_max_length=100000, queue_retry_limit=5, queue_max_payload_bytes=2_097_152,
        queue_batch_records=1000, queue_batch_wait_seconds=30,
        queue_read_messages=20, queue_claim_idle_seconds=120,
    )
    monkeypatch.setattr(queue, "settings", config)
    monkeypatch.setattr(worker, "settings", config)

    async def verify():
        await seed_batches(bulk_database, {1: 1000, 2: 1})
        engine, sessions = session_factory(bulk_database)
        client = Redis.from_url(url, decode_responses=False, socket_timeout=10, socket_connect_timeout=3)
        stop = asyncio.Event()
        second_written = asyncio.Event()
        calls = []

        async def processor(session, jobs):
            calls.append(sum(len(job.results) for job in jobs))
            accepted = await bulk.process_queued_results(session, jobs)
            if any(job.batch_id == 2 for job in jobs):
                second_written.set()
            return accepted

        consumer = worker.BatchWorker(client, "synthetic-consumer", session_factory=sessions, processor=processor)
        task = None
        try:
            await queue.ensure_consumer_group(client)
            for start in range(0, 1000, 50):
                values = [result(index + 1, f"synthetic-e2e-{prefix}-{index}").model_dump() for index in range(start, start + 50)]
                await queue.enqueue_results(client, 1, values, source_ip="203.0.113.7")
            started = time.monotonic()
            task = asyncio.create_task(consumer.run(stop))
            for _ in range(150):
                async with sessions() as session:
                    written = await session.scalar(select(ValidationBatch.received_count).where(ValidationBatch.id == 1))
                if written == 1000 and await client.xlen(prefix) == 0:
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("Full batch was not committed and acknowledged promptly")
            assert calls == [1000]
            assert time.monotonic() - started < 10

            identifier = await queue.enqueue_results(client, 2, [result(1, f"synthetic-e2e-partial-{prefix}").model_dump()])
            queued_at = int(identifier.split("-", 1)[0]) / 1000
            await asyncio.sleep(1)
            async with sessions() as session:
                assert await session.scalar(select(ValidationBatch.received_count).where(ValidationBatch.id == 2)) == 0
            await asyncio.wait_for(second_written.wait(), timeout=35)
            elapsed = time.time() - queued_at
            assert 29.8 <= elapsed < 36
            assert calls == [1000, 1]
            async with sessions() as session:
                assert await session.scalar(select(ValidationBatch.received_count).where(ValidationBatch.id == 2)) == 1
            print(f"Real worker: first batch=1000; partial batch=1 after {elapsed:.2f}s")
        finally:
            stop.set()
            try:
                if task is not None:
                    try:
                        await asyncio.wait_for(task, timeout=7)
                    except BaseException:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        raise
            finally:
                await client.delete(prefix, config.redis_dead_letter_stream, config.redis_retry_hash)
                await client.aclose()
                await engine.dispose()

    asyncio.run(verify())
