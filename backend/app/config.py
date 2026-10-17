import os
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Key Control API"
    database_url: str = "postgresql+asyncpg://root:change-me@localhost:5432/key_control"
    redis_url: str = "redis://localhost:6379/0"
    cache_redis_url: str = ""
    cache_ttl_seconds: int = Field(default=10, ge=1, le=300)
    cache_statistics_ttl_seconds: int = Field(default=60, ge=1, le=300)
    cache_timeout_seconds: float = Field(default=0.15, ge=0.01, le=2)
    cache_failure_backoff_seconds: float = Field(default=3, ge=0.1, le=60)
    cache_max_value_bytes: int = Field(default=262_144, ge=1024, le=1_048_576)
    frontend_origins: str = "http://localhost:3000"
    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: str = ""
    session_secret: str = "change-me"
    fernet_key: str = ""
    session_cookie_name: str = "kc_session"
    session_cookie_secure: bool = False
    session_lifetime_seconds: int = 8 * 60 * 60
    login_lock_minutes: int = 15
    max_login_failures: int = 5
    burst_window_seconds: int = Field(default=10, ge=1, le=300)
    burst_request_limit: int = Field(default=20, ge=1, le=10_000)
    redis_stream_name: str = "key-control:validation-results"
    redis_consumer_group: str = "key-control-workers"
    queue_job_max_length: int = 100_000
    queue_batch_records: int = Field(default=1000, ge=1, le=10_000)
    queue_batch_wait_seconds: float = Field(default=30, ge=0.1, le=300)
    queue_claim_idle_seconds: int = Field(default=120, ge=60, le=3600)
    queue_read_messages: int = Field(default=20, ge=1, le=100)
    queue_retry_limit: int = Field(default=5, ge=1, le=100)
    queue_max_payload_bytes: int = Field(default=2_097_152, ge=1024, le=16_777_216)
    export_batch_size: int = Field(default=1000, ge=1, le=5000)
    export_crypto_workers: int = Field(default=min(4, os.cpu_count() or 1), ge=0, le=8)
    export_max_concurrent: int = Field(default=2, ge=1, le=8)
    export_max_pending_files: int = Field(default=16, ge=1, le=100)
    export_max_file_bytes: int = Field(default=536_870_912, ge=1024)
    export_max_total_bytes: int = Field(default=1_073_741_824, ge=1024)
    export_download_ttl_seconds: int = Field(default=600, ge=30, le=3600)

    @property
    def redis_dead_letter_stream(self) -> str:
        return f"{self.redis_stream_name}:dead-letter"

    @property
    def redis_retry_hash(self) -> str:
        return f"{self.redis_stream_name}:retries"

    @property
    def allowed_origins(self) -> list[str]:
        return [value.strip().rstrip("/") for value in self.frontend_origins.split(",") if value.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
