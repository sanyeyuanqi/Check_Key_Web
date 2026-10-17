import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

import app.credential_service as credentials
from app.database import ValidationBatch
from app.platform_catalog import scoped_provider
from app.schemas import BatchRevealKeyInput, ValidationResultInput
from app.status_service import list_credential_statuses
from app.valid_key_service import list_valid_key_records


def sql(statement):
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


@pytest.fixture
def session():
    return MagicMock(spec=AsyncSession)


@pytest.mark.parametrize("provider,storage_type,expected", [
    (None, "openai_official", "OpenAI"),
    (None, "anthropic_enterprise_subscription", "Anthropic"),
    (None, "tokenrouter", "TokenRouter"),
    (None, "vercel", "Vercel"),
    (None, "opencode_zen", "OpenCode"),
    (None, "cloudflare_workers_ai", "Cloudflare"),
    (None, "unknown_legacy_type", None),
    (None, None, None),
    ("Anthropic", "openai_official", "Anthropic"),
])
def test_known_type_only_scopes_infer_platform_and_explicit_legacy_scopes_are_preserved(provider, storage_type, expected):
    assert scoped_provider(provider, storage_type) == expected


@pytest.mark.parametrize("input_provider,kind,requested_type,expected_type,expected_provider", [
    ("wrong-platform", "openai", None, "openai_official", "OpenAI"),
    ("OpenAI", "anthropic", "anthropic_official", "anthropic_official", "Anthropic"),
    ("Anthropic", "anthropic_oauth", None, "anthropic_enterprise_subscription", "Anthropic"),
    ("legacy-label", "aws_access_key", None, "aws_access", "AWS Bedrock"),
    ("legacy-label", "google_vertex_service_account", None, "google_vertex_gemini", "Google"),
    ("legacy-label", "tokenrouter", "unrecognized-type", "tokenrouter", "TokenRouter"),
    ("legacy-label", "vercel", "unrecognized-type", "vercel", "Vercel"),
    ("OpenCode", "opencode", None, "opencode_zen", "OpenCode"),
    ("OpenCode", "openai", None, "opencode_zen", "OpenCode"),
    ("OpenCode", "anthropic", None, "opencode_zen", "OpenCode"),
    ("OpenAI", "opencode", None, "opencode_zen", "OpenCode"),
    ("legacy-label", "opencode", "opencode", "opencode_zen", "OpenCode"),
    ("wrong-platform", "openai", "opencode_zen", "opencode_zen", "OpenCode"),
    ("Cloudflare", "cloudflare", None, "cloudflare_workers_ai", "Cloudflare"),
    ("Cloudflare", "openai", None, "cloudflare_workers_ai", "Cloudflare"),
    ("Cloudflare", "anthropic", None, "cloudflare_workers_ai", "Cloudflare"),
    ("OpenAI", "cloudflare", "unrecognized-type", "cloudflare_workers_ai", "Cloudflare"),
])
def test_upload_routes_resolved_type_to_canonical_platform_without_replacing_returned_id(
    monkeypatch, session, input_provider, kind, requested_type, expected_type, expected_provider,
):
    session.scalar.return_value = ValidationBatch(id=29, total=1)
    session.execute.side_effect = [
        SimpleNamespace(), SimpleNamespace(scalar_one=lambda: 41), SimpleNamespace(),
        SimpleNamespace(one=lambda: (1, 1, 0, 0, 0)),
    ]
    remember = AsyncMock()
    monkeypatch.setattr(credentials, "remember_valid_key", remember)
    item = ValidationResultInput.model_validate({
        "index": 1, "status": "valid", "usable": True,
        "credential": {
            "provider": input_provider, "kind": kind, "storage_type": requested_type,
            "secret": "synthetic-platform-routing-only",
        },
    })

    assert asyncio.run(credentials.process_batch_results(session, 29, [item])) == 1

    statement = session.execute.await_args_list[1].args[0].compile(dialect=postgresql.dialect())
    assert statement.params["provider"] == expected_provider
    assert statement.params["storage_type"] == expected_type
    assert "ON CONFLICT ON CONSTRAINT uq_credentials_type_fingerprint" in str(statement)
    assert "RETURNING credentials.id" in str(statement)
    assert remember.await_args.args[1] == 41
    batch_item = session.execute.await_args_list[2].args[0].compile(dialect=postgresql.dialect())
    assert batch_item.params["credential_id"] == 41


@pytest.mark.parametrize("provider,storage_type,expected", [
    (None, "openai_official", "OpenAI"),
    ("Anthropic", "openai_official", "Anthropic"),
    (None, None, None),
    (None, "legacy-type", None),
])
def test_list_and_status_counts_share_the_same_platform_scope(session, provider, storage_type, expected):
    session.execute.side_effect = [SimpleNamespace(all=lambda: []), SimpleNamespace(all=lambda: [])]
    session.scalar.return_value = 0
    session.scalars.return_value = SimpleNamespace(all=lambda: [])

    asyncio.run(credentials.list_credentials(
        session, status="all", provider=provider, storage_type=storage_type, query=None, page=1,
    ))

    statements = [
        session.execute.await_args_list[0].args[0], session.scalar.await_args.args[0],
        session.scalars.await_args.args[0], session.execute.await_args_list[1].args[0],
    ]
    for statement in statements:
        query = sql(statement)
        if expected is None:
            assert "credentials.provider =" not in query
        else:
            assert f"credentials.provider = '{expected}'" in query


def test_dictionary_type_scope_is_in_join_and_preserves_zero_status_counts(session):
    session.execute.return_value = SimpleNamespace(all=lambda: [])
    asyncio.run(list_credential_statuses(session, storage_type="azure_claude"))
    query = sql(session.execute.await_args.args[0])
    join, where = query.split("WHERE", 1)
    assert "LEFT OUTER JOIN credentials" in join
    assert "credentials.provider = 'Azure'" in join
    assert "credentials.provider =" not in where


@pytest.mark.parametrize("provider,expected", [(None, "Google"), ("OpenAI", "OpenAI")])
def test_valid_history_count_and_data_scope_the_same_partition(session, provider, expected):
    session.scalar.return_value = 0
    session.execute.return_value = SimpleNamespace(all=lambda: [])
    result = asyncio.run(list_valid_key_records(session, provider=provider, storage_type="google_vertex_claude"))
    assert result["total"] == 0
    for statement in (session.scalar.await_args.args[0], session.execute.await_args.args[0]):
        query = sql(statement)
        assert f"credentials.provider = '{expected}'" in query
        assert "credentials.storage_type = 'google_vertex_claude'" in query


@pytest.mark.parametrize("provider,expected", [(None, "AWS Bedrock"), ("OpenAI", "OpenAI")])
def test_export_type_scope_prunes_canonical_partition_and_keeps_explicit_mismatches(session, provider, expected):
    session.scalars.return_value = SimpleNamespace(all=lambda: [])
    assert asyncio.run(credentials.export_credentials(
        session, status="all", provider=provider, storage_type="aws_access",
    )) == []
    assert f"credentials.provider = '{expected}'" in sql(session.scalars.await_args.args[0])


def test_single_and_batch_reveal_include_platform_ownership(monkeypatch, session):
    monkeypatch.setattr(credentials, "decrypt_secret", lambda value: "synthetic-secret")
    session.scalar.return_value = SimpleNamespace(encrypted_secret="synthetic-cipher")
    assert asyncio.run(credentials.reveal_credential(session, 41, "tokenrouter", "valid")) == "synthetic-secret"
    assert "credentials.provider = 'TokenRouter'" in sql(session.scalar.await_args.args[0])

    session.scalars.return_value = SimpleNamespace(all=lambda: [
        SimpleNamespace(id=41, storage_type="tokenrouter", result_bucket="valid", encrypted_secret="synthetic-cipher"),
        SimpleNamespace(id=42, storage_type="openai_official", result_bucket="invalid", encrypted_secret="synthetic-cipher"),
    ])
    assert asyncio.run(credentials.reveal_credentials(session, [
        BatchRevealKeyInput(id=41, type="tokenrouter", status="valid"),
        BatchRevealKeyInput(id=42, type="openai_official", status="invalid"),
    ])) == ["synthetic-secret", "synthetic-secret"]
    query = sql(session.scalars.await_args.args[0])
    assert "credentials.provider = 'TokenRouter' AND credentials.storage_type = 'tokenrouter'" in query
    assert "credentials.provider = 'OpenAI' AND credentials.storage_type = 'openai_official'" in query
