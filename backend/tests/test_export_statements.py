import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.credential_service import credential_export_statement
from app.database import Credential
from app.valid_key_service import valid_key_export_statement
from test_credential_dates import DATE_OPTIONS, synthetic_database
from test_valid_key_export import archive


def compile_statement(statement):
    return str(statement.compile(dialect=postgresql.dialect()))


def assert_ciphertext_projection(statement):
    assert list(statement.selected_columns) == [Credential.encrypted_secret.property.columns[0]]
    text = compile_statement(statement)
    assert text.startswith("SELECT credentials.encrypted_secret \n")
    assert "LIMIT" not in text and "OFFSET" not in text
    return text


@pytest.mark.parametrize("status,mode,column,operator,expected", [
    ("valid", "bucket", "result_bucket", "=", "valid"),
    ("invalid", "bucket", "result_bucket", "=", "invalid"),
    ("valid,insufficient_credit", "bucket", "result_bucket", "IN", ["valid", "insufficient_credit"]),
    ("network_error", "exact", "status", "=", "network_error"),
    ("valid,network_error", "exact", "status", "IN", ["valid", "network_error"]),
])
def test_platform_export_selects_only_ciphertext_and_preserves_status_filter(status, mode, column, operator, expected):
    statement = credential_export_statement(
        status=status, status_mode=mode, provider="Anthropic", storage_type="anthropic_official",
    )
    text = assert_ciphertext_projection(statement)
    assert f"credentials.{column} {operator}" in text
    assert "credentials.provider =" in text and "credentials.storage_type =" in text
    assert "ORDER BY credentials.created_at DESC, credentials.id DESC" in text
    parameters = statement.compile().params
    assert expected in parameters.values()
    assert "Anthropic" in parameters.values() and "anthropic_official" in parameters.values()


def test_all_platform_export_has_no_status_filter_and_keeps_inferred_platform_and_half_open_dates():
    start = datetime(2026, 10, 17, tzinfo=UTC)
    before = start + timedelta(days=1)
    statement = credential_export_statement(
        status="all", provider=None, storage_type="aws_access", created_from=start, created_before=before,
    )
    text = assert_ciphertext_projection(statement)
    assert "credentials.status" not in text and "credentials.result_bucket" not in text
    assert "credentials.created_at >=" in text and "credentials.created_at <" in text
    assert set(statement.compile().params.values()) == {"AWS Bedrock", "aws_access", start, before}


def test_explicit_platform_scope_is_not_replaced_by_type_inference():
    statement = credential_export_statement(status="all", provider="OpenAI", storage_type="aws_access")
    assert set(statement.compile().params.values()) == {"OpenAI", "aws_access"}


def test_unscoped_legacy_platform_export_keeps_its_existing_unfiltered_behavior():
    statement = credential_export_statement(status="all", provider=None, storage_type=None)
    text = assert_ciphertext_projection(statement)
    assert "WHERE" not in text


def test_platform_ciphertext_query_executes_half_open_dates_exact_statuses_and_stable_order(synthetic_database):
    statement = credential_export_statement(
        status="valid,network_error", status_mode="exact", provider="OpenAI",
        storage_type="openai_official", **DATE_OPTIONS,
    )
    rows = asyncio.run(synthetic_database.scalars(statement)).all()
    assert len(rows) == 104
    assert rows[:2] == ["synthetic-cipher-2", "synthetic-cipher-3"]
    assert rows[-1] == "synthetic-cipher-1"
    assert not set(rows).intersection(f"synthetic-cipher-{identifier}" for identifier in (4, 5, 6, 7))


@pytest.mark.parametrize("status,expected", [("valid", 103), ("invalid", 1)])
def test_platform_ciphertext_query_preserves_bucket_selection(synthetic_database, status, expected):
    statement = credential_export_statement(
        status=status, provider="OpenAI", storage_type="openai_official", **DATE_OPTIONS,
    )
    rows = asyncio.run(synthetic_database.scalars(statement)).all()
    assert len(rows) == expected
    assert ("synthetic-cipher-3" in rows) == (status == "invalid")


def test_archive_export_selects_only_ciphertext_with_complete_archive_order():
    statement = valid_key_export_statement(provider="Google", storage_type="google_vertex_claude")
    text = assert_ciphertext_projection(statement)
    assert "JOIN valid_key_records ON valid_key_records.credential_id = credentials.id" in text
    assert "ORDER BY valid_key_records.last_valid_at DESC, valid_key_records.credential_id DESC" in text
    assert "credentials.status" not in text and "credentials.result_bucket" not in text
    assert set(statement.compile().params.values()) == {"Google", "google_vertex_claude"}


@pytest.mark.parametrize("options,expected", [
    ({}, [*range(224, 99, -1), 2, 1, 9, 8, 7, 6, 5]),
    ({"query": " match "}, [2, 1]),
    ({"query": "literal%_\\"}, [7]),
    ({"ip": "172.20.0.1"}, [1]),
    ({"ip": "172.20.0."}, [2, 1]),
    ({"ip": "172.20.%_\\"}, [7]),
    ({"ip": "2001:0db8:0:0:0:0:0:1"}, [5]),
    ({"ip": "::ffff:172.20.0.1"}, [1]),
    ({"ip": "2001:db8::1%synthetic-scope"}, [5]),
    ({"ip": "172.20.0.1", "query": "not-found"}, [1]),
    ({"ip": "  ", "query": "match"}, [2, 1]),
])
def test_archive_ciphertext_query_matches_page_filters_without_pagination_or_decryption(archive, options, expected):
    statement = valid_key_export_statement(provider="OpenAI", **options)
    assert_ciphertext_projection(statement)
    rows = asyncio.run(archive.session.scalars(statement)).all()
    assert rows == [f"synthetic-cipher-{identifier}" for identifier in expected]
    archive.decrypt.assert_not_called()


@pytest.mark.parametrize("provider,storage_type", [
    ("", None), ("unknown", None), ("OpenAI", "unknown"), ("OpenAI", "anthropic_official"),
])
def test_archive_statement_validates_scope_before_any_database_work(provider, storage_type):
    with pytest.raises(HTTPException) as error:
        valid_key_export_statement(provider=provider, storage_type=storage_type)
    assert error.value.status_code == 422


def test_archive_query_limits_and_escapes_prefix_and_prefers_ip():
    prefix_statement = valid_key_export_statement(provider="OpenAI", query="  " + "a" * 31 + "%_tail  ")
    assert "a" * 31 + "\\%%" in prefix_statement.compile().params.values()
    ip_statement = valid_key_export_statement(provider="OpenAI", query="ignored", ip="172.20.%_\\")
    text = compile_statement(ip_statement)
    assert "credentials.key_prefix" not in text
    assert "credentials.source_ip ILIKE" in text
    assert "172.20.\\%\\_\\\\%" in ip_statement.compile().params.values()
