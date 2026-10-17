import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.credential_service as credentials
import app.main as api
from app.database import Credential, CredentialStatus
from app.export_service import export_filename
from app.status_service import list_credential_statuses
from test_export_records import mock_query_export, request_api, session, sql


START = datetime(2026, 9, 1, 16, tzinfo=UTC)
BEFORE = START + timedelta(days=1)
DATE_OPTIONS = {"created_from": START, "created_before": BEFORE}


@pytest.fixture
def synthetic_database():
    # Run real selection/count/pagination SQL against synthetic, in-memory rows only.
    engine = create_engine("sqlite://")
    CredentialStatus.__table__.create(engine)
    Credential.__table__.create(engine)
    with Session(engine) as db:
        db.add_all([
            CredentialStatus(value=value, name=name, tone="neutral", sort_order=index)
            for index, (value, name) in enumerate([
                ("valid", "可用"), ("network_error", "网络异常"), ("invalid", "无效"),
            ])
        ])

        def row(identifier, when, *, status="valid", provider="OpenAI", storage_type="openai_official"):
            return Credential(
                id=identifier, fingerprint=f"synthetic-fingerprint-{identifier}",
                provider=provider, storage_type=storage_type, kind="openai",
                encrypted_secret=f"synthetic-cipher-{identifier}", key_prefix=f"synthetic-{identifier}",
                masked="synthetic…test", status=status,
                result_bucket="valid" if status == "valid" else "invalid",
                created_at=when, updated_at=BEFORE + timedelta(days=1),
            )

        db.add_all([
            row(1, START), row(2, BEFORE - timedelta(microseconds=1)),
            row(3, START + timedelta(hours=12), status="network_error"),
            row(4, START - timedelta(microseconds=1)), row(5, BEFORE),
            row(6, START, provider="Anthropic"), row(7, START, storage_type="azure_gpt"),
            *[row(identifier, START + timedelta(hours=1)) for identifier in range(10, 111)],
        ])
        db.commit()
        yield SimpleNamespace(
            execute=AsyncMock(side_effect=db.execute), scalar=AsyncMock(side_effect=db.scalar),
            scalars=AsyncMock(side_effect=db.scalars),
        )
    engine.dispose()


def test_upload_date_half_open_bounds_scope_status_counts_and_pagination(synthetic_database):
    result = asyncio.run(credentials.list_credentials(
        synthetic_database, status="valid", status_mode="exact", provider="OpenAI",
        storage_type="openai_official", query=None, page=2, **DATE_OPTIONS,
    ))

    assert (result["total"], result["page"], result["totalPages"]) == (103, 2, 2)
    assert [item["id"] for item in result["keys"]] == [11, 10, 1]
    assert result["summary"] == {
        "total": 104, "valid": 103, "recovery": 0, "insufficient": 0, "invalid": 1,
    }
    assert {item["value"]: item["count"] for item in result["statuses"]} == {
        "valid": 103, "network_error": 1, "invalid": 0,
    }

    first_page = asyncio.run(credentials.list_credentials(
        synthetic_database, status="valid", status_mode="exact", provider="OpenAI",
        storage_type="openai_official", query=None, page=1, **DATE_OPTIONS,
    ))
    assert first_page["keys"][0]["id"] == 2
    assert not {4, 5, 6, 7}.intersection(item["id"] for item in first_page["keys"])


def test_date_filtered_status_directory_keeps_zero_counts_and_independent_states(synthetic_database):
    result = asyncio.run(list_credential_statuses(
        synthetic_database, provider="OpenAI", storage_type="openai_official", **DATE_OPTIONS,
    ))
    assert [(item["value"], item["count"]) for item in result] == [
        ("valid", 103), ("network_error", 1), ("invalid", 0),
    ]


def test_export_date_range_matches_list_and_combines_exact_states(monkeypatch, synthetic_database):
    monkeypatch.setattr(credentials, "decrypt_secret", lambda value: value.replace("cipher", "secret"))
    values = asyncio.run(credentials.export_credentials(
        synthetic_database, status="valid,network_error", status_mode="exact", provider="OpenAI",
        storage_type="openai_official", **DATE_OPTIONS,
    ))

    assert len(values) == 104
    assert values[:2] == ["synthetic-secret-2", "synthetic-secret-3"]
    assert values[-1] == "synthetic-secret-1"
    assert all(f"synthetic-secret-{identifier}" not in values for identifier in (4, 5, 6, 7))


@pytest.mark.parametrize("bound", ["created_from", "created_before"])
def test_one_sided_date_filters_are_supported(monkeypatch, synthetic_database, bound):
    monkeypatch.setattr(credentials, "decrypt_secret", lambda value: value)
    values = asyncio.run(credentials.export_credentials(
        synthetic_database, status="all", provider="OpenAI", storage_type="openai_official",
        **{bound: DATE_OPTIONS[bound]},
    ))
    assert len(values) == 105
    assert ("synthetic-cipher-4" in values) == (bound == "created_before")
    assert ("synthetic-cipher-5" in values) == (bound == "created_from")


@pytest.mark.parametrize("path,target", [
    ("/api/keys?status=all&status_mode=exact", "list_credentials"),
    ("/api/credential-statuses?", "list_credential_statistics"),
    ("/api/export?status=valid", "write_query_export"),
])
@pytest.mark.parametrize("options", [
    {"created_from": "2026-09-02"},
    {"created_before": "2026-09-03T00:00:00"},
    {"created_from": "not-a-date"},
    {"created_from": BEFORE.isoformat(), "created_before": START.isoformat()},
    {"created_from": START.isoformat(), "created_before": START.isoformat()},
])
def test_api_rejects_naive_invalid_or_reversed_dates_before_querying(
    monkeypatch, request_api, path, target, options,
):
    request, _ = request_api
    reader, logger = AsyncMock(), AsyncMock()
    monkeypatch.setattr(api, target, reader)
    monkeypatch.setattr(api, "record_export", logger)
    response = request(path + "&" + str(httpx.QueryParams(options)))
    assert response.status_code == 422
    reader.assert_not_awaited()
    logger.assert_not_awaited()


@pytest.mark.parametrize("path,target,result", [
    ("/api/keys?status=all&status_mode=exact&page=2", "list_credentials", {"total": 0, "keys": []}),
    ("/api/credential-statuses?", "list_credential_statistics", []),
    ("/api/export?status=valid&status_mode=exact", "write_query_export", ["synthetic-only-secret"]),
])
def test_api_passes_timezone_aware_date_bounds_and_preserves_export_audit(
    monkeypatch, request_api, session, path, target, result,
):
    request, user = request_api
    if target == "list_credential_statistics":
        result = SimpleNamespace(data={"statuses": result}, status="BYPASS")
    reader = mock_query_export(monkeypatch, result) if target == "write_query_export" else AsyncMock(return_value=result)
    logger = AsyncMock()
    monkeypatch.setattr(api, target, reader)
    monkeypatch.setattr(api, "record_export", logger)
    monkeypatch.setattr(api, "normalize_exact_export_status", AsyncMock(return_value="valid"))
    # A local +08:00 date boundary must remain the same instant after request parsing.
    response = request(path + "&" + str(httpx.QueryParams({
        "created_from": "2026-09-02T00:00:00+08:00", "created_before": BEFORE.isoformat(),
    })))
    assert response.status_code == 200
    if target == "write_query_export":
        statement = reader.await_args.args[1]
        bounds = [value for value in statement.compile().params.values() if isinstance(value, datetime)]
        assert bounds == [START, BEFORE]
        assert "credentials.created_at >=" in sql(statement)
        assert "credentials.created_at <" in sql(statement)
        assert response.text == "synthetic-only-secret"
        assert logger.await_args.kwargs["status"] == "valid"
        assert logger.await_args.kwargs["count"] == 1
        assert logger.await_args.kwargs["user"] == user
        assert logger.await_args.kwargs["file_name"] == export_filename(None, "valid", **DATE_OPTIONS)
        assert "from-2026-09-01T160000Z-before-2026-09-02T160000Z" in response.headers["Content-Disposition"]
    else:
        assert reader.await_args.kwargs["created_from"] == START
        assert reader.await_args.kwargs["created_before"] == BEFORE


def test_export_date_filename_preserves_legacy_names_and_maximum_audit_length():
    assert export_filename("OpenAI", "valid") == "openai-valid.txt"
    dated = export_filename("OpenAI", "valid", **DATE_OPTIONS)
    assert dated == "openai-valid-from-2026-09-01T160000Z-before-2026-09-02T160000Z.txt"
    long_name = export_filename("AWS Bedrock", "x" * 100, **DATE_OPTIONS)
    assert len(long_name) <= 160
    assert long_name.endswith("-before-2026-09-02T160000Z.txt")
