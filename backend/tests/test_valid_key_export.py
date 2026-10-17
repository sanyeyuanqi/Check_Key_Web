import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

import app.valid_key_service as service
from app.database import Credential, CredentialRegistry, CredentialStatus, ValidKeyRecord
from app.platform_catalog import STORAGE_TYPE_PROVIDERS
from app.schemas import ValidKeyBatchExportInput, ValidKeyExportKeyInput


def run(coroutine):
    return asyncio.run(coroutine)


def key(identifier, storage_type="openai_official"):
    return ValidKeyExportKeyInput(id=identifier, type=storage_type)


def request(provider="OpenAI", keys=None, **extra):
    return ValidKeyBatchExportInput.model_validate({
        "provider": provider, "keys": keys if keys is not None else [{"id": 1, "type": "openai_official"}], **extra,
    })


@pytest.fixture
def archive(monkeypatch):
    engine = create_engine("sqlite://")
    for model in (CredentialStatus, CredentialRegistry, Credential, ValidKeyRecord):
        model.__table__.create(engine)
    secrets = {}
    when = datetime(2026, 10, 17, tzinfo=UTC)
    with Session(engine) as database:
        database.add_all([
            CredentialStatus(value="valid", name="可用", tone="valid", sort_order=0),
            CredentialStatus(value="invalid", name="无效", tone="invalid", sort_order=1),
        ])

        def add(identifier, *, storage_type="openai_official", status="invalid", archived=True, prefix=None, ip=None, secret=None):
            provider = STORAGE_TYPE_PROVIDERS[storage_type]
            key_fingerprint = f"synthetic-fingerprint-{identifier}"
            secrets[f"synthetic-cipher-{identifier}"] = secret or f"synthetic-complete-secret-{identifier}"
            database.add_all([
                CredentialRegistry(id=identifier, provider=provider, storage_type=storage_type, fingerprint=key_fingerprint),
                Credential(
                    id=identifier, provider=provider, storage_type=storage_type, fingerprint=key_fingerprint,
                    kind="synthetic", encrypted_secret=f"synthetic-cipher-{identifier}", key_prefix=prefix or f"other-{identifier}",
                    masked="synthetic…tail", status=status, result_bucket=status, source_ip=ip, created_at=when, updated_at=when,
                ),
            ])
            if archived:
                database.add(ValidKeyRecord(
                    credential_id=identifier, first_valid_at=when,
                    last_valid_at=when + timedelta(seconds=10 if identifier in (1, 2) else identifier),
                ))

        add(1, ip="172.20.0.1", prefix="match-one")
        add(2, ip="172.20.0.10", prefix="match-two", status="valid")
        add(3, storage_type="anthropic_official", ip="172.20.0.1", prefix="match-anthropic")
        add(4, storage_type="azure_gpt", ip="2001:db8::1")
        add(5, ip="2001:db8::1")
        add(6, ip="2001:db8::2")
        add(7, ip="172.20.%_\\host", prefix="literal%_\\secret")
        add(8, ip="172.20.XA\\host", prefix="literalAB\\secret")
        add(9)
        add(10, status="valid", archived=False, prefix="match-never-archived", ip="172.20.0.1")
        for identifier, storage_type in enumerate([
            "google_ai_studio_gemini", "google_vertex_gemini", "google_vertex_claude", "azure_claude",
            "aws_access", "aws_bedrock_api", "openrouter", "tokenrouter", "vercel", "opencode_zen", "cloudflare_workers_ai",
        ], 11):
            add(identifier, storage_type=storage_type, secret=f"synthetic-account-{identifier}|synthetic-full-token-{identifier}")
        add(22, storage_type="anthropic_enterprise_subscription")
        for identifier in range(100, 225):
            add(identifier, prefix=f"bulk-{identifier}", ip="198.51.100.1", status="invalid" if identifier % 2 else "valid")
        database.commit()
        decrypt = MagicMock(side_effect=secrets.__getitem__)
        monkeypatch.setattr(service, "decrypt_secret", decrypt)
        yield SimpleNamespace(
            session=SimpleNamespace(scalars=AsyncMock(side_effect=database.scalars), commit=AsyncMock()),
            secrets=secrets, decrypt=decrypt,
        )
    engine.dispose()


def test_selected_exports_only_archived_rows_in_requested_order_even_when_currently_invalid(archive):
    selected = [key(2), key(1), key(9)]
    assert run(service.export_selected_valid_keys(archive.session, provider="OpenAI", keys=selected)) == [
        "synthetic-complete-secret-2", "synthetic-complete-secret-1", "synthetic-complete-secret-9",
    ]
    archive.session.scalars.assert_awaited_once()
    archive.session.commit.assert_not_awaited()
    statement = archive.session.scalars.await_args.args[0]
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert "JOIN valid_key_records ON valid_key_records.credential_id = credentials.id" in compiled
    assert "credentials.provider =" in compiled
    assert "credentials.status =" not in compiled
    assert "credentials.result_bucket =" not in compiled


def test_selected_export_keeps_multiple_types_of_one_platform_and_complete_composite_secrets(archive):
    values = run(service.export_selected_valid_keys(archive.session, provider="AWS Bedrock", keys=[key(16, "aws_bedrock_api"), key(15, "aws_access")]))
    assert values == ["synthetic-account-16|synthetic-full-token-16", "synthetic-account-15|synthetic-full-token-15"]
    assert run(service.export_selected_valid_keys(archive.session, provider="Cloudflare", keys=[key(21, "cloudflare_workers_ai")])) == ["synthetic-account-21|synthetic-full-token-21"]


@pytest.mark.parametrize("storage_type,provider,identifier", [
    (storage_type, STORAGE_TYPE_PROVIDERS[storage_type], identifier)
    for storage_type, identifier in {
        "openai_official": 1, "anthropic_official": 3, "anthropic_enterprise_subscription": 22,
        "azure_gpt": 4, "google_ai_studio_gemini": 11, "google_vertex_gemini": 12,
        "google_vertex_claude": 13, "azure_claude": 14, "aws_access": 15, "aws_bedrock_api": 16,
        "openrouter": 17, "tokenrouter": 18, "vercel": 19, "opencode_zen": 20, "cloudflare_workers_ai": 21,
    }.items()
])
def test_selected_export_supports_every_catalog_type_with_its_canonical_platform(archive, storage_type, provider, identifier):
    assert run(service.export_selected_valid_keys(archive.session, provider=provider, keys=[key(identifier, storage_type)])) == [archive.secrets[f"synthetic-cipher-{identifier}"]]


@pytest.mark.parametrize("keys,provider", [
    ([key(1), key(9999)], "OpenAI"),
    ([key(1), key(10)], "OpenAI"),
    ([key(1), key(3)], "OpenAI"),
    ([key(15, "aws_bedrock_api")], "AWS Bedrock"),
    ([key(1, "synthetic_unknown_type")], "OpenAI"),
    ([key(3, "anthropic_official")], "OpenAI"),
])
def test_missing_nonarchive_wrong_type_or_wrong_provider_rejects_whole_selection_before_decrypt(archive, keys, provider):
    with pytest.raises(HTTPException) as error:
        run(service.export_selected_valid_keys(archive.session, provider=provider, keys=keys))
    assert error.value.status_code == 404
    archive.decrypt.assert_not_called()
    archive.session.commit.assert_not_awaited()


def test_selected_export_accepts_maximum_hundred_records(archive):
    selected = [key(identifier) for identifier in range(199, 99, -1)]
    exported = run(service.export_selected_valid_keys(archive.session, provider="OpenAI", keys=selected))
    assert exported == [f"synthetic-complete-secret-{identifier}" for identifier in range(199, 99, -1)]
    archive.session.scalars.assert_awaited_once()


@pytest.mark.parametrize("keys", [[], [key(1), key(1)], [key(identifier) for identifier in range(1, 102)]])
def test_service_rejects_invalid_selection_boundaries_without_querying(archive, keys):
    with pytest.raises(HTTPException) as error:
        run(service.export_selected_valid_keys(archive.session, provider="OpenAI", keys=keys))
    assert error.value.status_code == 422
    archive.session.scalars.assert_not_awaited()
    archive.decrypt.assert_not_called()


def test_selected_export_defensively_verifies_provider_returned_by_query(monkeypatch):
    session = SimpleNamespace(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [SimpleNamespace(
        id=1, storage_type="openai_official", provider="Anthropic", encrypted_secret="synthetic-mismatched-cipher",
    )])))
    decrypt = MagicMock()
    monkeypatch.setattr(service, "decrypt_secret", decrypt)
    with pytest.raises(HTTPException) as error:
        run(service.export_selected_valid_keys(session, provider="OpenAI", keys=[key(1)]))
    assert error.value.status_code == 404
    decrypt.assert_not_called()


def test_all_export_crosses_pagination_and_includes_current_invalid_records(archive):
    exported = run(service.export_all_valid_keys(archive.session, provider="OpenAI"))
    assert len(exported) == 132
    assert exported[:125] == [f"synthetic-complete-secret-{identifier}" for identifier in range(224, 99, -1)]
    assert "synthetic-complete-secret-1" in exported
    assert "synthetic-complete-secret-10" not in exported
    assert "synthetic-complete-secret-3" not in exported
    archive.session.scalars.assert_awaited_once()
    statement = archive.session.scalars.await_args.args[0]
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert "LIMIT" not in compiled and "OFFSET" not in compiled
    assert "ORDER BY valid_key_records.last_valid_at DESC, valid_key_records.credential_id DESC" in compiled
    archive.session.commit.assert_not_awaited()


@pytest.mark.parametrize("options,expected", [
    ({"query": " match "}, [2, 1]),
    ({"query": "literal%_\\"}, [7]),
    ({"query": "not-found"}, []),
    ({"ip": "172.20.0.1"}, [1]),
    ({"ip": "172.20.0."}, [2, 1]),
    ({"ip": "172.20.%_\\"}, [7]),
    ({"ip": "2001:0db8:0:0:0:0:0:1"}, [5]),
    ({"ip": "::ffff:172.20.0.1"}, [1]),
    ({"ip": "2001:db8::1%synthetic-scope"}, [5]),
    ({"ip": "172.20.0.1", "query": "not-found"}, [1]),
    ({"ip": "   ", "query": "match"}, [2, 1]),
])
def test_all_export_reuses_literal_prefix_ip_normalization_and_ip_precedence(archive, options, expected):
    assert run(service.export_all_valid_keys(archive.session, provider="OpenAI", **options)) == [f"synthetic-complete-secret-{identifier}" for identifier in expected]
    archive.session.scalars.assert_awaited_once()
    if not expected:
        archive.decrypt.assert_not_called()


def test_all_export_applies_optional_type_within_current_platform(archive):
    exported = run(service.export_all_valid_keys(archive.session, provider="Google", storage_type="google_vertex_claude"))
    assert exported == ["synthetic-account-13|synthetic-full-token-13"]


@pytest.mark.parametrize("provider,storage_type", [
    ("synthetic-unknown-platform", None), ("", None), ("OpenAI", "synthetic_unknown_type"),
    ("OpenAI", "anthropic_official"), ("OpenAI", ""),
])
def test_all_export_requires_known_platform_and_matching_known_type_before_querying(archive, provider, storage_type):
    with pytest.raises(HTTPException) as error:
        run(service.export_all_valid_keys(archive.session, provider=provider, storage_type=storage_type))
    assert error.value.status_code == 422
    archive.session.scalars.assert_not_awaited()
    archive.decrypt.assert_not_called()


@pytest.mark.parametrize("provider", ["synthetic-unknown-platform", "", " "])
def test_selected_export_requires_known_current_platform(archive, provider):
    with pytest.raises(HTTPException) as error:
        run(service.export_selected_valid_keys(archive.session, provider=provider, keys=[key(1)]))
    assert error.value.status_code == 422
    archive.session.scalars.assert_not_awaited()
    archive.decrypt.assert_not_called()


@pytest.mark.parametrize("identifier", [True, False, 1.0, "1", 0, -1, None, 9_223_372_036_854_775_808])
def test_selection_schema_ids_are_strict_positive_bigints(identifier):
    with pytest.raises(ValidationError):
        ValidKeyExportKeyInput.model_validate({"id": identifier, "type": "openai_official"})


@pytest.mark.parametrize("storage_type", [True, 1, None, "", "x" * 65])
def test_selection_schema_type_is_strict_bounded_string(storage_type):
    with pytest.raises(ValidationError):
        ValidKeyExportKeyInput.model_validate({"id": 1, "type": storage_type})


@pytest.mark.parametrize("provider", [True, 1, None, "", "x" * 81])
def test_batch_schema_provider_is_strict_bounded_string(provider):
    with pytest.raises(ValidationError):
        request(provider=provider)


@pytest.mark.parametrize("keys", [[], [{"id": 1, "type": "openai_official"}] * 2, [{"id": identifier, "type": "openai_official"} for identifier in range(1, 102)]])
def test_batch_schema_rejects_empty_duplicates_and_more_than_hundred(keys):
    with pytest.raises(ValidationError):
        request(keys=keys)


def test_batch_schema_accepts_hundred_and_distinguishes_identity_by_type():
    parsed = request(keys=[{"id": identifier, "type": "openai_official"} for identifier in range(1, 101)])
    assert len(parsed.keys) == 100
    assert len(request(keys=[{"id": 1, "type": "aws_access"}, {"id": 1, "type": "aws_bedrock_api"}]).keys) == 2


def test_batch_and_key_schemas_forbid_unexpected_fields():
    with pytest.raises(ValidationError):
        request(status="valid")
    with pytest.raises(ValidationError):
        request(keys=[{"id": 1, "type": "openai_official", "status": "valid"}])
