"""Bounded export pipeline lifecycle checks, with synthetic ciphertexts only."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

import app.export_files as files
from app.database import Credential


@pytest.fixture
def manager(monkeypatch):
    service = files.ExportFiles()
    monkeypatch.setattr(files, "export_files", service)
    yield service
    service.close()


class PipelineCursor:
    def __init__(self, count=9, before_close=None, fail_at=None):
        self.count = count
        self.before_close = before_close
        self.fail_at = fail_at
        self.closed = False

    async def partitions(self, size):
        for index in range(self.count):
            if index == self.fail_at:
                raise RuntimeError("synthetic database fetch failed")
            yield [f"synthetic-{index}"]

    async def close(self):
        if self.before_close:
            self.before_close()
        self.closed = True


def run_export(cursor, request=None):
    return files.write_query_export(
        SimpleNamespace(stream_scalars=AsyncMock(return_value=cursor)),
        select(Credential.encrypted_secret),
        request or SimpleNamespace(is_disconnected=AsyncMock(return_value=False)),
    )


def test_parallel_work_is_bounded_and_out_of_order_results_keep_source_order(manager, monkeypatch):
    monkeypatch.setattr(files, "export_crypto_parallelism", lambda: 4)
    state = {"active": 0, "peak": 0}
    completed = []

    async def decrypt(values):
        index = int(values[0].rsplit("-", 1)[1])
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        try:
            await asyncio.sleep(.02 if index % 4 == 0 else 0)
            completed.append(index)
            return f"synthetic-full-{index}".encode()
        finally:
            state["active"] -= 1

    monkeypatch.setattr(files, "decrypt_export_block", decrypt)
    cursor = PipelineCursor(before_close=lambda: pytest.fail("CPU tasks remain") if state["active"] else None)
    file = asyncio.run(run_export(cursor))
    assert state["peak"] == 4 and state["active"] == 0
    assert completed.index(1) < completed.index(0)
    assert file.path.read_text() == "\n".join(f"synthetic-full-{index}" for index in range(9))
    assert file.count == 9 and file.size == file.path.stat().st_size and cursor.closed


def test_disabled_pool_uses_thread_fallback(manager, monkeypatch):
    monkeypatch.setattr(files, "export_crypto_parallelism", lambda: 0)
    process_decrypt = AsyncMock(side_effect=AssertionError("disabled process pool used"))
    monkeypatch.setattr(files, "decrypt_export_block", process_decrypt)
    identities = []

    def decrypt(values):
        identities.append(threading.get_ident())
        return [value.replace("synthetic", "synthetic-full") for value in values]

    monkeypatch.setattr(files, "decrypt_secret_batch", decrypt)
    cursor = PipelineCursor(3)
    file = asyncio.run(run_export(cursor))
    assert file.path.read_text() == "synthetic-full-0\nsynthetic-full-1\nsynthetic-full-2"
    assert threading.get_ident() not in identities and len(identities) == 3
    process_decrypt.assert_not_awaited()
    assert cursor.closed


@pytest.mark.parametrize("failure", ["decrypt", "fetch", "disconnect"])
def test_failed_parallel_export_joins_cpu_tasks_closes_cursor_and_removes_file(manager, monkeypatch, failure):
    monkeypatch.setattr(files, "export_crypto_parallelism", lambda: 4)
    active = set()
    cancelled = set()

    async def decrypt(values):
        value = values[0]
        active.add(value)
        try:
            if failure == "decrypt" and value == "synthetic-0":
                await asyncio.sleep(0)
                raise ValueError("synthetic decryption failed")
            await asyncio.sleep(.01 if failure == "disconnect" else 10)
            return value.encode()
        except asyncio.CancelledError:
            cancelled.add(value)
            raise
        finally:
            active.discard(value)

    monkeypatch.setattr(files, "decrypt_export_block", decrypt)
    cursor = PipelineCursor(
        before_close=lambda: pytest.fail("cursor closed before CPU cleanup") if active else None,
        fail_at=2 if failure == "fetch" else None,
    )
    request = None
    if failure == "disconnect":
        request = SimpleNamespace(is_disconnected=AsyncMock(side_effect=[False] * 4 + [True]))
    with pytest.raises((HTTPException, RuntimeError, ValueError)):
        asyncio.run(run_export(cursor, request))
    assert not active and cursor.closed
    assert not manager._files and manager._bytes == 0
    if failure == "decrypt":
        assert cancelled


def test_parent_cancellation_joins_decryption_tasks_before_cursor_cleanup(manager, monkeypatch):
    monkeypatch.setattr(files, "export_crypto_parallelism", lambda: 4)
    active = set()
    cancelled = set()

    async def check():
        all_started = asyncio.Event()

        async def decrypt(values):
            value = values[0]
            active.add(value)
            if len(active) == 4:
                all_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                # An asynchronous finalizer must also complete before cursor close.
                await asyncio.sleep(0)
                cancelled.add(value)
                raise
            finally:
                active.discard(value)

        monkeypatch.setattr(files, "decrypt_export_block", decrypt)
        cursor = PipelineCursor(before_close=lambda: pytest.fail("unfinished CPU task") if active else None)
        task = asyncio.create_task(run_export(cursor))
        await asyncio.wait_for(all_started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cursor.closed

    asyncio.run(check())
    assert len(cancelled) == 4 and not active
    assert not manager._files and manager._bytes == 0


@pytest.mark.parametrize("repeat_cancel", [False, True])
def test_cancellation_waits_for_inflight_file_write_even_after_repeated_cancel(manager, monkeypatch, repeat_cancel):
    monkeypatch.setattr(files, "export_crypto_parallelism", lambda: 4)
    monkeypatch.setattr(files, "decrypt_export_block", AsyncMock(return_value=b"synthetic-full-key"))
    started = threading.Event()
    finish = threading.Event()
    completed = threading.Event()
    original = files.ExportFile.append_block
    state = {"disposed_while_writing": False}

    def blocking_append(file, block, count):
        started.set()
        try:
            assert finish.wait(5)
            state["disposed_while_writing"] = file.disposed
            original(file, block, count)
        finally:
            completed.set()

    monkeypatch.setattr(files.ExportFile, "append_block", blocking_append)

    async def check():
        cursor = PipelineCursor(1)
        task = asyncio.create_task(run_export(cursor))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        if repeat_cancel:
            task.cancel()
            await asyncio.sleep(0)
        # Releasing a thread before awaiting avoids any failure leaving a live writer.
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(completed.wait, 2)
        assert cursor.closed

    asyncio.run(check())
    assert not state["disposed_while_writing"]
    assert not manager._files and manager._bytes == 0


def test_block_appends_preserve_utf8_separator_count_and_size_limit(manager, monkeypatch):
    file = manager.create()
    first = "合成密钥".encode()
    second = "synthetic-user|合成令牌".encode()
    expected = first + b"\n" + second
    monkeypatch.setattr(files.settings, "export_max_file_bytes", len(expected))
    file.append_block(first, 1)
    file.append_block(b"", 0)
    file.append_block(second, 1)
    assert file.path.read_bytes() == expected
    assert file.count == 2 and file.size == len(expected)
    with pytest.raises(HTTPException) as error:
        file.append_block(b"synthetic-over-limit", 1)
    assert error.value.status_code == 413
    assert file.path.read_bytes() == expected
    assert file.count == 2 and manager._bytes == file.size


def test_empty_parallel_query_still_closes_cursor(manager, monkeypatch):
    monkeypatch.setattr(files, "export_crypto_parallelism", lambda: 4)
    decrypt = AsyncMock(side_effect=AssertionError("empty query submitted CPU work"))
    monkeypatch.setattr(files, "decrypt_export_block", decrypt)
    cursor = PipelineCursor(0)
    file = asyncio.run(run_export(cursor))
    assert cursor.closed and file.count == file.size == 0
    assert file.path.read_bytes() == b""
    decrypt.assert_not_awaited()


def test_file_write_failure_cancels_cpu_work_before_cursor_and_file_cleanup(manager, monkeypatch):
    monkeypatch.setattr(files, "export_crypto_parallelism", lambda: 4)
    active = set()

    async def decrypt(values):
        value = values[0]
        active.add(value)
        try:
            if value != "synthetic-0":
                await asyncio.sleep(10)
            return value.encode()
        finally:
            active.discard(value)

    def fail_append(*_):
        raise OSError("synthetic disk write failed")

    monkeypatch.setattr(files, "decrypt_export_block", decrypt)
    monkeypatch.setattr(files.ExportFile, "append_block", fail_append)
    cursor = PipelineCursor(before_close=lambda: pytest.fail("CPU cleanup incomplete") if active else None)
    with pytest.raises(OSError, match="synthetic disk write failed"):
        asyncio.run(run_export(cursor))
    assert not active and cursor.closed
    assert not manager._files and manager._bytes == 0


@pytest.mark.parametrize("parallelism", [0, 4])
def test_cancellation_finishes_inflight_database_fetch_before_cursor_close(manager, monkeypatch, parallelism):
    monkeypatch.setattr(files, "export_crypto_parallelism", lambda: parallelism)
    monkeypatch.setattr(files, "decrypt_secret_batch", lambda values: values)
    monkeypatch.setattr(files, "decrypt_export_block", AsyncMock(return_value=b"synthetic-full-key"))

    async def check():
        fetch_started = asyncio.Event()
        fetch_cancelled = asyncio.Event()

        class FetchingCursor(PipelineCursor):
            async def partitions(self, size):
                yield ["synthetic-first"]
                fetch_started.set()
                try:
                    await asyncio.Future()
                finally:
                    await asyncio.sleep(0)
                    fetch_cancelled.set()

            async def close(self):
                assert fetch_cancelled.is_set()
                self.closed = True

        cursor = FetchingCursor()
        task = asyncio.create_task(run_export(cursor))
        await asyncio.wait_for(fetch_started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert fetch_cancelled.is_set() and cursor.closed

    asyncio.run(check())
    assert not manager._files and manager._bytes == 0
