from sqlalchemy import create_mock_engine

from app.credential_service import infer_storage_type, result_bucket
from app.database import Base
from app.main import app
from app.schemas import BatchCompleteInput
from app.security import decrypt_job, encrypt_job


def test_api_contract_is_exposed():
    paths = {route.path for route in app.routes}
    assert {
        "/health",
        "/api/auth/login",
        "/api/auth/me",
        "/api/admin/users",
        "/api/keys",
        "/api/export",
        "/api/device/batches/start",
        "/api/device/batches/{batch_id}/complete",
    } <= paths


def test_storage_and_status_mapping():
    assert infer_storage_type("Anthropic", "anthropic") == "anthropic_official"
    assert infer_storage_type("AWS Bedrock", "aws_access_key") == "aws_access"
    assert infer_storage_type("Azure", "azure", "claude-sonnet-4-6") == "azure_claude"
    assert infer_storage_type("Google", "google_ai_studio") == "google_ai_studio_gemini"
    assert result_bucket("valid") == "valid"
    assert result_bucket("rate_limited") == "invalid"


def test_queue_payload_is_encrypted_round_trip():
    payload = {"batch_id": 1, "results": [{"secret": "sk-test-secret"}]}
    encrypted = encrypt_job(payload)
    assert "sk-test-secret" not in encrypted
    assert decrypt_job(encrypted) == payload


def test_batch_rejects_duplicate_indexes():
    item = {
        "index": 1,
        "status": "valid",
        "usable": True,
        "credential": {"provider": "OpenAI", "kind": "openai", "secret": "sk-test"},
    }
    try:
        BatchCompleteInput.model_validate({"results": [item, item]})
    except ValueError as exc:
        assert "结果序号不能重复" in str(exc)
    else:
        raise AssertionError("duplicate result indexes should fail")


def test_postgresql_schema_compiles():
    statements: list[str] = []

    def collect(sql, *args, **kwargs):
        statements.append(str(sql.compile(dialect=engine.dialect)))

    engine = create_mock_engine("postgresql+asyncpg://", collect)
    Base.metadata.create_all(engine)
    rendered = "\n".join(statements)
    assert "CREATE TABLE auth_users" in rendered
    assert "CREATE TABLE credentials" in rendered
    assert "CREATE TABLE validation_batch_items" in rendered
    assert "idx_credentials_prefix_created" in rendered
