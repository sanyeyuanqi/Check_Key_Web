import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError
from redis.asyncio.retry import Retry as AsyncRetry
from redis.exceptions import ConnectionError as RedisConnectionError

import app.credential_service as credentials
import app.export_service as exports
import app.query_cache as cache
import app.worker as worker
from app.config import Settings


class FakeRedis:
    """Redis semantics needed by the cache, with a synthetic clock and no sockets."""

    def __init__(self, clock):
        self.clock = clock
        self.values = {}
        self.expires = {}
        self.commands = []
        self.failure = None
        self.closed = 0

    async def get(self, key):
        self.commands.append(("get", key))
        if self.failure is not None:
            raise self.failure
        return self._get(key)

    def _get(self, key):
        if key in self.expires and self.expires[key] <= self.clock[0]:
            self.values.pop(key, None)
            self.expires.pop(key, None)
        return self.values.get(key)

    async def set(self, key, value, *, nx=False, ex=None):
        self.commands.append(("set", key, nx, ex))
        if self.failure is not None:
            raise self.failure
        return self._set(key, value, nx=nx, ex=ex)

    def _set(self, key, value, *, nx=False, ex=None):
        if nx and key in self.values:
            return None
        self.values[key] = value.encode() if isinstance(value, str) else value
        if ex is None:
            self.expires.pop(key, None)
        else:
            self.expires[key] = self.clock[0] + ex
        return True

    async def eval(self, script, key_count, version_key, candidate, prefix, digest, refresh):
        self.commands.append(("eval", version_key, digest, refresh))
        if self.failure is not None:
            raise self.failure
        assert script == cache._READ_SCRIPT
        assert key_count == 1
        # Simulate the complete Lua operation without yielding between commands.
        raw_version = self._get(version_key)
        version = cache._nonce(raw_version)
        if version is None:
            version = candidate
            self._set(version_key, version, nx=raw_version is None)
        key = f"{prefix}{version}:{digest}"
        return [key.encode(), None if refresh == "1" else self._get(key)]

    async def aclose(self):
        self.closed += 1
        if self.failure is not None:
            raise self.failure


@pytest.fixture
def core(monkeypatch):
    clock = [100.0]
    redis = FakeRedis(clock)
    real_factory = cache.Redis.from_url
    factory = MagicMock(return_value=redis)
    monkeypatch.setattr(cache, "settings", SimpleNamespace(
        cache_redis_url="redis://synthetic-cache.invalid:6379/1",
        cache_ttl_seconds=10,
        cache_statistics_ttl_seconds=60,
        cache_timeout_seconds=0.15,
        cache_failure_backoff_seconds=3,
        cache_max_value_bytes=262_144,
    ))
    # Replace this module's clock reference without altering asyncio's clock.
    monkeypatch.setattr(cache, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(cache.Redis, "from_url", factory)
    monkeypatch.setattr(cache, "_client", None)
    monkeypatch.setattr(cache, "_failure_until", 0.0)
    monkeypatch.setattr(cache, "_flights", {})
    return SimpleNamespace(redis=redis, clock=clock, factory=factory, real_factory=real_factory)


def run(coroutine):
    return asyncio.run(coroutine)


def data_keys(core):
    return [key for key in core.redis.values if not key.endswith(":version")]


def test_unconfigured_cache_has_no_redis_client_or_network(core):
    cache.settings.cache_redis_url = ""
    loader = AsyncMock(return_value={"records": [], "total": 0})

    result = run(cache.cached_query("keys", {}, loader, refresh=True))
    run(cache.invalidate_query_cache("credentials"))
    run(cache.close_query_cache())

    assert result == cache.CacheResult({"records": [], "total": 0}, "BYPASS")
    loader.assert_awaited_once()
    core.factory.assert_not_called()
    assert core.redis.commands == []


def test_miss_hit_refresh_and_single_shared_pool(core):
    loader = AsyncMock(side_effect=[{"records": [{"masked": "synthetic…tail"}], "total": 1}, {"total": 2}])

    async def scenario():
        first = await cache.cached_query("keys", {"page": 1}, loader)
        first.data["records"].clear()
        second = await cache.cached_query("keys", {"page": 1}, loader)
        third = await cache.cached_query("keys", {"page": 1}, loader, refresh=True)
        fourth = await cache.cached_query("keys", {"page": 1}, loader)
        return first, second, third, fourth

    first, second, third, fourth = run(scenario())
    assert [item.status for item in (first, second, third, fourth)] == ["MISS", "HIT", "REFRESH", "HIT"]
    assert second.data["records"] == [{"masked": "synthetic…tail"}]
    assert fourth.data == {"total": 2}
    assert loader.await_count == 2
    core.factory.assert_called_once()
    options = core.factory.call_args.kwargs
    assert options["max_connections"] == 32
    assert options["socket_timeout"] == options["socket_connect_timeout"] == 0.15
    assert options["retry_on_timeout"] is False
    assert isinstance(options["retry"], AsyncRetry)
    assert list(core.redis.expires.values()) == [110.0]
    assert cache._flights == {}


def test_real_redis_pool_uses_async_retry_and_bounds_without_opening_network(core, monkeypatch):
    monkeypatch.setattr(cache.Redis, "from_url", core.real_factory)
    client = cache._redis_client()
    assert isinstance(client, cache.Redis)
    options = client.connection_pool.connection_kwargs
    assert options["db"] == 1
    assert options["socket_timeout"] == options["socket_connect_timeout"] == 0.15
    assert isinstance(options["retry"], AsyncRetry)
    connection = client.connection_pool.connection_class(**options)
    assert isinstance(connection.retry, AsyncRetry)
    assert client.connection_pool.max_connections == 32
    run(cache.close_query_cache())


def test_configured_async_retry_runs_failure_cleanup_once_without_retry_or_network(core):
    cache._redis_client()
    retry = core.factory.call_args.kwargs["retry"]
    operation = AsyncMock(side_effect=RedisConnectionError("synthetic-connection-failure"))
    cleanup = AsyncMock()
    with pytest.raises(RedisConnectionError, match="synthetic-connection-failure"):
        run(retry.call_with_retry(operation, cleanup))
    operation.assert_awaited_once()
    cleanup.assert_awaited_once()


@pytest.mark.parametrize("payload", [{"statuses": []}, [{"value": "valid", "name": "可用"}], []])
def test_dictionary_and_list_dtos_round_trip(core, payload):
    loader = AsyncMock(return_value=payload)
    assert run(cache.cached_query("statuses", {}, loader)).status == "MISS"
    assert run(cache.cached_query("statuses", {}, loader)).data == payload
    loader.assert_awaited_once()


def test_parameters_are_stable_hashed_and_never_written_verbatim(core):
    parameters = {"q": "synthetic-prefix-private", "ip": "192.0.2.88", "username": "synthetic-操作账号", "page": 2}
    loader = AsyncMock(return_value={"total": 0})
    assert run(cache.cached_query("keys", parameters, loader)).status == "MISS"
    assert run(cache.cached_query("keys", dict(reversed(list(parameters.items()))), loader)).status == "HIT"
    key = data_keys(core)[0]
    assert len(key.rsplit(":", 1)[1]) == 64
    assert all(str(value) not in key for field, value in parameters.items() if field != "page")
    assert all(value.encode() not in core.redis.values[key] for value in (parameters["q"], parameters["ip"], parameters["username"]))
    loader.assert_awaited_once()


@pytest.mark.parametrize("field,value", [
    ("provider", "Anthropic"), ("type", "anthropic_official"), ("page", 2),
    ("status", "invalid"), ("status_mode", "exact"), ("q", "synthetic-prefix"),
    ("created_from", "2026-10-16T00:00:00+00:00"), ("created_before", "2026-10-17T00:00:00+00:00"),
    ("ip", "192.0.2.66"), ("username", "synthetic-user"), ("user_id", 22), ("role", "user"),
])
def test_every_parameter_participates_in_cache_identity(core, field, value):
    loader = AsyncMock(return_value={"total": 0})
    assert run(cache.cached_query("export-records", {}, loader)).status == "MISS"
    assert run(cache.cached_query("export-records", {field: value}, loader)).status == "MISS"
    assert loader.await_count == 2
    assert len(data_keys(core)) == 2


def test_ttl_expiry_reloads_without_changing_family_version(core):
    loader = AsyncMock(side_effect=[{"total": 1}, {"total": 2}])
    first = run(cache.cached_query("keys", {}, loader))
    version_key = next(key for key in core.redis.values if key.endswith(":version"))
    version = core.redis.values[version_key]
    core.clock[0] += 10.01
    second = run(cache.cached_query("keys", {}, loader))
    assert first.status == second.status == "MISS"
    assert second.data == {"total": 2}
    assert core.redis.values[version_key] == version


def test_statistics_use_independent_sixty_second_ttl_and_refresh_extends_it(core):
    statistics_loader = AsyncMock(side_effect=[{"total": 667}, {"total": 668}, {"total": 669}])
    query_loader = AsyncMock(return_value={"total": 667})
    assert run(cache.cached_query("credential-statistics", {}, statistics_loader)).status == "MISS"
    assert run(cache.cached_query("keys", {}, query_loader)).status == "MISS"
    statistics_key = next(key for key in data_keys(core) if ":credential-statistics:" in key)
    assert core.redis.expires[statistics_key] == 160.0
    core.clock[0] += 11
    assert run(cache.cached_query("credential-statistics", {}, statistics_loader)) == cache.CacheResult({"total": 667}, "HIT")
    assert run(cache.cached_query("keys", {}, query_loader)).status == "MISS"
    assert run(cache.cached_query("credential-statistics", {}, statistics_loader, refresh=True)) == cache.CacheResult({"total": 668}, "REFRESH")
    assert core.redis.expires[statistics_key] == 171.0
    core.clock[0] = 171.01
    assert run(cache.cached_query("credential-statistics", {}, statistics_loader)) == cache.CacheResult({"total": 669}, "MISS")
    assert statistics_loader.await_count == 3


@pytest.mark.parametrize("namespace", ["keys", "statuses", "valid-key-records", "export-records", "credential-statistics"])
def test_only_statistics_use_configured_statistics_ttl(core, namespace):
    cache.settings.cache_ttl_seconds = 9
    cache.settings.cache_statistics_ttl_seconds = 75
    assert run(cache.cached_query(namespace, {}, AsyncMock(return_value={"total": 0}))).status == "MISS"
    ttl = 75 if namespace == "credential-statistics" else 9
    assert core.redis.expires[data_keys(core)[0]] == core.clock[0] + ttl


def test_credential_write_invalidation_refreshes_statistics_before_sixty_second_expiry(core):
    loader = AsyncMock(side_effect=[{"total": 667}, {"total": 668}])
    assert run(cache.cached_query("credential-statistics", {}, loader)).data == {"total": 667}
    core.clock[0] += 1
    run(cache.invalidate_query_cache("credentials"))
    assert run(cache.cached_query("credential-statistics", {}, loader)) == cache.CacheResult({"total": 668}, "MISS")
    assert loader.await_count == 2


def test_credentials_and_export_families_invalidate_independently(core):
    loaders = {namespace: AsyncMock(return_value={"total": 0}) for namespace in cache._NAMESPACES}

    async def scenario():
        for namespace, loader in loaders.items():
            assert (await cache.cached_query(namespace, {}, loader)).status == "MISS"
        await cache.invalidate_query_cache("credentials")
        for namespace, loader in loaders.items():
            expected = "HIT" if namespace == "export-records" else "MISS"
            assert (await cache.cached_query(namespace, {}, loader)).status == expected
        await cache.invalidate_query_cache("exports")
        for namespace, loader in loaders.items():
            expected = "MISS" if namespace == "export-records" else "HIT"
            assert (await cache.cached_query(namespace, {}, loader)).status == expected

    run(scenario())
    assert all(loader.await_count == 2 for loader in loaders.values())


def test_evicted_version_gets_random_nonce_so_old_values_cannot_revive(core):
    loader = AsyncMock(side_effect=[{"total": 1}, {"total": 2}])
    run(cache.cached_query("keys", {}, loader))
    version_key = next(key for key in core.redis.values if key.endswith(":version"))
    old_version = core.redis.values.pop(version_key)
    old_key = data_keys(core)[0]
    second = run(cache.cached_query("keys", {}, loader))
    assert second == cache.CacheResult({"total": 2}, "MISS")
    assert core.redis.values[version_key] != old_version
    assert old_key in core.redis.values


def test_existing_version_is_read_atomically_in_one_round_trip(core):
    winning_version = "a" * 32
    core.redis.values[f"{cache._PREFIX}:credentials:version"] = winning_version.encode()
    result = run(cache.cached_query("keys", {}, AsyncMock(return_value={"total": 0})))
    assert result.status == "MISS"
    assert f":{winning_version}:" in data_keys(core)[0]
    assert [command[0] for command in core.redis.commands] == ["eval", "set"]
    core.redis.commands.clear()
    assert run(cache.cached_query("keys", {}, AsyncMock(return_value={}))).status == "HIT"
    assert [command[0] for command in core.redis.commands] == ["eval"]


def test_refresh_and_cold_miss_use_one_read_and_one_write(core):
    loader = AsyncMock(return_value={"total": 0})
    assert run(cache.cached_query("keys", {}, loader)).status == "MISS"
    assert [command[0] for command in core.redis.commands] == ["eval", "set"]
    core.redis.commands.clear()
    assert run(cache.cached_query("keys", {}, loader, refresh=True)).status == "REFRESH"
    assert [command[0] for command in core.redis.commands] == ["eval", "set"]


@pytest.mark.parametrize("version", [b"broken", b"a" * 31, b"a" * 33, b"g" * 32, b"\xff"])
def test_corrupt_family_nonce_is_replaced_without_using_previous_data(core, version):
    loader = AsyncMock(side_effect=[{"total": 1}, {"total": 2}])
    run(cache.cached_query("keys", {}, loader))
    version_key = f"{cache._PREFIX}:credentials:version"
    core.redis.values[version_key] = version
    assert run(cache.cached_query("keys", {}, loader)) == cache.CacheResult({"total": 2}, "MISS")
    assert cache._nonce(core.redis.values[version_key]) is not None


def test_sensitive_field_normalization_cache_is_bounded(core):
    cache._sensitive_field.cache_clear()
    for index in range(300):
        cache._sensitive_field(f"synthetic-field-{index}")
    assert cache._sensitive_field.cache_info().currsize == 256
    assert cache._sensitive_field("encrypted_secret") is True
    before = cache._sensitive_field.cache_info().hits
    assert cache._sensitive_field("encrypted_secret") is True
    assert cache._sensitive_field.cache_info().hits == before + 1


def test_inflight_old_read_cannot_fill_current_version(core):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def old_snapshot():
            started.set()
            await release.wait()
            return {"total": 1}

        task = asyncio.create_task(cache.cached_query("keys", {}, old_snapshot))
        await started.wait()
        await cache.invalidate_query_cache("credentials")
        release.set()
        old = await task
        fresh_loader = AsyncMock(return_value={"total": 2})
        fresh = await cache.cached_query("keys", {}, fresh_loader)
        hit = await cache.cached_query("keys", {}, fresh_loader)
        assert old.data == {"total": 1}
        assert fresh == cache.CacheResult({"total": 2}, "MISS")
        assert hit == cache.CacheResult({"total": 2}, "HIT")
        fresh_loader.assert_awaited_once()

    run(scenario())
    assert len(data_keys(core)) == 2


@pytest.mark.parametrize("raw", [
    b"not-json", b"null", b"[]", b"{}", b'{"format":1,"data":"text"}',
    b'{"format":true,"data":{}}', b'{"format":2,"data":{}}',
    b'{"format":1,"data":{},"extra":1}', b'{"format":1,"data":{"count":NaN}}',
    b'{"format":1,"data":{"records":[{"secret":"synthetic-secret"}]}}',
    b"\xff", 123, b" " * 262_145, b'{"format":1,"data":{"count":1e999}}',
], ids=[f"invalid-value-{index}" for index in range(14)])
def test_corrupt_invalid_or_oversized_cache_value_is_a_miss(core, raw):
    loader = AsyncMock(side_effect=[{"total": 1}, {"total": 2}])
    run(cache.cached_query("keys", {}, loader))
    core.redis.values[data_keys(core)[0]] = raw
    result = run(cache.cached_query("keys", {}, loader))
    assert result == cache.CacheResult({"total": 2}, "MISS")
    assert loader.await_count == 2


@pytest.mark.parametrize("field", [
    "secret", "secrets", "encrypted_secret", "encryptedSecret", "password", "passwordHash",
    "token", "accessToken", "refresh_token", "sessionToken", "authorization", "cookie",
])
def test_secret_or_authentication_fields_are_never_cached(core, field):
    payload = {"records": [{field: "synthetic-sensitive-value"}]}
    loader = AsyncMock(return_value=payload)
    result = run(cache.cached_query("keys", {}, loader))
    assert result == cache.CacheResult(payload, "BYPASS")
    assert data_keys(core) == []


@pytest.mark.parametrize("payload", ["synthetic-txt-content", None, 42, {"value": float("nan")}, {"when": datetime(2026, 10, 17, tzinfo=UTC)}])
def test_non_dto_or_non_json_loader_values_bypass(core, payload):
    result = run(cache.cached_query("keys", {}, AsyncMock(return_value=payload)))
    assert result.status == "BYPASS"
    assert result.data is payload
    assert data_keys(core) == []


def test_oversized_value_is_not_written(core):
    cache.settings.cache_max_value_bytes = 1024
    payload = {"message": "合成测试" * 300}
    result = run(cache.cached_query("keys", {}, AsyncMock(return_value=payload)))
    assert result == cache.CacheResult(payload, "BYPASS")
    assert data_keys(core) == []


@pytest.mark.parametrize("parameters", [[], {"when": datetime(2026, 10, 17, tzinfo=UTC)}, {"value": float("nan")}, {1: "not-string"}])
def test_non_json_parameters_bypass_before_client_creation(core, parameters):
    result = run(cache.cached_query("keys", parameters, AsyncMock(return_value={"total": 0})))
    assert result.status == "BYPASS"
    core.factory.assert_not_called()


def test_only_approved_namespaces_and_families_are_accepted(core):
    loader = AsyncMock(return_value={})
    with pytest.raises(ValueError, match="Unsupported query cache namespace"):
        run(cache.cached_query("auth", {}, loader))
    with pytest.raises(ValueError, match="Unsupported query cache family"):
        run(cache.invalidate_query_cache("users"))
    loader.assert_not_awaited()
    core.factory.assert_not_called()


def test_network_failure_backoff_and_fixed_redacted_logging(core, caplog):
    private = "redis://synthetic-user:synthetic-password@synthetic-private-host/1"
    core.redis.failure = RuntimeError(private)
    loader = AsyncMock(return_value={"total": 3})
    assert run(cache.cached_query("keys", {}, loader)).status == "BYPASS"
    initial_commands = len(core.redis.commands)
    assert run(cache.cached_query("keys", {}, loader)).status == "BYPASS"
    run(cache.invalidate_query_cache("credentials"))
    assert len(core.redis.commands) == initial_commands
    assert private not in caplog.text
    assert "synthetic-password" not in caplog.text
    assert caplog.messages == ["Query cache unavailable; using database."]
    core.redis.failure = None
    core.clock[0] += 3.01
    assert run(cache.cached_query("keys", {}, loader)).status == "MISS"
    assert run(cache.cached_query("keys", {}, loader)).status == "HIT"
    assert loader.await_count == 3


def test_client_creation_failure_falls_back_without_exposing_error(core, caplog):
    core.factory.side_effect = ValueError("synthetic-private-connection-string")
    assert run(cache.cached_query("keys", {}, AsyncMock(return_value={}))).status == "BYPASS"
    assert "synthetic-private" not in caplog.text
    assert cache._client is None


def test_redis_timeout_is_bounded_and_falls_back(core, monkeypatch):
    cache.settings.cache_timeout_seconds = 0.01

    async def unresponsive_eval(*_args):
        await asyncio.sleep(10)

    monkeypatch.setattr(core.redis, "eval", unresponsive_eval)

    async def scenario():
        result = await asyncio.wait_for(cache.cached_query("keys", {}, AsyncMock(return_value={"total": 5})), 0.3)
        assert result == cache.CacheResult({"total": 5}, "BYPASS")

    run(scenario())


def test_write_failure_preserves_loader_result_and_uses_backoff(core, monkeypatch):
    original_set = core.redis.set

    async def failing_data_write(key, value, **kwargs):
        if kwargs.get("ex"):
            raise RuntimeError("synthetic-private-data")
        return await original_set(key, value, **kwargs)

    monkeypatch.setattr(core.redis, "set", failing_data_write)
    result = run(cache.cached_query("keys", {}, AsyncMock(return_value={"total": 4})))
    assert result == cache.CacheResult({"total": 4}, "BYPASS")
    assert data_keys(core) == []
    assert cache._failure_until > core.clock[0]


def test_invalidation_fault_never_raises(core):
    core.redis.failure = RuntimeError("synthetic-private-data")
    run(cache.invalidate_query_cache("exports"))
    assert cache._failure_until > core.clock[0]


def test_loader_exception_propagates_without_caching_or_tripping_redis_circuit(core):
    loader = AsyncMock(side_effect=RuntimeError("synthetic-database-failure"))
    with pytest.raises(RuntimeError, match="synthetic-database-failure"):
        run(cache.cached_query("keys", {}, loader))
    assert cache._failure_until == 0
    assert cache._flights == {}
    assert data_keys(core) == []
    assert run(cache.cached_query("keys", {}, AsyncMock(return_value={}))).status == "MISS"


def test_concurrent_identical_misses_load_once_without_background_database_task(core):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        owner = None
        calls = []

        async def loader():
            calls.append(asyncio.current_task())
            started.set()
            await release.wait()
            return {"total": 1}

        owner = asyncio.create_task(cache.cached_query("keys", {}, loader))
        await started.wait()
        waiters = [asyncio.create_task(cache.cached_query("keys", {}, loader)) for _ in range(8)]
        await asyncio.sleep(0.01)
        release.set()
        results = await asyncio.gather(owner, *waiters)
        assert calls == [owner]
        assert [result.status for result in results] == ["MISS", *(["HIT"] * 8)]

    run(scenario())
    assert cache._flights == {}


def test_cancelled_owner_releases_flight_and_waiter_uses_its_own_loader(core):
    async def scenario():
        started = asyncio.Event()

        async def owner_loader():
            started.set()
            await asyncio.Event().wait()

        owner = asyncio.create_task(cache.cached_query("keys", {}, owner_loader))
        await started.wait()
        waiter_loader = AsyncMock(return_value={"from": "waiter-session"})
        waiter = asyncio.create_task(cache.cached_query("keys", {}, waiter_loader))
        await asyncio.sleep(0.01)
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        assert await waiter == cache.CacheResult({"from": "waiter-session"}, "MISS")
        waiter_loader.assert_awaited_once()

    run(scenario())
    assert cache._flights == {}
    assert cache._failure_until == 0


def test_cancelled_waiter_does_not_cancel_owner_or_retain_lock(core):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def loader():
            started.set()
            await release.wait()
            return {"total": 1}

        owner = asyncio.create_task(cache.cached_query("keys", {}, loader))
        await started.wait()
        waiter_loader = AsyncMock(return_value={})
        waiter = asyncio.create_task(cache.cached_query("keys", {}, waiter_loader))
        await asyncio.sleep(0.01)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not owner.done()
        release.set()
        assert (await owner).status == "MISS"
        waiter_loader.assert_not_awaited()

    run(scenario())
    assert cache._flights == {}


def test_slow_owner_does_not_hold_waiters_beyond_short_cache_timeout(core):
    cache.settings.cache_timeout_seconds = 0.01

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_loader():
            started.set()
            await release.wait()
            return {"from": "owner"}

        owner = asyncio.create_task(cache.cached_query("keys", {}, slow_loader))
        await started.wait()
        waiter = await asyncio.wait_for(cache.cached_query("keys", {}, AsyncMock(return_value={"from": "waiter"})), 0.3)
        assert waiter == cache.CacheResult({"from": "waiter"}, "BYPASS")
        assert not owner.done()
        release.set()
        assert (await owner).status == "MISS"

    run(scenario())
    assert cache._flights == {}


def test_singleflight_capacity_is_bounded_and_excess_queries_bypass(core, monkeypatch):
    monkeypatch.setattr(cache, "_MAX_FLIGHTS", 0)
    result = run(cache.cached_query("keys", {}, AsyncMock(return_value={"total": 0})))
    assert result.status == "BYPASS"
    assert cache._flights == {}
    assert data_keys(core) == []


def test_close_releases_shared_client_and_resets_circuit(core):
    run(cache.cached_query("keys", {}, AsyncMock(return_value={})))
    cache._failure_until = 200
    run(cache.close_query_cache())
    assert core.redis.closed == 1
    assert cache._client is None
    assert cache._failure_until == 0
    run(cache.close_query_cache())
    assert core.redis.closed == 1


def test_close_failure_is_redacted_and_nonfatal(core, caplog):
    run(cache.cached_query("keys", {}, AsyncMock(return_value={})))
    core.redis.failure = RuntimeError("synthetic-private-close-error")
    run(cache.close_query_cache())
    assert "synthetic-private" not in caplog.text
    assert caplog.messages == ["Query cache close failed."]
    assert cache._client is None


@pytest.mark.parametrize("commit_fails", [False, True])
def test_credential_family_invalidates_only_after_successful_commit(monkeypatch, commit_fails):
    events = []
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=SimpleNamespace(id=77, total=0)),
        execute=AsyncMock(return_value=SimpleNamespace(one=lambda: (0, 0, 0, 0, 0))),
    )

    async def commit():
        events.append("commit")
        if commit_fails:
            raise RuntimeError("synthetic-commit-failure")

    async def invalidate(family):
        events.append(family)

    session.commit = commit
    monkeypatch.setattr(credentials, "ensure_credential_statuses", AsyncMock())
    monkeypatch.setattr(credentials, "invalidate_query_cache", invalidate)
    if commit_fails:
        with pytest.raises(RuntimeError, match="synthetic-commit-failure"):
            run(credentials.process_batch_results(session, 77, []))
        assert events == ["commit"]
    else:
        assert run(credentials.process_batch_results(session, 77, [])) == 0
        assert events == ["commit", "credentials"]


def test_validation_failure_never_invalidates_query_cache(monkeypatch):
    session = SimpleNamespace(scalar=AsyncMock(return_value=SimpleNamespace(id=77, total=0)), commit=AsyncMock())
    invalidate = AsyncMock()
    monkeypatch.setattr(credentials, "ensure_credential_statuses", AsyncMock(side_effect=RuntimeError("synthetic-validation-failure")))
    monkeypatch.setattr(credentials, "invalidate_query_cache", invalidate)
    with pytest.raises(RuntimeError, match="synthetic-validation-failure"):
        run(credentials.process_batch_results(session, 77, []))
    invalidate.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.parametrize("commit_fails", [False, True])
def test_export_family_invalidates_only_after_successful_commit(monkeypatch, commit_fails):
    events = []

    async def commit():
        events.append("commit")
        if commit_fails:
            raise RuntimeError("synthetic-commit-failure")

    async def invalidate(family):
        events.append(family)

    session = SimpleNamespace(add=MagicMock(), commit=commit)
    monkeypatch.setattr(exports, "invalidate_query_cache", invalidate)
    operation = exports.record_export(
        session, user=SimpleNamespace(id=1, username="synthetic-user"),
        provider="OpenAI", storage_type="openai_official", status="valid", count=1, file_name="synthetic.txt",
    )
    if commit_fails:
        with pytest.raises(RuntimeError, match="synthetic-commit-failure"):
            run(operation)
        assert events == ["commit"]
    else:
        run(operation)
        assert events == ["commit", "exports"]


def test_worker_closes_cache_and_queue_clients_even_on_failure(monkeypatch):
    queue_redis = SimpleNamespace(aclose=AsyncMock())
    close_cache = AsyncMock()
    monkeypatch.setattr(worker, "redis_client", lambda: queue_redis)
    monkeypatch.setattr(worker, "ensure_consumer_group", AsyncMock(side_effect=RuntimeError("synthetic-worker-failure")))
    monkeypatch.setattr(worker, "close_query_cache", close_cache)
    with pytest.raises(RuntimeError, match="synthetic-worker-failure"):
        run(worker.run())
    close_cache.assert_awaited_once()
    queue_redis.aclose.assert_awaited_once()


@pytest.mark.parametrize("name,value", [
    ("cache_ttl_seconds", 0), ("cache_ttl_seconds", 301),
    ("cache_statistics_ttl_seconds", 0), ("cache_statistics_ttl_seconds", 301),
    ("cache_timeout_seconds", 0), ("cache_timeout_seconds", 2.1),
    ("cache_failure_backoff_seconds", 0), ("cache_failure_backoff_seconds", 61),
    ("cache_max_value_bytes", 1023), ("cache_max_value_bytes", 1_048_577),
])
def test_cache_settings_reject_unbounded_or_invalid_values(name, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{name: value})


def test_cache_settings_defaults_are_disabled_and_short_lived(monkeypatch):
    for name in ("CACHE_REDIS_URL", "CACHE_TTL_SECONDS", "CACHE_STATISTICS_TTL_SECONDS", "CACHE_TIMEOUT_SECONDS", "CACHE_FAILURE_BACKOFF_SECONDS", "CACHE_MAX_VALUE_BYTES"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)
    assert settings.cache_redis_url == ""
    assert settings.cache_ttl_seconds == 10
    assert settings.cache_statistics_ttl_seconds == 60
    assert settings.cache_timeout_seconds == 0.15
    assert settings.cache_failure_backoff_seconds == 3
    assert settings.cache_max_value_bytes == 262_144
