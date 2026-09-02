from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from .config import settings
from .security import encrypt_job


def redis_client() -> Redis:
    return Redis.from_url(settings.redis_url, decode_responses=False)


async def is_burst(redis: Redis, node_id: str) -> bool:
    key = f"key-control:burst:{node_id}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, settings.burst_window_seconds)
    return int(count) > settings.burst_request_limit


async def enqueue_results(redis: Redis, batch_id: int, results: list[dict[str, Any]]) -> str:
    payload = encrypt_job({"batch_id": batch_id, "results": results})
    message_id = await redis.xadd(
        settings.redis_stream_name,
        {"payload": payload},
        maxlen=settings.queue_job_max_length,
        approximate=True,
    )
    return message_id.decode() if isinstance(message_id, bytes) else str(message_id)


async def ensure_consumer_group(redis: Redis) -> None:
    try:
        await redis.xgroup_create(
            settings.redis_stream_name,
            settings.redis_consumer_group,
            id="0-0",
            mkstream=True,
        )
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise
