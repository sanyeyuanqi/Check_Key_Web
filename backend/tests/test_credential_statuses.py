import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import Text

import app.credential_service as credentials
import app.main as api
from app.database import Credential, CredentialStatus, ExportRecord, ValidationBatch, ValidationBatchItem
from app.export_service import export_filename, list_export_records, record_export
from app.schemas import ValidationResultInput
from app.status_catalog import initial_status_rows
from app.status_service import ensure_credential_statuses, list_credential_statuses, normalize_exact_export_status
from test_export_records import make_user, request_api, session, sql


def test_status_dictionary_references_and_unbounded_export_selection():
    assert CredentialStatus.__tablename__ == "credential_statuses"
    assert CredentialStatus.__table__.c.value.primary_key
    for model in (Credential, ValidationBatchItem):
        assert {key.target_fullname for key in model.__table__.c.status.foreign_keys} == {"credential_statuses.value"}
    assert isinstance(ExportRecord.__table__.c.status.type, Text)


def test_initial_dictionary_matches_detector_names():
    expected = {
        "valid": "可用", "invalid": "无效", "insufficient_credit": "余额不足",
        "usage_limit_reached": "额度待恢复", "billing_error": "账单异常", "permission_error": "权限不足",
        "rate_limited": "请求限速", "request_or_spend_limit_error": "请求/额度限制",
        "server_error": "服务异常", "network_error": "网络异常", "invalid_response": "响应异常",
        "http_error": "接口异常", "no_models": "无可用模型", "expired": "凭证过期",
        "invalid_signature": "签名错误", "model_unavailable": "模型不可用", "internal_error": "检测异常",
    }
    actual = {row["value"]: row["name"] for row in initial_status_rows()}
    assert expected.items() <= actual.items()
    assert "pending" not in actual


def test_unknown_status_registration_preserves_value_and_name_without_reclassifying(session):
    asyncio.run(ensure_credential_statuses(session, ["rate_limited", "future_detector_result", "rate_limited"]))

    session.execute.assert_awaited_once()
    statement = session.execute.await_args.args[0]
    compiled = statement.compile()
    assert "ON CONFLICT (value) DO NOTHING" in str(compiled)
    rows = {}
    for key, value in compiled.params.items():
        column, row_number = key.rsplit("_m", 1)
        rows.setdefault(row_number, {})[column] = value
    by_value = {row["value"]: row for row in rows.values()}
    assert len(by_value) == 2
    assert by_value["rate_limited"]["name"] == "请求限速"
    assert by_value["future_detector_result"]["name"] == "future_detector_result"
    session.commit.assert_not_awaited()


def test_upload_registers_unknown_status_before_credential_fk_insert_and_keeps_raw_value(session):
    session.scalar.return_value = ValidationBatch(id=29, total=1)
    session.execute.side_effect = [
        SimpleNamespace(), SimpleNamespace(scalar_one=lambda: 41), SimpleNamespace(),
        SimpleNamespace(one=lambda: (1, 0, 0, 0, 1)),
    ]
    result = ValidationResultInput.model_validate({
        "index": 1, "status": "future_detector_result", "usable": False,
        "credential": {"provider": "OpenAI", "kind": "openai", "secret": "synthetic-only-secret"},
    })

    assert asyncio.run(credentials.process_batch_results(session, 29, [result])) == 1

    statements = [call.args[0].compile() for call in session.execute.await_args_list]
    assert "INSERT INTO credential_statuses" in str(statements[0])
    assert "future_detector_result" in statements[0].params.values()
    assert "INSERT INTO credentials" in str(statements[1])
    assert statements[1].params["status"] == "future_detector_result"
    assert statements[1].params["result_bucket"] == "invalid"
    assert statements[2].params["status"] == "future_detector_result"


def test_dictionary_left_join_keeps_zero_counts_and_scopes_counts_by_provider_and_type(session):
    session.execute.return_value = SimpleNamespace(all=lambda: [
        ("valid", "可用", "valid", 0, 3),
        ("network_error", "网络异常", "neutral", 12, 0),
    ])

    result = asyncio.run(list_credential_statuses(session, provider="OpenAI", storage_type="openai_official"))

    assert result == [
        {"value": "valid", "name": "可用", "tone": "valid", "sortOrder": 0, "count": 3},
        {"value": "network_error", "name": "网络异常", "tone": "neutral", "sortOrder": 12, "count": 0},
    ]
    query = sql(session.execute.await_args.args[0])
    assert "LEFT OUTER JOIN credentials" in query
    assert "credentials.status = credential_statuses.value" in query
    assert "credentials.provider = 'OpenAI'" in query
    assert "credentials.storage_type = 'openai_official'" in query
    where_sql = query.split("WHERE", 1)[1]
    assert "credential_statuses.value != 'pending'" in where_sql
    assert "credentials.provider" not in where_sql
    assert "credentials.storage_type" not in where_sql
    assert "ORDER BY credential_statuses.sort_order, credential_statuses.value" in query


@pytest.mark.parametrize("selected", ["rate_limited", "all", None])
def test_prefix_key_list_ignores_selected_status_and_keeps_directory_counts_independent_of_search(session, selected):
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    row = Credential(
        id=1, key_prefix="synthetic", provider="OpenAI", storage_type="openai_official", masked="synthetic…",
        status="rate_limited", result_bucket="invalid", created_at=now, updated_at=now, source_ip=None,
    )
    session.execute.side_effect = [
        SimpleNamespace(all=lambda: [("invalid", 3)]),
        SimpleNamespace(all=lambda: [("invalid", "无效", "invalid", 3, 2), ("rate_limited", "请求限速", "recovery", 5, 1)]),
    ]
    session.scalar.return_value = 1
    session.scalars.return_value = SimpleNamespace(all=lambda: [row])

    result = asyncio.run(credentials.list_credentials(
        session, status=selected, status_mode="exact", provider="OpenAI", storage_type="openai_official",
        query="synthetic", page=1,
    ))

    assert result["keys"][0]["status"] == "rate_limited"
    assert [(entry["value"], entry["count"]) for entry in result["statuses"]] == [("invalid", 2), ("rate_limited", 1)]
    row_query = sql(session.scalars.await_args.args[0])
    count_query = sql(session.scalar.await_args.args[0])
    for query in (row_query, count_query):
        where_sql = query.split("WHERE", 1)[1]
        assert "credentials.status =" not in where_sql
        assert "credentials.status != 'pending'" in where_sql
        assert "credentials.result_bucket =" not in where_sql
        assert "credentials.key_prefix LIKE" in where_sql
    directory_query = sql(session.execute.await_args_list[1].args[0])
    assert "LIKE" not in directory_query
    assert "credentials.status = 'rate_limited'" not in directory_query


def test_exact_selection_uses_current_dictionary_order_and_deduplicates_unknown_to_old_code_values(session):
    session.scalars.return_value = SimpleNamespace(all=lambda: ["future_detector_result", "invalid", "rate_limited"])
    result = asyncio.run(normalize_exact_export_status(session, "rate_limited,future_detector_result,invalid,rate_limited"))
    assert result == "future_detector_result,invalid,rate_limited"
    assert "ORDER BY credential_statuses.sort_order, credential_statuses.value" in sql(session.scalars.await_args.args[0])


@pytest.mark.parametrize("selected", ["", "unknown", "invalid,", "invalid,,rate_limited", "all,invalid"])
def test_exact_selection_rejects_missing_or_unregistered_values(session, selected):
    session.scalars.return_value = SimpleNamespace(all=lambda: ["invalid", "rate_limited"])
    with pytest.raises(ValueError):
        asyncio.run(normalize_exact_export_status(session, selected))


def test_exact_selection_rejects_pending_and_excludes_legacy_pending_dictionary_entries(session):
    # The mocked result represents the database directory after its SQL filter.
    session.scalars.return_value = SimpleNamespace(all=lambda: ["valid", "future_detector_result"])
    with pytest.raises(ValueError):
        asyncio.run(normalize_exact_export_status(session, "pending"))
    query = sql(session.scalars.await_args.args[0])
    assert "credential_statuses.value != 'pending'" in query
    assert "DELETE" not in query


def test_exact_export_filters_requested_raw_states_without_other_errors_in_same_legacy_bucket(monkeypatch, session):
    session.scalars.return_value = SimpleNamespace(all=lambda: [SimpleNamespace(encrypted_secret="synthetic-ciphertext")])
    monkeypatch.setattr(credentials, "decrypt_secret", lambda value: "synthetic-selected-secret")

    result = asyncio.run(credentials.export_credentials(
        session, status="invalid,rate_limited", status_mode="exact", provider="OpenAI", storage_type="openai_official",
    ))

    assert result == ["synthetic-selected-secret"]
    session.scalars.assert_awaited_once()
    query = sql(session.scalars.await_args.args[0])
    where_sql = query.split("WHERE", 1)[1]
    assert "credentials.status IN ('invalid', 'rate_limited')" in where_sql
    assert "result_bucket" not in where_sql
    assert "permission_error" not in where_sql
    assert "credentials.provider = 'OpenAI'" in where_sql
    assert "credentials.storage_type = 'openai_official'" in where_sql
    assert "ORDER BY credentials.created_at DESC, credentials.id DESC" in where_sql


def test_exact_export_endpoint_combines_one_txt_and_records_canonical_dictionary_values(monkeypatch, request_api, session):
    request, user = request_api
    session.scalars.return_value = SimpleNamespace(all=lambda: ["invalid", "rate_limited", "permission_error"])
    exporter = AsyncMock(return_value=["synthetic-one", "synthetic-two"])
    logger = AsyncMock()
    monkeypatch.setattr(api, "export_credentials", exporter)
    monkeypatch.setattr(api, "record_export", logger)

    response = request("/api/export?status=rate_limited,invalid,rate_limited&status_mode=exact&provider=OpenAI&type=openai_official")

    assert response.status_code == 200
    assert response.text == "synthetic-one\nsynthetic-two"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Content-Disposition"] == 'attachment; filename="openai-invalid-rate_limited.txt"'
    exporter.assert_awaited_once_with(session, status="invalid,rate_limited", status_mode="exact", provider="OpenAI", storage_type="openai_official")
    logger.assert_awaited_once_with(
        session, user=user, status="invalid,rate_limited", status_mode="exact", provider="OpenAI",
        storage_type="openai_official", count=2, file_name="openai-invalid-rate_limited.txt", ip_address="127.0.0.1",
    )


def test_exact_export_endpoint_rejects_non_dictionary_value_before_reading_credentials(monkeypatch, request_api, session):
    request, _ = request_api
    session.scalars.return_value = SimpleNamespace(all=lambda: ["invalid", "rate_limited"])
    exporter, logger = AsyncMock(), AsyncMock()
    monkeypatch.setattr(api, "export_credentials", exporter)
    monkeypatch.setattr(api, "record_export", logger)
    assert request("/api/export?status=permission_error&status_mode=exact").status_code == 400
    exporter.assert_not_awaited()
    logger.assert_not_awaited()


@pytest.mark.parametrize("provider,selected,expected", [
    ("OpenAI", ",".join("status_" + str(index) * 20 for index in range(8)), "openai-selected.txt"),
    (None, ",".join("status_" + str(index) * 20 for index in range(8)), "all-selected-keys.txt"),
    ("OpenAI", "自定义状态,invalid", "openai-selected.txt"),
    (None, "../unsafe-status", "all-selected-keys.txt"),
    (None, "rate_limited", "all-rate_limited-keys.txt"),
])
def test_exact_status_filenames_are_safe_for_long_or_custom_values(provider, selected, expected):
    assert export_filename(provider, selected) == expected


def test_exact_record_retains_long_value_selection_and_mode(session):
    selected = ",".join("custom_status_" + str(index) * 20 for index in range(8))
    asyncio.run(record_export(
        session, user=make_user(), provider="OpenAI", storage_type=None, status=selected,
        status_mode="exact", count=8, file_name="openai-selected.txt",
    ))
    row = session.add.call_args.args[0]
    assert row.status == selected
    assert row.status_mode == "exact"


def test_export_history_maps_exact_names_from_dictionary_and_preserves_legacy_labels(session):
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    rows = [
        ExportRecord(id=1, username="tester", status="invalid,rate_limited,future_missing", status_mode="exact", credential_count=2, file_name="selected.txt", created_at=now),
        ExportRecord(id=2, username="tester", status="valid,invalid", status_mode="bucket", credential_count=4, file_name="old.txt", created_at=now),
    ]
    session.scalar.return_value = 2
    session.scalars.return_value = SimpleNamespace(all=lambda: rows)
    session.execute.return_value = SimpleNamespace(all=lambda: [("invalid", "无效"), ("rate_limited", "请求限速")])

    result = asyncio.run(list_export_records(session, user=make_user("admin"), page=1))

    assert [row["statusNames"] for row in result["records"]] == [["无效", "请求限速", "future_missing"], ["可用密钥", "不可用密钥"]]
    assert result["records"][0]["status"] == "invalid,rate_limited,future_missing"
    session.execute.assert_awaited_once()


def test_status_directory_endpoint_forwards_provider_type_scope(monkeypatch, request_api, session):
    request, _ = request_api
    statuses = [{"value": "rate_limited", "name": "请求限速", "tone": "recovery", "sortOrder": 5, "count": 0}]
    reader = AsyncMock(return_value=statuses)
    monkeypatch.setattr(api, "list_credential_statuses", reader)

    response = request("/api/credential-statuses?provider=OpenAI&type=openai_official")

    assert response.status_code == 200
    assert response.json() == {"statuses": statuses}
    reader.assert_awaited_once_with(session, provider="OpenAI", storage_type="openai_official")


def test_keys_endpoint_forwards_exact_status_mode(monkeypatch, request_api, session):
    request, _ = request_api
    reader = AsyncMock(return_value={"keys": [], "statuses": []})
    monkeypatch.setattr(api, "list_credentials", reader)
    assert request("/api/keys?status=rate_limited&status_mode=exact&provider=OpenAI").status_code == 200
    reader.assert_awaited_once_with(
        session, status="rate_limited", status_mode="exact", provider="OpenAI", storage_type=None, query=None, page=1,
    )


@pytest.mark.parametrize("path", ["/api/credential-statuses", "/api/export?status=rate_limited,invalid&status_mode=exact"])
def test_dictionary_and_exact_export_require_authentication(monkeypatch, request_api, path):
    request, _ = request_api
    monkeypatch.setattr(api, "current_user", AsyncMock(return_value=None))
    reader, exporter, logger = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(api, "list_credential_statuses", reader)
    monkeypatch.setattr(api, "export_credentials", exporter)
    monkeypatch.setattr(api, "record_export", logger)
    assert request(path).status_code == 401
    reader.assert_not_awaited()
    exporter.assert_not_awaited()
    logger.assert_not_awaited()
