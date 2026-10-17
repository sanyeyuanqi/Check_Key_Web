"""Opt-in real HTTP export benchmark with a disposable synthetic PostgreSQL DB.

Set EXPORT_END_TO_END_BENCHMARK=1 and TEST_PLATFORM_DATABASE_URL. Optional
EXPORT_BENCHMARK_OUTPUT writes aggregate JSON diagnostics. No application
record, application Fernet key, real account or real export audit is accessed.
Generation, insertion, process warming and server startup are outside timings.
"""

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import socket
from statistics import median
from time import perf_counter
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
from fastapi import HTTPException
import httpx
import pytest
import uvicorn

import app.export_crypto as crypto
import app.export_files as files
import app.export_service as export_service
import app.main as api
import app.security as security
from app.database import AuthUser
from test_bulk_ingest_postgres import bulk_database, connect, database_error, session_factory


pytestmark = pytest.mark.skipif(
    os.environ.get("EXPORT_END_TO_END_BENCHMARK") != "1",
    reason="Set EXPORT_END_TO_END_BENCHMARK=1 for disposable synthetic HTTP speed diagnostics",
)

COUNT = 100_000
OBSERVED = datetime(2026, 10, 17, tzinfo=UTC)


async def _serial_baseline(session, statement, request):
    """Previous deployed algorithm: finish each decrypt/write before next fetch."""
    file = files.export_files.create()
    try:
        result = await session.stream_scalars(statement.execution_options(yield_per=1000))
        try:
            async for ciphertexts in result.partitions(1000):
                if await request.is_disconnected():
                    raise HTTPException(499, "Synthetic benchmark request cancelled")
                await files._file_work(file.append_encrypted, list(ciphertexts))
            if await request.is_disconnected():
                raise HTTPException(499, "Synthetic benchmark request cancelled")
        finally:
            await result.close()
        return file
    except BaseException:
        await files._file_work(file.dispose)
        raise


def _synthetic_rows(cipher):
    rows = []
    expected_bytes = 0
    expected_hash = hashlib.sha256()
    for identifier in range(COUNT, 0, -1):
        secret = f"synthetic-http-export-key-{identifier:06d}-完整导出内容-abcdefghijklmnopqrstuvwxyz"
        plaintext = secret.encode("utf-8")
        if identifier != COUNT:
            expected_hash.update(b"\n")
            expected_bytes += 1
        expected_hash.update(plaintext)
        expected_bytes += len(plaintext)
        created = OBSERVED + timedelta(seconds=identifier)
        rows.append((
            identifier, hashlib.sha256(plaintext).hexdigest(),
            cipher.encrypt(plaintext).decode("ascii"), created,
        ))
    return rows, expected_bytes, expected_hash.hexdigest()


def test_real_http_one_hundred_thousand_keys_complete_export_speed(bulk_database, monkeypatch):
    # The key and session secret are generated for this test and never emitted.
    synthetic_key = Fernet.generate_key().decode("ascii")
    test_settings = api.settings.model_copy(update={
        "fernet_key": synthetic_key,
        "session_secret": "synthetic-http-benchmark-session-secret",
        "cache_redis_url": "",
        "export_batch_size": 1000,
        "export_crypto_workers": 4,
        "export_max_file_bytes": 256 * 1024 * 1024,
        "export_max_total_bytes": 512 * 1024 * 1024,
        "export_max_pending_files": 16,
        "export_max_concurrent": 2,
        "export_download_ttl_seconds": 120,
    })
    for module in (api, security, files, crypto):
        monkeypatch.setattr(module, "settings", test_settings)
    manager = files.ExportFiles()
    # Avoid pruning other instances' directories during a timing benchmark.
    monkeypatch.setattr(manager, "prune", lambda: None)
    monkeypatch.setattr(files, "export_files", manager)
    monkeypatch.setattr(api, "export_files", manager)
    invalidate = AsyncMock()
    monkeypatch.setattr(export_service, "invalidate_query_cache", invalidate)
    user = AuthUser(
        id=17, username="synthetic-speed-user", username_normalized="synthetic-speed-user",
        role="user", active=True, password_hash="synthetic-unused-password-hash",
    )
    monkeypatch.setattr(api, "current_user", AsyncMock(return_value=user))
    optimized_writer = files.write_query_export
    original_overrides = api.app.dependency_overrides.copy()

    async def scenario():
        rows, expected_bytes, expected_hash = _synthetic_rows(Fernet(synthetic_key.encode("ascii")))
        connection = await connect(bulk_database)
        try:
            async with connection.transaction():
                await connection.execute("""
                    INSERT INTO auth_users (id,username,username_normalized,password_hash,role,active,failed_login_count)
                    VALUES (17,'synthetic-speed-user','synthetic-speed-user',
                        'synthetic-unused-password-hash','user',true,0)
                """)
                await connection.executemany("""
                    INSERT INTO credentials (
                        id,fingerprint,storage_type,provider,kind,encrypted_secret,key_prefix,
                        masked,category,status,result_bucket,usable,source,source_ip,created_at,updated_at
                    ) VALUES ($1,$2,'openai_official','OpenAI','synthetic-http-speed',$3,
                        'synthetic-http-export','synthetic...test','synthetic','valid','valid',true,
                        'synthetic','203.0.113.7',$4,$4)
                """, rows)
            assert await connection.fetchval("SELECT COUNT(*) FROM credentials") == COUNT
            assert await connection.fetchval("SELECT COUNT(DISTINCT encrypted_secret) FROM credentials") == COUNT
            # Real registry triggers run during seed; do not bypass their contract.
            assert await connection.fetchval("SELECT COUNT(*) FROM credential_registry") == COUNT
            assert await connection.fetchval("SELECT COUNT(*) FROM export_records") == 0
            await connection.execute("ANALYZE credentials_openai")
            await connection.execute("ANALYZE credentials")
        finally:
            await connection.close()
        del rows

        engine, sessions = session_factory(bulk_database)

        async def isolated_session():
            async with sessions() as session:
                yield session

        api.app.dependency_overrides[api.get_session] = isolated_session
        server = None
        server_task = None
        listener = None
        measurements = []
        try:
            await crypto.start_export_crypto_workers()
            assert crypto.export_crypto_parallelism() == 4
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            listener.setblocking(False)
            port = listener.getsockname()[1]
            config = uvicorn.Config(
                api.app, host="127.0.0.1", port=port, lifespan="off",
                log_level="critical", access_log=False,
            )
            server = uvicorn.Server(config)
            server_task = asyncio.create_task(server.serve(sockets=[listener]))
            async with asyncio.timeout(15):
                while not server.started:
                    if server_task.done():
                        await server_task
                        raise RuntimeError("Synthetic benchmark HTTP server did not start")
                    await asyncio.sleep(0.01)

            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", timeout=120,
                cookies={test_settings.session_cookie_name: "synthetic-http-export-session"},
            ) as client:
                for iteration in range(3):
                    for mode in ("baseline", "optimized"):
                        monkeypatch.setattr(api, "write_query_export", _serial_baseline if mode == "baseline" else optimized_writer)
                        monkeypatch.setattr(files.PrivateFileResponse, "chunk_size", 64 * 1024 if mode == "baseline" else 1024 * 1024)
                        started = perf_counter()
                        prepared = await client.get("/api/export", params={
                            "status": "all", "provider": "OpenAI", "type": "openai_official", "prepare": "1",
                        })
                        prepare_seconds = perf_counter() - started
                        # Do not include response bodies/URLs in failed assertions.
                        assert prepared.status_code == 200
                        assert prepared.headers["cache-control"] == "no-store"
                        ticket = prepared.json()
                        assert ticket["count"] == COUNT and ticket["bytes"] == expected_bytes
                        received = 0
                        digest = hashlib.sha256()
                        download_started = perf_counter()
                        async with client.stream("GET", ticket["downloadUrl"]) as downloaded:
                            assert downloaded.status_code == 200
                            assert downloaded.headers["cache-control"] == "no-store"
                            assert int(downloaded.headers["content-length"]) == expected_bytes
                            async for block in downloaded.aiter_bytes():
                                digest.update(block)
                                received += len(block)
                        finished = perf_counter()
                        assert received == expected_bytes
                        assert digest.hexdigest() == expected_hash
                        measurements.append({
                            "mode": mode, "iteration": iteration + 1,
                            "prepare_seconds": round(prepare_seconds, 6),
                            "download_seconds": round(finished - download_started, 6),
                            "total_seconds": round(finished - started, 6),
                        })
                        # File disposal finishes after the HTTP send; wait outside timing.
                        async with asyncio.timeout(5):
                            while manager._files:
                                await asyncio.sleep(.01)
                        assert manager._bytes == 0 and not manager._tickets

            connection = await connect(bulk_database)
            try:
                records = await connection.fetch("""
                    SELECT user_id,username,provider,storage_type,status,credential_count,ip_address
                    FROM export_records ORDER BY id
                """)
                assert len(records) == 6
                assert all(tuple(record.values()) == (
                    17,"synthetic-speed-user","OpenAI","openai_official","all",COUNT,"127.0.0.1",
                ) for record in records)
                assert await connection.fetchval("SELECT COUNT(*) FROM credentials") == COUNT
            finally:
                await connection.close()
            assert invalidate.await_count == 6
            summary = {
                mode: {
                    name: round(median(item[name] for item in measurements if item["mode"] == mode), 6)
                    for name in ("prepare_seconds", "download_seconds", "total_seconds")
                }
                for mode in ("baseline", "optimized")
            }
            report = {
                "records": COUNT, "distinct_ciphertexts": COUNT, "bytes": expected_bytes,
                "synthetic_content_sha256": expected_hash,
                "crypto_workers": 4, "database_batch_size": 1000,
                "baseline_download_chunk_bytes": 64 * 1024,
                "optimized_download_chunk_bytes": 1024 * 1024,
                "medians": summary, "measurements": measurements,
                "total_speedup": round(summary["baseline"]["total_seconds"] / summary["optimized"]["total_seconds"], 4),
                "synthetic_audit_records": 6,
            }
            if destination := os.environ.get("EXPORT_BENCHMARK_OUTPUT"):
                output = Path(destination)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print("SYNTHETIC_EXPORT_END_TO_END_SPEED=" + json.dumps(report))
        finally:
            api.app.dependency_overrides.clear()
            api.app.dependency_overrides.update(original_overrides)
            if server is not None:
                server.should_exit = True
            if server_task is not None:
                with suppress(asyncio.CancelledError):
                    await server_task
            if listener is not None:
                listener.close()
            await crypto.close_export_crypto_workers()
            manager.close()
            await engine.dispose()

    try:
        asyncio.run(scenario())
    except (AssertionError, KeyboardInterrupt):
        raise
    except Exception as error:
        database_error("synthetic end-to-end HTTP export speed benchmark", error)
    finally:
        api.app.dependency_overrides.clear()
        api.app.dependency_overrides.update(original_overrides)
        manager.close()
