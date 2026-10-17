"""Initial PostgreSQL schema.

Revision ID: 20260902_0001
Revises:
Create Date: 2026-09-02

This snapshot is intentionally independent of current ORM models. Later
migrations must be able to start from the original single-table schema.
"""

from alembic import op

revision = "20260902_0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA_SQL = (
    """CREATE TABLE IF NOT EXISTS auth_login_attempts (
	id BIGSERIAL NOT NULL,
	username_normalized VARCHAR(32) NOT NULL,
	ip_hash VARCHAR(64) NOT NULL,
	succeeded BOOLEAN NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id)
)""",
    """CREATE INDEX IF NOT EXISTS idx_login_attempt_ip_created ON auth_login_attempts (ip_hash, created_at)""",
    """CREATE INDEX IF NOT EXISTS idx_login_attempt_user_created ON auth_login_attempts (username_normalized, created_at)""",
    """CREATE TABLE IF NOT EXISTS auth_users (
	id BIGSERIAL NOT NULL,
	username VARCHAR(32) NOT NULL,
	username_normalized VARCHAR(32) NOT NULL,
	password_hash TEXT NOT NULL,
	role VARCHAR(16) NOT NULL,
	active BOOLEAN NOT NULL,
	failed_login_count INTEGER NOT NULL,
	locked_until TIMESTAMP WITH TIME ZONE,
	last_login_at TIMESTAMP WITH TIME ZONE,
	created_by BIGINT,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (username_normalized),
	FOREIGN KEY(created_by) REFERENCES auth_users (id) ON DELETE SET NULL
)""",
    """CREATE TABLE IF NOT EXISTS credentials (
	id BIGSERIAL NOT NULL,
	fingerprint VARCHAR(64) NOT NULL,
	storage_type VARCHAR(64) NOT NULL,
	provider VARCHAR(80) NOT NULL,
	kind VARCHAR(80) NOT NULL,
	encrypted_secret TEXT NOT NULL,
	key_prefix VARCHAR(32) NOT NULL,
	masked VARCHAR(300) NOT NULL,
	category VARCHAR(80) NOT NULL,
	status VARCHAR(80) NOT NULL,
	result_bucket VARCHAR(32) NOT NULL,
	usable BOOLEAN NOT NULL,
	model VARCHAR(300),
	http_status INTEGER,
	attempts INTEGER,
	duration_ms INTEGER,
	region VARCHAR(120),
	message TEXT,
	reset_at VARCHAR(120),
	source VARCHAR(32) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_credentials_type_fingerprint UNIQUE (storage_type, fingerprint)
)""",
    """CREATE INDEX IF NOT EXISTS idx_credentials_prefix_created ON credentials (key_prefix varchar_pattern_ops, created_at DESC)""",
    """CREATE INDEX IF NOT EXISTS idx_credentials_provider_created ON credentials (provider, created_at DESC)""",
    """CREATE INDEX IF NOT EXISTS idx_credentials_type_bucket_created ON credentials (storage_type, result_bucket, created_at DESC)""",
    """CREATE TABLE IF NOT EXISTS validation_batches (
	id BIGSERIAL NOT NULL,
	node_id VARCHAR(64) NOT NULL,
	provider_label VARCHAR(120) NOT NULL,
	total INTEGER NOT NULL,
	valid_count INTEGER NOT NULL,
	recovery_count INTEGER NOT NULL,
	insufficient_count INTEGER NOT NULL,
	invalid_count INTEGER NOT NULL,
	received_count INTEGER NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	completed_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id)
)""",
    """CREATE INDEX IF NOT EXISTS idx_batches_node_started ON validation_batches (node_id, started_at DESC)""",
    """CREATE TABLE IF NOT EXISTS auth_sessions (
	id BIGSERIAL NOT NULL,
	user_id BIGINT NOT NULL,
	token_hash VARCHAR(64) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	revoked_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES auth_users (id) ON DELETE CASCADE,
	UNIQUE (token_hash)
)""",
    """CREATE TABLE IF NOT EXISTS validation_batch_items (
	id BIGSERIAL NOT NULL,
	batch_id BIGINT NOT NULL,
	credential_id BIGINT NOT NULL,
	item_index INTEGER NOT NULL,
	status VARCHAR(80) NOT NULL,
	result_bucket VARCHAR(32) NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_batch_item_index UNIQUE (batch_id, item_index),
	FOREIGN KEY(batch_id) REFERENCES validation_batches (id) ON DELETE CASCADE,
	FOREIGN KEY(credential_id) REFERENCES credentials (id) ON DELETE CASCADE
)""",
    """CREATE INDEX IF NOT EXISTS idx_batch_items_credential ON validation_batch_items (credential_id)""",
)


def upgrade() -> None:
    for statement in SCHEMA_SQL:
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("validation_batch_items")
    op.drop_table("auth_sessions")
    op.drop_table("validation_batches")
    op.drop_table("credentials")
    op.drop_table("auth_users")
    op.drop_table("auth_login_attempts")
