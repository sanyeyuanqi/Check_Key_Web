import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.main as api
import app.credential_statistics as statistics
from app.database import AuthIPBlock, AuthUser
from test_export_records import mock_query_export


ENDPOINTS = [
    ("/api/keys", "keys"),
    ("/api/credential-statuses", "credential-statistics"),
    ("/api/valid-key-records", "valid-key-records"),
    ("/api/export-records", "export-records"),
]


class MemoryQueryCache:
    """Deterministic API-level cache double; it never opens Redis or any sockets."""

    def __init__(self):
        self.entries = {}
        self.calls = []
        self.bypass = False

    async def __call__(self, namespace, parameters, loader, *, refresh=False):
        self.calls.append((namespace, copy.deepcopy(parameters), refresh))
        key = (namespace, json.dumps(parameters, sort_keys=True, default=str))
        if self.bypass:
            return SimpleNamespace(data=await loader(), status="BYPASS")
        if not refresh and key in self.entries:
            return SimpleNamespace(data=copy.deepcopy(self.entries[key]), status="HIT")
        data = await loader()
        self.entries[key] = copy.deepcopy(data)
        return SimpleNamespace(data=data, status="REFRESH" if refresh else "MISS")


@pytest.fixture
def cached_api(monkeypatch):
    state = SimpleNamespace(
        user=AuthUser(id=17, username="synthetic-user", role="user", active=True),
        blocked=False, auth_checks=0,
    )
    session = MagicMock(spec=AsyncSession)

    async def get_row(model, identity):
        if model is AuthIPBlock:
            return AuthIPBlock(ip_address=identity, active=True) if state.blocked else None
        if model is AuthUser and state.user and identity == state.user.id:
            return state.user
        return None

    session.get.side_effect = get_row

    async def authenticated_user(*args):
        state.auth_checks += 1
        return state.user if state.user and state.user.active else None

    monkeypatch.setattr(api, "current_user", authenticated_user)
    cache = MemoryQueryCache()
    monkeypatch.setattr(api, "cached_query", cache, raising=False)
    monkeypatch.setattr(statistics, "cached_query", cache)
    session.execute.return_value = SimpleNamespace(all=lambda: [("valid", 1)])
    readers = {}

    for namespace, function_name in [
        ("keys", "list_credentials"), ("credential-statistics", "list_credential_statuses"),
        ("valid-key-records", "list_valid_key_records"), ("export-records", "list_export_records"),
    ]:
        counter = {"loads": 0}

        async def read(*args, _namespace=namespace, _counter=counter, **kwargs):
            _counter["loads"] += 1
            epoch = _counter["loads"]
            if _namespace == "credential-statistics":
                return [{"value": "valid", "name": "可用", "tone": "valid", "sortOrder": 0, "count": epoch}]
            if _namespace == "keys":
                return {"keys": [{"id": epoch, "masked": "synthetic…test"}], "total": 1, "page": 1, "totalPages": 1, "epoch": epoch}
            if _namespace == "valid-key-records":
                return {"records": [{"credentialId": epoch, "masked": "synthetic…test"}], "total": 1, "page": 1, "totalPages": 1, "epoch": epoch}
            user = kwargs["user"]
            return {"records": [{"id": epoch, "userId": user.id, "role": user.role}], "total": 1, "page": 1, "totalPages": 1, "epoch": epoch}

        readers[namespace] = AsyncMock(side_effect=read)
        monkeypatch.setattr(statistics if namespace == "credential-statistics" else api, function_name, readers[namespace])

    async def fake_session():
        yield session

    async def fake_redis():
        yield SimpleNamespace()

    previous_overrides = api.app.dependency_overrides.copy()
    api.app.dependency_overrides[api.get_session] = fake_session
    api.app.dependency_overrides[api.redis_dependency] = fake_redis

    def request(path, *, query=None, method="GET", body=None):
        async def send():
            transport = httpx.ASGITransport(app=api.app, raise_app_exceptions=False, client=("203.0.113.7", 123))
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.request(method, path, params=query, json=body)

        return asyncio.run(send())

    yield SimpleNamespace(request=request, state=state, session=session, cache=cache, readers=readers)
    api.app.dependency_overrides.clear()
    api.app.dependency_overrides.update(previous_overrides)


@pytest.mark.parametrize("path,namespace", ENDPOINTS)
def test_list_cache_miss_hit_force_refresh_and_headers(cached_api, path, namespace):
    first = cached_api.request(path)
    second = cached_api.request(path)
    refreshed = cached_api.request(path, query={"refresh": "true"})
    after_refresh = cached_api.request(path)
    assert [response.status_code for response in (first, second, refreshed, after_refresh)] == [200] * 4
    assert [response.headers["X-Data-Cache"] for response in (first, second, refreshed, after_refresh)] == ["MISS", "HIT", "REFRESH", "HIT"]
    assert all(response.headers["Cache-Control"] == "no-store" for response in (first, second, refreshed, after_refresh))
    assert first.json() == second.json()
    assert refreshed.json() != first.json()
    assert after_refresh.json() == refreshed.json()
    assert cached_api.readers[namespace].await_count == 2
    assert {call[0] for call in cached_api.cache.calls} == {namespace}
    assert all("refresh" not in parameters for _, parameters, _ in cached_api.cache.calls)


def test_bypass_status_is_exposed_without_browser_caching(cached_api):
    cached_api.cache.bypass = True
    response = cached_api.request("/api/keys")
    assert response.status_code == 200
    assert response.headers["X-Data-Cache"] == "BYPASS"
    assert response.headers["Cache-Control"] == "no-store"
    cached_api.readers["keys"].assert_awaited_once()


@pytest.mark.parametrize("path,namespace", ENDPOINTS)
@pytest.mark.parametrize("revocation", ["logged-out", "inactive", "blocked-ip"])
def test_cache_hits_never_bypass_fresh_authentication_or_ip_block_checks(cached_api, path, namespace, revocation):
    assert cached_api.request(path).headers["X-Data-Cache"] == "MISS"
    assert cached_api.request(path).headers["X-Data-Cache"] == "HIT"
    if revocation == "logged-out":
        cached_api.state.user = None
    elif revocation == "inactive":
        cached_api.state.user.active = False
    else:
        cached_api.state.blocked = True
    response = cached_api.request(path)
    assert response.status_code == (403 if revocation == "blocked-ip" else 401)
    if revocation == "blocked-ip":
        assert response.json()["code"] == "IP_BLOCKED"
    assert len(cached_api.cache.calls) == 2
    cached_api.readers[namespace].assert_awaited_once()
    assert cached_api.session.get.await_count == 3
    assert cached_api.state.auth_checks == (2 if revocation == "blocked-ip" else 3)


def test_export_history_cache_keys_isolate_members_and_privilege_changes(cached_api):
    responses = []
    for identifier, role in [(17, "user"), (18, "user"), (17, "admin"), (19, "user"), (17, "user")]:
        cached_api.state.user = AuthUser(id=identifier, username=f"synthetic-{identifier}", role=role, active=True)
        response = cached_api.request("/api/export-records")
        assert response.status_code == 200
        assert response.json()["records"][0]["userId"] == identifier
        assert response.json()["records"][0]["role"] == role
        responses.append(response)
    assert [response.headers["X-Data-Cache"] for response in responses] == ["MISS", "MISS", "MISS", "MISS", "HIT"]
    assert cached_api.readers["export-records"].await_count == 4


@pytest.mark.parametrize("path,namespace,first_query,second_query", [
    ("/api/keys", "keys", {"provider": "OpenAI"}, {"provider": "Anthropic"}),
    ("/api/keys", "keys", {"type": "openai_official"}, {"type": "azure_gpt"}),
    ("/api/keys", "keys", {"status": "valid"}, {"status": "invalid"}),
    ("/api/keys", "keys", {"status_mode": "bucket"}, {"status_mode": "exact"}),
    ("/api/keys", "keys", {"q": "first"}, {"q": "second"}),
    ("/api/keys", "keys", {"page": 1}, {"page": 2}),
    ("/api/keys", "keys", {"created_from": "2026-10-01T00:00:00Z"}, {"created_from": "2026-10-02T00:00:00Z"}),
    ("/api/keys", "keys", {"created_before": "2026-10-02T00:00:00Z"}, {"created_before": "2026-10-03T00:00:00Z"}),
    ("/api/credential-statuses", "credential-statistics", {"provider": "OpenAI"}, {"provider": "Anthropic"}),
    ("/api/credential-statuses", "credential-statistics", {"type": "openai_official"}, {"type": "azure_gpt"}),
    ("/api/credential-statuses", "credential-statistics", {"created_from": "2026-10-01T00:00:00Z"}, {"created_from": "2026-10-02T00:00:00Z"}),
    ("/api/credential-statuses", "credential-statistics", {"created_before": "2026-10-02T00:00:00Z"}, {"created_before": "2026-10-03T00:00:00Z"}),
    ("/api/valid-key-records", "valid-key-records", {"provider": "OpenAI"}, {"provider": "Anthropic"}),
    ("/api/valid-key-records", "valid-key-records", {"type": "openai_official"}, {"type": "azure_gpt"}),
    ("/api/valid-key-records", "valid-key-records", {"q": "first"}, {"q": "second"}),
    ("/api/valid-key-records", "valid-key-records", {"ip": "172.20"}, {"ip": "192.168"}),
    ("/api/valid-key-records", "valid-key-records", {"page": 1}, {"page": 2}),
    ("/api/export-records", "export-records", {"username": "first"}, {"username": "second"}),
    ("/api/export-records", "export-records", {"ip": "172.20"}, {"ip": "192.168"}),
    ("/api/export-records", "export-records", {"page": 1}, {"page": 2}),
])
def test_effective_query_parameters_create_separate_cache_entries(cached_api, path, namespace, first_query, second_query):
    first = cached_api.request(path, query=first_query)
    second = cached_api.request(path, query=second_query)
    repeated = cached_api.request(path, query=second_query)
    assert [response.status_code for response in (first, second, repeated)] == [200] * 3
    assert [response.headers["X-Data-Cache"] for response in (first, second, repeated)] == ["MISS", "MISS", "HIT"]
    assert first.json() != second.json()
    assert repeated.json() == second.json()
    assert cached_api.readers[namespace].await_count == 2


def test_each_list_uses_its_own_namespace_and_payload_shape(cached_api):
    first = [cached_api.request(path) for path, _ in ENDPOINTS]
    second = [cached_api.request(path) for path, _ in ENDPOINTS]
    assert [response.headers["X-Data-Cache"] for response in first] == ["MISS"] * 4
    assert [response.headers["X-Data-Cache"] for response in second] == ["HIT"] * 4
    assert [call[0] for call in cached_api.cache.calls[:4]] == [namespace for _, namespace in ENDPOINTS]
    assert "keys" in first[0].json()
    assert "statuses" in first[1].json()
    assert "records" in first[2].json()
    assert "records" in first[3].json()
    assert len(cached_api.cache.entries) == 4


def test_key_prefix_cache_ignores_status_and_date_filters_that_do_not_affect_the_query(cached_api):
    first = cached_api.request("/api/keys", query={
        "q": "synthetic", "status": "invalid", "created_from": "2026-10-17T00:00:00Z", "created_before": "2026-10-01T00:00:00Z",
    })
    second = cached_api.request("/api/keys", query={"q": "synthetic", "status": "valid"})
    assert [first.status_code, second.status_code] == [200, 200]
    assert [first.headers["X-Data-Cache"], second.headers["X-Data-Cache"]] == ["MISS", "HIT"]
    cached_api.readers["keys"].assert_awaited_once()
    kwargs = cached_api.readers["keys"].await_args.kwargs
    assert kwargs["status"] == "all"
    assert "created_from" not in kwargs and "created_before" not in kwargs


def test_valid_ip_query_cache_ignores_the_inactive_key_prefix_input(cached_api):
    first = cached_api.request("/api/valid-key-records", query={"ip": "172.20", "q": "first"})
    second = cached_api.request("/api/valid-key-records", query={"ip": "172.20", "q": "second"})
    third = cached_api.request("/api/valid-key-records", query={"q": "first"})
    assert [first.status_code, second.status_code, third.status_code] == [200] * 3
    assert [first.headers["X-Data-Cache"], second.headers["X-Data-Cache"], third.headers["X-Data-Cache"]] == ["MISS", "HIT", "MISS"]
    assert cached_api.readers["valid-key-records"].await_count == 2


@pytest.mark.parametrize("path", ["/api/keys", "/api/credential-statuses"])
@pytest.mark.parametrize("dates", [
    {"created_from": "2026-10-17T00:00:00"},
    {"created_from": "invalid-date"},
    {"created_from": "2026-10-17T00:00:00Z", "created_before": "2026-10-01T00:00:00Z"},
])
def test_date_validation_runs_before_any_cache_or_query(cached_api, path, dates):
    assert cached_api.request(path, query=dates).status_code == 422
    assert cached_api.cache.calls == []
    assert all(reader.await_count == 0 for reader in cached_api.readers.values())


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/api/access", None),
    ("GET", "/api/admin/users", None),
    ("POST", "/api/auth/login", {"username": "synthetic", "password": "synthetic-password"}),
    ("GET", "/api/keys/1/reveal?type=openai_official&status=valid", None),
    ("POST", "/api/keys/batch-reveal", {"keys": [{"id": 1, "type": "openai_official", "status": "valid"}]}),
    ("GET", "/api/export?provider=OpenAI&status=valid", None),
    ("GET", "/api/valid-key-records/export?provider=OpenAI", None),
    ("POST", "/api/valid-key-records/batch-export", {"provider": "OpenAI", "keys": [{"id": 1, "type": "openai_official"}]}),
])
def test_access_admin_login_reveal_and_plaintext_exports_never_enter_query_cache(monkeypatch, cached_api, method, path, body):
    cached_api.state.user.role = "super_admin"
    monkeypatch.setattr(api, "list_users", AsyncMock(return_value=[]))
    monkeypatch.setattr(api, "login", AsyncMock(return_value=(cached_api.state.user, "synthetic-session-token")))
    monkeypatch.setattr(api, "reveal_credential", AsyncMock(return_value="synthetic-private-key"))
    monkeypatch.setattr(api, "reveal_credentials", AsyncMock(return_value=["synthetic-private-key"]))
    monkeypatch.setattr(api, "export_selected_valid_keys", AsyncMock(return_value=["synthetic-private-key"]))
    mock_query_export(monkeypatch, ["synthetic-private-key"])
    monkeypatch.setattr(api, "record_export", AsyncMock())
    for _ in range(2):
        response = cached_api.request(path, method=method, body=body)
        assert response.status_code == 200
        assert "X-Data-Cache" not in response.headers
    assert cached_api.cache.calls == []
    assert cached_api.cache.entries == {}
