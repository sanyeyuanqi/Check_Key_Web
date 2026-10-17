"""Synthetic-only checks for persistent export decryption and cancellation."""

import asyncio
import threading
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet

import app.export_crypto as crypto
import app.security as security


@pytest.fixture(autouse=True)
def isolated_workers(monkeypatch):
    asyncio.run(crypto.close_export_crypto_workers())
    key = Fernet.generate_key()
    monkeypatch.setattr(crypto.settings, "fernet_key", key.decode("ascii"))
    monkeypatch.setattr(crypto.settings, "export_crypto_workers", 0)
    yield Fernet(key)
    asyncio.run(crypto.close_export_crypto_workers())


def test_disabled_workers_use_thread_fallback_with_order_and_utf8(isolated_workers, monkeypatch):
    plaintexts = ["first", "中文 UTF8", "", "last"]
    ciphertexts = [isolated_workers.encrypt(value.encode()).decode() for value in plaintexts]
    original = security.decrypt_secret_batch
    thread_ids = []

    def tracked(values):
        thread_ids.append(threading.get_ident())
        return original(values)

    monkeypatch.setattr(security, "decrypt_secret_batch", tracked)

    async def run():
        await crypto.start_export_crypto_workers()
        assert crypto.export_crypto_parallelism() == 0
        assert await crypto.decrypt_export_block(ciphertexts) == "\n".join(plaintexts).encode()
        assert await crypto.decrypt_export_block([]) == b""

    asyncio.run(run())
    assert thread_ids and threading.get_ident() not in thread_ids


@pytest.mark.parametrize("bad_kind", ["tampered", "non_ascii", "non_utf8"])
def test_thread_fallback_rejects_corruption_without_returning_partial_plaintext(isolated_workers, bad_kind):
    valid = isolated_workers.encrypt(b"synthetic-first").decode()
    bad = {
        "tampered": "synthetic-corrupt-token",
        "non_ascii": "密文非法",
        "non_utf8": isolated_workers.encrypt(b"\xff").decode(),
    }[bad_kind]
    with pytest.raises(ValueError, match="^密钥数据无法解密$"):
        asyncio.run(crypto.decrypt_export_block([valid, bad]))


def test_real_spawn_pool_is_fully_initialized_and_reused(isolated_workers, monkeypatch):
    monkeypatch.setattr(crypto.settings, "export_crypto_workers", 2)
    plaintexts = [f"synthetic-{index}-中文" for index in range(1003)]
    ciphertexts = [isolated_workers.encrypt(value.encode()).decode() for value in plaintexts]

    async def run():
        await crypto.start_export_crypto_workers()
        pool = crypto._pool
        assert crypto.export_crypto_parallelism() == 2
        assert pool is not None and len(pool._processes) == 2
        assert all(process.is_alive() for process in pool._processes.values())
        await crypto.start_export_crypto_workers()
        assert crypto._pool is pool
        assert await crypto.decrypt_export_block(ciphertexts) == "\n".join(plaintexts).encode()
        for bad in ("synthetic-bad-token", isolated_workers.encrypt(b"\xff").decode()):
            with pytest.raises(ValueError, match="^密钥数据无法解密$"):
                await crypto.decrypt_export_block([ciphertexts[0], bad])
        # Rejected ciphertexts are normal errors and must not break the healthy pool.
        assert crypto._pool is pool
        assert await crypto.decrypt_export_block(ciphertexts[:1]) == plaintexts[0].encode()
        processes = list(pool._processes.values())
        await crypto.close_export_crypto_workers()
        assert crypto.export_crypto_parallelism() == 0
        assert all(not process.is_alive() for process in processes)

    asyncio.run(run())


@pytest.mark.parametrize("failure_at", ["submit", "future"])
def test_broken_process_pool_retires_and_uses_same_thread_algorithm(isolated_workers, monkeypatch, failure_at):
    pool = MagicMock()
    if failure_at == "submit":
        pool.submit.side_effect = BrokenProcessPool("synthetic private error")
    else:
        future = Future()
        future.set_exception(BrokenProcessPool("synthetic private error"))
        pool.submit.return_value = future
    monkeypatch.setattr(crypto, "_pool", pool)
    monkeypatch.setattr(crypto, "_parallelism", 2)
    ciphertext = isolated_workers.encrypt(b"synthetic-valid").decode()
    assert asyncio.run(crypto.decrypt_export_block([ciphertext])) == b"synthetic-valid"
    assert crypto.export_crypto_parallelism() == 0 and crypto._pool is None
    pool.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
    assert pool in crypto._retired_pools
    asyncio.run(crypto.close_export_crypto_workers())
    pool.shutdown.assert_called_with(wait=True, cancel_futures=True)


def test_cancellation_waits_for_inflight_thread_and_drops_its_output(isolated_workers, monkeypatch):
    started = threading.Event()
    finish = threading.Event()
    completed = threading.Event()

    def delayed(values):
        started.set()
        assert finish.wait(5)
        completed.set()
        return ["synthetic-plaintext"]

    monkeypatch.setattr(security, "decrypt_secret_batch", delayed)

    async def run():
        task = asyncio.create_task(crypto.decrypt_export_block(["synthetic-ciphertext"]))
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not completed.is_set()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert completed.is_set()

    asyncio.run(run())


def test_cancellation_waits_for_inflight_process_future_without_worker_file_access(isolated_workers, monkeypatch):
    pool = MagicMock()
    future = Future()
    pool.submit.return_value = future
    monkeypatch.setattr(crypto, "_pool", pool)
    monkeypatch.setattr(crypto, "_parallelism", 2)

    async def run():
        task = asyncio.create_task(crypto.decrypt_export_block(["synthetic-ciphertext"]))
        await asyncio.sleep(0)
        pool.submit.assert_called_once_with(crypto._decrypt_worker_block, ["synthetic-ciphertext"])
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not future.cancelled()
        future.set_result(b"synthetic-plaintext")
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())


def test_startup_failure_uses_fixed_log_and_thread_fallback(isolated_workers, monkeypatch, caplog):
    monkeypatch.setattr(crypto.settings, "export_crypto_workers", 2)
    monkeypatch.setattr(crypto, "ProcessPoolExecutor", MagicMock(side_effect=RuntimeError("synthetic private key data")))
    ciphertext = isolated_workers.encrypt(b"synthetic-valid").decode()

    async def run():
        await crypto.start_export_crypto_workers()
        assert crypto.export_crypto_parallelism() == 0
        assert await crypto.decrypt_export_block([ciphertext]) == b"synthetic-valid"

    asyncio.run(run())
    assert "synthetic private key data" not in caplog.text
    assert "thread fallback" in caplog.text


def test_cancelled_shutdown_waits_for_active_and_retired_pools(isolated_workers, monkeypatch):
    started = [threading.Event(), threading.Event()]
    finish = threading.Event()
    completed = [threading.Event(), threading.Event()]
    pools = [MagicMock(), MagicMock()]

    def shutdown(index, **kwargs):
        assert kwargs == {"wait": True, "cancel_futures": True}
        started[index].set()
        assert finish.wait(5)
        completed[index].set()

    for index, pool in enumerate(pools):
        pool.shutdown.side_effect = lambda index=index, **kwargs: shutdown(index, **kwargs)
    monkeypatch.setattr(crypto, "_pool", pools[0])
    monkeypatch.setattr(crypto, "_parallelism", 2)
    crypto._retired_pools.append(pools[1])

    async def run():
        task = asyncio.create_task(crypto.close_export_crypto_workers())
        for event in started:
            assert await asyncio.to_thread(event.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(event.is_set() for event in completed)
        assert crypto._pool is None and crypto._retired_pools == []

    asyncio.run(run())
