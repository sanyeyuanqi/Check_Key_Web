import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException, Request
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

import app.main as api
import app.credential_service as credential_service
import app.export_service as export_service
from app.database import AuthIPBlock, AuthUser, ExportRecord
from app.export_service import list_export_records, record_export
from app.request_context import client_ip as export_client_ip


def make_user(role="user"):
    return AuthUser(id=17, username="export-tester", role=role)


def sql(statement):
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


@pytest.fixture
def session():
    db_session = MagicMock(spec=AsyncSession)
    db_session.get.return_value = None
    db_session.get.side_effect = lambda model, *args, **kwargs: None if model is AuthIPBlock else db_session.get.return_value
    return db_session


@pytest.fixture
def request_api(monkeypatch, session):
    user = make_user()
    monkeypatch.setattr(api, "current_user", AsyncMock(return_value=user))

    async def fake_session():
        yield session

    old_overrides = api.app.dependency_overrides.copy()
    api.app.dependency_overrides[api.get_session] = fake_session

    def request(path, *, client=("127.0.0.1", 123), headers=None):
        async def send():
            transport = httpx.ASGITransport(app=api.app, raise_app_exceptions=False, client=client)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
                return await http_client.get(path, headers=headers)

        return asyncio.run(send())

    yield request, user
    api.app.dependency_overrides.clear()
    api.app.dependency_overrides.update(old_overrides)


@pytest.mark.parametrize("ip_address", [None, "203.0.113.7"])
def test_record_export_saves_actor_snapshot_and_scope(session, ip_address):
    user = make_user()
    ip_options = {} if ip_address is None else {"ip_address": ip_address}
    asyncio.run(record_export(
        session, user=user, provider="OpenAI", storage_type="openai_official",
        status="valid", count=3, file_name="openai-valid.txt", **ip_options,
    ))

    session.add.assert_called_once()
    record = session.add.call_args.args[0]
    assert isinstance(record, ExportRecord)
    assert (record.user_id, record.username) == (user.id, user.username)
    assert (record.provider, record.storage_type, record.status) == ("OpenAI", "openai_official", "valid")
    assert (record.credential_count, record.file_name) == (3, "openai-valid.txt")
    assert record.ip_address == ip_address
    assert record.status_mode == "bucket"
    session.commit.assert_awaited_once()


@pytest.mark.parametrize("role,requested_page,expected_page,offset,ip_address", [
    ("admin", 99, 3, 100, "2001:db8::1"),
    ("user", 2, 2, 50, None),
])
def test_records_scope_pagination_and_serialization(monkeypatch, session, role, requested_page, expected_page, offset, ip_address):
    user = make_user(role)
    ip_region = "测试地区" if ip_address else None
    resolver = MagicMock(return_value=ip_region)
    monkeypatch.setattr(export_service, "resolve_ip_region", resolver)
    created_at = datetime(2026, 10, 16, 9, 30, tzinfo=timezone.utc)
    record = ExportRecord(
        id=42, user_id=user.id, username="original-account-name", provider="OpenAI",
        storage_type=None, status="valid", credential_count=12,
        file_name="openai-valid.txt", created_at=created_at, ip_address=ip_address,
    )
    session.scalar.return_value = 101
    session.scalars.return_value = SimpleNamespace(all=lambda: [record])

    result = asyncio.run(list_export_records(session, user=user, page=requested_page))

    assert result == {
        "records": [{
            "id": 42, "createdAt": created_at.isoformat(), "username": "original-account-name",
            "provider": "OpenAI", "storageType": None, "status": "valid", "count": 12,
            "fileName": "openai-valid.txt", "ipAddress": ip_address, "ipRegion": ip_region, "statusNames": ["可用密钥"],
        }],
        "total": 101, "page": expected_page, "totalPages": 3, "pageSize": 50,
    }
    count_query = sql(session.scalar.call_args.args[0])
    records_query = sql(session.scalars.call_args.args[0])
    for query in (count_query, records_query):
        if role == "admin":
            assert "WHERE" not in query
        else:
            assert "WHERE export_records.user_id = 17" in query
    assert "ORDER BY export_records.created_at DESC, export_records.id DESC" in records_query
    assert f"LIMIT 50 OFFSET {offset}" in records_query
    resolver.assert_called_once_with(ip_address)


def test_empty_records_have_first_page_and_no_rows(monkeypatch, session):
    resolver = MagicMock()
    monkeypatch.setattr(export_service, "resolve_ip_region", resolver)
    session.scalar.return_value = 0
    session.scalars.return_value = SimpleNamespace(all=lambda: [])
    result = asyncio.run(list_export_records(session, user=make_user(), page=99))
    assert result == {"records": [], "total": 0, "page": 1, "totalPages": 1, "pageSize": 50}
    assert "LIMIT 50 OFFSET 0" in sql(session.scalars.call_args.args[0])
    resolver.assert_not_called()


def test_regions_resolve_distinct_stored_ips_without_changing_record_order(monkeypatch, session):
    addresses = ["8.8.8.8", "172.20.0.1", "8.8.8.8", None]
    regions = {"8.8.8.8": "测试公网地区", "172.20.0.1": "内网", None: None}
    resolver = MagicMock(side_effect=regions.__getitem__)
    monkeypatch.setattr(export_service, "resolve_ip_region", resolver)
    records = [ExportRecord(
        id=index, user_id=17, username="export-tester", provider="OpenAI", storage_type=None,
        status="valid", status_mode="bucket", credential_count=0, file_name="openai-valid.txt",
        ip_address=address, created_at=datetime(2026, 10, 16, tzinfo=timezone.utc),
    ) for index, address in enumerate(addresses, 1)]
    session.scalar.return_value = len(records)
    session.scalars.return_value = SimpleNamespace(all=lambda: records)

    result = asyncio.run(list_export_records(session, user=make_user("admin"), page=1))

    assert [row["id"] for row in result["records"]] == [1, 2, 3, 4]
    assert [row["ipAddress"] for row in result["records"]] == addresses
    assert [row["ipRegion"] for row in result["records"]] == [regions[address] for address in addresses]
    assert resolver.call_count == 3
    assert {call.args[0] for call in resolver.call_args_list} == set(addresses)


class SQLiteAsyncSession:
    """Run the real service's SQL against a disposable, in-memory SQLite DB."""

    def __init__(self, sync_session):
        self.sync_session = sync_session

    async def scalar(self, statement):
        return self.sync_session.scalar(statement)

    async def scalars(self, statement):
        return self.sync_session.scalars(statement)

    async def execute(self, statement):
        return self.sync_session.execute(statement)


@pytest.fixture
def export_filter_database(monkeypatch):
    monkeypatch.setattr(export_service, "resolve_ip_region", lambda address: "synthetic-region" if address else None)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    AuthUser.__table__.create(engine)
    ExportRecord.__table__.create(engine)
    with Session(engine) as local_session:
        local_session.add_all([
            AuthUser(id=user_id, username=f"user{user_id}", username_normalized=f"user{user_id}", password_hash="synthetic-only")
            for user_id in (17, 18)
        ])
        samples = [
            (1, 17, "Alice", "172.20.0.1"),
            (2, 17, "ALICE-helper", "172.20.0.10"),
            (3, 18, "Alice", "172.20.0.1"),
            (4, 18, "Bob", "2001:db8::1"),
            # Synthetic legacy text also checks literal wildcard/backslash handling.
            (5, 17, "literal%_\\name", "172.20.%_\\host"),
            (6, 17, "literalAB\\name", "172.20.XA\\host"),
            (7, 17, "slash/user", "172.20.\\literal"),
            (8, 17, "IPv6-self", "2001:db8::2"),
            (9, 17, "No-IP", None),
        ]
        local_session.add_all([
            ExportRecord(
                id=record_id, user_id=user_id, username=username, status="valid", status_mode="bucket",
                credential_count=1, file_name="synthetic.txt", ip_address=address,
                created_at=datetime(2026, 10, 16, tzinfo=timezone.utc) + timedelta(seconds=record_id),
            )
            for record_id, user_id, username, address in samples
        ])
        local_session.commit()
        yield SQLiteAsyncSession(local_session)
    engine.dispose()


@pytest.mark.parametrize("filters,expected_ids", [
    ({"username": "  aLiCe  "}, [3, 2, 1]),
    ({"ip": "172.20.0.1"}, [3, 1]),
    ({"ip": "  ::FFFF:172.20.0.1  "}, [3, 1]),
    ({"ip": "2001:0DB8:0000:0000:0000:0000:0000:0001"}, [4]),
    ({"ip": "2001:db8::1%eth0"}, [4]),
    ({"ip": "2001:DB8"}, [8, 4]),
    ({"username": "alice", "ip": "172.20.0.1"}, [3, 1]),
    ({"username": "Bob", "ip": "172.20"}, []),
    ({"username": "literal%_\\name"}, [5]),
    ({"username": "literal%_"}, [5]),
    ({"username": "slash/user"}, [7]),
    ({"ip": "172.20.%"}, [5]),
    ({"ip": "172.20.%_\\"}, [5]),
    ({"ip": "172.20.\\"}, [7]),
    ({"username": " \t ", "ip": " "}, list(range(9, 0, -1))),
])
def test_export_record_filters_execute_literal_combined_and_canonical_ip_queries(export_filter_database, filters, expected_ids):
    result = asyncio.run(list_export_records(export_filter_database, user=make_user("admin"), page=1, **filters))
    assert [row["id"] for row in result["records"]] == expected_ids
    assert result["total"] == len(expected_ids)
    assert result["page"] == 1
    assert result["totalPages"] == 1
    assert all(row["ipRegion"] == ("synthetic-region" if row["ipAddress"] else None) for row in result["records"])


@pytest.mark.parametrize("filters,expected_ids", [
    ({"username": "Alice"}, [2, 1]),
    ({"username": "Alice", "ip": "172.20.0.1"}, [1]),
    ({"username": "Bob"}, []),
    ({"ip": "2001:db8::1"}, []),
    ({"username": "Bob", "ip": "2001:db8::1"}, []),
])
def test_account_and_ip_filters_cannot_override_member_ownership(export_filter_database, filters, expected_ids):
    result = asyncio.run(list_export_records(export_filter_database, user=make_user(), page=1, **filters))
    assert [row["id"] for row in result["records"]] == expected_ids
    assert result["total"] == len(expected_ids)


def test_filtered_count_and_rows_share_scope_before_pagination_and_page_clamping(export_filter_database):
    export_filter_database.sync_session.add_all([
        ExportRecord(
            id=record_id, user_id=17, username="Batch-user", status="valid", status_mode="bucket",
            credential_count=0, file_name="synthetic.txt", ip_address=f"198.18.0.{record_id - 99}",
            created_at=datetime(2026, 10, 17, tzinfo=timezone.utc) + timedelta(seconds=record_id),
        )
        for record_id in range(100, 160)
    ])
    export_filter_database.sync_session.commit()
    result = asyncio.run(list_export_records(
        export_filter_database, user=make_user(), page=99, username="batch", ip="198.18.0.",
    ))
    assert result["total"] == 60
    assert result["totalPages"] == 2
    assert result["page"] == 2
    assert result["pageSize"] == 50
    assert [row["id"] for row in result["records"]] == list(range(109, 99, -1))


@pytest.mark.parametrize("values", [[], ["synthetic-key-one", "synthetic-key-two"]])
def test_export_success_records_exact_actor_scope_and_count(monkeypatch, request_api, session, values):
    request, user = request_api
    exporter = AsyncMock(return_value=values)
    logger = AsyncMock()
    monkeypatch.setattr(api, "export_credentials", exporter)
    monkeypatch.setattr(api, "record_export", logger)

    response = request("/api/export?provider=OpenAI&status=valid&type=openai_official")

    assert response.status_code == 200
    assert response.text == "\n".join(values)
    assert response.headers["Content-Disposition"] == 'attachment; filename="openai-valid.txt"'
    assert response.headers["Cache-Control"] == "no-store"
    exporter.assert_awaited_once_with(session, provider="OpenAI", status="valid", storage_type="openai_official")
    logger.assert_awaited_once_with(
        session, user=user, provider="OpenAI", storage_type="openai_official",
        status="valid", count=len(values), file_name="openai-valid.txt", ip_address="127.0.0.1",
    )


def test_unscoped_export_preserves_legacy_filename_and_records_whole_scope(monkeypatch, request_api, session):
    request, user = request_api
    logger = AsyncMock()
    monkeypatch.setattr(api, "export_credentials", AsyncMock(return_value=["synthetic-key"]))
    monkeypatch.setattr(api, "record_export", logger)

    response = request("/api/export?status=valid")

    assert response.status_code == 200
    assert response.headers["Content-Disposition"] == 'attachment; filename="all-valid-keys.txt"'
    logger.assert_awaited_once_with(
        session, user=user, provider=None, storage_type=None,
        status="valid", count=1, file_name="all-valid-keys.txt", ip_address="127.0.0.1",
    )


@pytest.mark.parametrize("selected,canonical,provider,storage_type,file_name", [
    ("valid,usage_limit_reached", "valid,usage_limit_reached", "OpenAI", "openai_official", "openai-valid-usage_limit_reached.txt"),
    ("invalid,valid,invalid", "valid,invalid", None, None, "all-valid-invalid-keys.txt"),
    (
        "invalid,insufficient_credit,usage_limit_reached,valid",
        "valid,usage_limit_reached,insufficient_credit,invalid",
        "AWS Bedrock", "aws_access", "aws-valid-usage_limit_reached-insufficient_credit-invalid.txt",
    ),
    ("valid,valid", "valid", "OpenAI", None, "openai-valid.txt"),
    ("all", "all", None, None, "all-keys.txt"),
])
def test_export_merges_selection_into_one_text_download_and_one_canonical_record(
    monkeypatch, request_api, session, selected, canonical, provider, storage_type, file_name,
):
    request, user = request_api
    values = ["synthetic-first-secret", "synthetic-second-secret", "synthetic-third-secret"]
    exporter = AsyncMock(return_value=values)
    logger = AsyncMock()
    monkeypatch.setattr(api, "export_credentials", exporter)
    monkeypatch.setattr(api, "record_export", logger)
    query = str(httpx.QueryParams({
        "status": selected,
        **({"provider": provider} if provider else {}),
        **({"type": storage_type} if storage_type else {}),
    }))

    response = request("/api/export?" + query)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["Content-Disposition"] == f'attachment; filename="{file_name}"'
    assert response.headers["Cache-Control"] == "no-store"
    assert response.text == "\n".join(values)
    exporter.assert_awaited_once_with(session, provider=provider, status=canonical, storage_type=storage_type)
    logger.assert_awaited_once_with(
        session, user=user, provider=provider, storage_type=storage_type,
        status=canonical, count=3, file_name=file_name, ip_address="127.0.0.1",
    )


@pytest.mark.parametrize("status,expected_filter", [
    ("valid,usage_limit_reached", "credentials.result_bucket IN ('valid', 'usage_limit_reached')"),
    (
        "valid,usage_limit_reached,insufficient_credit,invalid",
        "credentials.result_bucket IN ('valid', 'usage_limit_reached', 'insufficient_credit', 'invalid')",
    ),
    ("valid", "credentials.result_bucket = 'valid'"),
    ("all", None),
])
def test_export_service_fetches_combined_statuses_once_and_preserves_scope_and_order(
    monkeypatch, session, status, expected_filter,
):
    session.scalars.return_value = SimpleNamespace(all=lambda: [
        SimpleNamespace(encrypted_secret="ciphertext-first"),
        SimpleNamespace(encrypted_secret="ciphertext-second"),
    ])
    decrypt = MagicMock(side_effect=lambda value: "synthetic-decrypted-" + value)
    monkeypatch.setattr(credential_service, "decrypt_secret", decrypt)

    values = asyncio.run(credential_service.export_credentials(
        session, status=status, provider="OpenAI", storage_type="openai_official",
    ))

    session.scalars.assert_awaited_once()
    query = sql(session.scalars.await_args.args[0])
    assert "credentials.provider = 'OpenAI'" in query
    assert "credentials.storage_type = 'openai_official'" in query
    assert "ORDER BY credentials.created_at DESC, credentials.id DESC" in query
    if expected_filter is None:
        assert "result_bucket" not in query.split("WHERE", 1)[1]
    else:
        assert expected_filter in query
    assert values == ["synthetic-decrypted-ciphertext-first", "synthetic-decrypted-ciphertext-second"]
    assert decrypt.call_count == 2


@pytest.mark.parametrize("client,expected", [
    (("203.0.113.7", 123), "203.0.113.7"),
    (("2001:0db8:0000:0000:0000:0000:0000:0001", 123), "2001:db8::1"),
    (("::ffff:203.0.113.7", 123), "203.0.113.7"),
    (("fe80::1%eth0", 123), "fe80::1"),
    (("not-an-ip-address", 123), None),
    (None, None),
])
def test_export_client_ip_normalizes_addresses_and_handles_missing_peer(client, expected):
    request = Request({"type": "http", "client": client, "headers": []})
    assert export_client_ip(request) == expected


@pytest.mark.parametrize("client,expected", [
    (("203.0.113.7", 123), "203.0.113.7"),
    (("2001:0db8:0000:0000:0000:0000:0000:0001", 123), "2001:db8::1"),
])
def test_export_records_peer_ip_and_ignores_forged_forwarding_headers(monkeypatch, request_api, client, expected):
    request, _ = request_api
    logger = AsyncMock()
    monkeypatch.setattr(api, "export_credentials", AsyncMock(return_value=[]))
    monkeypatch.setattr(api, "record_export", logger)

    response = request(
        "/api/export?status=valid", client=client,
        headers={"X-Forwarded-For": "198.51.100.10", "Forwarded": "for=198.51.100.10"},
    )

    assert response.status_code == 200
    assert logger.await_args.kwargs["ip_address"] == expected


@pytest.mark.parametrize("query,status_code", [
    ("status=unknown", 400),
    ("status=", 400),
    ("status=valid,unknown", 400),
    ("status=valid,,invalid", 400),
    ("status=,valid", 400),
    ("status=valid,", 400),
    ("status=all,valid", 400),
    ("status=valid,all", 400),
    ("status=valid&provider=" + "x" * 81, 422),
])
def test_invalid_export_does_not_read_credentials_or_write_record(monkeypatch, request_api, query, status_code):
    request, _ = request_api
    exporter, logger = AsyncMock(), AsyncMock()
    monkeypatch.setattr(api, "export_credentials", exporter)
    monkeypatch.setattr(api, "record_export", logger)
    assert request("/api/export?" + query).status_code == status_code
    exporter.assert_not_awaited()
    logger.assert_not_awaited()


def test_failed_export_does_not_write_success_record(monkeypatch, request_api):
    request, _ = request_api
    logger = AsyncMock()
    monkeypatch.setattr(api, "export_credentials", AsyncMock(side_effect=HTTPException(500, "export failed")))
    monkeypatch.setattr(api, "record_export", logger)
    assert request("/api/export?status=valid").status_code == 500
    logger.assert_not_awaited()


def test_record_failure_does_not_return_download_success(monkeypatch, request_api):
    request, _ = request_api
    monkeypatch.setattr(api, "export_credentials", AsyncMock(return_value=["synthetic-private-key"]))
    monkeypatch.setattr(api, "record_export", AsyncMock(side_effect=RuntimeError("record write failed")))
    response = request("/api/export?status=valid")
    assert response.status_code == 500
    assert "Content-Disposition" not in response.headers
    assert "synthetic-private-key" not in response.text


def test_records_endpoint_forwards_authenticated_account_and_rejects_invalid_page(monkeypatch, request_api, session):
    request, user = request_api
    body = {"records": [], "total": 0, "page": 1, "totalPages": 1, "pageSize": 50}
    reader = AsyncMock(return_value=body)
    monkeypatch.setattr(api, "list_export_records", reader)

    response = request("/api/export-records?page=2")
    assert response.status_code == 200
    assert response.json() == body
    assert response.headers["Cache-Control"] == "no-store"
    reader.assert_awaited_once_with(session, user=user, page=2)
    assert request("/api/export-records?page=0").status_code == 422
    assert reader.await_count == 1


@pytest.mark.parametrize("filters", [
    {"username": "Alice"}, {"ip": "172.20"}, {"username": "Alice", "ip": "172.20"}, {"username": "", "ip": ""},
])
def test_records_endpoint_passes_separate_or_combined_optional_filters(monkeypatch, request_api, session, filters):
    request, user = request_api
    reader = AsyncMock(return_value={"records": [], "total": 0})
    monkeypatch.setattr(api, "list_export_records", reader)
    response = request("/api/export-records?" + str(httpx.QueryParams(filters)))
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    reader.assert_awaited_once_with(session, user=user, page=1, **filters)


@pytest.mark.parametrize("filters", [{"username": "a" * 33}, {"ip": "a" * 81}])
def test_records_endpoint_rejects_filters_above_length_limits(monkeypatch, request_api, filters):
    request, _ = request_api
    reader = AsyncMock()
    monkeypatch.setattr(api, "list_export_records", reader)
    assert request("/api/export-records?" + str(httpx.QueryParams(filters))).status_code == 422
    reader.assert_not_awaited()


@pytest.mark.parametrize("path", [
    "/api/export-records", "/api/export-records?username=Alice&ip=172.20", "/api/export?status=valid", "/api/export?status=valid,invalid",
])
def test_export_and_records_require_login(monkeypatch, request_api, path):
    request, _ = request_api
    monkeypatch.setattr(api, "current_user", AsyncMock(return_value=None))
    exporter, logger, reader = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(api, "export_credentials", exporter)
    monkeypatch.setattr(api, "record_export", logger)
    monkeypatch.setattr(api, "list_export_records", reader)

    assert request(path).status_code == 401
    exporter.assert_not_awaited()
    logger.assert_not_awaited()
    reader.assert_not_awaited()
