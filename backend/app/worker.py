import asyncio
import os
import socket
from typing import Any

from redis.asyncio import Redis

from .config import settings
from .credential_service import process_batch_results
from .database import SessionFactory
from .queue import ensure_consumer_group, redis_client
from .schemas import ValidationResultInput
from .security import decrypt_job


def _message_value(fields: dict[Any, Any], key: str) -> Any:
    return fields.get(key) or fields.get(key.encode())


async def process_message(redis: Redis, message_id: str | bytes, fields: dict[Any, Any]) -> None:
    payload_value = _message_value(fields, "payload")
    if not payload_value:
        await redis.xack(settings.redis_stream_name, settings.redis_consumer_group, message_id)
        return
    payload = decrypt_job(payload_value)
    results = [ValidationResultInput.model_validate(item) for item in payload["results"]]
    async with SessionFactory() as session:
        await process_batch_results(session, int(payload["batch_id"]), results)
    await redis.xack(settings.redis_stream_name, settings.redis_consumer_group, message_id)


async def process_entries(redis: Redis, entries: list[tuple[Any, dict[Any, Any]]]) -> None:
    for message_id, fields in entries:
        try:
            await process_message(redis, message_id, fields)
        except Exception as exc:  # noqa: BLE001 - leave failed jobs pending for Redis reclaim.
            print(f"queue job {message_id!r} failed: {exc}", flush=True)


async def reclaim_stale(redis: Redis, consumer_name: str) -> None:
    pending = await redis.xpending_range(
        settings.redis_stream_name,
        settings.redis_consumer_group,
        min="-",
        max="+",
        count=20,
        idle=30_000,
    )
    message_ids = [item["message_id"] for item in pending]
    if not message_ids:
        return
    claimed = await redis.xclaim(
        settings.redis_stream_name,
        settings.redis_consumer_group,
        consumer_name,
        min_idle_time=30_000,
        message_ids=message_ids,
    )
    await process_entries(redis, claimed)


async def run() -> None:
    redis = redis_client()
    consumer_name = f"{socket.gethostname()}-{os.getpid()}"
    try:
        await ensure_consumer_group(redis)
        while True:
            await reclaim_stale(redis, consumer_name)
            streams = await redis.xreadgroup(
                settings.redis_consumer_group,
                consumer_name,
                {settings.redis_stream_name: ">"},
                count=20,
                block=5_000,
            )
            for _stream_name, entries in streams:
                await process_entries(redis, entries)
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(run())
