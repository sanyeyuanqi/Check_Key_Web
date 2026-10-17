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
from test_export_records import request_api, session


START = datetime(2026, 10, 1, tzinfo=UTC)
BEFORE = START + timedelta(days=1)
TYPE = "google_vertex_gemini"
DATE_OPTIONS = {"created_from": START, "created_before": BEFORE}


@pytest.fixture
def synthetic_search_database():
    engine = create_engine("sqlite://")
    CredentialStatus.__table__.create(engine)
    Credential.__table__.create(engine)
    with Session(engine) as db:
        db.add_all([
            CredentialStatus(value=value, name=name, tone="neutral", sort_order=index)
            for index, (value, name) in enumerate([
                ("valid", "可用"), ("invalid", "无效"), ("network_error", "网络异常"),
                ("rate_limited", "请求限速"), ("pending", "待检测"),
            ])
        ])

        def row(identifier, *, prefix="target-", status="valid", when=START, provider="Google", storage_type=TYPE):
            return Credential(
                id=identifier, fingerprint=f"synthetic-fingerprint-{identifier}",
                storage_type=storage_type, provider=provider, kind="synthetic",
                encrypted_secret=f"synthetic-cipher-{identifier}", key_prefix=f"{prefix}{identifier}"[:32],
                masked="synthetic…test", status=status, result_bucket=credentials.result_bucket(status),
                created_at=when, updated_at=when,
            )

        db.add_all([
            row(1, status="invalid", when=START - timedelta(seconds=1)),
            row(2, status="network_error", when=BEFORE), row(3),
            row(4, prefix="other-"), row(5, status="pending"),
            row(6, provider="Anthropic", storage_type="anthropic_official"),
            row(7, storage_type="google_vertex_claude"),
            row(8, prefix="%_\\literal-"), row(9, prefix="AXliteral-", status="invalid"),
            *[row(identifier, when=START + timedelta(hours=1)) for identifier in range(10, 111)],
        ])
        db.commit()
        yield SimpleNamespace(
            execute=AsyncMock(side_effect=db.execute), scalar=AsyncMock(side_effect=db.scalar),
            scalars=AsyncMock(side_effect=db.scalars), add_row=lambda item: (db.add(item), db.commit()),
            row=row,
        )
    engine.dispose()


def read(database, *, status="all", query=None, **options):
    return asyncio.run(credentials.list_credentials(
        database, status=status, status_mode="exact", provider=None,
        storage_type=TYPE, query=query, page=options.pop("page", 1), **options,
    ))


@pytest.mark.parametrize("selected", ["valid", "network_error", "pending"])
def test_nonempty_prefix_ignores_status_and_date_and_paginates_all_matching_nonpending_keys(
    synthetic_search_database, selected,
):
    result = read(synthetic_search_database, status=selected, query="  target-  ", page=99, **DATE_OPTIONS)

    assert (result["total"], result["page"], result["totalPages"]) == (104, 2, 2)
    assert [item["id"] for item in result["keys"]] == [11, 10, 3, 1]
    assert result["keys"][-1]["status"] == "invalid"
    first_page = read(synthetic_search_database, status=selected, query="target-", **DATE_OPTIONS)
    assert len(first_page["keys"]) == 100
    assert first_page["keys"][0]["id"] == 2
    assert first_page["keys"][0]["status"] == "network_error"
    assert not {4, 5, 6, 7, 8, 9}.intersection(item["id"] for item in first_page["keys"])


def test_prefix_directory_and_summary_keep_platform_type_export_scope(synthetic_search_database):
    result = read(synthetic_search_database, status="invalid", query="%_\\literal-", **DATE_OPTIONS)

    assert result["total"] == 1
    assert [item["id"] for item in result["keys"]] == [8]
    assert result["summary"] == {
        "total": 107, "valid": 104, "recovery": 0, "insufficient": 0, "invalid": 3,
    }
    assert {item["value"]: item["count"] for item in result["statuses"]} == {
        "valid": 104, "invalid": 2, "network_error": 1, "rate_limited": 0,
    }
    assert sum(item["count"] for item in result["statuses"]) == result["summary"]["total"]


def test_prefix_truncates_to_32_characters_after_trimming(synthetic_search_database):
    synthetic_search_database.add_row(synthetic_search_database.row(120, prefix="x" * 32))
    result = read(synthetic_search_database, status="invalid", query="  " + "x" * 32 + "ignored  ", **DATE_OPTIONS)
    assert [item["id"] for item in result["keys"]] == [120]
    assert result["keys"][0]["keyPrefix"] == "x" * 32


@pytest.mark.parametrize("query", [None, "", " \t "])
def test_empty_prefix_uses_status_and_half_open_dates_together(synthetic_search_database, query):
    result = read(synthetic_search_database, status="valid", query=query, page=99, **DATE_OPTIONS)

    assert (result["total"], result["page"], result["totalPages"]) == (104, 2, 2)
    assert [item["id"] for item in result["keys"]] == [10, 8, 4, 3]
    assert all(item["status"] == "valid" for item in result["keys"])
    assert result["summary"] == {
        "total": 105, "valid": 104, "recovery": 0, "insufficient": 0, "invalid": 1,
    }
    counts = {item["value"]: item["count"] for item in result["statuses"]}
    assert counts == {"valid": 104, "invalid": 1, "network_error": 0, "rate_limited": 0}
    assert counts["valid"] == result["total"]
    assert sum(counts.values()) == result["summary"]["total"]


def test_filter_pending_is_empty_and_invalid_date_filter_does_not_match_outside_range(synthetic_search_database):
    invalid = read(synthetic_search_database, status="invalid", query="   ", **DATE_OPTIONS)
    assert invalid["total"] == 1
    assert [item["id"] for item in invalid["keys"]] == [9]
    pending = read(synthetic_search_database, status="pending", **DATE_OPTIONS)
    assert (pending["total"], pending["page"], pending["totalPages"]) == (0, 1, 1)
    assert pending["keys"] == []


def test_prefix_endpoint_ignores_valid_but_reversed_dates_and_selected_status(monkeypatch, request_api, session):
    request, _ = request_api
    reader = AsyncMock(return_value={"keys": [], "total": 0})
    monkeypatch.setattr(api, "list_credentials", reader)
    response = request("/api/keys?" + str(httpx.QueryParams({
        "q": " target- ", "status": "invalid", "status_mode": "exact", "type": TYPE, "page": 2,
        "created_from": BEFORE.isoformat(), "created_before": START.isoformat(),
    })))
    assert response.status_code == 200
    reader.assert_awaited_once_with(
        session, status="all", status_mode="exact", provider=None, storage_type=TYPE,
        query=" target- ", page=2,
    )


@pytest.mark.parametrize("date", ["not-a-date", "2026-10-01", "2026-10-01T00:00:00"])
def test_prefix_endpoint_preserves_date_syntax_and_timezone_validation(monkeypatch, request_api, date):
    request, _ = request_api
    reader = AsyncMock()
    monkeypatch.setattr(api, "list_credentials", reader)
    response = request("/api/keys?" + str(httpx.QueryParams({"q": "target-", "created_from": date})))
    assert response.status_code == 422
    reader.assert_not_awaited()


def test_whitespace_prefix_endpoint_keeps_date_order_validation(monkeypatch, request_api):
    request, _ = request_api
    reader = AsyncMock()
    monkeypatch.setattr(api, "list_credentials", reader)
    response = request("/api/keys?" + str(httpx.QueryParams({
        "q": "  ", "created_from": BEFORE.isoformat(), "created_before": START.isoformat(),
    })))
    assert response.status_code == 422
    reader.assert_not_awaited()
