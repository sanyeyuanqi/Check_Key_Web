"""Opt-in cursor exports from a disposable PostgreSQL database with synthetic keys.

Set TEST_PLATFORM_DATABASE_URL to an expendable PostgreSQL admin database.
The reused fixture migrates and drops only a new checkapi_bulk_ingest_test_* DB;
the application DATABASE_URL and existing credentials are never queried.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
from sqlalchemy import event, text

import app.export_files as exports
import app.security as security
from app.credential_service import credential_export_statement
from app.valid_key_service import valid_key_export_statement
from test_bulk_ingest_postgres import bulk_database, connect, database_error, session_factory


OBSERVED = datetime(2026, 10, 17, tzinfo=UTC)


async def seed_cursor_data(database):
    rows = []
    archives = []
    expected = []
    for identifier in range(1, 6006):
        provider, storage_type = (
            ("Anthropic", "anthropic_official") if identifier % 4 == 0
            else ("OpenAI", "openai_official")
        )
        status = "network_error" if identifier % 3 == 0 else "valid" if identifier % 2 == 0 else "invalid"
        prefix = "synthetic-cursor-match" if identifier % 3 == 0 else "synthetic-cursor-other"
        address = "203.0.113.7" if identifier % 7 == 0 else "2001:db8::9"
        secret = f"synthetic-cursor-key-{identifier:06d}-完整导出"
        created = OBSERVED + timedelta(seconds=identifier // 2)
        last_valid = OBSERVED + timedelta(seconds=identifier // 3)
        rows.append((
            identifier, security.fingerprint(secret), storage_type, provider,
            security.encrypt_secret(secret), prefix, status,
            "valid" if status == "valid" else "invalid", status == "valid", address, created,
        ))
        if identifier % 5:
            archives.append((identifier, OBSERVED, last_valid))
        expected.append({
            "id": identifier, "provider": provider, "secret": secret, "status": status,
            "created": created, "last_valid": last_valid, "archived": bool(identifier % 5),
            "prefix": prefix, "ip": address,
        })
    connection = await connect(database)
    try:
        async with connection.transaction():
            await connection.executemany("""
                INSERT INTO credentials (
                    id,fingerprint,storage_type,provider,kind,encrypted_secret,key_prefix,
                    masked,category,status,result_bucket,usable,source,source_ip,created_at,updated_at
                ) VALUES ($1,$2,$3,$4,'synthetic-cursor',$5,$6,'synthetic…test',
                    '合成导出测试',$7,$8,$9,'synthetic',$10,$11,$11)
            """, rows)
            await connection.executemany("""
                INSERT INTO valid_key_records (credential_id,first_valid_at,last_valid_at)
                VALUES ($1,$2,$3)
            """, archives)
        assert await connection.fetchval("SELECT COUNT(*) FROM credentials") == 6005
        assert await connection.fetchval("SELECT COUNT(*) FROM credential_registry") == 6005
        assert await connection.fetchval("SELECT COUNT(*) FROM export_records") == 0
    finally:
        await connection.close()
    return expected


def test_real_postgres_stream_writes_all_pages_filters_stable_order_and_releases_cursor(bulk_database, monkeypatch):
    # This key exists only for this disposable database; no application Fernet
    # configuration, Redis client, export audit, or existing secret is accessed.
    monkeypatch.setattr(security, "settings", SimpleNamespace(fernet_key=Fernet.generate_key().decode("ascii")))
    manager = exports.ExportFiles()
    monkeypatch.setattr(manager, "prune", lambda: None)
    monkeypatch.setattr(exports, "export_files", manager)
    monkeypatch.setattr(exports, "settings", SimpleNamespace(
        export_batch_size=1000, export_max_file_bytes=256 * 1024 * 1024,
        export_max_total_bytes=512 * 1024 * 1024, export_max_pending_files=16,
        export_max_concurrent=2, export_download_ttl_seconds=120,
    ))
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    append_sizes = []
    append_original = exports.ExportFile.append_encrypted

    def observe_partition(file, ciphertexts):
        append_sizes.append(len(ciphertexts))
        append_original(file, ciphertexts)

    monkeypatch.setattr(exports.ExportFile, "append_encrypted", observe_partition)

    async def scenario():
        rows = await seed_cursor_data(bulk_database)
        platform_rows = [row for row in rows if row["provider"] == "OpenAI"]
        archive_rows = [row for row in platform_rows if row["archived"]]
        lower = OBSERVED + timedelta(seconds=500)
        upper = OBSERVED + timedelta(seconds=1500)
        cases = [
            (credential_export_statement(status="all", provider="OpenAI", storage_type="openai_official"), platform_rows, "created"),
            (credential_export_statement(
                status="valid,network_error", status_mode="exact", provider="OpenAI", storage_type="openai_official",
                created_from=lower, created_before=upper,
            ), [row for row in platform_rows if row["status"] in {"valid", "network_error"} and lower <= row["created"] < upper], "created"),
            (valid_key_export_statement(provider="OpenAI"), archive_rows, "last_valid"),
            (valid_key_export_statement(provider="OpenAI", query="synthetic-cursor-match"),
                [row for row in archive_rows if row["prefix"] == "synthetic-cursor-match"], "last_valid"),
            (valid_key_export_statement(provider="OpenAI", ip="::ffff:203.0.113.7", query="ignored-prefix"),
                [row for row in archive_rows if row["ip"] == "203.0.113.7"], "last_valid"),
            (valid_key_export_statement(provider="OpenAI", query="no-synthetic-matches"), [], "last_valid"),
        ]
        statements = []
        engine, sessions = session_factory(bulk_database)

        def observe_cursor(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("SELECT credentials.encrypted_secret"):
                statements.append((
                    statement, context.execution_options.get("yield_per"),
                    context.execution_options.get("stream_results"), context._is_server_side,
                ))

        event.listen(engine.sync_engine, "before_cursor_execute", observe_cursor)
        try:
            async with sessions() as session:
                for statement, selected, sort_field in cases:
                    selected = sorted(selected, key=lambda row: (row[sort_field], row["id"]), reverse=True)
                    content = "\n".join(row["secret"] for row in selected)
                    append_sizes.clear()
                    file = await exports.write_query_export(session, statement, request)
                    try:
                        assert file.count == len(selected)
                        assert file.size == len(content.encode("utf-8")) == file.path.stat().st_size
                        assert file.path.read_text(encoding="utf-8") == content
                        assert append_sizes == [
                            min(1000, len(selected) - offset) for offset in range(0, len(selected), 1000)
                        ]
                        assert await session.scalar(text("SELECT 1")) == 1
                    finally:
                        file.dispose()
                    assert not file.path.exists()
                assert len(session.identity_map) == 0
                assert await session.scalar(text("SELECT COUNT(*) FROM credentials")) == 6005
                assert await session.scalar(text("SELECT COUNT(*) FROM export_records")) == 0
            # Reuse the same pool after the exporting session returns its connection.
            async with sessions() as next_session:
                assert await next_session.scalar(text("SELECT 1")) == 1
            assert len(statements) == len(cases)
            assert all(batch == 1000 and streamed and server_side for _, batch, streamed, server_side in statements)
            assert all("LIMIT" not in sql and "OFFSET" not in sql for sql, *_ in statements)
            assert len(platform_rows) == 4504 and len(archive_rows) == 3603
            assert any(row["status"] == "invalid" for row in archive_rows)
        finally:
            await engine.dispose()

    try:
        try:
            asyncio.run(scenario())
        except (AssertionError, KeyboardInterrupt):
            raise
        except Exception as error:
            database_error("cursor export verification", error)
    finally:
        manager.close()
