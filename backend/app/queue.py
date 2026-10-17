from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import ResponseError

from .config import settings
from .security import encrypt_job


class QueueFullError(Exception):
    pass


class QueuePayloadTooLargeError(Exception):
    pass


_ENQUEUE_SCRIPT = """
if redis.call('XLEN', KEYS[1]) + redis.call('XLEN', KEYS[2]) >= tonumber(ARGV[1]) then
    return false
end
return redis.call('XADD', KEYS[1], '*', 'payload', ARGV[2], 'record_count', ARGV[3])
"""

_FINISH_SCRIPT = """
if #ARGV == 1 then return 0 end
local ids = {}
for i = 2, #ARGV do ids[#ids + 1] = ARGV[i] end
local acknowledged = redis.call('XACK', KEYS[1], ARGV[1], unpack(ids))
redis.call('XDEL', KEYS[1], unpack(ids))
redis.call('HDEL', KEYS[2], unpack(ids))
return acknowledged
"""

_FAILURE_SCRIPT = """
local moved = 0
for i = 4, #ARGV do
    local id = ARGV[i]
    local entries = redis.call('XRANGE', KEYS[1], id, id, 'COUNT', 1)
    if #entries > 0 then
        local attempts = redis.call('HINCRBY', KEYS[2], id, 1)
        if attempts >= tonumber(ARGV[2]) then
            local fields = entries[1][2]
            fields[#fields + 1] = 'source_message_id'; fields[#fields + 1] = id
            fields[#fields + 1] = 'error_type'; fields[#fields + 1] = ARGV[3]
            fields[#fields + 1] = 'attempts'; fields[#fields + 1] = tostring(attempts)
            redis.call('XADD', KEYS[3], '*', unpack(fields))
            redis.call('XACK', KEYS[1], ARGV[1], id)
            redis.call('XDEL', KEYS[1], id)
            redis.call('HDEL', KEYS[2], id)
            moved = moved + 1
        end
    end
end
return moved
"""


def redis_client() -> Redis:
    return Redis.from_url(
        settings.redis_url, decode_responses=False,
        socket_connect_timeout=2, socket_timeout=10,
        retry=Retry(NoBackoff(), 0), retry_on_timeout=False,
    )


async def enqueue_results(
    redis: Redis, batch_id: int, results: list[dict[str, Any]], *, source_ip: str | None = None,
) -> str:
    payload = encrypt_job({
        "batch_id": batch_id, "results": results, "source_ip": source_ip,
        "enqueued_at": datetime.now(UTC).isoformat(),
    })
    if len(payload.encode("ascii")) > settings.queue_max_payload_bytes:
        raise QueuePayloadTooLargeError
    message_id = await redis.eval(
        _ENQUEUE_SCRIPT, 2, settings.redis_stream_name, settings.redis_dead_letter_stream,
        settings.queue_job_max_length, payload, len(results),
    )
    if not message_id:
        raise QueueFullError
    return message_id.decode() if isinstance(message_id, bytes) else str(message_id)


async def finish_messages(redis: Redis, message_ids: list[str | bytes]) -> None:
    for start in range(0, len(message_ids), 1000):
        await redis.eval(
            _FINISH_SCRIPT, 2, settings.redis_stream_name, settings.redis_retry_hash,
            settings.redis_consumer_group, *message_ids[start:start + 1000],
        )


async def handle_failed_messages(
    redis: Redis, message_ids: list[str | bytes], *, error_type: str,
) -> None:
    if message_ids:
        # Only a class name is recorded; never store validation errors or secrets.
        safe_type = "".join(char for char in error_type if char.isascii() and (char.isalnum() or char == "_"))[:80] or "Error"
        await redis.eval(
            _FAILURE_SCRIPT, 3, settings.redis_stream_name, settings.redis_retry_hash,
            settings.redis_dead_letter_stream, settings.redis_consumer_group,
            settings.queue_retry_limit, safe_type, *message_ids,
        )


async def ensure_consumer_group(redis: Redis) -> None:
    try:
        await redis.xgroup_create(
            settings.redis_stream_name, settings.redis_consumer_group, id="0-0", mkstream=True,
        )
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise
