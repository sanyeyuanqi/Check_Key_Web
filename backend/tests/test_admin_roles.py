import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import HTTPException, Request
import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession

import app.auth_service as auth
import app.export_service as exports
from app.database import AuthIPBlock, AuthUser


NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)


def account(role="admin", identifier=1):
    return AuthUser(
        id=identifier, username=f"synthetic-{role}-{identifier}",
        username_normalized=f"synthetic-{role}-{identifier}",
        password_hash="synthetic-preserved-hash", role=role, active=True,
        failed_login_count=3, locked_until=NOW + timedelta(minutes=15),
        created_at=NOW, last_login_at=None,
    )


@pytest.fixture
def session():
    return MagicMock(spec=AsyncSession)


@pytest.fixture
def generated(monkeypatch):
    password = MagicMock(return_value="synthetic-generated-password")
    hasher = MagicMock(side_effect=lambda _: "synthetic-new-hash")
    monkeypatch.setattr(auth, "generated_password", password)
    monkeypatch.setattr(auth, "hash_password", hasher)
    monkeypatch.setattr(auth, "utcnow", lambda: NOW)
    return password, hasher


def sql(statement):
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


@pytest.mark.parametrize("actor_role,new_role,status", [
    ("super_admin", "admin", None), ("super_admin", "user", None),
    ("super_admin", "super_admin", 400), ("super_admin", "unknown", 400),
    ("admin", "user", None), ("admin", "admin", 403),
    ("admin", "super_admin", 400),
    ("user", "user", 403), ("user", "admin", 403), ("user", "super_admin", 403),
])
def test_creation_enforces_actor_and_target_roles_before_generating_password(
    session, generated, actor_role, new_role, status,
):
    password, hasher = generated
    actor = account(actor_role)
    session.refresh.side_effect = lambda user: setattr(user, "id", 42)
    operation = auth.create_user(session, actor, "synthetic-new-account", new_role)
    if status:
        with pytest.raises(HTTPException) as error:
            asyncio.run(operation)
        assert error.value.status_code == status
        password.assert_not_called()
        hasher.assert_not_called()
        session.add.assert_not_called()
        session.commit.assert_not_awaited()
    else:
        result = asyncio.run(operation)
        created = session.add.call_args.args[0]
        assert (created.role, created.created_by, created.active) == (new_role, actor.id, True)
        assert (result["id"], result["role"]) == (42, new_role)
        assert created.password_hash == "synthetic-new-hash"
        password.assert_called_once()
        session.commit.assert_awaited_once()


def test_creation_collision_rolls_back_without_promoting_existing_accounts(session, generated):
    session.commit.side_effect = IntegrityError("synthetic statement", {}, Exception("synthetic duplicate"))
    with pytest.raises(HTTPException) as error:
        asyncio.run(auth.create_user(session, account("super_admin"), "synthetic-existing", "admin"))
    assert error.value.status_code == 400
    session.rollback.assert_awaited_once()


@pytest.mark.parametrize("actor_role,target_role,own,status", [
    ("admin", "user", False, None), ("admin", "admin", False, 403),
    ("admin", "super_admin", False, 403), ("admin", "admin", True, 400),
    ("super_admin", "user", False, None), ("super_admin", "admin", False, None),
    ("super_admin", "super_admin", False, 403), ("super_admin", "super_admin", True, 400),
    ("super_admin", "unknown", False, 403), ("user", "user", False, 403),
    ("user", "user", True, 403),
])
@pytest.mark.parametrize("action", ["reset", "disable", "enable"])
def test_management_role_matrix_protects_self_and_super_admin_and_revokes_sessions(
    session, generated, actor_role, target_role, own, status, action,
):
    actor = account(actor_role)
    target = account(target_role, actor.id if own else 2)
    session.get.return_value = target
    operation = (
        auth.reset_password(session, actor, target.id) if action == "reset"
        else auth.set_user_active(session, actor, target.id, action == "enable")
    )
    if status:
        with pytest.raises(HTTPException) as error:
            asyncio.run(operation)
        assert error.value.status_code == status
        assert target.password_hash == "synthetic-preserved-hash"
        assert target.active is True
        generated[0].assert_not_called()
        session.execute.assert_not_awaited()
        session.commit.assert_not_awaited()
        if actor_role == "user":
            session.get.assert_not_awaited()
        return

    asyncio.run(operation)
    assert target.failed_login_count == 0
    assert target.locked_until is None
    if action == "reset":
        assert target.password_hash == "synthetic-new-hash"
    else:
        assert target.active is (action == "enable")
    if action in {"reset", "disable"}:
        session.execute.assert_awaited_once()
        query = sql(session.execute.await_args.args[0])
        assert "auth_sessions.user_id = 2" in query
        assert "auth_sessions.revoked_at IS NULL" in query
        assert "revoked_at=" in query
    else:
        session.execute.assert_not_awaited()
    session.commit.assert_awaited_once()


@pytest.mark.parametrize("action", ["reset", "active"])
def test_missing_management_target_fails_without_mutations(session, generated, action):
    session.get.return_value = None
    operation = (
        auth.reset_password(session, account("super_admin"), 999) if action == "reset"
        else auth.set_user_active(session, account("super_admin"), 999, False)
    )
    with pytest.raises(HTTPException) as error:
        asyncio.run(operation)
    assert error.value.status_code == 404
    generated[0].assert_not_called()
    session.execute.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.parametrize("role", ["super_admin", "admin", "user"])
def test_account_listing_is_scoped_by_actor(session, role):
    actor = account(role)
    session.scalars.return_value = SimpleNamespace(all=lambda: [actor])
    if role == "user":
        with pytest.raises(HTTPException) as error:
            asyncio.run(auth.list_users(session, actor))
        assert error.value.status_code == 403
        session.scalars.assert_not_awaited()
        return
    result = asyncio.run(auth.list_users(session, actor))
    assert result[0]["role"] == role
    assert "password_hash" not in result[0]
    query = sql(session.scalars.await_args.args[0])
    if role == "admin":
        assert "auth_users.role = 'user' OR auth_users.id = 1" in query
    else:
        assert "WHERE" not in query


def test_fresh_bootstrap_creates_only_super_admin(session, generated, monkeypatch):
    monkeypatch.setattr(auth, "settings", SimpleNamespace(
        bootstrap_admin_username="  SyntheticRoot  ", bootstrap_admin_password="synthetic-bootstrap-password",
    ))
    session.scalar.return_value = 0
    asyncio.run(auth.ensure_bootstrap_admin(session))
    created = session.add.call_args.args[0]
    assert (created.username, created.username_normalized, created.role, created.active) == (
        "SyntheticRoot", "syntheticroot", "super_admin", True,
    )
    session.commit.assert_awaited_once()


@pytest.mark.parametrize("has_super,matched", [(True, False), (False, False), (False, True)])
def test_existing_bootstrap_only_promotes_matching_active_admin_without_reset(
    session, generated, monkeypatch, has_super, matched,
):
    monkeypatch.setattr(auth, "settings", SimpleNamespace(
        bootstrap_admin_username="  SyntheticRoot  ", bootstrap_admin_password="",
    ))
    target = account("admin")
    session.scalar.side_effect = [2, 9 if has_super else None, target if matched else None]
    asyncio.run(auth.ensure_bootstrap_admin(session))
    generated[0].assert_not_called()
    generated[1].assert_not_called()
    session.add.assert_not_called()
    assert target.password_hash == "synthetic-preserved-hash"
    assert target.failed_login_count == 3
    assert target.locked_until == NOW + timedelta(minutes=15)
    assert target.active is True
    if matched and not has_super:
        assert target.role == "super_admin"
        session.commit.assert_awaited_once()
    else:
        assert target.role == "admin"
        session.commit.assert_not_awaited()
    if not has_super:
        lookup = sql(session.scalar.await_args.args[0])
        assert "auth_users.username_normalized = 'syntheticroot'" in lookup
        assert "auth_users.role = 'admin'" in lookup
        assert "auth_users.active IS true" in lookup
        assert "FOR UPDATE" in lookup


def test_fresh_bootstrap_without_password_does_not_create_an_account(session, generated, monkeypatch):
    monkeypatch.setattr(auth, "settings", SimpleNamespace(
        bootstrap_admin_username="synthetic-root", bootstrap_admin_password="",
    ))
    session.scalar.return_value = 0
    asyncio.run(auth.ensure_bootstrap_admin(session))
    session.add.assert_not_called()
    session.commit.assert_not_awaited()
    generated[1].assert_not_called()


@pytest.mark.parametrize("peer,expected", [
    ("203.0.113.7", "203.0.113.7"), ("::ffff:203.0.113.7", "203.0.113.7"),
    ("invalid-peer", "unknown"), (None, "unknown"),
])
def test_login_rate_limit_uses_trusted_peer_and_ignores_forged_forwarding_headers(session, monkeypatch, peer, expected):
    redis = MagicMock()
    redis.incr = AsyncMock(return_value=1)
    redis.expire = AsyncMock()
    session.scalar.return_value = None
    request = Request({
        "type": "http", "client": (peer, 123) if peer else None,
        "headers": [(b"x-forwarded-for", b"198.51.100.99")],
    })
    with pytest.raises(HTTPException) as error:
        asyncio.run(auth.login(session, redis, request, "synthetic-user", "synthetic-login-attempt"))
    assert error.value.status_code == 401
    expected_hash = hashlib.sha256(expected.encode()).hexdigest()
    assert redis.incr.await_args_list[0].args == (f"login:ip:{expected_hash}",)
    assert session.add.call_args.args[0].ip_hash == expected_hash


@pytest.mark.parametrize("role,all_accounts", [("super_admin", True), ("admin", True), ("user", False)])
def test_super_admin_export_history_scope_matches_admin(session, role, all_accounts):
    session.scalar.return_value = 0
    session.scalars.return_value = SimpleNamespace(all=lambda: [])
    asyncio.run(exports.list_export_records(session, user=account(role), page=1))
    for query in (session.scalar.await_args.args[0], session.scalars.await_args.args[0]):
        rendered = sql(query)
        assert ("export_records.user_id = 1" in rendered) is (not all_accounts)


def test_ip_block_metadata_has_requested_defaults_and_creator_deletion_behavior():
    table = AuthIPBlock.__table__
    assert list(table.primary_key.columns.keys()) == ["ip_address"]
    assert table.c.ip_address.type.length == 45
    assert table.c.reason.type.length == 200
    assert table.c.active.default.arg is True
    assert table.c.reason.default.arg == ""
    creator = next(iter(table.c.created_by.foreign_keys))
    assert (creator.target_fullname, creator.ondelete) == ("auth_users.id", "SET NULL")
    assert table.c.created_at.type.timezone and table.c.updated_at.type.timezone


def test_migration_diagnostics_redact_plain_and_url_encoded_credentials():
    from urllib.parse import quote
    from test_platform_partition_migration import redact_migration_output

    synthetic_password = "synthetic-punctuation-p@ss:%!"
    database = URL.create("postgresql+asyncpg", username="synthetic-user", password=synthetic_password,
                          host="127.0.0.1", database="synthetic-test")
    encoded = quote(synthetic_password, safe="")
    output = redact_migration_output(f"raw={synthetic_password} encoded={encoded} escaped={encoded.replace('%', '%%')}", database)
    assert synthetic_password not in output
    assert encoded not in output
    assert output.count("[REDACTED]") == 3
