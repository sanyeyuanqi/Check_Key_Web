"""Role, session, bootstrap, and IP-block checks using isolated synthetic PostgreSQL."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import asyncpg
from fastapi import HTTPException, Request
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.auth_service as auth
from app.database import AuthIPBlock, AuthSession, AuthUser
from app.ip_access import block_ip, list_ip_blocks, set_ip_block_active
from test_platform_partition_migration import connect, insert_credential, isolated_database, migrate, run, snapshot


def session_factory(database):
    engine = create_async_engine(database.set(drivername="postgresql+asyncpg"))
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def test_role_lifecycle_scopes_accounts_and_revokes_disabled_or_reset_sessions(isolated_database, monkeypatch):
    database = isolated_database
    migrate(database)
    generated = MagicMock(side_effect=[
        "synthetic-created-admin-secret", "synthetic-created-member-secret", "synthetic-reset-admin-secret",
    ])
    monkeypatch.setattr(auth, "generated_password", generated)

    async def verify():
        engine, sessions = session_factory(database)
        try:
            async with sessions() as session:
                existing_hash = auth.hash_password("synthetic-existing-account-secret")
                root, admin, member = [AuthUser(
                    username=f"synthetic-{role}", username_normalized=f"synthetic-{role}",
                    password_hash=existing_hash, role=role, active=True,
                ) for role in ("super_admin", "admin", "user")]
                session.add_all([root, admin, member])
                await session.commit()

                created_admin = await auth.create_user(session, root, "synthetic-created-admin", "admin")
                created_member = await auth.create_user(session, admin, "synthetic-created-member", "user")
                admin_rows = await auth.list_users(session, admin)
                assert {row["id"] for row in admin_rows} == {admin.id, member.id, created_member["id"]}
                assert len(await auth.list_users(session, root)) == 5
                with pytest.raises(HTTPException) as error:
                    await auth.list_users(session, member)
                assert error.value.status_code == 403
                with pytest.raises(HTTPException) as error:
                    await auth.create_user(session, admin, "synthetic-forbidden-admin", "admin")
                assert error.value.status_code == 403

                redis = SimpleNamespace(incr=AsyncMock(return_value=1), expire=AsyncMock())
                request = Request({"type": "http", "client": ("203.0.113.7", 123), "headers": []})
                _, admin_token = await auth.login(
                    session, redis, request, created_admin["username"], "synthetic-created-admin-secret",
                )
                _, member_token = await auth.login(
                    session, redis, request, created_member["username"], "synthetic-created-member-secret",
                )
                assert await auth.current_user(session, member_token) is not None

                await auth.set_user_active(session, admin, created_member["id"], False)
                assert await auth.current_user(session, member_token) is None
                member_session = await session.scalar(select(AuthSession).where(AuthSession.user_id == created_member["id"]))
                assert member_session.revoked_at is not None
                assert (await auth.current_user(session, admin_token)).id == created_admin["id"]

                await auth.set_user_active(session, root, created_member["id"], True)
                assert await auth.current_user(session, member_token) is None
                assert (await session.get(AuthUser, created_member["id"])).active is True

                with pytest.raises(HTTPException) as error:
                    await auth.reset_password(session, admin, created_admin["id"])
                assert error.value.status_code == 403
                await auth.reset_password(session, root, created_admin["id"])
                assert await auth.current_user(session, admin_token) is None
                updated_admin = await session.get(AuthUser, created_admin["id"])
                assert auth.verify_password(updated_admin.password_hash, "synthetic-reset-admin-secret")
                assert not auth.verify_password(updated_admin.password_hash, "synthetic-created-admin-secret")
                assert updated_admin.role == "admin"

                for actor in (root, admin):
                    with pytest.raises(HTTPException) as error:
                        await auth.set_user_active(session, actor, actor.id, False)
                    assert error.value.status_code == 400
                with pytest.raises(HTTPException) as error:
                    await auth.reset_password(session, admin, root.id)
                assert error.value.status_code == 403
                assert root.active is True
                assert generated.call_count == 3
        finally:
            await engine.dispose()

    run(verify())


def test_bootstrap_promotes_only_configured_active_admin_without_changing_credentials(isolated_database, monkeypatch):
    database = isolated_database
    migrate(database)
    configuration = SimpleNamespace(
        bootstrap_admin_username="  SyntheticRoot  ",
        bootstrap_admin_password="synthetic-bootstrap-secret",
    )
    monkeypatch.setattr(auth, "settings", configuration)

    async def verify():
        engine, sessions = session_factory(database)
        try:
            async with sessions() as session:
                await auth.ensure_bootstrap_admin(session)
                root = await session.scalar(select(AuthUser).where(AuthUser.username_normalized == "syntheticroot"))
                assert root.role == "super_admin"
                assert auth.verify_password(root.password_hash, "synthetic-bootstrap-secret")
                original_hash = root.password_hash

                root.role = "admin"
                root.failed_login_count = 4
                root.locked_until = datetime.now(UTC) + timedelta(minutes=15)
                original_lock = root.locked_until
                other = AuthUser(
                    username="synthetic-other-admin", username_normalized="synthetic-other-admin",
                    password_hash=original_hash, role="admin", active=True,
                )
                session.add(other)
                await session.commit()
                configuration.bootstrap_admin_password = "synthetic-must-not-replace-existing-secret"
                await auth.ensure_bootstrap_admin(session)
                assert root.role == "super_admin"
                assert other.role == "admin"
                assert root.password_hash == original_hash
                assert root.failed_login_count == 4
                assert root.locked_until == original_lock

                configuration.bootstrap_admin_username = other.username
                await auth.ensure_bootstrap_admin(session)
                assert other.role == "admin"

                configuration.bootstrap_admin_username = root.username
                root.role, root.active = "admin", False
                await session.commit()
                await auth.ensure_bootstrap_admin(session)
                assert (root.role, root.active, other.role) == ("admin", False, "admin")
                root.role, root.active = "user", True
                await session.commit()
                await auth.ensure_bootstrap_admin(session)
                assert root.role == "user"
                assert other.role == "admin"
                configuration.bootstrap_admin_username = "synthetic-missing-admin"
                await auth.ensure_bootstrap_admin(session)
                assert await session.scalar(select(func.count()).select_from(AuthUser)) == 2
                assert await session.scalar(select(func.count()).select_from(AuthUser).where(AuthUser.role == "super_admin")) == 0
                assert root.password_hash == original_hash
        finally:
            await engine.dispose()

    run(verify())


def test_ip_block_migration_preserves_business_rows_and_real_postgres_upsert_returns_fresh_values(isolated_database):
    database = isolated_database
    migrate(database, "20261017_0013")

    async def seed():
        connection = await connect(database)
        try:
            await connection.execute("""
                INSERT INTO auth_users (id,username,username_normalized,password_hash,role,active,failed_login_count)
                VALUES (1,'synthetic-ip-manager','synthetic-ip-manager','synthetic-preserved-hash','admin',true,0)
            """)
            await insert_credential(connection, 1, "OpenAI", "openai_official")
            return dict(await connection.fetchrow("SELECT * FROM auth_users WHERE id=1"))
        finally:
            await connection.close()

    user_before = run(seed())
    before = run(snapshot(database))
    migrate(database)
    assert run(snapshot(database)) == before

    async def verify():
        connection = await connect(database)
        try:
            assert dict(await connection.fetchrow("SELECT * FROM auth_users WHERE id=1")) == user_before
            defaults = await connection.fetchrow("""
                INSERT INTO auth_ip_blocks (ip_address,created_by) VALUES ('2001:db8::1',1)
                RETURNING active,reason,created_at,updated_at
            """)
            assert defaults["active"] is True and defaults["reason"] == ""
            assert defaults["created_at"].tzinfo is not None and defaults["updated_at"].tzinfo is not None
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await connection.execute("INSERT INTO auth_ip_blocks (ip_address,created_by) VALUES ('192.0.2.9',999)")
        finally:
            await connection.close()

        engine, sessions = session_factory(database)
        try:
            async with sessions() as session:
                actor = await session.get(AuthUser, 1)
                actor.role = "super_admin"
                await session.commit()
                first = await block_ip(session, actor, "::ffff:203.0.113.7", " initial reason ", "192.0.2.1")
                assert (first["ipAddress"], first["active"], first["createdBy"]) == (
                    "203.0.113.7", True, actor.username,
                )
                assert datetime.fromisoformat(first["createdAt"]).tzinfo is not None
                held = await session.get(AuthIPBlock, "203.0.113.7")
                disabled = await set_ip_block_active(session, actor, "203.0.113.7", False, "192.0.2.1")
                assert disabled["active"] is False and held.active is False
                again = await block_ip(session, actor, "203.0.113.7", " revised reason ", "192.0.2.1")
                assert again["active"] is True and held.active is True
                assert again["reason"] == held.reason == "revised reason"
                assert again["createdAt"] == first["createdAt"]
                assert datetime.fromisoformat(again["updatedAt"]) >= datetime.fromisoformat(first["updatedAt"])
                assert await session.scalar(select(func.count()).select_from(AuthIPBlock).where(AuthIPBlock.ip_address == "203.0.113.7")) == 1
                rows = await list_ip_blocks(session)
                assert {row["createdBy"] for row in rows} == {actor.username}

                await session.delete(actor)
                await session.commit()
                await session.refresh(held)
                assert held.created_by is None
                assert all(row["createdBy"] is None for row in await list_ip_blocks(session))
        finally:
            await engine.dispose()

    run(verify())
    assert run(snapshot(database)) == before
    migrate(database, "20261017_0013", downgrade=True)
    assert run(snapshot(database)) == before
    migrate(database)
    assert run(snapshot(database)) == before
