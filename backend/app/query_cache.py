"""Short-lived cache for authenticated, redacted query DTOs only."""

import asyncio
import hashlib
import json
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any
from uuid import uuid4

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from .config import settings


logger = logging.getLogger(__name__)
_NAMESPACES = {
    "keys": "credentials",
    "statuses": "credentials",
    "credential-statistics": "credentials",
    "valid-key-records": "credentials",
    "export-records": "exports",
}
_PREFIX = "key-control:query-cache:v1"
_NONCE = re.compile(r"[0-9a-f]{32}\Z")
_FIELD_NORMALIZATION = re.compile(r"[^a-z]")
_SCALAR_TYPES = frozenset({str, bool, int})
_SENSITIVE_FIELDS = {
    "secret", "secrets", "encryptedsecret", "encryptedsecrets",
    "password", "passwordhash", "token", "accesstoken", "refreshtoken",
    "sessiontoken", "authorization", "cookie",
}
_MISSING = object()
_UNAVAILABLE = object()
_MAX_FLIGHTS = 1024
_client: Redis | None = None
_failure_until = 0.0
_READ_SCRIPT = """
local version = redis.call('GET', KEYS[1])
if not version then
    version = ARGV[1]
    redis.call('SETNX', KEYS[1], version)
elseif string.len(version) ~= 32 or not string.match(version, '^[0-9a-f]+$') then
    version = ARGV[1]
    redis.call('SET', KEYS[1], version)
end
local key = ARGV[2] .. version .. ':' .. ARGV[3]
if ARGV[4] == '1' then
    return {key, false}
end
return {key, redis.call('GET', key)}
"""


@dataclass(frozen=True)
class CacheResult:
    data: Any
    status: str


@dataclass
class _Flight:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


_flights: dict[str, _Flight] = {}


def _mark_unavailable() -> None:
    global _failure_until
    if time.monotonic() >= _failure_until:
        # Never include exception text: Redis errors may contain a URL or data.
        logger.warning("Query cache unavailable; using database.")
    _failure_until = time.monotonic() + settings.cache_failure_backoff_seconds


def _redis_client() -> Redis | None:
    global _client
    if not settings.cache_redis_url.strip() or time.monotonic() < _failure_until:
        return None
    if _client is None:
        try:
            _client = Redis.from_url(
                settings.cache_redis_url,
                decode_responses=False,
                socket_connect_timeout=settings.cache_timeout_seconds,
                socket_timeout=settings.cache_timeout_seconds,
                retry=Retry(NoBackoff(), 0),
                retry_on_timeout=False,
                max_connections=32,
                health_check_interval=30,
            )
        except Exception:
            _mark_unavailable()
            return None
    return _client


async def _redis_call(operation: Callable[[], Awaitable[Any]]) -> Any:
    if time.monotonic() < _failure_until:
        return _UNAVAILABLE
    try:
        async with asyncio.timeout(settings.cache_timeout_seconds):
            return await operation()
    except Exception:
        _mark_unavailable()
        return _UNAVAILABLE


@lru_cache(maxsize=256)
def _sensitive_field(key: str) -> bool:
    return _FIELD_NORMALIZATION.sub("", key.lower()) in _SENSITIVE_FIELDS


def _json_value(value: Any, *, redacted: bool) -> bool:
    if value is None or type(value) in _SCALAR_TYPES:
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(_json_value(item, redacted=redacted) for item in value)
    if type(value) is dict:
        return all(
            type(key) is str
            and (not redacted or not _sensitive_field(key))
            and _json_value(item, redacted=redacted)
            for key, item in value.items()
        )
    return False


def _parameter_digest(parameters: dict[str, Any]) -> str | None:
    try:
        if type(parameters) is not dict or not _json_value(parameters, redacted=False):
            return None
        encoded = json.dumps(parameters, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    except (TypeError, ValueError, RecursionError):
        return None


def _encode(data: Any) -> bytes | None:
    try:
        if type(data) not in {dict, list} or not _json_value(data, redacted=True):
            return None
        encoded = json.dumps({"format": 1, "data": data}, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        return encoded if len(encoded) <= settings.cache_max_value_bytes else None
    except (TypeError, ValueError, RecursionError):
        return None


def _reject_constant(_: str) -> None:
    raise ValueError("Non-JSON numeric constant")


def _decode(raw: Any) -> Any:
    if not isinstance(raw, (bytes, str)) or len(raw) > settings.cache_max_value_bytes:
        return _MISSING
    try:
        envelope = json.loads(raw, parse_constant=_reject_constant)
        if (
            type(envelope) is not dict or set(envelope) != {"format", "data"}
            or type(envelope["format"]) is not int or envelope["format"] != 1
            or type(envelope["data"]) not in {dict, list}
            or not _json_value(envelope["data"], redacted=True)
        ):
            return _MISSING
        return envelope["data"]
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return _MISSING


def _nonce(raw: Any) -> str | None:
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("ascii")
        except UnicodeError:
            return None
    return raw if isinstance(raw, str) and _NONCE.fullmatch(raw) else None


async def _read(client: Redis, namespace: str, digest: str, *, refresh: bool) -> Any:
    async def operation():
        # One atomic round trip prevents version eviction/invalidation from
        # separating the version lookup from the data lookup.
        result = await client.eval(
            _READ_SCRIPT, 1, f"{_PREFIX}:{_NAMESPACES[namespace]}:version",
            uuid4().hex, f"{_PREFIX}:{namespace}:", digest, "1" if refresh else "0",
        )
        if not isinstance(result, (tuple, list)) or len(result) != 2:
            return _UNAVAILABLE
        key, raw = result
        if isinstance(key, bytes):
            key = key.decode("ascii")
        prefix = f"{_PREFIX}:{namespace}:"
        if not isinstance(key, str) or not key.startswith(prefix):
            return _UNAVAILABLE
        version, separator, returned_digest = key[len(prefix):].partition(":")
        if not separator or _nonce(version) is None or returned_digest != digest:
            return _UNAVAILABLE
        return key, _MISSING if raw is None else _decode(raw)

    return await _redis_call(operation)


async def _load_and_store(
    client: Redis, key: str, loader: Callable[[], Awaitable[Any]], *, refresh: bool, ttl_seconds: int,
) -> CacheResult:
    # Await in the owning request. No background task may outlive its DB session.
    data = await loader()
    encoded = _encode(data)
    if encoded is None:
        return CacheResult(data, "BYPASS")
    stored = await _redis_call(lambda: client.set(key, encoded, ex=ttl_seconds))
    return CacheResult(data, "BYPASS" if stored is _UNAVAILABLE or not stored else "REFRESH" if refresh else "MISS")


async def cached_query(
    namespace: str,
    parameters: dict[str, Any],
    loader: Callable[[], Awaitable[Any]],
    *,
    refresh: bool = False,
) -> CacheResult:
    """Return a redacted DTO; auth/IP checks and actor scoping remain with callers."""
    if namespace not in _NAMESPACES:
        raise ValueError("Unsupported query cache namespace")
    digest = _parameter_digest(parameters)
    client = _redis_client() if digest is not None else None
    if client is None:
        return CacheResult(await loader(), "BYPASS")
    initial = await _read(client, namespace, digest, refresh=refresh)
    if initial is _UNAVAILABLE:
        return CacheResult(await loader(), "BYPASS")
    key, data = initial
    if data is not _MISSING:
        return CacheResult(data, "HIT")

    ttl_seconds = settings.cache_statistics_ttl_seconds if namespace == "credential-statistics" else settings.cache_ttl_seconds

    flight = _flights.get(key)
    owner = flight is None
    if owner:
        if len(_flights) >= _MAX_FLIGHTS:
            return CacheResult(await loader(), "BYPASS")
        flight = _flights[key] = _Flight()
    flight.users += 1
    acquired = False
    try:
        try:
            if owner:
                await flight.lock.acquire()
            else:
                await asyncio.wait_for(flight.lock.acquire(), timeout=settings.cache_timeout_seconds)
            acquired = True
        except TimeoutError:
            return CacheResult(await loader(), "BYPASS")
        if owner:
            return await _load_and_store(client, key, loader, refresh=refresh, ttl_seconds=ttl_seconds)
        # Recheck after another request's load or a concurrent invalidation.
        current = await _read(client, namespace, digest, refresh=refresh)
        if current is _UNAVAILABLE:
            return CacheResult(await loader(), "BYPASS")
        current_key, data = current
        if data is not _MISSING:
            return CacheResult(data, "HIT")
        # Preserve the pre-load version. A later invalidation makes this key
        # unreachable even if an older DB read completes after the write.
        return await _load_and_store(client, current_key, loader, refresh=refresh, ttl_seconds=ttl_seconds)
    finally:
        if acquired:
            flight.lock.release()
        flight.users -= 1
        if flight.users == 0 and _flights.get(key) is flight:
            del _flights[key]


async def invalidate_query_cache(family: str) -> None:
    if family not in set(_NAMESPACES.values()):
        raise ValueError("Unsupported query cache family")
    client = _redis_client()
    if client is not None:
        await _redis_call(lambda: client.set(f"{_PREFIX}:{family}:version", uuid4().hex))


async def close_query_cache() -> None:
    global _client, _failure_until
    client, _client = _client, None
    _failure_until = 0.0
    _flights.clear()
    if client is not None:
        try:
            async with asyncio.timeout(settings.cache_timeout_seconds):
                await client.aclose()
        except Exception:
            logger.warning("Query cache close failed.")
