import asyncio
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.credential_service as credentials
import app.credential_statistics as statistics
import app.main as api
import app.query_cache as cache
from app.database import AuthIPBlock, AuthUser, Credential, CredentialStatus
from test_query_cache import core, data_keys


NOW = datetime(2026, 10, 17, tzinfo=UTC)
SCOPE = {"provider": "OpenAI", "storage_type": "openai_official"}
SUMMARY = {"total": 6, "valid": 2, "recovery": 1, "insufficient": 1, "invalid": 2}
STATUS_COUNTS = {
    "valid": 2, "usage_limit_reached": 1, "insufficient_credit": 1,
    "invalid": 0, "rate_limited": 1, "future_detector_result": 1, "no_models": 0,
}


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture
def synthetic_database():
    """Real aggregate/filter SQL over an isolated SQLite database, never production."""
    engine = create_engine("sqlite://")
    CredentialStatus.__table__.create(engine)
    Credential.__table__.create(engine)
    with Session(engine) as db:
        db.add_all([
            CredentialStatus(value=value, name=name, tone=tone, sort_order=index)
            for index, (value, name, tone) in enumerate([
                ("valid", "可用", "valid"),
                ("usage_limit_reached", "额度待恢复", "recovery"),
                ("insufficient_credit", "余额不足", "insufficient"),
                ("invalid", "无效", "invalid"),
                ("rate_limited", "请求限速", "recovery"),
                ("future_detector_result", "自定义检测结果", "neutral"),
                ("no_models", "无可用模型", "invalid"),
                ("pending", "待检测", "neutral"),
            ])
        ])

        def add_row(identifier, *, status="valid", provider="OpenAI",
                    storage_type="openai_official", when=NOW, prefix=None):
            row = Credential(
                id=identifier, fingerprint=f"synthetic-fingerprint-{identifier}",
                provider=provider, storage_type=storage_type, kind="synthetic",
                encrypted_secret=f"synthetic-cipher-{identifier}",
                key_prefix=prefix or f"synthetic-{identifier}", masked="synthetic…test",
                status=status, result_bucket=credentials.result_bucket(status),
                created_at=when, updated_at=when,
            )
            db.add(row)
            return row

        add_row(1, prefix="target-one")
        add_row(2, status="rate_limited", prefix="target-two")
        add_row(3, status="future_detector_result")
        add_row(4, status="usage_limit_reached")
        add_row(5, status="insufficient_credit")
        add_row(6, status="pending")
        add_row(7, provider="Anthropic", storage_type="anthropic_official")
        add_row(8, status="invalid", storage_type="synthetic_other_type")
        add_row(9, when=NOW - timedelta(days=7))
        db.commit()
        yield SimpleNamespace(
            execute=AsyncMock(side_effect=db.execute),
            scalar=AsyncMock(side_effect=db.scalar),
            scalars=AsyncMock(side_effect=db.scalars),
            # The global IP gate still runs in API tests, without real IP block data.
            get=AsyncMock(side_effect=lambda model, *args: None if model is AuthIPBlock else db.get(model, *args)),
            db=db, add_row=add_row,
        )
    engine.dispose()


@pytest.fixture
def request_statistics(monkeypatch, synthetic_database, core):
    user = AuthUser(id=17, username="synthetic-statistics-user", role="user")
    monkeypatch.setattr(api, "current_user", AsyncMock(return_value=user))
    # Exercise the real cache and statistics integration, rather than a DTO mock.
    monkeypatch.setattr(api, "cached_query", cache.cached_query)
    monkeypatch.setattr(api, "list_credentials", credentials.list_credentials)
    monkeypatch.setattr(api, "list_credential_statistics", statistics.list_credential_statistics)
    monkeypatch.setattr(credentials, "list_credential_statistics", statistics.list_credential_statistics)

    async def fake_session():
        yield synthetic_database

    previous = api.app.dependency_overrides.copy()
    api.app.dependency_overrides[api.get_session] = fake_session

    def request(path, **params):
        async def send():
            transport = httpx.ASGITransport(app=api.app, raise_app_exceptions=True, client=("127.0.0.1", 123))
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.get(path, params=params)
        return run(send())

    yield request
    api.app.dependency_overrides.clear()
    api.app.dependency_overrides.update(previous)


def test_real_aggregates_cache_summary_custom_statuses_and_zero_counts(core, synthetic_database):
    first = run(statistics.list_credential_statistics(synthetic_database, **SCOPE))
    second = run(statistics.list_credential_statistics(synthetic_database, **SCOPE))

    assert (first.status, second.status) == ("MISS", "HIT")
    assert second.data == first.data
    assert first.data["summary"] == SUMMARY
    assert {item["value"]: item["count"] for item in first.data["statuses"]} == STATUS_COUNTS
    assert next(item for item in first.data["statuses"] if item["value"] == "future_detector_result") == {
        "value": "future_detector_result", "name": "自定义检测结果", "tone": "neutral", "sortOrder": 5, "count": 1,
    }
    assert "pending" not in {item["value"] for item in first.data["statuses"]}
    assert synthetic_database.execute.await_count == 2
    assert len(data_keys(core)) == 1
    assert all(b"synthetic-cipher" not in value for value in core.redis.values.values())


def test_type_only_and_explicit_provider_share_the_canonical_scope(core, synthetic_database):
    inferred = run(statistics.list_credential_statistics(synthetic_database, storage_type="openai_official"))
    explicit = run(statistics.list_credential_statistics(synthetic_database, **SCOPE))
    blank = run(statistics.list_credential_statistics(synthetic_database, provider="", storage_type="openai_official"))

    assert [item.status for item in (inferred, explicit, blank)] == ["MISS", "HIT", "HIT"]
    assert inferred.data["summary"] == SUMMARY
    assert synthetic_database.execute.await_count == 2


def test_blank_and_missing_global_scope_share_one_statistics_entry(core, synthetic_database):
    first = run(statistics.list_credential_statistics(synthetic_database))
    second = run(statistics.list_credential_statistics(synthetic_database, provider="", storage_type=""))
    assert (first.status, second.status) == ("MISS", "HIT")
    assert first.data["summary"]["total"] == 8
    assert synthetic_database.execute.await_count == 2


def test_platform_type_and_both_date_bounds_have_independent_scopes(core, synthetic_database):
    inputs = [
        (SCOPE, 6),
        ({"provider": "OpenAI"}, 7),
        ({"provider": "Anthropic", "storage_type": "anthropic_official"}, 1),
        ({**SCOPE, "created_from": NOW - timedelta(days=1)}, 5),
        ({**SCOPE, "created_before": NOW}, 1),
        ({**SCOPE, "created_from": NOW - timedelta(days=1), "created_before": NOW + timedelta(days=1)}, 5),
    ]
    for scope, expected in inputs:
        result = run(statistics.list_credential_statistics(synthetic_database, **scope))
        assert result.status == "MISS"
        assert result.data["summary"]["total"] == expected
        assert run(statistics.list_credential_statistics(synthetic_database, **scope)).status == "HIT"
    assert synthetic_database.execute.await_count == 2 * len(inputs)
    assert len(data_keys(core)) == len(inputs)


def test_equivalent_timezone_boundaries_share_the_utc_cache_identity(core, synthetic_database):
    start, before = NOW - timedelta(days=1), NOW + timedelta(days=1)
    first = run(statistics.list_credential_statistics(
        synthetic_database, **SCOPE, created_from=start, created_before=before,
    ))
    local_zone = timezone(timedelta(hours=8))
    second = run(statistics.list_credential_statistics(
        synthetic_database, **SCOPE,
        created_from=start.astimezone(local_zone), created_before=before.astimezone(local_zone),
    ))
    assert (first.status, second.status) == ("MISS", "HIT")
    assert first.data == second.data
    assert synthetic_database.execute.await_count == 2


def test_empty_platform_still_keeps_complete_nonpending_zero_directory(core, synthetic_database):
    result = run(statistics.list_credential_statistics(synthetic_database, provider="Azure", storage_type="azure_gpt"))
    assert result.data["summary"] == dict.fromkeys(SUMMARY, 0)
    assert [item["value"] for item in result.data["statuses"]] == list(STATUS_COUNTS)
    assert all(item["count"] == 0 for item in result.data["statuses"])
    assert synthetic_database.execute.await_count == 2


def test_pages_status_modes_and_prefix_search_reuse_one_unfiltered_statistics_scope(core, synthetic_database):
    for identifier in range(100, 201):
        synthetic_database.add_row(identifier)
    synthetic_database.db.commit()
    options = {**SCOPE, "status": "valid", "query": None, "page": 1, "status_mode": "exact"}

    first = run(credentials.list_credentials(synthetic_database, **options))
    second = run(credentials.list_credentials(synthetic_database, **{**options, "page": 2}))
    custom = run(credentials.list_credentials(synthetic_database, **{**options, "status": "rate_limited"}))
    legacy = run(credentials.list_credentials(synthetic_database, **{**options, "status": "invalid", "status_mode": "bucket"}))
    prefix = run(credentials.list_credentials(synthetic_database, **{**options, "query": "target-", "status": "invalid"}))
    # Prefix search ignores both selected status and date bounds, including reversed bounds.
    prefix_with_dates = run(credentials.list_credentials(
        synthetic_database, **{**options, "query": "target-", "status": "no_models"},
        created_from=NOW + timedelta(days=1), created_before=NOW - timedelta(days=1),
    ))

    assert (first["total"], second["page"], len(first["keys"]), len(second["keys"])) == (103, 2, 100, 3)
    assert [item["id"] for item in custom["keys"]] == [2]
    assert {item["id"] for item in legacy["keys"]} == {2, 3}
    assert {item["id"] for item in prefix["keys"]} == {1, 2}
    assert prefix_with_dates == prefix
    assert all(item["summary"] == {**SUMMARY, "total": 107, "valid": 103} for item in (
        first, second, custom, legacy, prefix, prefix_with_dates,
    ))
    assert all(item["statuses"] == first["statuses"] for item in (second, custom, legacy, prefix, prefix_with_dates))
    assert synthetic_database.execute.await_count == 2  # The two aggregates ran once for six lists.
    assert synthetic_database.scalar.await_count == synthetic_database.scalars.await_count == 6
    assert len(data_keys(core)) == 1


def test_refresh_reloads_both_aggregates_and_writes_updated_statistics(core, synthetic_database):
    first = run(statistics.list_credential_statistics(synthetic_database, **SCOPE))
    synthetic_database.add_row(100)
    synthetic_database.db.commit()
    stale = run(statistics.list_credential_statistics(synthetic_database, **SCOPE))
    fresh = run(statistics.list_credential_statistics(synthetic_database, **SCOPE, refresh=True))
    hit = run(statistics.list_credential_statistics(synthetic_database, **SCOPE))

    assert [item.status for item in (first, stale, fresh, hit)] == ["MISS", "HIT", "REFRESH", "HIT"]
    assert stale.data["summary"] == SUMMARY
    assert fresh.data["summary"] == {**SUMMARY, "total": 7, "valid": 3}
    assert fresh.data == hit.data
    assert synthetic_database.execute.await_count == 4


def test_only_credential_invalidation_refreshes_cached_counts_and_new_status_names(core, synthetic_database):
    run(statistics.list_credential_statistics(synthetic_database, **SCOPE))
    synthetic_database.db.add(CredentialStatus(value="new_detector_status", name="新检测结果", tone="neutral", sort_order=20))
    synthetic_database.add_row(100, status="new_detector_status")
    synthetic_database.db.commit()

    run(cache.invalidate_query_cache("exports"))
    old = run(statistics.list_credential_statistics(synthetic_database, **SCOPE))
    assert old.status == "HIT"
    assert old.data["summary"] == SUMMARY
    assert "new_detector_status" not in {item["value"] for item in old.data["statuses"]}

    run(cache.invalidate_query_cache("credentials"))
    fresh = run(statistics.list_credential_statistics(synthetic_database, **SCOPE))
    assert fresh.status == "MISS"
    assert fresh.data["summary"] == {**SUMMARY, "total": 7, "invalid": 3}
    assert fresh.data["statuses"][-1] == {
        "value": "new_detector_status", "name": "新检测结果", "tone": "neutral", "sortOrder": 20, "count": 1,
    }
    assert synthetic_database.execute.await_count == 4


def test_statistics_expire_after_sixty_seconds_independently_of_page_cache(core, synthetic_database):
    page_loader = AsyncMock(return_value={"keys": [], "total": 0})
    assert run(statistics.list_credential_statistics(synthetic_database, **SCOPE)).status == "MISS"
    assert run(cache.cached_query("keys", {"page": 1}, page_loader)).status == "MISS"
    core.clock[0] += 11
    assert run(statistics.list_credential_statistics(synthetic_database, **SCOPE)).status == "HIT"
    assert run(cache.cached_query("keys", {"page": 1}, page_loader)).status == "MISS"
    core.clock[0] += 50
    assert run(statistics.list_credential_statistics(synthetic_database, **SCOPE)).status == "MISS"
    assert synthetic_database.execute.await_count == 4
    assert page_loader.await_count == 2


def test_redis_failure_preserves_real_counts_without_network_or_sensitive_error(core, synthetic_database, caplog):
    core.redis.failure = RedisConnectionError("synthetic-private-redis-error")
    result = run(statistics.list_credential_statistics(synthetic_database, **SCOPE))
    assert result.status == "BYPASS"
    assert result.data["summary"] == SUMMARY
    assert synthetic_database.execute.await_count == 2
    assert "synthetic-private" not in caplog.text
    assert data_keys(core) == []


def test_status_api_and_keys_share_statistics_cache_without_second_wrapper(request_statistics, synthetic_database, core):
    statuses = request_statistics("/api/credential-statuses", provider="OpenAI", type="openai_official")
    keys = request_statistics("/api/keys", provider="OpenAI", type="openai_official", status="valid", status_mode="exact")
    statuses_again = request_statistics("/api/credential-statuses", provider="OpenAI", type="openai_official")

    assert [response.status_code for response in (statuses, keys, statuses_again)] == [200, 200, 200]
    assert [response.headers["X-Data-Cache"] for response in (statuses, keys, statuses_again)] == ["MISS", "MISS", "HIT"]
    assert all(response.headers["Cache-Control"] == "no-store" for response in (statuses, keys, statuses_again))
    assert statuses.json()["statuses"] == keys.json()["statuses"] == statuses_again.json()["statuses"]
    assert keys.json()["summary"] == SUMMARY
    assert synthetic_database.execute.await_count == 2
    assert len(data_keys(core)) == 2
    assert not any(":statuses:" in key for key in data_keys(core))


@pytest.mark.parametrize("path", ["/api/keys", "/api/credential-statuses"])
def test_api_manual_refresh_recomputes_shared_statistics_and_updates_directory(
    request_statistics, synthetic_database, path,
):
    params = {"provider": "OpenAI", "type": "openai_official"}
    assert request_statistics("/api/credential-statuses", **params).headers["X-Data-Cache"] == "MISS"
    if path == "/api/keys":
        assert request_statistics(path, **params).headers["X-Data-Cache"] == "MISS"
    synthetic_database.add_row(100)
    synthetic_database.db.commit()

    refreshed = request_statistics(path, **params, refresh="true")
    assert refreshed.status_code == 200
    assert refreshed.headers["X-Data-Cache"] == "REFRESH"
    counts = {item["value"]: item["count"] for item in refreshed.json()["statuses"]}
    assert counts["valid"] == 3
    if path == "/api/keys":
        assert refreshed.json()["summary"] == {**SUMMARY, "total": 7, "valid": 3}
    subsequent = request_statistics("/api/credential-statuses", **params)
    assert subsequent.headers["X-Data-Cache"] == "HIT"
    assert subsequent.json()["statuses"] == refreshed.json()["statuses"]
    assert synthetic_database.execute.await_count == 4
