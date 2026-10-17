"""Synthetic large-export and private-download checks; no real secrets or IO services."""

import asyncio
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from cryptography.fernet import Fernet

import app.export_files as files
import app.main as api
import app.security as security
from app.database import AuthIPBlock, AuthUser, Credential


@pytest.fixture
def manager(monkeypatch):
    service = files.ExportFiles()
    monkeypatch.setattr(files, "export_files", service)
    monkeypatch.setattr(api, "export_files", service)
    yield service
    service.close()


class Cursor:
    def __init__(self, count, batch_size):
        self.count = count
        self.batch_size = batch_size
        self.max_partition = 0
        self.closed = False

    async def partitions(self, size):
        assert size == self.batch_size
        for offset in range(0, self.count, size):
            batch = [f"synthetic-cipher-{index}" for index in range(offset, min(offset + size, self.count))]
            self.max_partition = max(self.max_partition, len(batch))
            yield batch

    async def close(self):
        self.closed = True


def test_hundred_thousand_rows_use_bounded_partitions_and_threaded_decryption(manager, monkeypatch):
    batch_size = 1000
    cursor = Cursor(100_005, batch_size)
    session = SimpleNamespace(stream_scalars=AsyncMock(return_value=cursor))
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    thread_ids = set()
    sizes = []

    def decrypt(values):
        thread_ids.add(threading.get_ident())
        sizes.append(len(values))
        return [value.replace("cipher", "full-key") for value in values]

    monkeypatch.setattr(files, "decrypt_secret_batch", decrypt)
    file = asyncio.run(files.write_query_export(session, select(Credential.encrypted_secret), request))
    assert file.count == 100_005
    assert max(sizes) == cursor.max_partition == batch_size
    assert len(sizes) == 101
    assert threading.get_ident() not in thread_ids
    assert cursor.closed
    statement = session.stream_scalars.await_args.args[0]
    assert statement.get_execution_options()["yield_per"] == batch_size
    with file.path.open("rb") as output:
        assert output.readline() == b"synthetic-full-key-0\n"
        output.seek(-len(b"synthetic-full-key-100004"), 2)
        assert output.read() == b"synthetic-full-key-100004"
    assert file.path.stat().st_size == file.size
    file.dispose()
    assert not file.path.exists()
    assert not manager._files and manager._bytes == 0


@pytest.mark.parametrize("failure", ["decrypt", "cursor_close", "disconnect", "query"])
def test_preparation_failure_closes_cursor_and_removes_partial_plaintext(manager, monkeypatch, failure):
    cursor = Cursor(2001, 1000)
    if failure == "cursor_close":
        cursor.close = AsyncMock(side_effect=RuntimeError("synthetic close error"))
    session = SimpleNamespace(stream_scalars=AsyncMock(
        return_value=cursor, side_effect=RuntimeError("synthetic query error") if failure == "query" else None,
    ))
    request = SimpleNamespace(is_disconnected=AsyncMock(side_effect=[False, True] if failure == "disconnect" else None, return_value=False))
    decrypt = MagicMock(side_effect=ValueError("synthetic decrypt failure") if failure == "decrypt" else lambda values: values)
    monkeypatch.setattr(files, "decrypt_secret_batch", decrypt)
    with pytest.raises((HTTPException, RuntimeError, ValueError)):
        asyncio.run(files.write_query_export(session, select(Credential.encrypted_secret), request))
    assert not manager._files and manager._bytes == 0
    if failure != "query" and failure != "cursor_close":
        assert cursor.closed


def test_cancellation_waits_for_writer_before_removing_file(manager, monkeypatch):
    started = threading.Event()
    finish = threading.Event()
    cursor = Cursor(1, 1000)

    def decrypt(values):
        started.set()
        assert finish.wait(5)
        return ["synthetic-key"]

    monkeypatch.setattr(files, "decrypt_secret_batch", decrypt)

    async def cancel():
        task = asyncio.create_task(files.write_query_export(
            SimpleNamespace(stream_scalars=AsyncMock(return_value=cursor)),
            select(Credential.encrypted_secret), SimpleNamespace(is_disconnected=AsyncMock(return_value=False)),
        ))
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert manager._files
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel())
    assert cursor.closed and not manager._files and manager._bytes == 0


def test_next_database_batch_fetch_overlaps_decryption_and_keeps_output_order(manager, monkeypatch):
    writer_started = threading.Event()
    fetch_started = threading.Event()
    fetch_finished = threading.Event()

    class OverlappingCursor:
        closed = False

        async def partitions(self, size):
            yield ["synthetic-first"]
            fetch_started.set()
            assert await asyncio.to_thread(writer_started.wait, 2)
            fetch_finished.set()
            yield ["synthetic-second"]

        async def close(self):
            self.closed = True

    def decrypt(values):
        if values == ["synthetic-first"]:
            writer_started.set()
            assert fetch_started.wait(2)
            assert fetch_finished.wait(2)
        return values

    monkeypatch.setattr(files, "decrypt_secret_batch", decrypt)
    cursor = OverlappingCursor()
    file = asyncio.run(files.write_query_export(
        SimpleNamespace(stream_scalars=AsyncMock(return_value=cursor)), select(Credential.encrypted_secret),
        SimpleNamespace(is_disconnected=AsyncMock(return_value=False)),
    ))
    assert file.path.read_text() == "synthetic-first\nsynthetic-second"
    assert cursor.closed and file.count == 2


def test_prefetch_failure_waits_for_inflight_writer_before_cleanup(manager, monkeypatch):
    writer_started = threading.Event()

    class FailingCursor:
        closed = False

        async def partitions(self, size):
            yield ["synthetic-first"]
            assert await asyncio.to_thread(writer_started.wait, 2)
            raise RuntimeError("synthetic prefetch failed")

        async def close(self):
            self.closed = True

    def decrypt(values):
        writer_started.set()
        time.sleep(.02)
        return values

    monkeypatch.setattr(files, "decrypt_secret_batch", decrypt)
    cursor = FailingCursor()
    with pytest.raises(RuntimeError, match="prefetch failed"):
        asyncio.run(files.write_query_export(
            SimpleNamespace(stream_scalars=AsyncMock(return_value=cursor)), select(Credential.encrypted_secret),
            SimpleNamespace(is_disconnected=AsyncMock(return_value=False)),
        ))
    assert cursor.closed and not manager._files and manager._bytes == 0


def test_file_and_total_size_limits_release_every_reserved_byte(manager, monkeypatch):
    monkeypatch.setattr(files.settings, "export_max_file_bytes", 8)
    file = manager.create()
    file.append(["12345678"])
    with pytest.raises(HTTPException) as error:
        file.append(["9"])
    assert error.value.status_code == 413
    file.dispose()
    assert manager._bytes == 0
    monkeypatch.setattr(files.settings, "export_max_total_bytes", 7)
    with pytest.raises(HTTPException) as error:
        asyncio.run(files.write_selected_export(["12345678"]))
    assert error.value.status_code == 503
    assert manager._bytes == 0 and not manager._files


def test_concurrency_and_pending_file_limits_fail_fast(manager, monkeypatch):
    monkeypatch.setattr(files.settings, "export_max_concurrent", 1)

    async def check():
        async with manager.writer():
            with pytest.raises(HTTPException) as error:
                async with manager.writer():
                    pass
            assert error.value.status_code == 503
        assert manager._writers == 0

    asyncio.run(check())
    monkeypatch.setattr(files.settings, "export_max_pending_files", 1)
    manager.create()
    with pytest.raises(HTTPException):
        manager.create()


def test_tickets_expire_and_cannot_be_consumed_by_other_user_or_session(manager, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(files.time, "monotonic", lambda: now[0])
    file = asyncio.run(files.write_selected_export(["synthetic-key"]))
    body = manager.register(file, file_name="synthetic.txt", user_id=1, session_token="synthetic-session")
    token = body["downloadUrl"].rsplit("/", 1)[1]
    assert body["count"] == 1 and body["bytes"] == 13
    for user_id, session in [(2, "synthetic-session"), (1, "synthetic-different-session")]:
        with pytest.raises(HTTPException):
            manager.claim(token, user_id=user_id, session_token=session)
    assert file.path.exists()
    now[0] += 601
    manager.prune()
    assert not file.path.exists() and not manager._tickets and not manager._files


@pytest.fixture
def prepared_api(manager, monkeypatch):
    state = SimpleNamespace(user=AuthUser(id=17, username="synthetic-user", role="user", active=True), blocked=False)
    session = MagicMock()
    session.rollback = AsyncMock()
    session.get = AsyncMock(side_effect=lambda model, identity: AuthIPBlock(ip_address=identity, active=True) if model is AuthIPBlock and state.blocked else None)
    monkeypatch.setattr(api, "current_user", AsyncMock(side_effect=lambda *_: state.user))
    monkeypatch.setattr(api, "export_selected_valid_keys", AsyncMock(return_value=["synthetic-full-key", "synthetic-id|synthetic-token"]))
    logger = AsyncMock()
    monkeypatch.setattr(api, "record_export", logger)

    async def fake_session():
        yield session

    overrides = api.app.dependency_overrides.copy()
    api.app.dependency_overrides[api.get_session] = fake_session

    async def request(method, path, *, token="synthetic-session", headers=None):
        transport = httpx.ASGITransport(app=api.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", cookies={api.settings.session_cookie_name: token}) as client:
            return await client.request(method, path, headers=headers, json={"provider": "OpenAI", "keys": [{"id": 1, "type": "openai_official"}]} if method == "POST" else None)

    yield SimpleNamespace(state=state, request=request, logger=logger, session=session)
    api.app.dependency_overrides.clear()
    api.app.dependency_overrides.update(overrides)


def test_preparation_has_only_metadata_and_native_download_is_complete_once(prepared_api, manager):
    response = asyncio.run(prepared_api.request("POST", "/api/valid-key-records/batch-export?prepare=true"))
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"downloadUrl", "fileName", "count", "bytes", "expiresIn"}
    assert "synthetic-full-key" not in response.text and "synthetic-token" not in response.text
    assert response.headers["Cache-Control"] == "no-store"
    path = body["downloadUrl"]
    # A forged partial-range request cannot consume a truncated file.
    download = asyncio.run(prepared_api.request("GET", path, headers={"Range": "bytes=0-4"}))
    assert download.status_code == 200
    assert download.text == "synthetic-full-key\nsynthetic-id|synthetic-token"
    assert int(download.headers["Content-Length"]) == body["bytes"]
    assert download.headers["Cache-Control"] == "no-store"
    assert download.headers["Accept-Ranges"] == "none"
    assert download.headers["Content-Disposition"] == f'attachment; filename="{body["fileName"]}"'
    prepared_api.session.rollback.assert_awaited_once()
    assert not manager._files and not manager._tickets
    assert asyncio.run(prepared_api.request("GET", path)).status_code == 404
    prepared_api.logger.assert_awaited_once()


@pytest.mark.parametrize("block", ["logout", "ip", "other_user", "other_session"])
def test_native_download_rechecks_permissions_and_session(prepared_api, manager, block):
    body = asyncio.run(prepared_api.request("POST", "/api/valid-key-records/batch-export?prepare=true")).json()
    if block == "logout":
        prepared_api.state.user = None
    elif block == "ip":
        prepared_api.state.blocked = True
    elif block == "other_user":
        prepared_api.state.user = AuthUser(id=18, username="other-synthetic-user", role="user", active=True)
    response = asyncio.run(prepared_api.request("GET", body["downloadUrl"], token="other-session" if block == "other_session" else "synthetic-session"))
    assert response.status_code == (401 if block == "logout" else 403 if block == "ip" else 404)
    assert "synthetic-full-key" not in response.text
    assert manager._files and manager._tickets


def test_audit_failure_removes_file_and_never_issues_download_token(prepared_api, manager):
    prepared_api.logger.side_effect = RuntimeError("synthetic audit failure")
    response = asyncio.run(prepared_api.request("POST", "/api/valid-key-records/batch-export?prepare=true"))
    assert response.status_code == 500
    assert "synthetic-full-key" not in response.text
    assert not manager._files and not manager._tickets and manager._bytes == 0


def test_download_connection_release_failure_does_not_consume_ticket(prepared_api, manager):
    body = asyncio.run(prepared_api.request("POST", "/api/valid-key-records/batch-export?prepare=true")).json()
    prepared_api.session.rollback.side_effect = RuntimeError("synthetic release failure")
    response = asyncio.run(prepared_api.request("GET", body["downloadUrl"]))
    assert response.status_code == 500
    assert len(manager._files) == len(manager._tickets) == 1
    assert "synthetic-full-key" not in response.text


def test_batch_decryption_reuses_one_cipher_and_preserves_full_unicode_credentials(monkeypatch):
    key = Fernet.generate_key()
    cipher = Fernet(key)
    factory = MagicMock(return_value=cipher)
    monkeypatch.setattr(security, "_fernet", factory)
    values = ["synthetic-account|synthetic-token", "synthetic-account\tsynthetic-token", "合成凭证"]
    encrypted = [cipher.encrypt(value.encode()).decode() for value in values]
    assert security.decrypt_secret_batch(encrypted) == values
    factory.assert_called_once()
    with pytest.raises(ValueError, match="无法解密"):
        security.decrypt_secret_batch([encrypted[0], "corrupted-synthetic-cipher"])


def test_download_disconnect_always_disposes_temporary_file(manager):
    file = asyncio.run(files.write_selected_export(["synthetic-only-key"]))
    response = files.PrivateFileResponse(file, "synthetic.txt")

    async def failed_send(message):
        if message["type"] == "http.response.body":
            raise OSError("synthetic browser disconnected")

    with pytest.raises(OSError):
        asyncio.run(response(
            {"type": "http", "method": "GET", "headers": [], "extensions": {}, "asgi": {"spec_version": "2.4"}},
            AsyncMock(), failed_send,
        ))
    assert not file.path.exists() and manager._bytes == 0 and not manager._files


def test_pruner_removes_only_marked_inactive_private_directories(manager, monkeypatch, tmp_path):
    monkeypatch.setattr(files.tempfile, "gettempdir", lambda: str(tmp_path))
    old = tmp_path / "key-control-exports-synthetic-old"
    old.mkdir()
    (old / ".export-owner").write_text("key-control-private-exports-v1")
    (old / "synthetic.txt").write_text("synthetic-only")
    unmarked = tmp_path / "key-control-exports-not-ours"
    unmarked.mkdir()
    (unmarked / "keep.txt").write_text("keep")
    active = tmp_path / "key-control-exports-synthetic-active"
    active.mkdir()
    (active / ".export-owner").write_text("key-control-private-exports-v1")
    for path in (old, unmarked):
        os.utime(path, (time.time() - 1000, time.time() - 1000))
    manager.prune()
    assert not old.exists()
    assert (unmarked / "keep.txt").exists()
    assert active.exists()
