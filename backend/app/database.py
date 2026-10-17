from collections.abc import AsyncIterator
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import settings


class Base(DeclarativeBase):
    pass


class AuthUser(Base):
    __tablename__ = "auth_users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(32), nullable=False)
    username_normalized: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("auth_users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthLoginAttempt(Base):
    __tablename__ = "auth_login_attempts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username_normalized: Mapped[str] = mapped_column(String(32), nullable=False)
    ip_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_login_attempt_ip_created", "ip_hash", "created_at"),
        Index("idx_login_attempt_user_created", "username_normalized", "created_at"),
    )


class ExportRecord(Base):
    __tablename__ = "export_records"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("auth_users.id", ondelete="SET NULL"))
    username: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(80))
    storage_type: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(Text, nullable=False)
    status_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="bucket", server_default="bucket")
    credential_count: Mapped[int] = mapped_column(Integer, nullable=False)
    file_name: Mapped[str] = mapped_column(String(160), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_export_records_created", created_at.desc(), id.desc()),
        Index("idx_export_records_user_created", "user_id", created_at.desc(), id.desc()),
    )


class CredentialStatus(Base):
    __tablename__ = "credential_statuses"

    value: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    tone: Mapped[str] = mapped_column(String(24), nullable=False, default="neutral", server_default="neutral")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=1000, server_default="1000")


class CredentialRegistry(Base):
    __tablename__ = "credential_registry"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    storage_type: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        UniqueConstraint("storage_type", "fingerprint", name="uq_credential_registry_type_fingerprint"),
    )


class Credential(Base):
    __tablename__ = "credentials"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_type: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), primary_key=True)
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    encrypted_secret: Mapped[str] = mapped_column(Text, nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    masked: Mapped[str] = mapped_column(String(300), nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False, default="未分类")
    status: Mapped[str] = mapped_column(String(80), ForeignKey("credential_statuses.value", name="fk_credentials_status"), nullable=False)
    result_bucket: Mapped[str] = mapped_column(String(32), nullable=False)
    usable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    model: Mapped[str | None] = mapped_column(String(300))
    http_status: Mapped[int | None] = mapped_column(Integer)
    attempts: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    region: Mapped[str | None] = mapped_column(String(120))
    message: Mapped[str | None] = mapped_column(Text)
    reset_at: Mapped[str | None] = mapped_column(String(120))
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="desktop")
    source_ip: Mapped[str | None] = mapped_column(String(45))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("provider", "storage_type", "fingerprint", name="uq_credentials_type_fingerprint"),
        Index("idx_credentials_type_bucket_created", "storage_type", "result_bucket", created_at.desc()),
        Index("idx_credentials_provider_created", "provider", created_at.desc()),
        Index("idx_credentials_type_status_created", "storage_type", "status", created_at.desc()),
        Index(
            "idx_credentials_prefix_created",
            "key_prefix",
            created_at.desc(),
            postgresql_ops={"key_prefix": "varchar_pattern_ops"},
        ),
        Index(
            "idx_credentials_status_created",
            "status",
            created_at.desc(),
        ),
        {"postgresql_partition_by": "LIST (provider)"},
    )


class ValidKeyRecord(Base):
    __tablename__ = "valid_key_records"

    credential_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("credential_registry.id", name="fk_valid_key_records_registry"), primary_key=True)
    first_valid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_valid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("idx_valid_key_records_recent", last_valid_at.desc(), credential_id.desc()),
    )


class ValidationBatch(Base):
    __tablename__ = "validation_batches"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_label: Mapped[str] = mapped_column(String(120), nullable=False)
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recovery_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    insufficient_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    invalid_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    received_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("idx_batches_node_started", "node_id", started_at.desc()),)


class ValidationBatchItem(Base):
    __tablename__ = "validation_batch_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("validation_batches.id", ondelete="CASCADE"), nullable=False)
    credential_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("credential_registry.id", name="fk_batch_items_registry", ondelete="CASCADE"), nullable=False)
    item_index: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(80), ForeignKey("credential_statuses.value", name="fk_batch_items_status"), nullable=False)
    result_bucket: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        UniqueConstraint("batch_id", "item_index", name="uq_batch_item_index"),
        Index("idx_batch_items_credential", "credential_id"),
    )


engine = create_async_engine(settings.database_url, pool_pre_ping=True, pool_size=10, max_overflow=20)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session
