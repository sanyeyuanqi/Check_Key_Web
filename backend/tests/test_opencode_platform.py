import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.credential_service as credentials
import app.main as api
from app.database import AuthIPBlock, Credential, CredentialRegistry, CredentialStatus, ValidKeyRecord
from app.export_service import export_filename
from app.schemas import BatchRevealKeyInput
from app.security import encrypt_secret, fingerprint
from app.valid_key_service import list_valid_key_records
from test_export_records import SQLiteScalarStream, request_api, session


NOW = datetime(2026, 10, 17, tzinfo=UTC)


@pytest.fixture
def opencode_database():
    # All payloads are synthetic and remain inside an in-memory database.
    engine = create_engine("sqlite://")
    for table in (CredentialStatus.__table__, CredentialRegistry.__table__, Credential.__table__, ValidKeyRecord.__table__):
        table.create(engine)
    with Session(engine) as db:
        db.add_all([
            CredentialStatus(value=value, name=name, tone="neutral", sort_order=index)
            for index, (value, name) in enumerate([
                ("valid", "可用"), ("invalid", "无效"), ("network_error", "网络异常"),
            ])
        ])
        for identifier, provider, storage_type, status, secret in (
            (1, "OpenCode", "opencode_zen", "valid", "synthetic-shared-key"),
            (2, "OpenCode", "opencode_zen", "invalid", "synthetic-later-invalid-key"),
            (3, "OpenAI", "openai_official", "valid", "synthetic-shared-key"),
            (4, "Vercel", "vercel", "valid", "synthetic-shared-key"),
        ):
            digest = fingerprint(secret)
            db.add(CredentialRegistry(id=identifier, provider=provider, storage_type=storage_type, fingerprint=digest))
            db.add(Credential(
                id=identifier, fingerprint=digest, storage_type=storage_type, provider=provider,
                kind="opencode" if provider == "OpenCode" else "openai", encrypted_secret=encrypt_secret(secret),
                key_prefix="synthetic-shared-prefix", masked="synthetic…test", status=status,
                result_bucket=credentials.result_bucket(status), created_at=NOW, updated_at=NOW,
            ))
            db.add(ValidKeyRecord(
                credential_id=identifier, first_valid_at=NOW - timedelta(days=2), last_valid_at=NOW - timedelta(days=1),
            ))
        db.commit()
        yield SimpleNamespace(
            execute=AsyncMock(side_effect=db.execute), scalar=AsyncMock(side_effect=db.scalar),
            scalars=AsyncMock(side_effect=db.scalars),
            stream_scalars=AsyncMock(side_effect=lambda statement: SQLiteScalarStream(db.scalars(statement))),
            get=AsyncMock(side_effect=lambda model, identity: None if model is AuthIPBlock else db.get(model, identity)),
        )
    engine.dispose()


def test_opencode_list_and_status_counts_are_isolated_from_same_keys_on_other_platforms(opencode_database):
    result = asyncio.run(credentials.list_credentials(
        opencode_database, status="all", status_mode="exact", provider=None,
        storage_type="opencode_zen", query=None, page=1,
    ))
    assert result["total"] == 2
    assert [row["id"] for row in result["keys"]] == [2, 1]
    assert {row["provider"] for row in result["keys"]} == {"OpenCode"}
    assert {row["storageType"] for row in result["keys"]} == {"opencode_zen"}
    assert [(row["value"], row["count"]) for row in result["statuses"]] == [
        ("valid", 1), ("invalid", 1), ("network_error", 0),
    ]


def test_opencode_single_and_bulk_reveal_preserve_selected_platform_identities(opencode_database):
    assert asyncio.run(credentials.reveal_credential(opencode_database, 1, "opencode_zen", "valid")) == "synthetic-shared-key"
    selected = [
        BatchRevealKeyInput(id=2, type="opencode_zen", status="invalid"),
        BatchRevealKeyInput(id=3, type="openai_official", status="valid"),
        BatchRevealKeyInput(id=1, type="opencode_zen", status="valid"),
    ]
    assert asyncio.run(credentials.reveal_credentials(opencode_database, selected)) == [
        "synthetic-later-invalid-key", "synthetic-shared-key", "synthetic-shared-key",
    ]
    with pytest.raises(HTTPException) as error:
        asyncio.run(credentials.reveal_credential(opencode_database, 3, "opencode_zen", "valid"))
    assert error.value.status_code == 404


def test_opencode_bulk_reveal_rejects_cross_platform_ids_before_decryption(monkeypatch, opencode_database):
    decrypt = MagicMock()
    monkeypatch.setattr(credentials, "decrypt_secret", decrypt)
    with pytest.raises(HTTPException) as error:
        asyncio.run(credentials.reveal_credentials(opencode_database, [
            BatchRevealKeyInput(id=1, type="opencode_zen", status="valid"),
            BatchRevealKeyInput(id=3, type="opencode_zen", status="valid"),
        ]))
    assert error.value.status_code == 404
    decrypt.assert_not_called()


def test_opencode_archive_keeps_later_invalid_result_with_current_status_name(opencode_database):
    result = asyncio.run(list_valid_key_records(opencode_database, storage_type="opencode_zen"))
    assert result["total"] == 2
    assert [row["credentialId"] for row in result["records"]] == [2, 1]
    assert [(row["status"], row["statusName"]) for row in result["records"]] == [("invalid", "无效"), ("valid", "可用")]
    assert {row["storageType"] for row in result["records"]} == {"opencode_zen"}
    assert "synthetic-shared-key" not in str(result)
    assert "synthetic-later-invalid-key" not in str(result)


def test_opencode_export_combines_requested_states_and_uses_platform_filename(opencode_database):
    values = asyncio.run(credentials.export_credentials(
        opencode_database, status="valid,invalid", status_mode="exact", provider=None, storage_type="opencode_zen",
    ))
    assert values == ["synthetic-later-invalid-key", "synthetic-shared-key"]
    assert export_filename("OpenCode", "valid,invalid") == "opencode-valid-invalid.txt"
    assert export_filename("Vercel", "valid") == "vercel-valid.txt"


def test_opencode_export_api_records_platform_and_type_without_cross_platform_keys(
    monkeypatch, request_api, opencode_database,
):
    request, user = request_api

    async def scoped_session():
        yield opencode_database

    api.app.dependency_overrides[api.get_session] = scoped_session
    logger = AsyncMock()
    monkeypatch.setattr(api, "record_export", logger)
    response = request("/api/export?provider=OpenCode&type=opencode_zen&status=invalid,valid&status_mode=exact")
    assert response.status_code == 200
    assert response.text == "synthetic-later-invalid-key\nsynthetic-shared-key"
    assert response.headers["Content-Disposition"] == 'attachment; filename="opencode-valid-invalid.txt"'
    logger.assert_awaited_once_with(
        opencode_database, user=user, provider="OpenCode", storage_type="opencode_zen", status="valid,invalid",
        status_mode="exact", count=2, file_name="opencode-valid-invalid.txt", ip_address="127.0.0.1",
    )
