import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

import app.credential_service as credentials
import app.main as api
from app.database import AuthUser, Credential
from app.platform_catalog import STORAGE_TYPE_PROVIDERS
from app.schemas import BatchRevealInput, BatchRevealKeyInput
from app.security import encrypt_secret


def selected(identifier=1, storage_type="openai_official", status="valid"):
    return {"id": identifier, "type": storage_type, "status": status}


def parsed(*items):
    return BatchRevealInput.model_validate({"keys": list(items)}).keys


@pytest.fixture
def session():
    return MagicMock(spec=AsyncSession)


@pytest.fixture
def request_batch(monkeypatch, session):
    monkeypatch.setattr(api, "current_user", AsyncMock(return_value=AuthUser(
        id=17, username="synthetic-batch-copy-tester", role="user",
    )))

    async def fake_session():
        yield session

    old_overrides = api.app.dependency_overrides.copy()
    api.app.dependency_overrides[api.get_session] = fake_session

    def request(payload, *, headers=None):
        async def send():
            transport = httpx.ASGITransport(app=api.app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.post("/api/keys/batch-reveal", json=payload, headers=headers)

        return asyncio.run(send())

    yield request
    api.app.dependency_overrides.clear()
    api.app.dependency_overrides.update(old_overrides)


@pytest.fixture
def synthetic_database():
    # Real filtering SQL, isolated entirely from the application's database.
    engine = create_engine("sqlite://")
    Credential.__table__.create(engine)
    with Session(engine) as db:
        def row(identifier, storage_type="openai_official", bucket="valid"):
            return Credential(
                id=identifier, fingerprint=f"synthetic-fingerprint-{identifier}",
                storage_type=storage_type, provider=STORAGE_TYPE_PROVIDERS[storage_type], kind="synthetic",
                encrypted_secret=f"synthetic-cipher-{identifier}", key_prefix=f"synthetic-{identifier}",
                masked="synthetic…test", status=bucket, result_bucket=bucket,
                created_at=datetime(2026, 10, 17, tzinfo=UTC),
                updated_at=datetime(2026, 10, 17, tzinfo=UTC),
            )

        db.add_all([
            row(1), row(2, bucket="usage_limit_reached"),
            row(3, storage_type="anthropic_official", bucket="invalid"),
            row(4, bucket="invalid"), row(5, bucket="insufficient_credit"),
            *[row(identifier) for identifier in range(10, 110)],
        ])
        db.commit()
        yield SimpleNamespace(scalars=AsyncMock(side_effect=db.scalars))
    engine.dispose()


def test_service_queries_once_and_returns_requested_order_across_types_and_buckets(
    monkeypatch, synthetic_database,
):
    decrypt = MagicMock(side_effect=lambda value: value.replace("cipher", "secret"))
    monkeypatch.setattr(credentials, "decrypt_secret", decrypt)
    keys = parsed(
        selected(5, status="insufficient_credit"), selected(1),
        selected(4, status="invalid"), selected(2, status="usage_limit_reached"),
        selected(3, storage_type="anthropic_official", status="invalid"),
    )

    result = asyncio.run(credentials.reveal_credentials(synthetic_database, keys))

    assert result == [f"synthetic-secret-{identifier}" for identifier in (5, 1, 4, 2, 3)]
    synthetic_database.scalars.assert_awaited_once()
    assert decrypt.call_count == 5


def test_service_handles_maximum_selection_in_one_query(monkeypatch, synthetic_database):
    monkeypatch.setattr(credentials, "decrypt_secret", lambda value: value)
    keys = parsed(*(selected(identifier) for identifier in reversed(range(10, 110))))

    result = asyncio.run(credentials.reveal_credentials(synthetic_database, keys))

    assert result == [f"synthetic-cipher-{identifier}" for identifier in reversed(range(10, 110))]
    synthetic_database.scalars.assert_awaited_once()


@pytest.mark.parametrize("missing", [
    selected(999), selected(1, status="invalid"),
    selected(1, storage_type="anthropic_official"),
    selected(3, storage_type="openai_official", status="invalid"),
])
def test_missing_or_changed_identity_fails_entire_batch_before_decryption(
    monkeypatch, synthetic_database, missing,
):
    decrypt = MagicMock()
    monkeypatch.setattr(credentials, "decrypt_secret", decrypt)

    with pytest.raises(HTTPException) as error:
        asyncio.run(credentials.reveal_credentials(synthetic_database, parsed(selected(2, status="usage_limit_reached"), missing)))

    assert error.value.status_code == 404
    decrypt.assert_not_called()
    synthetic_database.scalars.assert_awaited_once()


def test_identity_mapping_keeps_repeated_numeric_ids_in_distinct_types_and_buckets(monkeypatch, session):
    keys = parsed(
        selected(7, status="invalid"), selected(7),
        selected(7, storage_type="anthropic_official"),
    )
    session.scalars.return_value = SimpleNamespace(all=lambda: [
        SimpleNamespace(id=7, storage_type="openai_official", result_bucket="valid", encrypted_secret="synthetic-a"),
        SimpleNamespace(id=7, storage_type="anthropic_official", result_bucket="valid", encrypted_secret="synthetic-b"),
        SimpleNamespace(id=7, storage_type="openai_official", result_bucket="invalid", encrypted_secret="synthetic-c"),
    ])
    monkeypatch.setattr(credentials, "decrypt_secret", lambda value: value)

    assert asyncio.run(credentials.reveal_credentials(session, keys)) == ["synthetic-c", "synthetic-a", "synthetic-b"]


def test_service_uses_existing_secret_encryption(monkeypatch, session):
    session.scalars.return_value = SimpleNamespace(all=lambda: [SimpleNamespace(
        id=1, storage_type="openai_official", result_bucket="valid",
        encrypted_secret=encrypt_secret("synthetic-round-trip-only"),
    )])

    assert asyncio.run(credentials.reveal_credentials(session, parsed(selected()))) == ["synthetic-round-trip-only"]


def test_unknown_type_is_rejected_before_any_query(monkeypatch, session):
    decrypt = MagicMock()
    monkeypatch.setattr(credentials, "decrypt_secret", decrypt)

    with pytest.raises(HTTPException) as error:
        asyncio.run(credentials.reveal_credentials(session, parsed(selected(), selected(2, storage_type="unknown"))))

    assert error.value.status_code == 422
    session.scalars.assert_not_awaited()
    decrypt.assert_not_called()


@pytest.mark.parametrize("payload", [
    {"keys": []},
    {"keys": [selected(identifier) for identifier in range(1, 102)]},
    {"keys": [selected(), selected()]},
    {"keys": [selected(0)]},
    {"keys": [selected(-1)]},
    {"keys": [selected(True)]},
    {"keys": [selected("1")]},
    {"keys": [selected(1.5)]},
    {"keys": [selected(9_223_372_036_854_775_808)]},
    {"keys": [selected(status="all")]},
    {"keys": [selected(status="network_error")]},
    {"keys": [selected(status="pending")]},
    {"keys": [selected(storage_type="")]},
    {"keys": [selected(storage_type="x" * 65)]},
    {"keys": [{"id": 1, "type": "openai_official"}]},
    {"keys": [{**selected(), "secret": "synthetic-unexpected"}]},
    {},
])
def test_api_rejects_invalid_batch_before_reading_secrets(monkeypatch, request_batch, payload):
    reader = AsyncMock()
    monkeypatch.setattr(api, "reveal_credentials", reader)

    response = request_batch(payload)

    assert response.status_code == 422
    reader.assert_not_awaited()
    assert "secrets" not in response.json()


@pytest.mark.parametrize("items", [
    [selected()],
    [selected(identifier) for identifier in range(1, 101)],
    [selected(1), selected(1, status="invalid"), selected(1, storage_type="tokenrouter")],
])
def test_api_accepts_boundaries_and_uses_non_cacheable_response(monkeypatch, request_batch, session, items):
    secrets = [f"synthetic-response-{index}" for index in range(len(items))]
    reader = AsyncMock(return_value=secrets)
    monkeypatch.setattr(api, "reveal_credentials", reader)

    response = request_batch({"keys": items})

    assert response.status_code == 200
    assert response.json() == {"secrets": secrets}
    assert response.headers["Cache-Control"] == "no-store"
    reader.assert_awaited_once_with(session, [BatchRevealKeyInput.model_validate(item) for item in items])


def test_api_requires_login(monkeypatch, request_batch):
    monkeypatch.setattr(api, "current_user", AsyncMock(return_value=None))
    reader = AsyncMock()
    monkeypatch.setattr(api, "reveal_credentials", reader)

    response = request_batch({"keys": [selected()]})

    assert response.status_code == 401
    reader.assert_not_awaited()


def test_api_rejects_untrusted_origin(monkeypatch, request_batch):
    reader = AsyncMock()
    monkeypatch.setattr(api, "reveal_credentials", reader)

    response = request_batch({"keys": [selected()]}, headers={"Origin": "https://synthetic-untrusted.example"})

    assert response.status_code == 403
    reader.assert_not_awaited()


def test_api_returns_no_partial_secrets_when_a_record_is_missing(monkeypatch, request_batch, session):
    session.scalars.return_value = SimpleNamespace(all=lambda: [SimpleNamespace(
        id=1, storage_type="openai_official", result_bucket="valid", encrypted_secret="synthetic-cipher",
    )])
    decrypt = MagicMock(return_value="synthetic-must-not-be-returned")
    monkeypatch.setattr(credentials, "decrypt_secret", decrypt)

    response = request_batch({"keys": [selected(), selected(999)]})

    assert response.status_code == 404
    assert "synthetic-must-not-be-returned" not in response.text
    assert "secrets" not in response.json()
    decrypt.assert_not_called()


def test_api_returns_no_partial_secrets_when_decryption_fails(monkeypatch, request_batch, session):
    session.scalars.return_value = SimpleNamespace(all=lambda: [
        SimpleNamespace(id=identifier, storage_type="openai_official", result_bucket="valid", encrypted_secret=f"synthetic-{identifier}")
        for identifier in (1, 2)
    ])
    monkeypatch.setattr(credentials, "decrypt_secret", MagicMock(side_effect=[
        "synthetic-must-not-be-returned", ValueError("synthetic-decryption-failure"),
    ]))

    response = request_batch({"keys": [selected(), selected(2)]})

    assert response.status_code == 500
    assert "synthetic-must-not-be-returned" not in response.text
