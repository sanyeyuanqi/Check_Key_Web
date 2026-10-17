import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

import app.main as api
from app.database import AuthIPBlock, AuthUser
from app.ip_access import block_ip, list_ip_blocks, normalize_block_ip, set_ip_block_active


@pytest.mark.parametrize("value,expected", [
    ("203.0.113.7", "203.0.113.7"),
    (" 172.20.0.1 ", "172.20.0.1"),
    ("2001:0DB8:0000:0000:0000:0000:0000:0001", "2001:db8::1"),
    ("::FFFF:203.0.113.7", "203.0.113.7"),
    ("::1", "::1"),
])
def test_block_ip_normalization_accepts_one_address_and_collapses_aliases(value, expected):
    assert normalize_block_ip(value) == expected


@pytest.mark.parametrize("value", [
    "", " ", "example.com", "203.0.113.7/32", "2001:db8::/64", "10.0.0.0/8",
    "fe80::1%eth0", "::ffff:203.0.113.7%zone", "999.1.1.1", "01.2.3.4",
    "203.0.113.7,198.51.100.1", "[::1]:443", "203.0.113.7:443", "https://203.0.113.7",
])
def test_block_ip_normalization_rejects_networks_scopes_hostnames_ports_and_multiple_addresses(value):
    with pytest.raises(HTTPException) as error:
        normalize_block_ip(value)
    assert error.value.status_code == 422


@pytest.fixture
def block_database():
    engine = create_engine("sqlite://")
    AuthUser.__table__.create(engine)
    AuthIPBlock.__table__.create(engine)
    with Session(engine, expire_on_commit=False) as db:
        admin = AuthUser(id=17, username="synthetic-super", username_normalized="synthetic-super", password_hash="synthetic-only", role="super_admin")
        db.add(admin)
        db.commit()
        bridge = SimpleNamespace(
            scalars=AsyncMock(side_effect=db.scalars), scalar=AsyncMock(side_effect=db.scalar),
            execute=AsyncMock(side_effect=db.execute), get=AsyncMock(side_effect=db.get),
            commit=AsyncMock(side_effect=db.commit), sync_session=db,
        )
        yield bridge, admin
    engine.dispose()


def test_block_upsert_reuses_normalized_ip_and_patch_controls_active_state(block_database):
    session, admin = block_database
    first = asyncio.run(block_ip(session, admin, "::FFFF:203.0.113.7", " first reason ", "192.0.2.1"))
    assert first["ipAddress"] == "203.0.113.7"
    assert first["active"] is True
    assert first["reason"] == "first reason"
    assert first["createdBy"] == admin.username
    assert datetime.fromisoformat(first["createdAt"])
    assert datetime.fromisoformat(first["updatedAt"])
    disabled = asyncio.run(set_ip_block_active(session, admin, "203.0.113.7", False, "192.0.2.1"))
    assert disabled["active"] is False
    again = asyncio.run(block_ip(session, admin, "203.0.113.7", "updated reason", "192.0.2.1"))
    assert again["active"] is True
    assert again["reason"] == "updated reason"
    assert again["createdAt"] == first["createdAt"]
    assert session.sync_session.scalar(select(func.count()).select_from(AuthIPBlock)) == 1
    assert session.commit.await_count == 3


def test_list_blocks_includes_missing_creator_and_orders_active_records_first(block_database):
    session, admin = block_database
    now = datetime(2026, 10, 17, tzinfo=timezone.utc)
    session.sync_session.add_all([
        AuthIPBlock(ip_address="203.0.113.1", active=True, reason="active", created_by=admin.id, created_at=now, updated_at=now),
        AuthIPBlock(ip_address="2001:db8::1", active=False, reason="inactive", created_by=None, created_at=now, updated_at=now),
    ])
    session.sync_session.commit()
    result = asyncio.run(list_ip_blocks(session))
    assert [row["ipAddress"] for row in result] == ["203.0.113.1", "2001:db8::1"]
    assert [row["createdBy"] for row in result] == [admin.username, None]
    assert [row["active"] for row in result] == [True, False]
    assert set(result[0]) == {"ipAddress", "active", "reason", "createdAt", "updatedAt", "createdBy"}


@pytest.mark.parametrize("role", ["admin", "user"])
@pytest.mark.parametrize("operation", ["block", "patch"])
def test_non_super_admin_cannot_mutate_blocks_even_via_service(role, operation):
    session = MagicMock(spec=AsyncSession)
    admin = AuthUser(id=17, username="synthetic", role=role)
    with pytest.raises(HTTPException) as error:
        if operation == "block":
            asyncio.run(block_ip(session, admin, "203.0.113.7", "", "192.0.2.1"))
        else:
            asyncio.run(set_ip_block_active(session, admin, "203.0.113.7", False, "192.0.2.1"))
    assert error.value.status_code == 403
    session.commit.assert_not_awaited()
    session.scalars.assert_not_awaited()
    session.get.assert_not_awaited()


@pytest.mark.parametrize("target,current", [
    ("203.0.113.7", "203.0.113.7"),
    ("::ffff:203.0.113.7", "203.0.113.7"),
    ("2001:0db8:0:0:0:0:0:1", "2001:db8::1"),
])
@pytest.mark.parametrize("operation", ["block", "enable"])
def test_current_request_ip_cannot_be_blocked_through_equivalent_address_forms(target, current, operation):
    session = MagicMock(spec=AsyncSession)
    admin = AuthUser(id=17, username="synthetic", role="super_admin")
    with pytest.raises(HTTPException) as error:
        if operation == "block":
            asyncio.run(block_ip(session, admin, target, "", current))
        else:
            asyncio.run(set_ip_block_active(session, admin, target, True, current))
    assert error.value.status_code == 400
    session.commit.assert_not_awaited()
    session.scalars.assert_not_awaited()
    session.get.assert_not_awaited()


def test_patch_missing_ip_returns_404_and_does_not_commit(block_database):
    session, admin = block_database
    with pytest.raises(HTTPException) as error:
        asyncio.run(set_ip_block_active(session, admin, "203.0.113.7", False, "192.0.2.1"))
    assert error.value.status_code == 404
    session.commit.assert_not_awaited()


def test_oversized_reason_is_rejected_before_mutation(block_database):
    session, admin = block_database
    with pytest.raises(HTTPException) as error:
        asyncio.run(block_ip(session, admin, "203.0.113.7", "x" * 201, "192.0.2.1"))
    assert error.value.status_code == 422
    session.scalars.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.fixture
def ip_api(monkeypatch):
    session = MagicMock(spec=AsyncSession)
    session.get.return_value = None
    user = AuthUser(id=17, username="synthetic-super", role="super_admin")
    session.get.side_effect = lambda model, identity: (user if identity == user.id else None) if model is AuthUser else session.get.return_value
    auth_lookup = AsyncMock(return_value=user)
    monkeypatch.setattr(api, "current_user", auth_lookup)
    redis = SimpleNamespace(ping=AsyncMock())

    async def fake_session():
        yield session

    async def fake_redis():
        yield redis

    old_overrides = api.app.dependency_overrides.copy()
    api.app.dependency_overrides[api.get_session] = fake_session
    api.app.dependency_overrides[api.redis_dependency] = fake_redis

    def request(method, path, *, peer="203.0.113.7", headers=None, body=None):
        async def send():
            transport = httpx.ASGITransport(app=api.app, raise_app_exceptions=False, client=(peer, 123))
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.request(method, path, headers=headers, json=body)

        return asyncio.run(send())

    yield SimpleNamespace(request=request, session=session, user=user, auth=auth_lookup, redis=redis)
    api.app.dependency_overrides.clear()
    api.app.dependency_overrides.update(old_overrides)


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/api/access", None),
    ("POST", "/api/auth/login", {"username": "synthetic", "password": "synthetic-password"}),
    ("GET", "/api/auth/me", None),
    ("GET", "/api/device/ping", None),
    ("POST", "/api/device/batches/start", {"provider_label": "OpenAI", "total": 1}),
    ("GET", "/api/keys", None),
    ("GET", "/api/export?status=valid", None),
    ("GET", "/api/export-records", None),
    ("GET", "/api/valid-key-records", None),
    ("GET", "/api/admin/ip-blocks", None),
])
def test_enabled_block_rejects_public_login_device_and_authenticated_api_before_route_actions(monkeypatch, ip_api, method, path, body):
    ip_api.session.get.return_value = AuthIPBlock(ip_address="203.0.113.7", active=True)
    login = AsyncMock()
    start = AsyncMock()
    monkeypatch.setattr(api, "login", login)
    monkeypatch.setattr(api, "start_batch", start)
    response = ip_api.request(method, path, body=body)
    assert response.status_code == 403
    assert response.json() == {"error": "此 IP 地址已被禁止访问", "code": "IP_BLOCKED"}
    ip_api.session.get.assert_awaited_once_with(AuthIPBlock, "203.0.113.7")
    ip_api.auth.assert_not_awaited()
    login.assert_not_awaited()
    start.assert_not_awaited()


def test_forged_forwarding_headers_cannot_bypass_a_block_or_block_an_unrelated_peer(ip_api):
    blocked = AuthIPBlock(ip_address="203.0.113.7", active=True)
    ip_api.session.get.side_effect = lambda model, address: blocked if address == "203.0.113.7" else None
    denied = ip_api.request("GET", "/api/access", headers={"X-Forwarded-For": "198.51.100.1", "Forwarded": "for=198.51.100.1"})
    assert denied.status_code == 403
    allowed = ip_api.request("GET", "/api/access", peer="198.51.100.1", headers={"X-Forwarded-For": "203.0.113.7", "Forwarded": "for=203.0.113.7"})
    assert allowed.status_code == 200
    assert allowed.json() == {"allowed": True}
    assert [call.args[1] for call in ip_api.session.get.await_args_list] == ["203.0.113.7", "198.51.100.1"]


@pytest.mark.parametrize("peer,canonical", [
    ("::FFFF:203.0.113.7", "203.0.113.7"),
    ("2001:0DB8:0000:0000:0000:0000:0000:0001", "2001:db8::1"),
])
def test_global_guard_looks_up_canonical_peer_and_denies_equivalent_forms(ip_api, peer, canonical):
    ip_api.session.get.side_effect = lambda model, address: AuthIPBlock(ip_address=address, active=True) if address == canonical else None
    response = ip_api.request("GET", "/api/access", peer=peer)
    assert response.status_code == 403
    assert response.json()["code"] == "IP_BLOCKED"
    ip_api.session.get.assert_awaited_once_with(AuthIPBlock, canonical)


def test_inactive_block_allows_public_access_without_login(ip_api):
    ip_api.session.get.return_value = AuthIPBlock(ip_address="203.0.113.7", active=False)
    ip_api.auth.return_value = None
    response = ip_api.request("GET", "/api/access")
    assert response.status_code == 200
    assert response.json() == {"allowed": True}
    ip_api.auth.assert_not_awaited()


def test_health_and_cors_preflight_skip_ip_block_lookup(ip_api):
    ip_api.session.get.side_effect = AssertionError("Health and OPTIONS must skip the block lookup")
    assert ip_api.request("GET", "/health").status_code == 200
    response = ip_api.request("OPTIONS", "/api/access", headers={
        "Origin": api.settings.allowed_origins[0], "Access-Control-Request-Method": "GET",
    })
    assert response.status_code == 200
    ip_api.session.get.assert_not_awaited()


@pytest.mark.parametrize("role", ["admin", "user"])
@pytest.mark.parametrize("method,body", [("GET", None), ("POST", {"ip": "198.51.100.1"}), ("PATCH", {"ip": "198.51.100.1", "active": False})])
def test_management_routes_require_super_admin(monkeypatch, ip_api, role, method, body):
    ip_api.user.role = role
    list_blocks, create, update = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(api, "list_ip_blocks", list_blocks)
    monkeypatch.setattr(api, "block_ip", create)
    monkeypatch.setattr(api, "set_ip_block_active", update)
    assert ip_api.request(method, "/api/admin/ip-blocks", body=body).status_code == 403
    list_blocks.assert_not_awaited()
    create.assert_not_awaited()
    update.assert_not_awaited()


def test_management_routes_require_login_and_accept_super_admin_with_peer_ip(monkeypatch, ip_api):
    ip_api.auth.return_value = None
    assert ip_api.request("GET", "/api/admin/ip-blocks").status_code == 401
    ip_api.auth.return_value = ip_api.user
    create = AsyncMock(return_value={"ipAddress": "198.51.100.1", "active": True})
    monkeypatch.setattr(api, "block_ip", create)
    response = ip_api.request("POST", "/api/admin/ip-blocks", body={"ip": "198.51.100.1", "reason": "synthetic"}, headers={"X-Forwarded-For": "198.51.100.1"})
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    create.assert_awaited_once_with(ip_api.session, ip_api.user, "198.51.100.1", "synthetic", "203.0.113.7")
