import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

import app.credential_service as credentials
import app.main as api
from app.database import Credential, CredentialRegistry, CredentialStatus, ValidationBatch, ValidKeyRecord
from app.schemas import ValidationResultInput
from app.valid_key_service import list_valid_key_records, remember_valid_key
from test_export_records import request_api, session, sql


def test_valid_archive_uses_credential_primary_key_and_preserves_credential_reference():
    table = ValidKeyRecord.__table__
    assert {column.name for column in table.primary_key} == {"credential_id"}
    foreign_key = next(iter(table.c.credential_id.foreign_keys))
    assert foreign_key.target_fullname == "credential_registry.id"
    assert foreign_key.ondelete in (None, "RESTRICT")
    assert not table.c.first_valid_at.nullable
    assert not table.c.last_valid_at.nullable


def test_valid_retries_upsert_one_archive_and_never_move_first_or_last_time_backwards(session):
    later = datetime(2026, 10, 17, 10, 45, tzinfo=timezone.utc)
    earlier = datetime(2026, 10, 16, 9, 30, tzinfo=timezone.utc)
    asyncio.run(remember_valid_key(session, 41, observed_at=later))
    asyncio.run(remember_valid_key(session, 41, observed_at=earlier))

    assert session.execute.await_count == 2
    for call, observed_at in zip(session.execute.await_args_list, [later, earlier], strict=True):
        compiled = call.args[0].compile(dialect=postgresql.dialect())
        query = str(compiled)
        assert "INSERT INTO valid_key_records" in query
        assert "ON CONFLICT (credential_id) DO UPDATE SET" in query
        assert "least(valid_key_records.first_valid_at, excluded.first_valid_at)" in query
        assert "greatest(valid_key_records.last_valid_at, excluded.last_valid_at)" in query
        assert compiled.params["credential_id"] == 41
        assert compiled.params["first_valid_at"] == observed_at
        assert compiled.params["last_valid_at"] == observed_at
    session.commit.assert_not_awaited()


@pytest.mark.parametrize("status,usable,should_remember", [
    ("valid", True, True),
    ("valid", False, True),
    ("invalid", False, False),
    ("rate_limited", False, False),
    ("usage_limit_reached", False, False),
])
def test_upload_remembers_only_valid_results_and_other_states_never_delete_history(monkeypatch, session, status, usable, should_remember):
    observed_at = datetime(2026, 10, 16, 9, 30, tzinfo=timezone.utc)
    session.scalar.return_value = ValidationBatch(id=29, total=1)
    bucket = credentials.result_bucket(status)
    counts = (1, *(int(bucket == name) for name in ("valid", "usage_limit_reached", "insufficient_credit", "invalid")))
    session.execute.side_effect = [
        SimpleNamespace(), SimpleNamespace(scalar_one=lambda: 41), SimpleNamespace(),
        SimpleNamespace(one=lambda: counts),
    ]
    remember = AsyncMock()
    monkeypatch.setattr(credentials, "remember_valid_key", remember)
    monkeypatch.setattr(credentials, "utcnow", lambda: observed_at)
    item = ValidationResultInput.model_validate({
        "index": 1, "status": status, "usable": usable,
        "credential": {"provider": "OpenAI", "kind": "openai", "secret": "synthetic-only-secret"},
    })

    assert asyncio.run(credentials.process_batch_results(session, 29, [item])) == 1

    if should_remember:
        remember.assert_awaited_once_with(session, 41, observed_at=observed_at)
    else:
        remember.assert_not_awaited()
    assert all("DELETE" not in str(call.args[0]) for call in session.execute.await_args_list)
    session.commit.assert_awaited_once()


def test_archive_list_retains_current_invalid_key_and_uses_current_status_dictionary_metadata(session):
    first_valid_at = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
    last_valid_at = datetime(2026, 10, 8, 10, 45, tzinfo=timezone.utc)
    updated_at = datetime(2026, 10, 16, 11, 0, tzinfo=timezone.utc)
    archive = ValidKeyRecord(credential_id=41, first_valid_at=first_valid_at, last_valid_at=last_valid_at)
    credential = Credential(
        id=41, key_prefix="synthetic-prefix", masked="synthetic…test", provider="OpenAI",
        storage_type="openai_official", status="invalid", result_bucket="invalid", updated_at=updated_at,
        source_ip="2001:db8::1", encrypted_secret="synthetic-full-secret-must-not-be-returned",
    )
    status = CredentialStatus(value="invalid", name="字典当前名称", tone="neutral", sort_order=3)
    session.scalar.return_value = 101
    session.execute.return_value = SimpleNamespace(all=lambda: [(archive, credential, status)])

    result = asyncio.run(list_valid_key_records(
        session, provider="OpenAI", storage_type="openai_official", query="ab%_\\end", page=99,
    ))

    assert result == {
        "records": [{
            "credentialId": 41, "keyPrefix": "synthetic-prefix", "masked": "synthetic…test", "provider": "OpenAI",
            "storageType": "openai_official", "status": "invalid", "statusName": "字典当前名称", "statusTone": "neutral",
            "firstValidAt": first_valid_at.isoformat(), "lastValidAt": last_valid_at.isoformat(),
            "updatedAt": updated_at.isoformat(), "ipAddress": "2001:db8::1",
        }],
        "total": 101, "page": 2, "totalPages": 2, "pageSize": 100,
    }
    assert "synthetic-full-secret-must-not-be-returned" not in str(result)
    count_statement = session.scalar.await_args.args[0]
    records_statement = session.execute.await_args.args[0]
    for statement in (count_statement, records_statement):
        compiled = statement.compile()
        assert "ab\\%\\_\\\\end%" in compiled.params.values()
        query = sql(statement)
        assert "JOIN credentials ON credentials.id = valid_key_records.credential_id" in query
        assert "credentials.provider = 'OpenAI'" in query
        assert "credentials.storage_type = 'openai_official'" in query
        where_sql = query.split("WHERE", 1)[1]
        assert "credentials.status = 'valid'" not in where_sql
        assert "credentials.result_bucket = 'valid'" not in where_sql
    record_query = sql(records_statement)
    assert "JOIN credential_statuses ON credential_statuses.value = credentials.status" in record_query
    assert "ORDER BY valid_key_records.last_valid_at DESC, valid_key_records.credential_id DESC" in record_query
    assert "LIMIT 100 OFFSET 100" in record_query


@pytest.mark.parametrize("requested_page", [-1, 1, 99])
def test_empty_valid_archive_clamps_to_first_page(session, requested_page):
    session.scalar.return_value = 0
    session.execute.return_value = SimpleNamespace(all=lambda: [])
    result = asyncio.run(list_valid_key_records(session, page=requested_page))
    assert result == {"records": [], "total": 0, "page": 1, "totalPages": 1, "pageSize": 100}
    assert "LIMIT 100 OFFSET 0" in sql(session.execute.await_args.args[0])


def add_synthetic_archive(db, identifier, *, provider="OpenAI", storage_type="openai_official", source_ip=None, prefix=None, status="invalid"):
    when = datetime(2026, 10, 17, tzinfo=timezone.utc)
    fingerprint = f"synthetic-fingerprint-{identifier}"
    db.add_all([
        CredentialRegistry(id=identifier, provider=provider, storage_type=storage_type, fingerprint=fingerprint),
        Credential(
            id=identifier, fingerprint=fingerprint, provider=provider, storage_type=storage_type, kind="synthetic",
            encrypted_secret=f"synthetic-cipher-{identifier}", key_prefix=prefix or f"other-{identifier}",
            masked="synthetic…test", status=status, result_bucket="valid" if status == "valid" else "invalid",
            source_ip=source_ip, created_at=when, updated_at=when + timedelta(days=1),
        ),
        ValidKeyRecord(credential_id=identifier, first_valid_at=when, last_valid_at=when + timedelta(seconds=identifier)),
    ])


@pytest.fixture
def valid_archive_ip_database():
    engine = create_engine("sqlite://")
    for model in (CredentialStatus, CredentialRegistry, Credential, ValidKeyRecord):
        model.__table__.create(engine)
    with Session(engine) as db:
        db.add_all([
            CredentialStatus(value="valid", name="可用", tone="valid", sort_order=0),
            CredentialStatus(value="invalid", name="无效", tone="invalid", sort_order=1),
        ])
        add_synthetic_archive(db, 1, source_ip="172.20.0.1")
        add_synthetic_archive(db, 2, source_ip="172.20.0.10", prefix="match-openai", status="valid")
        add_synthetic_archive(db, 3, provider="Anthropic", storage_type="anthropic_official", source_ip="172.20.0.1", prefix="match-anthropic")
        add_synthetic_archive(db, 4, provider="Azure", storage_type="azure_gpt", source_ip="2001:db8::1")
        add_synthetic_archive(db, 5, source_ip="2001:db8::1")
        add_synthetic_archive(db, 6, source_ip="2001:db8::2")
        # Malformed legacy strings deliberately exercise literal wildcard escaping.
        add_synthetic_archive(db, 7, source_ip="172.20.%_\\host", prefix="literal%_\\secret")
        add_synthetic_archive(db, 8, source_ip="172.20.XA\\host", prefix="literalAB\\secret")
        add_synthetic_archive(db, 9, source_ip="172.20.\\literal")
        add_synthetic_archive(db, 10)
        db.commit()
        yield SimpleNamespace(
            scalar=AsyncMock(side_effect=db.scalar), execute=AsyncMock(side_effect=db.execute), sync_session=db,
        )
    engine.dispose()


@pytest.mark.parametrize("options,expected_ids", [
    ({"ip": "172.20.0.1"}, [1]),
    ({"ip": "172.20.0.1", "query": "match-"}, [1]),
    ({"ip": "172.20.0.10", "query": "definitely-not-present"}, [2]),
    ({"ip": "  ", "query": "match-"}, [2]),
    ({"ip": " ::FFFF:172.20.0.1 "}, [1]),
    ({"ip": "2001:0DB8:0000:0000:0000:0000:0000:0001"}, [5]),
    ({"ip": "2001:db8::1%eth0"}, [5]),
    ({"ip": "2001:DB8"}, [6, 5]),
    ({"ip": "172.20.%"}, [7]),
    ({"ip": "172.20.%_\\"}, [7]),
    ({"ip": "172.20.\\"}, [9]),
    ({"ip": " ", "query": "literal%_\\secret"}, [7]),
    ({"ip": "172.20", "provider": "Anthropic"}, [3]),
    ({"ip": "172.20.0.1", "provider": None, "storage_type": "anthropic_official"}, [3]),
    ({"ip": "2001:db8::1", "provider": "Azure", "storage_type": "azure_gpt"}, [4]),
    ({"ip": "8.8.8.8"}, []),
])
def test_archived_ip_search_is_independent_literal_canonical_and_platform_scoped(valid_archive_ip_database, options, expected_ids):
    result = asyncio.run(list_valid_key_records(valid_archive_ip_database, page=1, **{"provider": "OpenAI", **options}))
    assert [row["credentialId"] for row in result["records"]] == expected_ids
    assert result["total"] == len(expected_ids)
    assert result["page"] == 1
    assert result["totalPages"] == 1
    for row in result["records"]:
        if row["credentialId"] != 2:
            assert row["status"] == "invalid"
            assert row["statusName"] == "无效"


def test_ip_search_count_and_pagination_ignore_prefix_but_keep_platform(valid_archive_ip_database):
    db = valid_archive_ip_database.sync_session
    for identifier in range(100, 210):
        add_synthetic_archive(db, identifier, source_ip=f"198.18.0.{identifier - 99}")
    for identifier in range(300, 310):
        add_synthetic_archive(db, identifier, provider="Anthropic", storage_type="anthropic_official", source_ip="198.18.0.1")
    db.commit()

    result = asyncio.run(list_valid_key_records(
        valid_archive_ip_database, provider="OpenAI", query="no-matching-prefix", ip="198.18.0.", page=99,
    ))

    assert result["total"] == 110
    assert result["page"] == 2
    assert result["totalPages"] == 2
    assert result["pageSize"] == 100
    assert [row["credentialId"] for row in result["records"]] == list(range(109, 99, -1))


def test_valid_archive_endpoint_forwards_scope_search_and_page_with_no_cache(monkeypatch, request_api, session):
    request, _ = request_api
    result = {"records": [], "total": 0, "page": 1, "totalPages": 1}
    reader = AsyncMock(return_value=result)
    monkeypatch.setattr(api, "list_valid_key_records", reader)

    response = request("/api/valid-key-records?provider=OpenAI&type=openai_official&q=synthetic&page=2")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == result
    reader.assert_awaited_once_with(session, provider="OpenAI", storage_type="openai_official", query="synthetic", page=2)


@pytest.mark.parametrize("filters", [{"ip": "172.20"}, {"ip": "2001:DB8", "q": "ignored"}, {"ip": "", "q": "prefix"}])
def test_valid_archive_endpoint_passes_optional_ip_without_changing_legacy_shape(monkeypatch, request_api, session, filters):
    request, _ = request_api
    reader = AsyncMock(return_value={"records": [], "total": 0})
    monkeypatch.setattr(api, "list_valid_key_records", reader)
    response = request("/api/valid-key-records?" + str(httpx.QueryParams({"provider": "OpenAI", **filters})))
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    reader.assert_awaited_once_with(session, provider="OpenAI", storage_type=None, query=filters.get("q"), page=1, ip=filters["ip"])


def test_valid_archive_endpoint_rejects_overlong_ip(monkeypatch, request_api):
    request, _ = request_api
    reader = AsyncMock()
    monkeypatch.setattr(api, "list_valid_key_records", reader)
    assert request("/api/valid-key-records?ip=" + "a" * 81).status_code == 422
    reader.assert_not_awaited()


@pytest.mark.parametrize("path", ["/api/valid-key-records", "/api/valid-key-records?ip=172.20&q=ignored"])
def test_valid_archive_requires_login(monkeypatch, request_api, path):
    request, _ = request_api
    monkeypatch.setattr(api, "current_user", AsyncMock(return_value=None))
    reader = AsyncMock()
    monkeypatch.setattr(api, "list_valid_key_records", reader)
    assert request(path).status_code == 401
    reader.assert_not_awaited()
