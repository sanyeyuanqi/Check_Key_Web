import ast
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

import app.credential_service as credentials
import app.main as api
from app.database import AuthIPBlock, Credential, CredentialRegistry, CredentialStatus, ValidationBatch, ValidKeyRecord
from app.platform_catalog import STORAGE_TYPE_PROVIDERS
from app.schemas import BatchRevealKeyInput, ValidationResultInput
from app.security import decrypt_secret, encrypt_secret, fingerprint
from app.valid_key_service import list_valid_key_records
from test_export_records import request_api, session


NOW = datetime(2026, 10, 17, tzinfo=UTC)
ACCOUNT_ONE = "a" * 32
ACCOUNT_TWO = "c" * 32
TOKEN = "b" * 40
TYPE = "cloudflare_workers_ai"


@pytest.fixture(params=["|", "\t"], ids=["desktop-pipe", "literal-tab"])
def cloudflare_database(request):
    first_secret = ACCOUNT_ONE + request.param + TOKEN
    second_secret = ACCOUNT_TWO + request.param + TOKEN
    engine = create_engine("sqlite://")
    for table in (CredentialStatus.__table__, CredentialRegistry.__table__, Credential.__table__, ValidKeyRecord.__table__):
        table.create(engine)
    with Session(engine) as db:
        db.add_all([
            CredentialStatus(value=value, name=name, tone="neutral", sort_order=index)
            for index, (value, name) in enumerate([("valid", "可用"), ("invalid", "无效"), ("network_error", "网络异常")])
        ])
        for identifier, provider, storage_type, status, secret in (
            (1, "Cloudflare", TYPE, "valid", first_secret),
            (2, "Cloudflare", TYPE, "invalid", second_secret),
            (3, "OpenAI", "openai_official", "valid", first_secret),
            (4, "OpenCode", "opencode_zen", "valid", first_secret),
        ):
            digest = fingerprint(secret)
            db.add(CredentialRegistry(id=identifier, provider=provider, storage_type=storage_type, fingerprint=digest))
            db.add(Credential(
                id=identifier, fingerprint=digest, storage_type=storage_type, provider=provider,
                kind="cloudflare" if provider == "Cloudflare" else "synthetic", encrypted_secret=encrypt_secret(secret),
                key_prefix=secret[:32], masked="synthetic-account…token", status=status,
                result_bucket=credentials.result_bucket(status), created_at=NOW, updated_at=NOW,
            ))
            db.add(ValidKeyRecord(
                credential_id=identifier, first_valid_at=NOW - timedelta(days=2), last_valid_at=NOW - timedelta(days=1),
            ))
        db.commit()
        yield SimpleNamespace(
            execute=AsyncMock(side_effect=db.execute), scalar=AsyncMock(side_effect=db.scalar),
            scalars=AsyncMock(side_effect=db.scalars), first_secret=first_secret, second_secret=second_secret,
            get=AsyncMock(side_effect=lambda model, identity: None if model is AuthIPBlock else db.get(model, identity)),
        )
    engine.dispose()


@pytest.mark.parametrize("separator", ["|", "\t"])
def test_upload_encrypts_and_deduplicates_complete_account_and_token_without_separator_changes(monkeypatch, session, separator):
    secret = ACCOUNT_ONE + separator + TOKEN
    session.scalar.return_value = ValidationBatch(id=29, total=1)
    session.execute.side_effect = [
        SimpleNamespace(), SimpleNamespace(scalar_one=lambda: 41), SimpleNamespace(),
        SimpleNamespace(one=lambda: (1, 1, 0, 0, 0)),
    ]
    monkeypatch.setattr(credentials, "remember_valid_key", AsyncMock())
    item = ValidationResultInput.model_validate({
        "index": 1, "status": "valid", "usable": True,
        "credential": {"provider": "Cloudflare", "kind": "cloudflare", "storage_type": TYPE, "secret": secret},
    })
    assert asyncio.run(credentials.process_batch_results(session, 29, [item])) == 1
    values = session.execute.await_args_list[1].args[0].compile(dialect=postgresql.dialect()).params
    assert (values["provider"], values["storage_type"]) == ("Cloudflare", TYPE)
    assert values["key_prefix"] == ACCOUNT_ONE
    assert decrypt_secret(values["encrypted_secret"]) == secret
    assert values["fingerprint"] == fingerprint(secret)
    assert values["fingerprint"] != fingerprint(TOKEN)
    assert values["fingerprint"] != fingerprint(ACCOUNT_TWO + separator + TOKEN)


def test_cloudflare_list_and_archive_preserve_platform_identity_and_current_invalid_status(cloudflare_database):
    result = asyncio.run(credentials.list_credentials(
        cloudflare_database, status="all", status_mode="exact", provider=None, storage_type=TYPE, query=None, page=1,
    ))
    assert result["total"] == 2
    assert [row["id"] for row in result["keys"]] == [2, 1]
    assert {row["provider"] for row in result["keys"]} == {"Cloudflare"}
    assert [(row["value"], row["count"]) for row in result["statuses"]] == [("valid", 1), ("invalid", 1), ("network_error", 0)]
    assert [row["keyPrefix"] for row in result["keys"]] == [ACCOUNT_TWO, ACCOUNT_ONE]
    history = asyncio.run(list_valid_key_records(cloudflare_database, storage_type=TYPE))
    assert history["total"] == 2
    assert [row["credentialId"] for row in history["records"]] == [2, 1]
    assert [(row["status"], row["statusName"]) for row in history["records"]] == [("invalid", "无效"), ("valid", "可用")]
    assert cloudflare_database.first_secret not in str(history)
    assert cloudflare_database.second_secret not in str(history)


def test_cloudflare_single_and_mixed_batch_reveal_return_account_and_token_verbatim(cloudflare_database):
    assert asyncio.run(credentials.reveal_credential(cloudflare_database, 1, TYPE, "valid")) == cloudflare_database.first_secret
    assert asyncio.run(credentials.reveal_credentials(cloudflare_database, [
        BatchRevealKeyInput(id=2, type=TYPE, status="invalid"),
        BatchRevealKeyInput(id=3, type="openai_official", status="valid"),
        BatchRevealKeyInput(id=1, type=TYPE, status="valid"),
    ])) == [cloudflare_database.second_secret, cloudflare_database.first_secret, cloudflare_database.first_secret]


def test_cloudflare_batch_reveal_rejects_another_platform_identity_before_decrypting(monkeypatch, cloudflare_database):
    decrypt = MagicMock()
    monkeypatch.setattr(credentials, "decrypt_secret", decrypt)
    with pytest.raises(HTTPException) as error:
        asyncio.run(credentials.reveal_credentials(cloudflare_database, [
            BatchRevealKeyInput(id=1, type=TYPE, status="valid"), BatchRevealKeyInput(id=3, type=TYPE, status="valid"),
        ]))
    assert error.value.status_code == 404
    decrypt.assert_not_called()


def test_cloudflare_export_txt_and_audit_keep_account_separator_and_platform_type(
    monkeypatch, request_api, cloudflare_database,
):
    request, user = request_api

    async def scoped_session():
        yield cloudflare_database

    api.app.dependency_overrides[api.get_session] = scoped_session
    logger = AsyncMock()
    monkeypatch.setattr(api, "record_export", logger)
    response = request("/api/export?provider=Cloudflare&type=cloudflare_workers_ai&status=invalid,valid&status_mode=exact")
    assert response.status_code == 200
    assert response.text == cloudflare_database.second_secret + "\n" + cloudflare_database.first_secret
    assert response.headers["Content-Disposition"] == 'attachment; filename="cloudflare-valid-invalid.txt"'
    logger.assert_awaited_once_with(
        cloudflare_database, user=user, provider="Cloudflare", storage_type=TYPE, status="valid,invalid",
        status_mode="exact", count=2, file_name="cloudflare-valid-invalid.txt", ip_address="127.0.0.1",
    )


def test_backend_platform_and_storage_catalog_covers_desktop_sources_without_importing_gui():
    desktop = next((parent / "desktop" for parent in Path(__file__).resolve().parents
                    if (parent / "desktop" / "bedrock_checker.py").is_file()), None)
    if desktop is None:
        pytest.skip("Desktop source parity is only checked in the combined repository")
    credential_source = desktop / "bedrock_checker.py"
    app_source = desktop / "anthropic_key_checker_app.pyw"
    if not credential_source.exists() or not app_source.exists():
        pytest.skip("Desktop source parity is only checked in the combined repository")
    checker_tree = ast.parse(credential_source.read_text(encoding="utf-8-sig"))
    credential_class = next(node for node in checker_tree.body if isinstance(node, ast.ClassDef) and node.name == "ApiCredential")
    provider_property = next(node for node in credential_class.body if isinstance(node, ast.FunctionDef) and node.name == "provider")
    desktop_providers = {
        node.value.value for node in ast.walk(provider_property)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
    }
    assert desktop_providers <= set(STORAGE_TYPE_PROVIDERS.values())
    app_tree = ast.parse(app_source.read_text(encoding="utf-8-sig"))
    storage_function = next(node for node in app_tree.body if isinstance(node, ast.FunctionDef) and node.name == "remote_storage_type")
    desktop_storage_types = {
        literal.value for node in ast.walk(storage_function) if isinstance(node, ast.Return)
        for literal in ast.walk(node.value) if isinstance(literal, ast.Constant) and isinstance(literal.value, str)
    }
    assert desktop_storage_types <= STORAGE_TYPE_PROVIDERS.keys()
