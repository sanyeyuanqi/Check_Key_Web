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

    @property
    def allowed_origins(self) -> list[str]:
        return [value.strip().rstrip("/") for value in self.frontend_origins.split(",") if value.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
