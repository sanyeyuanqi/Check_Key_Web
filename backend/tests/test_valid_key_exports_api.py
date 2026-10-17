"""Synthetic API checks for plaintext archive downloads; no database or Redis IO."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import HTTPException
import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.main as api
from app.database import AuthIPBlock, AuthUser
from test_export_records import sql


BATCH_PATH = "/api/valid-key-records/batch-export"
ALL_PATH = "/api/valid-key-records/export"
SELECTION = {"provider": "OpenAI", "keys": [{"id": 7, "type": "openai_official"}]}
VALUES = ["synthetic-full-key-first", "synthetic-account-id|synthetic-full-token-second"]


@pytest.fixture
def archive_api(monkeypatch):
    state = SimpleNamespace(
        user=AuthUser(id=17, username="synthetic-archive-user", role="user", active=True),
        blocked=False, auth_checks=0, values=list(VALUES),
    )
    session = MagicMock(spec=AsyncSession)

    async def get_row(model, identity):
        if model is AuthIPBlock:
            return AuthIPBlock(ip_address=identity, active=True) if state.blocked else None
        if model is AuthUser and state.user and identity == state.user.id:
            return state.user
        return None

    session.get.side_effect = get_row

    async def current_user(*args):
        state.auth_checks += 1
        return state.user if state.user is not None and state.user.active else None

    monkeypatch.setattr(api, "current_user", current_user)
    monkeypatch.setattr(api.settings, "frontend_origins", "http://localhost:3000")
    selected = AsyncMock(return_value=list(VALUES))
    async def write_all(*args):
        return await api.write_selected_export(state.values)

    all_keys = AsyncMock(side_effect=write_all)
    logger = AsyncMock()
    cache = AsyncMock(side_effect=AssertionError("Archive plaintext must not enter query caches"))
    monkeypatch.setattr(api, "export_selected_valid_keys", selected, raising=False)
    monkeypatch.setattr(api, "write_query_export", all_keys)
    monkeypatch.setattr(api, "record_export", logger)
    monkeypatch.setattr(api, "cached_query", cache)

    async def fake_session():
        yield session

    previous = api.app.dependency_overrides.copy()
    api.app.dependency_overrides[api.get_session] = fake_session

    def request(mode, *, body=None, query=None, headers=None, client=("203.0.113.7", 123)):
        async def send():
            transport = httpx.ASGITransport(app=api.app, raise_app_exceptions=False, client=client)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
                if mode == "selected":
                    return await http_client.post(BATCH_PATH, json=SELECTION if body is None else body, headers=headers)
                return await http_client.get(ALL_PATH, params={"provider": "OpenAI"} if query is None else query, headers=headers)
        return asyncio.run(send())

    yield SimpleNamespace(
        request=request, session=session, state=state, selected=selected,
        all_keys=all_keys, logger=logger, cache=cache,
    )
    api.app.dependency_overrides.clear()
    api.app.dependency_overrides.update(previous)


def assert_download(response, values):
    assert response.status_code == 200
    assert response.text == "\n".join(values)
    assert response.headers["Content-Type"].startswith("text/plain")
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Content-Disposition"].startswith('attachment; filename="')
    assert response.headers["Content-Disposition"].endswith('.txt"')
    assert "X-Data-Cache" not in response.headers


def assert_archive_query(reader, session, *, storage_type=None, query=None, ip=None):
    reader.assert_awaited_once()
    assert reader.await_args.args[0] is session
    selected = reader.await_args.args[1]
    statement = sql(selected)
    assert statement.startswith("SELECT credentials.encrypted_secret")
    assert "JOIN valid_key_records" in statement
    assert "credentials.provider = 'OpenAI'" in statement
    assert "ORDER BY valid_key_records.last_valid_at DESC, valid_key_records.credential_id DESC" in statement
    assert "LIMIT" not in statement and "OFFSET" not in statement
    assert "credentials.status" not in statement and "result_bucket" not in statement
    if storage_type:
        assert f"credentials.storage_type = '{storage_type}'" in statement
    if ip:
        assert "credentials.source_ip" in statement
        assert "credentials.key_prefix" not in statement
    elif query:
        assert "credentials.key_prefix LIKE" in statement
        assert f"{query}%" in selected.compile().params.values()
        assert "credentials.source_ip" not in statement
    else:
        assert "credentials.key_prefix" not in statement and "credentials.source_ip" not in statement


@pytest.mark.parametrize("mode,status", [("selected", "archive_selected"), ("all", "archive_all")])
@pytest.mark.parametrize("role", ["user", "admin", "super_admin"])
def test_archive_download_returns_full_secrets_once_and_logs_authenticated_actor(archive_api, mode, status, role):
    archive_api.state.user.role = role
    response = archive_api.request(mode)
    assert_download(response, VALUES)
    reader = archive_api.selected if mode == "selected" else archive_api.all_keys
    reader.assert_awaited_once()
    assert reader.await_args.args[0] is archive_api.session
    if mode == "selected":
        assert reader.await_args.kwargs["provider"] == "OpenAI"
        assert [item.model_dump() for item in reader.await_args.kwargs["keys"]] == SELECTION["keys"]
    else:
        assert_archive_query(reader, archive_api.session)
    archive_api.logger.assert_awaited_once()
    audit = archive_api.logger.await_args
    assert audit.args[0] is archive_api.session
    assert audit.kwargs["user"] is archive_api.state.user
    assert audit.kwargs["provider"] == "OpenAI"
    assert audit.kwargs["storage_type"] is None
    assert audit.kwargs["status"] == status
    assert audit.kwargs["status_mode"] == "archive"
    assert audit.kwargs["count"] == len(VALUES)
    assert audit.kwargs["ip_address"] == "203.0.113.7"
    assert response.headers["Content-Disposition"] == f'attachment; filename="{audit.kwargs["file_name"]}"'
    assert audit.kwargs["file_name"] == f"openai-valid-records-{'selected' if mode == 'selected' else 'all'}.txt"
    archive_api.cache.assert_not_awaited()


def test_selected_mixed_types_log_provider_scope_and_preserve_composite_secret(archive_api):
    selection = {"provider": "Azure", "keys": [{"id": 7, "type": "azure_gpt"}, {"id": 9, "type": "azure_claude"}]}
    response = archive_api.request("selected", body=selection)
    assert_download(response, VALUES)
    assert [item.model_dump() for item in archive_api.selected.await_args.kwargs["keys"]] == selection["keys"]
    assert archive_api.logger.await_args.kwargs["provider"] == "Azure"
    assert archive_api.logger.await_args.kwargs["storage_type"] is None
    archive_api.cache.assert_not_awaited()


@pytest.mark.parametrize("size", [1, 100])
def test_batch_selection_accepts_both_size_boundaries(archive_api, size):
    keys = [{"id": identifier, "type": "openai_official"} for identifier in range(1, size + 1)]
    assert archive_api.request("selected", body={"provider": "OpenAI", "keys": keys}).status_code == 200
    assert len(archive_api.selected.await_args.kwargs["keys"]) == size


def test_all_export_crosses_page_size_and_ignores_page_query_parameter(archive_api):
    values = [f"synthetic-complete-key-{identifier}" for identifier in range(205)]
    archive_api.state.values = values
    response = archive_api.request("all", query={"provider": "OpenAI", "page": 99})
    assert_download(response, values)
    assert_archive_query(archive_api.all_keys, archive_api.session)
    assert archive_api.logger.await_args.kwargs["count"] == 205
    archive_api.cache.assert_not_awaited()


@pytest.mark.parametrize("filters", [
    {"type": "openai_official"}, {"q": "synthetic-prefix"}, {"ip": "172.20"},
    {"q": "synthetic-prefix", "ip": "2001:DB8"},
])
def test_all_export_forwards_current_platform_type_and_independent_search_inputs(archive_api, filters):
    response = archive_api.request("all", query={"provider": "OpenAI", **filters})
    assert_download(response, VALUES)
    assert_archive_query(archive_api.all_keys, archive_api.session,
        storage_type=filters.get("type"), query=filters.get("q"), ip=filters.get("ip"))
    assert archive_api.logger.await_args.kwargs["storage_type"] == filters.get("type")


def test_empty_all_export_is_a_single_empty_download_and_zero_count_audit(archive_api):
    archive_api.state.values = []
    response = archive_api.request("all")
    assert_download(response, [])
    archive_api.logger.assert_awaited_once()
    assert archive_api.logger.await_args.kwargs["count"] == 0


@pytest.mark.parametrize("mode", ["selected", "all"])
@pytest.mark.parametrize("peer,expected", [
    (("203.0.113.7", 123), "203.0.113.7"),
    (("2001:0db8:0:0:0:0:0:7", 123), "2001:db8::7"),
    (("::ffff:203.0.113.7", 123), "203.0.113.7"),
    (None, None),
])
def test_audit_ip_comes_from_peer_and_never_forged_forwarding_headers(archive_api, mode, peer, expected):
    response = archive_api.request(mode, client=peer, headers={
        "X-Forwarded-For": "198.51.100.99", "Forwarded": "for=198.51.100.99",
    })
    assert_download(response, VALUES)
    assert archive_api.logger.await_args.kwargs["ip_address"] == expected


@pytest.mark.parametrize("mode", ["selected", "all"])
@pytest.mark.parametrize("reason", ["missing", "disabled"])
def test_fresh_authentication_is_required_even_after_successful_download(archive_api, mode, reason):
    assert archive_api.request(mode).status_code == 200
    if reason == "missing":
        archive_api.state.user = None
    else:
        archive_api.state.user.active = False
    rejected = archive_api.request(mode)
    assert rejected.status_code == 401
    assert "Content-Disposition" not in rejected.headers
    assert all(value not in rejected.text for value in VALUES)
    assert archive_api.state.auth_checks == 2
    assert (archive_api.selected if mode == "selected" else archive_api.all_keys).await_count == 1
    assert archive_api.logger.await_count == 1
    archive_api.cache.assert_not_awaited()


@pytest.mark.parametrize("mode", ["selected", "all"])
def test_current_ip_block_prevents_export_and_audit_even_with_forged_forwarded_ip(archive_api, mode):
    assert archive_api.request(mode).status_code == 200
    archive_api.state.blocked = True
    response = archive_api.request(mode, headers={"X-Forwarded-For": "198.51.100.99"})
    assert response.status_code == 403
    assert response.json()["code"] == "IP_BLOCKED"
    assert (archive_api.selected if mode == "selected" else archive_api.all_keys).await_count == 1
    assert archive_api.logger.await_count == 1
    assert "Content-Disposition" not in response.headers
    archive_api.cache.assert_not_awaited()


@pytest.mark.parametrize("mode", ["selected", "all"])
@pytest.mark.parametrize("origin", ["https://synthetic-attacker.invalid", "null", "http://localhost:3000.synthetic-attacker.invalid"])
def test_untrusted_origin_is_rejected_before_reading_or_recording(archive_api, mode, origin):
    response = archive_api.request(mode, headers={"Origin": origin})
    assert response.status_code == 403
    archive_api.selected.assert_not_awaited()
    archive_api.all_keys.assert_not_awaited()
    archive_api.logger.assert_not_awaited()
    archive_api.cache.assert_not_awaited()


@pytest.mark.parametrize("mode", ["selected", "all"])
def test_configured_origin_is_allowed(archive_api, mode):
    assert_download(archive_api.request(mode, headers={"Origin": "http://localhost:3000"}), VALUES)


@pytest.mark.parametrize("body", [
    {}, {"provider": "OpenAI"}, {"provider": "OpenAI", "keys": []},
    {"provider": "OpenAI", "keys": [{"id": 1, "type": "openai_official"}] * 2},
    {"provider": "OpenAI", "keys": [{"id": identifier, "type": "openai_official"} for identifier in range(1, 102)]},
    {**SELECTION, "ip_address": "198.51.100.99"},
    {**SELECTION, "status": "valid"},
    {**SELECTION, "secret": "synthetic-client-supplied-secret"},
    {"provider": "OpenAI", "keys": [{"id": 7, "type": "openai_official", "status": "valid"}]},
    {"provider": "OpenAI", "keys": [{"id": 7, "type": "openai_official", "provider": "OpenAI"}]},
    {"provider": "OpenAI", "keys": [{"id": 7, "type": "openai_official", "secret": "synthetic-client-supplied-secret"}]},
    {"provider": "OpenAI", "keys": [{"id": 7}]},
    {"provider": "OpenAI", "keys": [{"id": 7, "type": ""}]},
    {"provider": "OpenAI", "keys": [{"id": 7, "type": "x" * 65}]},
])
def test_invalid_selection_shape_is_rejected_before_services(archive_api, body):
    assert archive_api.request("selected", body=body).status_code == 422
    archive_api.selected.assert_not_awaited()
    archive_api.logger.assert_not_awaited()


@pytest.mark.parametrize("identifier", [0, -1, True, 1.5, "7", 9_223_372_036_854_775_808])
def test_selection_ids_must_be_strict_positive_database_integers(archive_api, identifier):
    body = {"provider": "OpenAI", "keys": [{"id": identifier, "type": "openai_official"}]}
    assert archive_api.request("selected", body=body).status_code == 422
    archive_api.selected.assert_not_awaited()
    archive_api.logger.assert_not_awaited()


@pytest.mark.parametrize("query", [
    {}, {"provider": "x" * 81}, {"provider": "OpenAI", "type": "x" * 65},
    {"provider": "OpenAI", "ip": "x" * 81},
])
def test_all_export_requires_bounded_platform_and_filters(archive_api, query):
    assert archive_api.request("all", query=query).status_code == 422
    archive_api.all_keys.assert_not_awaited()
    archive_api.logger.assert_not_awaited()


@pytest.mark.parametrize("mode,options", [
    ("selected", {"body": {"provider": "synthetic-unknown-platform", "keys": SELECTION["keys"]}}),
    ("selected", {"body": {"provider": "OpenAI", "keys": [{"id": 7, "type": "anthropic_official"}]}}),
    ("selected", {"body": {"provider": "OpenAI", "keys": [{"id": 7, "type": "synthetic-unknown-type"}]}}),
    ("all", {"query": {"provider": "synthetic-unknown-platform"}}),
    ("all", {"query": {"provider": "OpenAI", "type": "anthropic_official"}}),
    ("all", {"query": {"provider": "OpenAI", "type": "synthetic-unknown-type"}}),
])
def test_invalid_platform_scope_service_rejection_is_not_logged_as_a_success(archive_api, mode, options):
    reader = archive_api.selected if mode == "selected" else archive_api.all_keys
    reader.side_effect = HTTPException(422, "平台或密钥类型无效")
    response = archive_api.request(mode, **options)
    assert response.status_code == 422
    assert "Content-Disposition" not in response.headers
    assert all(value not in response.text for value in VALUES)
    archive_api.logger.assert_not_awaited()
    archive_api.cache.assert_not_awaited()


@pytest.mark.parametrize("mode", ["selected", "all"])
@pytest.mark.parametrize("error", [
    HTTPException(422, "平台或密钥类型无效"),
    HTTPException(404, "所选有效密钥记录不存在"),
    ValueError("synthetic decrypt failure"),
])
def test_scope_lookup_or_decryption_failure_never_logs_or_returns_partial_secrets(archive_api, mode, error):
    reader = archive_api.selected if mode == "selected" else archive_api.all_keys
    reader.side_effect = error
    response = archive_api.request(mode)
    assert response.status_code == (error.status_code if isinstance(error, HTTPException) else 500)
    assert "Content-Disposition" not in response.headers
    assert all(value not in response.text for value in VALUES)
    archive_api.logger.assert_not_awaited()
    archive_api.cache.assert_not_awaited()


@pytest.mark.parametrize("mode", ["selected", "all"])
def test_audit_commit_failure_cannot_return_plaintext_or_download_success(archive_api, mode):
    archive_api.logger.side_effect = RuntimeError("synthetic audit commit failure")
    response = archive_api.request(mode)
    assert response.status_code == 500
    assert "Content-Disposition" not in response.headers
    assert all(value not in response.text for value in VALUES)
    archive_api.logger.assert_awaited_once()
    archive_api.cache.assert_not_awaited()
